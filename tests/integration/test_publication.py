"""通过真实数据库验证文件持久化边界和重复恢复。"""

import subprocess
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from mycomfy.artifacts import publication
from mycomfy.artifacts.storage import LocalStore
from mycomfy.database.models import (
    Asset,
    Batch,
    EngineInstance,
    Execution,
    Job,
    Publication,
    SourceEvent,
    StrategySeries,
    StrategyVersion,
    User,
)


def publishing_fixture(sessions, tmp_path, *, cancelled=False, exposed=False):
    """构造引擎已完成且生成尝试已扣费的任务，提交独立发布意图。"""
    video = tmp_path / "work-out" / "e" / "clip.mp4"
    video.parent.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=32x24:rate=2",
            "-t",
            "1",
            "-c:v",
            "mpeg4",
            "-y",
            str(video),
        ],
        check=True,
    )
    now = datetime(2026, 11, 18)
    output = {"role": "final_video", "min_count": 1, "max_count": 1, "media_types": ["video/mp4"]}
    with sessions.begin() as db:
        db.add_all(
            [
                User(id="u", username="u", password_hash="unused"),
                StrategySeries(key="video"),
                EngineInstance(id="engine", generation=1, manifest={}),
            ]
        )
        db.flush()
        strategy = StrategyVersion(
            id="v",
            strategy_key="video",
            version=1,
            name="video",
            content_digest="d" * 64,
            published_at=now,
            request_id="r",
            content={
                "output_contract": [output],
                "artifacts": {"expose_after_cancel": exposed},
                "retry": {"max_retries": 0, "interval_seconds": 0},
            },
        )
        db.add_all(
            [
                strategy,
                Batch(id="b", user_id="u", client_key="b", request_digest="d" * 64, created_at=now),
            ]
        )
        db.flush()
        job = Job(
            id="j",
            user_id="u",
            batch_id="b",
            strategy_version_id="v",
            client_key="t",
            position=0,
            inputs={},
            state="CANCELLED" if cancelled else "PUBLISHING",
            version=3,
            base_priority=0,
            budget_used=1,
            attempt_count=1,
            created_at=now,
            enqueued_at=now,
            current_execution_id="e",
        )
        db.add(job)
        db.flush()
        execution = Execution(
            id="e",
            job_id="j",
            instance_id="engine",
            generation=1,
            attempt_no=1,
            phase="SUCCEEDED",
            environment={},
            workspace={
                "output_dir": str(tmp_path / "work-out"),
                "input_dir": str(tmp_path / "work-in"),
            },
            created_at=now,
            updated_at=now,
            budget_charged=True,
        )
        db.add(execution)
        db.flush()
        publication.register_outputs(db, execution, job, strategy, {"final_video": [video]}, now)
        row = db.scalar(select(Publication))
    return LocalStore(tmp_path), row, video


@pytest.mark.mysql
def test_restart_at_file_and_database_boundaries(mysql_sessions, tmp_path):
    """验证半写暂存、发布后登记前崩溃和重复恢复都只生成一条可用文件。"""
    sessions = mysql_sessions
    store, row, video = publishing_fixture(sessions, tmp_path)
    stage = store.stage_path(row.object_key)
    stage.parent.mkdir(parents=True)
    stage.write_bytes(b"partial")
    with (
        patch.object(publication, "_register_asset", side_effect=RuntimeError("crash")),
        pytest.raises(RuntimeError, match="crash"),
    ):
        publication.publish_one(sessions, store, row.id)
    with sessions.begin() as db:
        assert db.get(Job, "j").state == "PUBLISHING"
        assert db.scalar(select(func.count()).select_from(Asset)) == 0
    # 正式文件已有可信摘要，恢复数据库登记时无需重新复制源文件。
    stage.unlink()
    video.unlink()
    assert publication.publish_pending(sessions, store) == 1
    assert publication.publish_pending(sessions, store) == 0
    with sessions.begin() as db:
        asset = db.scalar(select(Asset))
        assert db.get(Job, "j").state == "SUCCEEDED"
        assert db.scalar(select(func.count()).select_from(Asset)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(SourceEvent)
                .where(SourceEvent.type == "job.succeeded")
            )
            == 1
        )
    store.path(row.object_key).unlink()
    assert not publication.reconcile_asset(sessions, store, asset.id)
    with sessions.begin() as db:
        assert db.get(Job, "j").state == "SUCCEEDED"
        assert not db.get(Asset, asset.id).available


@pytest.mark.mysql
@pytest.mark.parametrize("cancelled,exposed", [(False, False), (True, False), (True, True)])
def test_invalid_output_retained(mysql_sessions, tmp_path, cancelled, exposed):
    """坏输出登记实际字节，预算与事件按业务终态和开放策略处理。"""
    sessions = mysql_sessions
    store, row, video = publishing_fixture(sessions, tmp_path, cancelled=cancelled, exposed=exposed)
    video.write_bytes(b"incomplete")
    assert publication.publish_one(sessions, store, row.id)
    assert video.read_bytes() == b"incomplete"
    with sessions.begin() as db:
        asset = db.scalar(select(Asset))
        assert not asset.complete
        assert store.path(asset.object_key).read_bytes() == b"incomplete"
        assert db.get(Job, "j").state == ("CANCELLED" if cancelled else "FAILED")
        assert db.get(Job, "j").budget_used == 1
        assert db.scalar(
            select(func.count())
            .select_from(SourceEvent)
            .where(SourceEvent.type == "asset.available")
        ) == (not cancelled or exposed)


@pytest.mark.mysql
def test_commit_before_worker_ack_and_cleanup(mysql_sessions, tmp_path):
    """资产事务完成后中止，再次扫描只回收已验证的副本并保留陌生文件。"""
    sessions = mysql_sessions
    store, row, video = publishing_fixture(sessions, tmp_path)
    unknown = video.parent / "unknown.mp4"
    unknown.write_bytes(b"unattributed")
    with (
        patch.object(publication, "_cleanup_registered", side_effect=RuntimeError("crash")),
        pytest.raises(RuntimeError, match="crash"),
    ):
        publication.publish_one(sessions, store, row.id)
    with sessions.begin() as db:
        assert db.get(Publication, row.id).phase == "REGISTERED"
        assert db.get(Job, "j").state == "SUCCEEDED"
        asset = db.scalar(select(Asset))
    assert video.exists()
    # 即使正式文件在清理前丢失，仍可先从已验证的受保护源重建。
    store.path(row.object_key).unlink()
    assert publication.reconcile_asset(sessions, store, asset.id, repair=True)
    assert publication.publish_pending(sessions, store) == 0
    with sessions.begin() as db:
        assert db.get(Publication, row.id).phase == "DONE"
        assert db.scalar(select(func.count()).select_from(Asset)) == 1
    assert not video.exists()
    assert not store.stage_path(row.object_key).exists()
    assert unknown.read_bytes() == b"unattributed"
