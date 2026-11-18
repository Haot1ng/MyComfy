"""覆盖 MySQL 短事务下的卷预留、隔离恢复和限制日志读取。"""

import hashlib
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from starlette.requests import Request

from mycomfy import capacity as capacity_module
from mycomfy.backup import create_backup, restore_backup, verify_backup
from mycomfy.capacity import Capacity, credit_published, reserve_publication
from mycomfy.config import Settings
from mycomfy.db import make_sessions
from mycomfy.errors import ApiError
from mycomfy.models import (
    Asset,
    Batch,
    EngineInstance,
    Execution,
    Job,
    Publication,
    StrategySeries,
    StrategyVersion,
    User,
)
from mycomfy.monitor import log_page, overview
from mycomfy.ops_models import DiskReservation
from mycomfy.publication import publish_pending, reconcile_asset
from mycomfy.schema import migrate
from mycomfy.storage import LocalStore


def policy_for(root):
    """在所有角色共享同一卷的测试拓扑中使用很小的配额。"""
    return Capacity(
        Settings("unused", root, max_upload_bytes=100),
        {
            "user_upload_bytes": 100,
            "user_jobs": 1,
            "global_jobs": 2,
            "volumes": {
                role: {
                    "path": str(root),
                    "stop_bytes": 100,
                    "resume_bytes": 200,
                    "reserve_bytes": 0,
                }
                for role in ("storage", "spool", "database")
            },
        },
    )


@pytest.mark.mysql
def test_capacity_concurrency_hysteresis_and_recovery(mysql_sessions, tmp_path, monkeypatch):
    """并发上传共享同一卷，重启保留活动预留，跨水位后按较高阈值恢复。"""
    capacity = policy_for(tmp_path)
    free = [240]
    monkeypatch.setattr(capacity_module, "measured_volume", lambda path: ("dev1", free[0]))
    capacity.initialize(mysql_sessions)

    def second_upload():
        """断言其他调用者无法消耗同一份空间余量。"""
        with pytest.raises(ApiError) as rejected, capacity.upload(mysql_sessions, "other", 30):
            pytest.fail("Unexpected admission")
        return rejected.value.code

    with capacity.upload(mysql_sessions, "one", 60):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(second_upload).result(timeout=5) == "VOLUME_LOW"
        capacity.initialize(mysql_sessions)
        with mysql_sessions() as db:
            info = overview(db, capacity)["volumes"]["dev1"]
            assert info["reserved_bytes"] == 120
    with capacity.upload(mysql_sessions, "one", 60) as owner:
        capacity.finish_spool(mysql_sessions, owner)
        with mysql_sessions.begin() as db:
            assert overview(db, capacity)["volumes"]["dev1"]["reserved_bytes"] == 60
            capacity.credit_upload(db, owner, 55)
            assert overview(db, capacity)["volumes"]["dev1"]["reserved_bytes"] == 0

    free[0] = 90
    with pytest.raises(ApiError, match="disk space"), mysql_sessions.begin() as db:
        capacity.check_batch(db, "one", 1)
    free[0] = 150
    with pytest.raises(ApiError, match="disk space"), mysql_sessions.begin() as db:
        capacity.check_batch(db, "one", 1)
    free[0] = 200
    with mysql_sessions.begin() as db:
        capacity.check_batch(db, "one", 1)
    free[0] = 2000
    with capacity.upload(mysql_sessions, "one", 70):
        with pytest.raises(ApiError) as exc, capacity.upload(mysql_sessions, "one", 40):
            pytest.fail("Unexpected user quota admission")
        assert exc.value.status == 429


@pytest.mark.mysql
def test_publication_reservation_rebases_oversize_source(mysql_sessions, tmp_path):
    """实际输出大于估计时按目标卷重估；首次 Asset 登记释放已兑现份额。"""
    output = tmp_path / "source"
    output.write_bytes(b"x" * 250)
    with mysql_sessions.begin() as db:
        for device, target in (("engine", 0), ("archive", 100)):
            db.add(
                DiskReservation(
                    owner="exec",
                    device=device,
                    kind="execution",
                    user_id="u",
                    size=100,
                    upload_bytes=0,
                    target_bytes=target,
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
    with mysql_sessions.begin() as db:
        reserve_publication(db, "exec", {"video": [output]})
        assert db.get(DiskReservation, ("exec", "engine")) is None
        reserve_publication(db, "exec", {"video": [output]})
        assert db.get(DiskReservation, ("exec", "archive")).size == 250
        credit_published(db, "exec", 50)
        assert db.get(DiskReservation, ("exec", "archive")).size == 200


def test_log_rotation_redaction_and_cursor(tmp_path):
    """在轮转间继续读取原游标，并验证输出时剔除凭据。"""
    log = tmp_path / "engine.log"
    log.write_text("first\nAuthorization: Bearer value\nlast\n")
    row = Execution(
        id="execution",
        instance_id="engine",
        generation=4,
        workspace={"run_dir": str(tmp_path), "log_path": str(log)},
    )
    request = Request({"type": "http", "query_string": b""})
    first = log_page(row, request, 1, None)
    assert first["items"][0]["message"] == "first"
    log.rename(tmp_path / "engine.log.1")
    log.write_text("after rotation\n")
    following = log_page(row, request, 5, first["next_cursor"])
    assert [item["message"] for item in following["items"]] == [
        "[redacted]",
        "last",
        "after rotation",
    ]


@pytest.fixture
def restore_sessions(mysql_sessions):
    """创建第二个独立迁移的空数据库，结束后仅回收此次恢复库。"""
    source_url = mysql_sessions.kw["bind"].url
    name = f"mycomfy_restore_{uuid4().hex}"
    admin = create_engine(source_url.set(database="mysql"))
    with admin.begin() as sql:
        sql.execute(text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4"))
    try:
        restore_url = source_url.set(database=name).render_as_string(hide_password=False)
        migrate(restore_url)
        sessions = make_sessions(restore_url)
        try:
            yield sessions
        finally:
            sessions.kw["bind"].dispose()
    finally:
        with admin.begin() as sql:
            sql.execute(text(f"DROP DATABASE `{name}`"))
        admin.dispose()


def seed_backup(db, data, protected):
    """保存已成功、已取消、待发布任务和受保护文件，形成可恢复点。"""
    now = datetime.now(UTC).replace(tzinfo=None)
    contract = {"role": "video", "min_count": 1, "max_count": 1, "media_types": ["video/mp4"]}
    store = LocalStore(data)
    store.root.mkdir()
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=32x32:r=1",
            "-frames:v",
            "1",
            "-c:v",
            "mpeg4",
            "-f",
            "mp4",
            str(protected / "output"),
        ],
        check=True,
    )
    generated = (protected / "output").read_bytes()
    store.path("video").write_bytes(generated)
    db.add(User(id="u", username="one", password_hash="hash"))
    db.add(StrategySeries(key="video", latest_version=1))
    db.flush()
    db.add(
        StrategyVersion(
            id="version",
            strategy_key="video",
            version=1,
            name="video",
            content={
                "output_contract": [contract],
                "retry": {"max_retries": 1, "interval_seconds": 0},
                "artifacts": {"expose_after_cancel": False},
            },
            content_digest="a" * 64,
            published_at=now,
            request_id="request",
        )
    )
    db.add(
        Batch(id="batch", user_id="u", client_key="batch", request_digest="b" * 64, created_at=now)
    )
    db.add(
        EngineInstance(
            id="engine", generation=1, manifest={}, runtime={"output_dir": str(protected)}
        )
    )
    db.flush()
    db.add(
        EngineInstance(
            id="busy",
            generation=2,
            manifest={},
            active_execution_id="busy-exec",
            runtime={"pid": 12345, "run_dir": str(protected), "base_url": "http://127.0.0.1:8188"},
        )
    )
    db.flush()
    for pos, state in enumerate(("SUCCEEDED", "CANCELLED", "PUBLISHING", "RECOVERING")):
        db.add(
            Job(
                id=state,
                batch_id="batch",
                user_id="u",
                strategy_version_id="version",
                client_key=state,
                position=pos,
                inputs={},
                current_execution_id={"PUBLISHING": "exec", "RECOVERING": "busy-exec"}.get(state),
                state=state,
                base_priority=0,
                created_at=now,
                enqueued_at=now,
            )
        )
    db.flush()
    db.add(
        Execution(
            id="exec",
            job_id="PUBLISHING",
            instance_id="engine",
            generation=1,
            attempt_no=1,
            phase="ENGINE_SUCCESS",
            environment={},
            workspace={"output_dir": str(protected)},
            updated_at=now,
            created_at=now,
        )
    )
    db.flush()
    db.add(
        Execution(
            id="busy-exec",
            job_id="RECOVERING",
            instance_id="busy",
            generation=2,
            attempt_no=1,
            phase="UNCONFIRMED",
            environment={},
            workspace={
                "output_dir": str(protected),
                "pid": 12345,
                "base_url": "http://127.0.0.1:8188",
            },
            updated_at=now,
            created_at=now,
        )
    )
    db.add(
        Publication(
            id="pub",
            execution_id="exec",
            role="video",
            output_index=0,
            source_path=str(protected / "output"),
            object_key="outputs/exec/video-0",
            contract=contract,
            phase="PENDING",
        )
    )
    db.add(
        Asset(
            id="asset",
            user_id="u",
            job_id="SUCCEEDED",
            role="video",
            object_key="video",
            media_type="video/mp4",
            size=len(generated),
            digest=hashlib.sha256(generated).hexdigest(),
            media={},
            complete=True,
            available=True,
            created_at=now,
        )
    )


@pytest.mark.mysql
def test_backup_isolated_restore_and_corruption(mysql_sessions, restore_sessions, tmp_path):
    """从同一恢复点还原文件和数据库事实，并阻止覆盖与损坏介质恢复。"""
    data, protected = tmp_path / "data", tmp_path / "protected"
    data.mkdir()
    protected.mkdir()
    credentials = data / "maintenance.key"
    credentials.write_text("test fixture")
    credentials.chmod(0o600)
    with mysql_sessions.begin() as db:
        seed_backup(db, data, protected)
    source = mysql_sessions.kw["bind"]
    target = restore_sessions.kw["bind"]
    backup, recovered = tmp_path / "backup", tmp_path / "recovered"
    manifest = create_backup(source, backup, {"data": data, "work": protected})
    assert manifest == verify_backup(backup)
    restore_backup(target, backup, recovered)
    assert (recovered / "data" / "maintenance.key").stat().st_mode & 0o777 == 0o600
    with restore_sessions() as db:
        assert db.get(User, "u").username == "one"
        assert set(db.scalars(select(Job.state))) == {
            "SUCCEEDED",
            "CANCELLED",
            "PUBLISHING",
            "RECOVERING",
        }
        busy = db.get(EngineInstance, "busy")
        assert busy.quarantine_reason and not busy.accepting and busy.runtime["paused"]
        assert busy.active_execution_id == "busy-exec"
        assert "pid" not in busy.runtime and "pid" not in db.get(Execution, "busy-exec").workspace
        assert Path(busy.runtime["run_dir"]).parent == recovered / ".restored-control"
        assert busy.runtime["base_url"] != "http://127.0.0.1:8188"
        assert db.get(Asset, "asset").user_id == "u"
        pub = db.get(Publication, "pub")
        assert pub.phase == "PENDING"
        assert Path(pub.source_path) == recovered / "work" / "output"
        assert Path(pub.source_path).read_bytes() == (protected / "output").read_bytes()
    store = LocalStore(recovered / "data")
    assert reconcile_asset(restore_sessions, store, "asset")
    assert publish_pending(restore_sessions, store) == 1
    with restore_sessions() as db:
        assert db.get(Job, "PUBLISHING").state == "SUCCEEDED"
        assert db.get(Job, "CANCELLED").state == "CANCELLED"
    assert (store.root / "outputs/exec/video-0").read_bytes() == (protected / "output").read_bytes()
    store.path("video").unlink()
    assert not reconcile_asset(restore_sessions, store, "asset")
    with pytest.raises(ValueError, match="already contains"):
        restore_backup(target, backup, tmp_path / "second")
    assert not (tmp_path / "second").exists()
    (backup / "roots" / "data" / "objects" / "video").write_bytes(b"six")
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_backup(backup)


@pytest.mark.mysql
def test_monitor_filters_logs_and_marks_stale_gpu(mysql_sessions, tmp_path):
    """经监控路由按实例、级别及时间读取日志，分别判断进程和 GPU 采样新鲜度。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mycomfy.monitor import build_monitor_router

    settings = Settings("unused", tmp_path)
    target = tmp_path / "engine.log"
    target.write_text("2026-01-01 00:00:00,002 ERROR old\n2026-01-02 00:00:00,003 ERROR current\n")
    now = datetime.now(UTC).replace(tzinfo=None)
    with mysql_sessions.begin() as db:
        db.add(
            EngineInstance(
                id="engine",
                generation=3,
                heartbeat_at=now,
                manifest={},
                runtime={
                    "metrics": {
                        "sampled_at": "2000-01-01T00:00:00+00:00",
                        "gpu_memory_used_mib": 42,
                    },
                    "run_dir": str(tmp_path),
                    "log_path": str(target),
                },
            )
        )
    app = FastAPI()
    app.include_router(build_monitor_router(settings, mysql_sessions))
    with TestClient(app) as http:
        metrics = http.get("/local/v1/monitor/metrics/engine").json()
        assert not metrics["stale"] and metrics["metrics_stale"]
        page = http.get(
            "/local/v1/monitor/logs",
            params={"instance_id": "engine", "level": "ERROR", "since": "2026-01-02T00:00:00Z"},
        )
        assert page.status_code == 200
        assert [r["message"] for r in page.json()["items"]] == [
            "2026-01-02 00:00:00,003 ERROR current"
        ]
