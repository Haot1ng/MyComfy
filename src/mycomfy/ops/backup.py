"""停写备份、完整性核验和隔离恢复。运行方式见 docs/operations.md。"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import DateTime, MetaData, func, inspect, select, text

from mycomfy.database.session import make_sessions


def file_digest(path):
    """流式计算文件的大小及 SHA-256。"""
    with path.open("rb") as stream:
        return {
            "size": os.fstat(stream.fileno()).st_size,
            "sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
        }


def sync_tree(root):
    """逐文件并自底向上同步目录项，使备份清单或数据库能在数据落盘后发布。"""
    for directory, _, files in os.walk(root, topdown=False):
        for name in files:
            with (Path(directory) / name).open("rb") as stream:
                os.fsync(stream.fileno())
        sync_directory(Path(directory))


def sync_directory(path):
    """要求本地文件系统持久化该目录的元数据。"""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def safe_child(root, name):
    """解析已有介质中的相对路径，拒绝越界、符号链接和路径别名。"""
    rel = PurePosixPath(name)
    if not rel.parts or rel.is_absolute() or ".." in rel.parts or str(rel) != name:
        raise ValueError(f"Invalid archive path {name!r}")
    dest = root.joinpath(*rel.parts)
    if dest.resolve() != dest:
        raise ValueError(f"Unexpected symbolic link in {name!r}")
    return dest


def checked_roots(roots, dest):
    """校验备份源互不嵌套，并确认备份目标位于源目录之外。"""
    parsed = {}
    for name, path in roots.items():
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,39}", name):
            raise ValueError("Use an alphanumeric backup root name")
        root = Path(path).resolve(strict=True)
        if not root.is_dir() or dest.is_relative_to(root):
            raise ValueError(f"Invalid backup source or destination: {root}")
        if any(root.is_relative_to(p) or p.is_relative_to(root) for p in parsed.values()):
            raise ValueError(f"Overlapping backup source: {root}")
        parsed[name] = root
    if "data" not in parsed:
        raise ValueError("Provide --root data=MYCOMFY_DATA_ROOT and protected engine directories")
    return parsed


def inventory(root):
    """列出目录树，只接受普通文件和目录。"""
    result = {}
    for path in root.rglob("*"):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            result[path.relative_to(root).as_posix()] = None
        elif stat.S_ISREG(info.st_mode):
            result[path.relative_to(root).as_posix()] = (info.st_size, info.st_mtime_ns)
        else:
            raise ValueError(f"Snapshot source must be a regular file or directory: {path}")
    return result


def serialize_row(row):
    """序列化单行业务记录并保持 JSON 字段和 UTC 时间的精度。"""
    return {
        k: v.isoformat(timespec="microseconds") if isinstance(v, datetime) else v
        for k, v in row.items()
    }


def dump_database(engine, destination, source_roots):
    """在一致性读事务中依次导出迁移版本、表记录并核实待发布源覆盖。"""
    tables = {}
    with engine.connect() as conn:
        if engine.dialect.name == "mysql":
            conn = conn.execution_options(isolation_level="REPEATABLE READ")
        with conn.begin():
            metadata = MetaData()
            metadata.reflect(conn)
            revisions = sorted(conn.scalars(text("SELECT version_num FROM alembic_version")))
            for table in metadata.sorted_tables:
                if table.name == "alembic_version":
                    continue
                if not re.fullmatch(r"[a-z][a-z0-9_]*", table.name):
                    raise ValueError("Unexpected database table name")
                path = destination / f"{table.name}.jsonl"
                count = 0
                with path.open("x", encoding="utf-8") as stream:
                    rows = conn.execution_options(stream_results=True).execute(select(table))
                    for row in rows.mappings():
                        if table.name == "executions" and row["phase"] in {
                            "PREPARING",
                            "PREPARED",
                            "SUBMITTING",
                            "RUNNING",
                            "ACCEPTED",
                            "UNCONFIRMED",
                            "QUARANTINED",
                        }:
                            for key in (
                                "run_dir",
                                "input_dir",
                                "output_dir",
                                "temp_dir",
                                "state_dir",
                            ):
                                raw = row["workspace"].get(key)
                                if (
                                    raw
                                    and Path(raw).exists()
                                    and not any(
                                        Path(raw).resolve().is_relative_to(p) for p in source_roots
                                    )
                                ):
                                    raise ValueError(
                                        f"Active execution directory missing from backup: {raw}"
                                    )
                        if table.name == "publications" and row["phase"] not in {
                            "DONE",
                            "REGISTERED",
                        }:
                            source = Path(row["source_path"]).resolve()
                            if not any(source.is_relative_to(p) for p in source_roots):
                                raise ValueError(
                                    f"Protected output missing from backup roots: {source}"
                                )
                        stream.write(json.dumps(serialize_row(row), ensure_ascii=True) + "\n")
                        count += 1
                tables[table.name] = count
    return tables, revisions


def create_backup(engine, dest, roots):
    """将静止的数据库和指定目录写入一组备份，最后发布完整性清单。"""
    dest = Path(dest).resolve()
    roots = checked_roots(roots, dest)
    dest.mkdir(mode=0o700, parents=False)
    try:
        at = datetime.now(UTC).isoformat()
        dbdir = dest / "database"
        dbdir.mkdir()
        counts, revisions = dump_database(engine, dbdir, roots.values())
        files = {p.relative_to(dest).as_posix(): file_digest(p) for p in dbdir.iterdir()}
        directories, modes = [], {}
        for label, source in roots.items():
            mapped = dest / "roots" / label
            mapped.mkdir(parents=True)
            directories.append(mapped.relative_to(dest).as_posix())
            modes[mapped.relative_to(dest).as_posix()] = source.stat().st_mode & 0o777
            before = inventory(source)
            for relative, stamp in before.items():
                target = mapped / relative
                modes[target.relative_to(dest).as_posix()] = (
                    source / relative
                ).stat().st_mode & 0o777
                if stamp is None:
                    target.mkdir(parents=True, exist_ok=True)
                    directories.append(target.relative_to(dest).as_posix())
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source / relative, target)
                    files[target.relative_to(dest).as_posix()] = file_digest(target)
            if before != inventory(source):
                raise RuntimeError(f"Source changed while taking snapshot: {source}")
        # 对照数据库登记核验全部标记可用的本地正式对象，排除只复制了目录名的残缺恢复点。
        with (dbdir / "assets.jsonl").open() as assets:
            for line in assets:
                asset = json.loads(line)
                if asset["provider"] != "local" or not asset["available"]:
                    continue
                name = f"roots/data/objects/{asset['object_key']}"
                if files.get(name) != {"size": asset["size"], "sha256": asset["digest"]}:
                    raise ValueError(f"Registered asset missing or corrupt: {asset['id']}")
        manifest = {
            "format": 1,
            "recovery_point": at,
            "schema": revisions,
            "tables": counts,
            "roots": {k: str(v) for k, v in roots.items()},
            "directories": directories,
            "modes": modes,
            "files": files,
        }
        staging = dest / ".manifest.tmp"
        staging.write_text(json.dumps(manifest, indent=2))
        sync_tree(dest)
        staging.rename(dest / "manifest.json")
        sync_directory(dest)
        sync_directory(dest.parent)
        return manifest
    except Exception:
        shutil.rmtree(dest)
        raise


def verify_backup(directory):
    """逐项核对备份内容摘要、表清单、文件数量和空目录。"""
    root = Path(directory).resolve(strict=True)
    manifest = json.loads(safe_child(root, "manifest.json").read_text())
    if manifest.get("format") != 1:
        raise ValueError("Unknown backup format")
    listed = manifest["files"]
    for name, metadata in listed.items():
        path = safe_child(root, name)
        if not path.is_file() or file_digest(path) != metadata:
            raise ValueError(f"Backup digest mismatch: {name}")
    actual = {k for k, v in inventory(root).items() if v is not None}
    if actual != set(listed) | {"manifest.json"}:
        raise ValueError("Backup file set differs from manifest")
    for name in manifest["directories"]:
        if not name.startswith("roots/") or not safe_child(root, name).is_dir():
            raise ValueError(f"Missing directory: {name}")
    for table in manifest["tables"]:
        if f"database/{table}.jsonl" not in listed:
            raise ValueError(f"Missing table: {table}")
    return manifest


def mapped_path(value, mapping):
    """把备份覆盖的原绝对路径映射到隔离恢复目录。"""
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("Unexpected relative path in stored location")
    for old, new in mapping.items():
        if path.is_relative_to(old):
            return str(new / path.relative_to(old))
    return None


def restore_row(table, record, mapping):
    """恢复日期和运行目录，复原业务事实并暂停实例接单等待核对。"""
    for col in table.columns:
        if isinstance(col.type, DateTime) and record.get(col.name):
            record[col.name] = datetime.fromisoformat(record[col.name])
    if table.name in ("engine_instances", "executions"):
        key = "runtime" if table.name == "engine_instances" else "workspace"
        runtime = record.get(key) or {}
        # 恢复器只能在本次恢复的目录中寻找文件，旧机器的进程标识也不能用于控制进程。
        for name, value in list(runtime.items()):
            if isinstance(value, str) and Path(value).is_absolute():
                mapped = mapped_path(value, mapping)
                if mapped:
                    runtime[name] = mapped
                else:
                    runtime.pop(name)
        runtime.pop("pid", None)
        runtime["base_url"] = "http://127.0.0.1:0"
        runtime["directed_interrupt"] = False
        if table.name == "engine_instances":
            # 原进程身份和启动清单作为历史证据保留；恢复实例只读取新控制目录。
            runtime.pop("run_dir", None)
            runtime["paused"] = True
            runtime["stop_requested"] = False
            record["accepting"] = False
            record["heartbeat_at"] = None
            runtime["process_alive"] = False
            runtime["api_reachable"] = False
            if record["active_execution_id"]:
                record["quarantine_reason"] = "RESTORE_RECONCILIATION_REQUIRED"
                label = hashlib.sha256(record["id"].encode()).hexdigest()
                control = next(iter(mapping.values())).parent / ".restored-control" / label
                control.mkdir(mode=0o700, parents=True, exist_ok=False)
                runtime["run_dir"] = str(control)
                runtime.setdefault("launched_at", datetime.now(UTC).timestamp())
                runtime.setdefault("config_hash", "")
        record[key] = runtime
    if table.name == "publications":
        dest = mapped_path(record["source_path"], mapping)
        if dest:
            record["source_path"] = dest
        elif record["phase"] not in {"DONE", "REGISTERED"}:
            raise ValueError("Cannot remap protected publication source")
        else:
            # 已完成源可能早已清理，保存隔离目录内的诊断定位。
            record["source_path"] = str(
                next(iter(mapping.values())) / ".absent-sources" / record["id"]
            )
    return record


def restore_backup(engine, directory, dest):
    """完整核验后在新的文件目录和已迁移的空业务库恢复，任一步失败则回滚。"""
    root = Path(directory).resolve(strict=True)
    manifest = verify_backup(root)
    dest = Path(dest).resolve()
    if dest.is_relative_to(root):
        raise ValueError("Restore into a separate directory")
    mapped = {Path(source): safe_child(dest, label) for label, source in manifest["roots"].items()}
    if not mapped or any(
        Path(source).is_relative_to(dest) or dest.is_relative_to(source) for source in mapped
    ):
        raise ValueError("Use an isolated restore directory outside the original roots")
    dest.mkdir(mode=0o700)
    try:
        with engine.begin() as conn:
            versions = sorted(conn.scalars(text("SELECT version_num FROM alembic_version")))
            expected_tables = set(manifest["tables"]) | {"alembic_version"}
            if (
                versions != manifest["schema"]
                or set(inspect(conn).get_table_names()) != expected_tables
            ):
                raise ValueError("Migrate the isolated restore database to the backup revision")
            metadata = MetaData()
            metadata.reflect(conn)
            tables = [t for t in metadata.sorted_tables if t.name != "alembic_version"]
            for table in tables:
                exists = select(func.count()).select_from(table)
                if table.name == "capacity_gates":
                    exists = exists.where(table.c.id != "admission")
                if conn.scalar(exists):
                    raise ValueError("Restore database already contains application data")
            # 在提交数据库前先恢复文件，派生的运行时路径仅指向该隔离副本。
            for label in manifest["roots"]:
                shutil.copytree(safe_child(root, f"roots/{label}"), safe_child(dest, label))
            for name, expected in manifest["files"].items():
                if (
                    name.startswith("roots/")
                    and file_digest(safe_child(dest, name[6:])) != expected
                ):
                    raise ValueError(f"Restored file digest mismatch: {name}")
            for table in tables:
                if table.name == "capacity_gates":
                    conn.execute(table.delete().where(table.c.id == "admission"))
                count = 0
                batch = []
                reservations = {}
                with (root / "database" / f"{table.name}.jsonl").open() as stream:
                    for line in stream:
                        row = restore_row(table, json.loads(line), mapped)
                        count += 1
                        # 新根目录均在本次创建的同一目标卷上；合并旧设备号上的占用。
                        if table.name == "disk_reservations":
                            row["device"] = str(dest.stat().st_dev)
                            prior = reservations.get(row["owner"])
                            if prior is None:
                                reservations[row["owner"]] = row
                            else:
                                prior["size"] += row["size"]
                                prior["upload_bytes"] += row["upload_bytes"]
                                if "target_bytes" in row:
                                    prior["target_bytes"] += row["target_bytes"]
                            continue
                        if table.name == "capacity_gates" and row["id"] != "admission":
                            continue
                        batch.append(row)
                        if len(batch) == 500:
                            conn.execute(table.insert(), batch)
                            batch.clear()
                if reservations:
                    batch.extend(reservations.values())
                if batch:
                    conn.execute(table.insert(), batch)
                if count != manifest["tables"][table.name]:
                    raise ValueError(f"Backup row count mismatch: {table.name}")
            # 保留受限凭据的读写权限以及部署工具的可执行位；控制目录只对当前用户开放。
            for name, mode in sorted(
                manifest.get("modes", {}).items(), key=lambda pair: -pair[0].count("/")
            ):
                if not name.startswith("roots/") or type(mode) is not int or not 0 <= mode <= 0o777:
                    raise ValueError("Invalid restore permissions")
                safe_child(dest, name[6:]).chmod(mode)
            sync_tree(dest)
            sync_directory(dest.parent)
        return manifest
    except Exception:
        shutil.rmtree(dest)
        raise


def reconcile_files(sessions, data_root):
    """在工作进程停写时恢复发布意图，并核对每个已登记对象与其摘要。"""
    from mycomfy.artifacts.publication import publish_pending, reconcile_asset
    from mycomfy.artifacts.storage import LocalStore
    from mycomfy.database.models import Asset, Publication

    store = LocalStore(data_root)
    with sessions() as db:
        total = db.scalar(
            select(func.count()).select_from(Publication).where(Publication.phase != "DONE")
        )
    # 扫完整次待发布队列。无法修复的记录保留原意图和原因供运维检查。
    for _ in range((total + 49) // 50 + 1):
        publish_pending(sessions, store, limit=50)
    checked, unavailable, cursor = 0, [], ""
    while True:
        with sessions() as db:
            ids = db.scalars(
                select(Asset.id).where(Asset.id > cursor).order_by(Asset.id).limit(100)
            ).all()
        if not ids:
            break
        for asset_id in ids:
            if not reconcile_asset(sessions, store, asset_id, repair=True):
                unavailable.append(asset_id)
        checked += len(ids)
        cursor = ids[-1]
    with sessions() as db:
        waiting = db.scalars(select(Publication.id).where(Publication.phase != "DONE")).all()
    return {
        "checked_assets": checked,
        "unavailable_assets": unavailable,
        "waiting_publications": waiting,
    }


def main():
    """运行停写备份、核验或隔离恢复命令并输出恢复点与耗时。"""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("create")
    create.add_argument("backup", type=Path)
    create.add_argument("--root", metavar="NAME=PATH", action="append", required=True)
    check = sub.add_parser("verify")
    check.add_argument("backup", type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("backup", type=Path)
    restore.add_argument("destination", type=Path)
    repair = sub.add_parser("reconcile-files")
    repair.add_argument("data_root", type=Path)
    args = parser.parse_args()
    if args.action == "reconcile-files":
        sessions = make_sessions(os.environ["MYCOMFY_DATABASE_URL"])
        try:
            result = reconcile_files(sessions, args.data_root)
        finally:
            sessions.kw["bind"].dispose()
        print(json.dumps(result))
        if result["unavailable_assets"] or result["waiting_publications"]:
            raise SystemExit(1)
        return
    start = datetime.now(UTC)
    if args.action == "verify":
        result = verify_backup(args.backup)
    else:
        engine = make_sessions(os.environ["MYCOMFY_DATABASE_URL"]).kw["bind"]
        try:
            if args.action == "restore":
                result = restore_backup(engine, args.backup, args.destination)
            else:
                roots = dict(item.split("=", 1) for item in args.root)
                result = create_backup(engine, args.backup, roots)
        finally:
            engine.dispose()
    print(
        json.dumps(
            {
                "recovery_point": result["recovery_point"],
                "files": len(result["files"]),
                "elapsed_seconds": (datetime.now(UTC) - start).total_seconds(),
            }
        )
    )


if __name__ == "__main__":
    main()
