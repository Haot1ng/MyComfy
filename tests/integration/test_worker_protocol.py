"""用可控制回执及完成顺序的 ComfyUI HTTP 替身检验 Worker 协议。"""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import httpx
import pytest
from sqlalchemy import func, select

from mycomfy.artifacts.storage import LocalStore
from mycomfy.config import Settings
from mycomfy.database.models import (
    Asset,
    Batch,
    EngineInstance,
    Execution,
    Incompatibility,
    Job,
    User,
)
from mycomfy.database.session import db_now
from mycomfy.engine.adapter import ComfyAdapter
from mycomfy.generation.jobs import cancel_job
from mycomfy.generation.state import effective_priority
from mycomfy.generation.strategy import sql_publish
from mycomfy.generation.worker import Worker, coordinator_lock


class EngineDouble:
    def __init__(self):
        """记录所有到达替身的请求，分别控制提交回执、队列状态与终态。"""
        self.posts = []
        self.interrupts = []
        self.running = set()
        self.history = {}
        self.drop_receipt = False
        self.reject = False

    def client(self, runtime):
        """每次返回相同引擎状态绑定的真实适配器。"""
        return ComfyAdapter(runtime, transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        """实现本组用例所需的提交、队列、历史和定向中断协议。"""
        path = request.url.path
        if path == "/prompt":
            payload = json.loads(request.content)
            self.posts.append(payload)
            if self.reject:
                return httpx.Response(400, json={"error": {"type": "invalid_prompt"}})
            self.running.add(payload["prompt_id"])
            if self.drop_receipt:
                # 引擎已经入队，丢弃响应模拟最易导致重复执行的窗口。
                raise httpx.ReadTimeout("simulated response loss")
            return httpx.Response(200, json={"prompt_id": payload["prompt_id"]})
        if path == "/queue":
            return httpx.Response(200, json={"queue_running": [[0, p] for p in self.running]})
        if path.startswith("/history/"):
            prompt = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200, json={prompt: self.history[prompt]} if prompt in self.history else {}
            )
        if path == "/interrupt":
            self.interrupts.append(json.loads(request.content)["prompt_id"])
            return httpx.Response(200)
        raise AssertionError(f"Unexpected engine request {path}")

    def complete(self, root, *, failure=False):
        """写出小型真实 MP4，并让历史查询返回对应完成证据。"""
        payload = self.posts[-1]
        prompt_id = payload["prompt_id"]
        self.running.discard(prompt_id)
        if failure:
            self.history[prompt_id] = {"status": {"status_str": "error", "completed": False}}
            return
        prefix = payload["prompt"]["42"]["inputs"]["filename_prefix"]
        path = root / (prefix + "_00001.mp4")
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=32x32:d=0.12",
                "-c:v",
                "mpeg4",
                str(path),
            ],
            check=True,
        )
        self.history[prompt_id] = {
            "status": {"status_str": "success", "completed": True},
            "outputs": {
                "42": {
                    "videos": [
                        {
                            "filename": path.name,
                            "type": "output",
                            "subfolder": str(path.parent.relative_to(root)),
                        }
                    ]
                }
            },
        }


@pytest.fixture
def setup_worker(mysql_sessions, tmp_path):
    """建立一个固定策略、实例与数据库任务，使用真实 MySQL 锁执行 Worker。"""
    settings = Settings(database_url="test", data_root=tmp_path)
    engine = EngineDouble()
    store = LocalStore(tmp_path)
    worker = Worker(
        settings, mysql_sessions, store, engine.client, stop_generation=lambda *args: False
    )
    worker.capacity.initialize(mysql_sessions)
    for sub in ("input", "output"):
        (tmp_path / sub).mkdir()
    with mysql_sessions.begin() as db:
        now = db_now(db)
        db.add(User(id="u", username="worker-test", password_hash="placeholder"))
        db.flush()
        version = sql_publish(
            db,
            {
                "strategy_key": "worker-test",
                "name": "Worker test",
                "workflow": {
                    "42": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "original"}}
                },
                "input_schema": {"type": "object"},
                "bindings": [],
                "requirements": {},
                "progress_mapping": [],
                "output_contract": [
                    {"role": "video", "node_id": "42", "media_types": ["video/mp4"]}
                ],
                "retry": {"max_retries": 2, "interval_seconds": 0},
                "scheduling": {"base_priority": 5},
                "artifacts": {"expose_after_cancel": False},
            },
            "request",
        )
        db.add(
            Batch(
                id="batch", user_id="u", client_key="batch", request_digest="0" * 64, created_at=now
            )
        )
        db.flush()
        db.add(
            Job(
                id="job",
                batch_id="batch",
                user_id="u",
                strategy_version_id=version.id,
                client_key="t",
                position=0,
                inputs={},
                state="QUEUED",
                version=1,
                base_priority=5,
                created_at=now,
                enqueued_at=now,
            )
        )
        db.add(
            EngineInstance(
                id="engine",
                generation=7,
                manifest={},
                configured=True,
                accepting=True,
                heartbeat_at=now,
                runtime={
                    "base_url": "http://127.0.0.1:8188",
                    "input_dir": str(tmp_path / "input"),
                    "output_dir": str(tmp_path / "output"),
                    "timeout_seconds": 120,
                    "reconcile_seconds": 30,
                    "stale_seconds": 1000,
                    "directed_interrupt": True,
                },
            )
        )
    return worker, engine


@pytest.mark.mysql
@pytest.mark.parametrize("finish_before_restart", [True, False])
def test_response_loss_and_restart(setup_worker, finish_before_restart):
    """R01 在回执丢失、引擎运行或完成时重启，验证一笔提交与一个可读产物。"""
    worker, engine = setup_worker
    engine.drop_receipt = True
    worker.tick()
    if finish_before_restart:
        engine.complete(worker.settings.data_root / "output")
    # 新协调器只使用数据库与文件恢复，原进程中的内存状态不可见。
    restarted = Worker(
        worker.settings,
        worker.sessions,
        worker.store,
        engine.client,
        stop_generation=worker.stop_generation,
    )
    restarted.tick()
    if not finish_before_restart:
        engine.complete(worker.settings.data_root / "output")
        restarted.tick()
    with worker.sessions.begin() as db:
        assert db.get(Job, "job").state == "SUCCEEDED"
        assert db.get(EngineInstance, "engine").active_execution_id is None
        assert len(engine.posts) == db.scalar(select(func.count()).select_from(Execution)) == 1
        asset = db.scalars(select(Asset)).one()
        assert worker.store.path(asset.object_key).stat().st_size == asset.size


@pytest.mark.mysql
def test_cancel_before_authorization(setup_worker):
    """A06 在准备完成、授权提交前取消，引擎请求数和预算扣费均为零。"""
    worker, engine = setup_worker
    exec_id = worker.claim("engine")
    worker.prepare("engine", exec_id)
    with worker.sessions.begin() as db:
        cancel_job(db, "job", "u", None)
    worker.tick()
    with worker.sessions.begin() as db:
        assert (db.get(Job, "job").state, db.get(Job, "job").budget_used) == ("CANCELLED", 0)
        assert db.get(Execution, exec_id).phase == "NOT_SUBMITTED"
        assert db.get(EngineInstance, "engine").active_execution_id is None
    assert not engine.posts


@pytest.mark.mysql
def test_cancel_after_submit_keeps_slot(setup_worker):
    """A07 建立已授权执行后取消，只能确认终止后释放占用。"""
    worker, engine = setup_worker
    worker.tick()
    with worker.sessions.begin() as db:
        cancel_job(db, "job", "u", None)
    worker.tick()
    with worker.sessions.begin() as db:
        assert db.get(EngineInstance, "engine").active_execution_id
        assert db.get(Job, "job").state == "CANCELLED"
    assert engine.interrupts == [engine.posts[0]["prompt_id"]]
    engine.complete(worker.settings.data_root / "output")
    worker.tick()
    with worker.sessions.begin() as db:
        assert db.get(Job, "job").state == "CANCELLED"
        assert db.get(EngineInstance, "engine").active_execution_id is None
        assert db.scalars(select(Asset)).one().complete
    assert len(engine.posts) == 1


@pytest.mark.mysql
def test_explicit_rejection_refunded_once(setup_worker):
    """A05 明确拒收退回一次预算并阻塞不兼容的部署组合。"""
    worker, engine = setup_worker
    engine.reject = True
    for _ in range(3):
        worker.tick()
    with worker.sessions.begin() as db:
        job = db.get(Job, "job")
        assert (job.state, job.budget_used, job.attempt_count) == ("QUEUED", 0, 1)
        assert job.blocked_reason == "NO_COMPATIBLE_DEPLOYMENT"
        assert db.scalars(select(Incompatibility)).one()
    assert len(engine.posts) == 1


@pytest.mark.mysql
def test_retry_budget(setup_worker):
    """A05 连续失败使用同一策略的三个生成额度，额外轮询保持最终失败。"""
    worker, engine = setup_worker
    worker.tick()
    for _ in range(3):
        engine.complete(worker.settings.data_root / "output", failure=True)
        worker.tick()
    worker.tick()
    with worker.sessions.begin() as db:
        assert db.get(Job, "job").state == "FAILED"
        assert db.get(Job, "job").budget_used == 3
    assert len(engine.posts) == 3


@pytest.mark.mysql
def test_unconfirmed_exit_quarantines_instance(setup_worker):
    """R02 无法证明旧进程退出时保留占用，禁止启动替代执行。"""
    worker, engine = setup_worker
    engine.drop_receipt = True
    worker.tick()
    engine.running.clear()
    with worker.sessions.begin() as db:
        execution = db.scalars(select(Execution)).one()
        execution.updated_at = db_now(db) - timedelta(minutes=30)
    worker.tick()
    worker.tick()
    with worker.sessions.begin() as db:
        instance = db.get(EngineInstance, "engine")
        assert instance.quarantine_reason == "PROCESS_EXIT_UNCONFIRMED"
        assert instance.active_execution_id == execution.id
        assert db.get(Job, "job").state == "RECOVERING"
    assert len(engine.posts) == 1


@pytest.mark.mysql
def test_competing_claims(setup_worker):
    """A04 同时领取同一实例只允许一个执行，所有线程观察到一致占用。"""
    worker, _ = setup_worker
    barrier = Barrier(2)

    def concurrent_claim():
        """同步两个独立数据库连接的领取开始时刻。"""
        barrier.wait(timeout=10)
        return worker.claim("engine")

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(concurrent_claim) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results.count(None) == 1
    with worker.sessions.begin() as db:
        assert db.get(EngineInstance, "engine").active_execution_id in results
        assert db.scalar(select(func.count()).select_from(Execution)) == 1


def test_aging_boundary_and_process_exclusion(tmp_path):
    """验证等待满周期才加分，以及第二个协调器无法获得同一把本机锁。"""
    from datetime import datetime

    start = datetime(2026, 1, 1)
    assert effective_priority(5, start, start + timedelta(seconds=59), 60) == 5
    assert effective_priority(5, start, start + timedelta(seconds=60), 60) == 6
    with coordinator_lock(tmp_path), pytest.raises(RuntimeError), coordinator_lock(tmp_path):
        pytest.fail("Second coordinator acquired the lock")


def test_real_websocket_ignores_other_prompts():
    """通过真实回环 WebSocket 验证节点映射只接纳当前 prompt 的采样。"""
    import time
    from threading import Event, Thread

    from websockets.sync.server import serve

    from mycomfy.engine.progress import ProgressChannel

    done = Event()

    def publish(connection):
        """发送正确与过时的样本，并保持连接直到测试读取结束。"""
        assert connection.request.path == "/ws?clientId=execution-7"
        for prompt, node in [("current", "8"), ("old-generation", "9")]:
            connection.send(
                json.dumps(
                    {
                        "type": "progress",
                        "data": {
                            "prompt_id": prompt,
                            "node": node,
                            "value": 2,
                            "max": 5,
                        },
                    }
                )
            )
        done.wait(5)

    with serve(publish, "127.0.0.1", 0) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.socket.getsockname()[1]
        channel = ProgressChannel({"base_url": f"http://127.0.0.1:{port}"}, "execution-7")
        try:
            sample = None
            deadline = time.monotonic() + 3
            while sample is None and time.monotonic() < deadline:
                sample = channel.sample("current", {"8": "sampling", "9": "encoding"})
                if sample is None:
                    time.sleep(0.01)
            assert sample == {"stage": "sampling", "node_id": "8", "node_progress": 0.4}
        finally:
            done.set()
            channel.close()
            server.shutdown()
            thread.join(5)


@pytest.mark.mysql
def test_resolved_quarantine_recovers_completed_output(setup_worker):
    """R02 只凭已核实的同代停止凭据恢复独占目录，重启轮询不能重复生成。"""
    from mycomfy.engine.runtime import administrative_action, stop_generation

    worker, engine = setup_worker
    directory = worker.settings.data_root / "generation-7"
    directory.mkdir()
    with worker.sessions.begin() as db:
        instance = db.get(EngineInstance, "engine")
        instance.runtime = {**instance.runtime, "run_dir": str(directory)}
    worker.tick()
    engine.complete(worker.settings.data_root / "output")
    engine.history.clear()
    with worker.sessions.begin() as db:
        execution = db.scalars(select(Execution)).one()
        execution_id = execution.id
    worker.quarantine("engine", execution_id, "PROCESS_EXIT_UNCONFIRMED")
    worker.tick()
    with worker.sessions.begin() as db:
        assert db.get(Execution, execution_id).phase == "QUARANTINED"
        assert db.get(EngineInstance, "engine").active_execution_id == execution_id
    administrative_action(
        worker.sessions, "resolve", "engine", 7, "verified old process tree has exited"
    )
    worker.stop_generation = stop_generation
    worker.tick()
    with worker.sessions.begin() as db:
        assert db.get(Job, "job").state == "SUCCEEDED"
        assert db.get(EngineInstance, "engine").active_execution_id is None
        assert db.scalars(select(Asset)).one().complete
    assert len(engine.posts) == 1


@pytest.mark.mysql
def test_stale_generation_cannot_release_slot(setup_worker):
    """R03 延迟的旧执行结果无法推进任务或释放新代次的占用。"""
    worker, _ = setup_worker
    worker.tick()
    with worker.sessions.begin() as db:
        instance = db.get(EngineInstance, "engine")
        execution_id = instance.active_execution_id
        instance.generation += 1
    worker.finish("engine", execution_id, "FAILED", {}, "DELAYED_OLD_GENERATION")
    with worker.sessions.begin() as db:
        assert db.get(EngineInstance, "engine").active_execution_id == execution_id
        assert db.get(Job, "job").state == "EXECUTING"


@pytest.mark.mysql
def test_draining_finishes_inflight_without_claiming_more(setup_worker):
    """停机请求发生后完成已提交执行，同时保持后到的任务处于排队状态。"""
    worker, engine = setup_worker
    worker.tick()
    worker.draining.set()
    assert not worker.drained()
    with worker.sessions.begin() as db:
        job = db.get(Job, "job")
        now = db_now(db)
        db.add(
            Job(
                id="later",
                batch_id=job.batch_id,
                user_id=job.user_id,
                position=1,
                client_key="later",
                strategy_version_id=job.strategy_version_id,
                inputs={},
                state="QUEUED",
                version=1,
                base_priority=1,
                created_at=now,
                enqueued_at=now,
            )
        )
    engine.complete(worker.settings.data_root / "output")
    worker.tick()
    assert worker.drained()
    with worker.sessions.begin() as db:
        assert db.get(Job, "job").state == "SUCCEEDED"
        assert db.get(Job, "later").state == "QUEUED"
    assert len(engine.posts) == 1


@pytest.mark.mysql
def test_missing_required_source_is_charged_once(setup_worker):
    """A05 引擎报告成功但独占目录中的必需源缺失时，进入新的生成尝试。"""
    worker, engine = setup_worker
    worker.tick()
    engine.complete(worker.settings.data_root / "output")
    initial = engine.posts[0]
    prefix = initial["prompt"]["42"]["inputs"]["filename_prefix"]
    (worker.settings.data_root / "output" / (prefix + "_00001.mp4")).unlink()
    worker.tick()
    with worker.sessions.begin() as db:
        first = db.scalar(select(Execution).where(Execution.prompt_id == initial["prompt_id"]))
        assert first.budget_charged and first.budget_settled
        assert db.get(Job, "job").budget_used == 2
    assert len(engine.posts) == 2


@pytest.mark.mysql
def test_priority_orders_by_age_time_and_id(setup_worker, monkeypatch):
    """A04 通过统一数据库时刻验证完整周期、相同有效分及同时间 ID 排序。"""
    worker, engine = setup_worker
    with worker.sessions.begin() as db:
        now = db_now(db)
        job = db.get(Job, "job")
        job.enqueued_at = now - timedelta(seconds=60)
        for index, (name, priority, seconds) in enumerate(
            [("j-59", 5, 59), ("j-b", 6, 5), ("j-a", 6, 5)], start=1
        ):
            db.add(
                Job(
                    id=name,
                    batch_id=job.batch_id,
                    user_id=job.user_id,
                    position=index,
                    client_key=name,
                    strategy_version_id=job.strategy_version_id,
                    inputs={},
                    state="QUEUED",
                    version=1,
                    base_priority=priority,
                    created_at=now,
                    enqueued_at=now - timedelta(seconds=seconds),
                )
            )
    monkeypatch.setattr("mycomfy.generation.worker.db_now", lambda db: now)
    ordered = []
    for _ in range(4):
        execution_id = worker.claim("engine")
        with worker.sessions.begin() as db:
            job_id = db.get(Execution, execution_id).job_id
            ordered.append(job_id)
            cancel_job(db, job_id, "u", None)
        worker.process("engine", execution_id)
    assert ordered == ["job", "j-a", "j-b", "j-59"]
    assert not engine.posts
