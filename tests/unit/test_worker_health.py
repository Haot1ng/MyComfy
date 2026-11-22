"""验证容器探针只接受当前 Worker 的进度，并能识别停滞和数据库故障。"""

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from mycomfy.config import Settings
from mycomfy.database.schema import migrate
from mycomfy.generation import worker, worker_health


def test_probe_needs_live_owner_and_unexpired_progress(tmp_path, monkeypatch):
    """检查启动、长文件操作、协调器停滞及重启留下的旧心跳。"""
    path = tmp_path / "worker.heartbeat"
    now = 1000.0
    monkeypatch.setattr(worker_health, "time", SimpleNamespace(monotonic=lambda: now))
    assert worker_health.unhealthy_reason(path)
    with worker_health.report_health(path) as beat:
        assert worker_health.unhealthy_reason(path)
        beat(worker_health.POLL_GRACE)
        assert worker_health.unhealthy_reason(path) is None
        now += worker_health.POLL_GRACE + 1
        assert worker_health.unhealthy_reason(path)

        beat(worker_health.FILE_GRACE)
        now += worker_health.POLL_GRACE + 1
        assert worker_health.unhealthy_reason(path) is None
        now += worker_health.FILE_GRACE
        assert worker_health.unhealthy_reason(path)
        beat(worker_health.POLL_GRACE)
    # 即使上一次心跳仍新鲜，无持锁者或下一进程未推进时都不能报告健康。
    assert worker_health.unhealthy_reason(path)
    with worker_health.report_health(path):
        assert worker_health.unhealthy_reason(path)


def test_idle_worker_invalidates_heartbeat_until_db_recovers(tmp_path, monkeypatch):
    """在真实的空队列轮询中注入连续两次数据库失败，恢复后才接受下一次心跳。"""
    path = tmp_path / "worker.heartbeat"
    monkeypatch.setenv("MYCOMFY_WORKER_HEALTH_FILE", str(path))
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'db'}", data_root=tmp_path)
    migrate(settings.database_url)

    tick = worker.Worker.tick
    healthy = []

    def tick_with_outage(self, *, progress):
        """模拟中段查询失败，以及重试时部分步骤成功但整轮仍然失败。"""
        if len(healthy) in (1, 2):
            progress(worker_health.POLL_GRACE)
            if len(healthy) == 2:
                assert worker_health.unhealthy_reason(path)
            raise SQLAlchemyError("test database unavailable")
        return tick(self, progress=progress)

    def record_sleep(seconds):
        """在循环间隔读取真实心跳文件，在指定次数后结束测试。"""
        healthy.append(worker_health.unhealthy_reason(path) is None)
        if len(healthy) == 4:
            raise StopIteration

    monkeypatch.setattr(worker.Worker, "tick", tick_with_outage)
    monkeypatch.setattr(worker, "time", SimpleNamespace(sleep=record_sleep))
    with pytest.raises(StopIteration):
        worker.run_worker(settings)
    assert healthy == [True, False, False, True]
    assert worker_health.unhealthy_reason(path)
