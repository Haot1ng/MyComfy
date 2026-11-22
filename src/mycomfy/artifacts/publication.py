"""本地正式文件发布与重启恢复。数据库阶段在文件 I/O 两侧分别提交。"""

import fcntl
import logging
import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import exists, select

from mycomfy.database.models import (
    Asset,
    EngineInstance,
    Execution,
    Job,
    JobInput,
    Publication,
    StrategyVersion,
)
from mycomfy.database.session import db_now, new_id
from mycomfy.generation.state import advance, emit, settle_failure

from .storage import (
    MediaInvalid,
    ObjectConflict,
    copy_bytes,
    durable_directory,
    file_signature,
    fsync_directory,
    inspect_media,
    materialized_input_name,
)

LOG = logging.getLogger(__name__)


def register_outputs(db, execution, job, strategy, outputs, now):
    """在引擎结束事务中固定来源、角色与正式对象键，同时核对数量。"""
    from mycomfy.ops.capacity import release_reservation, reserve_publication

    existing = {
        (row.role, row.output_index): row
        for row in db.scalars(select(Publication).where(Publication.execution_id == execution.id))
    }
    total = 0
    wrong_counts = []
    for contract in strategy.content["output_contract"]:
        role = contract["role"]
        sources = outputs.get(role, [])
        if not contract["min_count"] <= len(sources) <= contract["max_count"]:
            wrong_counts.append(role)
        for index, source in enumerate(sources):
            total += 1
            if (role, index) in existing:
                continue
            db.add(
                Publication(
                    id=new_id("pub"),
                    execution_id=execution.id,
                    role=role,
                    output_index=index,
                    source_path=str(Path(source).absolute()),
                    object_key=f"outputs/{execution.id}/{role}-{index}",
                    contract=contract,
                    phase="PENDING",
                )
            )
    # 只有当次完整的引擎结果可以驱动当前任务的生成预算。
    if wrong_counts and job.state == "PUBLISHING":
        settle_failure(db, job, execution, strategy.content, now, "OUTPUT_COUNT_MISMATCH")
    if not total:
        release_reservation(db, execution.id)
    elif not existing:
        # 引擎已结束，本次执行只需为尚未发布的正式副本持有目标卷预算。
        reserve_publication(db, execution.id, outputs)


@contextmanager
def object_lock(target):
    """用目标目录的本机文件锁串行化发布器和完整性修复。"""
    durable_directory(target.parent)
    fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _inspect_stage(path, contract):
    """保留部分文件的实际摘要，并区分永久媒体错误与临时读取错误。"""
    try:
        kind, media = inspect_media(path, contract)
        complete = True
        reason = None
    except MediaInvalid as exc:
        reason = str(exc)
        complete = False
        try:
            kind, media = inspect_media(path, {"require_decodable": False})
        except MediaInvalid:
            kind, media = "application/octet-stream", {}
    return {"media_type": kind, "media": media, "complete": complete}, reason


def _stage(publication, store):
    """将引擎源文件复制到目标卷，在该卷验证内容并持久化目录项。"""
    path = store.stage_path(publication.object_key)
    # 中断留下的暂存可能是正式对象的硬链接；重新复制前先摘除旧目录项。
    path.unlink(missing_ok=True)
    with Path(publication.source_path).open("rb") as source, path.open("xb") as target:
        size, digest = copy_bytes(source, target)
    verification, reason = _inspect_stage(path, publication.contract)
    verification.update(size=size, digest=digest)
    fsync_directory(path.parent)
    return verification, reason


def _make_readable(publication, store):
    """依据已落库的摘要恢复正式文件，拒绝覆盖不同内容。"""
    verified = publication.verification
    expected = (verified["size"], verified["digest"])
    key = publication.object_key
    target = store.path(key)
    if target.exists():
        if file_signature(target) != expected:
            raise ObjectConflict("OBJECT_DIGEST_CONFLICT")
        fsync_directory(target.parent)
        return
    stage = store.stage_path(key)
    if not stage.exists() or file_signature(stage) != expected:
        # 已有验证记录时，回放也必须与原始字节一致。
        stage.unlink(missing_ok=True)
        with Path(publication.source_path).open("rb") as source, stage.open("xb") as out:
            if copy_bytes(source, out) != expected:
                raise ObjectConflict("SOURCE_DIGEST_CONFLICT")
    store.publish(key, *expected)


def _visible(job, strategy):
    """按任务固定的策略计算取消后文件是否对用户可见。"""
    return job.state != "CANCELLED" or strategy.content["artifacts"]["expose_after_cancel"]


def _register_asset(db, row, execution, job, strategy, now):
    """按发布意图的稳定标识登记文件，只在首次可见时产生事件。"""
    asset_id = row.id.replace("pub_", "a_", 1)
    asset = db.get(Asset, asset_id)
    if asset is None:
        asset = Asset(
            id=asset_id,
            user_id=job.user_id,
            job_id=job.id,
            execution_id=execution.id,
            role=row.role,
            output_index=row.output_index,
            object_key=row.object_key,
            provider="local",
            available=True,
            created_at=now,
            **row.verification,
        )
        from mycomfy.ops.capacity import credit_published

        db.add(asset)
        credit_published(db, execution.id, asset.size)
        if _visible(job, strategy):
            job.version += 1
            emit(db, job, now, "asset.available", asset_id=asset.id, complete=asset.complete)
    row.phase = "REGISTERED"
    if row.verification["complete"]:
        row.reason = None
    elif not row.reason or row.reason.startswith("STORAGE_WAIT_"):
        row.reason = "INVALID_OUTPUT"
    db.flush()
    return asset


def _finish_group(db, execution, job, strategy, now, store):
    """核验全部必需产物后提交任务结论，整组处理完后释放预留。"""
    rows = db.scalars(select(Publication).where(Publication.execution_id == execution.id)).all()
    if job.state != "PUBLISHING" or job.current_execution_id != execution.id:
        return
    for contract in strategy.content["output_contract"]:
        if not contract["min_count"]:
            continue
        required = [r for r in rows if r.role == contract["role"]]
        if not contract["min_count"] <= len(required) <= contract["max_count"]:
            settle_failure(db, job, execution, strategy.content, now, "OUTPUT_COUNT_MISMATCH")
            return
        if any(
            r.phase in {"REGISTERED", "DONE"} and not r.verification["complete"] for r in required
        ):
            settle_failure(db, job, execution, strategy.content, now, "INVALID_REQUIRED_OUTPUT")
            return
        if any(
            r.phase not in {"REGISTERED", "DONE"}
            or not store.readable(r.object_key, r.verification["size"])
            for r in required
        ):
            return
    advance(db, job, "SUCCEEDED", now)
    execution.budget_settled = True


def _publish_one(sessions, store, publication_id):
    """恢复单个发布意图，支持暂存落盘、排他发布和登记后的重复运行。"""
    with sessions.begin() as db:
        row = db.get(Publication, publication_id)
        if row is None:
            return False
        key = row.object_key
    with object_lock(store.path(key)):
        with sessions.begin() as db:
            row = db.get(Publication, publication_id)
        if row.phase in {"REGISTERED", "DONE"}:
            return False
        if row.phase == "PENDING":
            info, reason = _stage(row, store)
            with sessions.begin() as db:
                row = db.get(Publication, publication_id, with_for_update=True)
                row.verification, row.reason, row.phase = info, reason, "VERIFIED"
        _make_readable(row, store)
        # 正式文件已持久化，此后崩溃可直接依据对象键和摘要恢复登记。
        with sessions.begin() as db:
            execution = db.get(Execution, row.execution_id)
            job = db.get(Job, execution.job_id, with_for_update=True)
            db.refresh(execution, with_for_update=True)
            current = db.get(Publication, publication_id, with_for_update=True)
            strategy = db.get(StrategyVersion, job.strategy_version_id)
            now = db_now(db)
            _register_asset(db, current, execution, job, strategy, now)
            _finish_group(db, execution, job, strategy, now, store)
        return True


def _execution_directory(execution, setting):
    """只认领已保存运行配置下、以当前执行 ID 命名的真实目录。"""
    parent = execution.workspace.get(setting)
    if not parent or not Path(parent).is_absolute() or "/" in execution.id:
        return None
    expected = Path(parent).resolve() / execution.id
    if expected.is_dir() and expected.resolve() == expected:
        return expected
    return None


def _remove_empty(directory):
    """尝试回收空执行目录，目录中还有未知文件时保留现场。"""
    import errno

    try:
        directory.rmdir()
    except FileNotFoundError:
        pass
    except OSError as exc:
        if exc.errno != errno.ENOTEMPTY:
            raise


def cleanup_inputs(sessions, store, execution_id):
    """在执行已结清且没有活跃占用时，回收与授权素材完全相同的输入副本。"""
    with sessions.begin() as db:
        execution = db.get(Execution, execution_id)
        if execution is None or db.scalar(
            select(EngineInstance.id).where(EngineInstance.active_execution_id == execution_id)
        ):
            return
        directory = _execution_directory(execution, "input_dir")
        if directory is None:
            return
        assets = {
            materialized_input_name(asset): asset
            for asset in db.scalars(
                select(Asset).join(JobInput).where(JobInput.job_id == execution.job_id)
            )
        }
    for path in directory.iterdir():
        asset = assets.get(path.name)
        if (
            asset
            and not path.is_symlink()
            and path.is_file()
            and asset.available
            and file_signature(path) == (asset.size, asset.digest)
            and store.readable(asset.object_key, asset.size)
            and file_signature(store.path(asset.object_key)) == (asset.size, asset.digest)
        ):
            path.unlink(missing_ok=True)
    _remove_empty(directory)


def _cleanup_registered(sessions, store, publication_id):
    """在正式对象和 Asset 全组提交后回收可证明的副本，再核销磁盘预留。"""
    from mycomfy.ops.capacity import release_reservation

    with sessions.begin() as db:
        current = db.get(Publication, publication_id)
        if current is None or current.phase != "REGISTERED":
            return
    # 整组共享同一执行正式目录；不要让两个恢复器同时清除源或重复释放。
    with object_lock(store.path(current.object_key)):
        with sessions.begin() as db:
            execution = db.get(Execution, current.execution_id)
            rows = db.scalars(
                select(Publication).where(Publication.execution_id == execution.id)
            ).all()
            if not all(r.phase in {"REGISTERED", "DONE"} for r in rows):
                return
            if not any(r.phase == "REGISTERED" for r in rows):
                return
        # 单个部分或损坏输出使该执行的源目录成为诊断依据。
        directory = _execution_directory(execution, "output_dir")
        clean_sources = directory and all(r.verification["complete"] for r in rows)
        for row in rows:
            _make_readable(row, store)
        for row in rows:
            store.stage_path(row.object_key).unlink(missing_ok=True)
            source = Path(row.source_path)
            if (
                clean_sources
                and source.resolve() == source
                and source.is_relative_to(directory)
                and source.is_file()
                and file_signature(source) == (row.verification["size"], row.verification["digest"])
            ):
                source.unlink()
                parent = source.parent
                while parent != directory:
                    _remove_empty(parent)
                    parent = parent.parent
        if clean_sources:
            _remove_empty(directory)
        cleanup_inputs(sessions, store, execution.id)
        with sessions.begin() as db:
            for row in db.scalars(
                select(Publication)
                .where(Publication.execution_id == execution.id)
                .with_for_update()
            ):
                row.phase = "DONE"
            release_reservation(db, execution.id)


def publish_one(sessions, store, publication_id):
    """从持久阶段继续发布或清理一个产物所在的完整产物组。"""
    registered = _publish_one(sessions, store, publication_id)
    _cleanup_registered(sessions, store, publication_id)
    return registered


def publish_pending(sessions, store, limit=20, *, before_file=None):
    """按稳定游标轮询未完成意图，让持续失败的文件不会阻塞后续任务。"""
    pending = select(Publication.id).where(
        Publication.phase.in_(("PENDING", "VERIFIED", "CONFLICT", "REGISTERED"))
    )
    cursor = getattr(store, "_publication_cursor", "")
    with sessions.begin() as db:
        ids = list(
            db.scalars(pending.where(Publication.id > cursor).order_by(Publication.id).limit(limit))
        )
        if not ids and cursor:
            ids = list(db.scalars(pending.order_by(Publication.id).limit(limit)))
    complete = 0
    for publication_id in ids:
        # 每个文件都有独立的复制与解码时间预算，队列长度不影响健康判定。
        if before_file is not None:
            before_file()
        store._publication_cursor = publication_id
        try:
            complete += int(publish_one(sessions, store, publication_id))
        except OSError as exc:
            conflict = isinstance(exc, ObjectConflict)
            reason = str(exc) if conflict else f"STORAGE_WAIT_{exc.errno or 'IO'}"
            with sessions.begin() as db:
                row = db.get(Publication, publication_id, with_for_update=True)
                if row.phase == "DONE":
                    continue
                if row.reason != reason:
                    LOG.warning("publication %s: %s", publication_id, reason)
                row.reason = reason
                if conflict and row.phase != "REGISTERED":
                    row.phase = "CONFLICT"
    # 同组必需文件都已登记、任务快照仍在发布时，从持久事实补齐最终结论。
    unfinished = exists(
        select(Publication.id).where(
            Publication.execution_id == Job.current_execution_id, Publication.phase != "DONE"
        )
    )
    with sessions.begin() as db:
        jobs = db.scalars(
            select(Job.id)
            .where(Job.state == "PUBLISHING", ~unfinished)
            .order_by(Job.id)
            .limit(limit)
        ).all()
    for job_id in jobs:
        with sessions.begin() as db:
            job = db.get(Job, job_id, with_for_update=True)
            if job.state == "PUBLISHING" and job.current_execution_id:
                execution = db.get(Execution, job.current_execution_id, with_for_update=True)
                version = db.get(StrategyVersion, job.strategy_version_id)
                _finish_group(db, execution, job, version, db_now(db), store)
    return complete


def reconcile_asset(sessions, store, asset_id, repair=False):
    """核对正式文件摘要，缺失时可从受保护源恢复，并独立更新可用性。"""
    with sessions.begin() as db:
        asset = db.get(Asset, asset_id)
        if asset is None:
            raise KeyError(asset_id)
        publication = (
            db.scalar(
                select(Publication).where(
                    Publication.execution_id == asset.execution_id,
                    Publication.role == asset.role,
                    Publication.output_index == asset.output_index,
                )
            )
            if asset.execution_id
            else None
        )
    expected = (asset.size, asset.digest)
    target = store.path(asset.object_key)
    with object_lock(target):
        try:
            if not target.exists() and repair and publication:
                _make_readable(publication, store)
            available = file_signature(target) == expected
        except (FileNotFoundError, ObjectConflict):
            available = False
        with sessions.begin() as db:
            job = db.get(Job, asset.job_id, with_for_update=True) if asset.job_id else None
            asset = db.get(Asset, asset_id, with_for_update=True)
            if asset.available != available:
                asset.available = available
                LOG.warning("asset %s availability changed to %s", asset_id, available)
                if job and _visible(job, db.get(StrategyVersion, job.strategy_version_id)):
                    job.version += 1
                    event = "asset.available" if available else "asset.unavailable"
                    emit(db, job, db_now(db), event, asset_id=asset.id)
        if available and repair and publication:
            store.stage_path(asset.object_key).unlink(missing_ok=True)
    return available
