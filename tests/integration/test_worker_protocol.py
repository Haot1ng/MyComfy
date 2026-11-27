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
    EngineNodeCatalog,
    Execution,
    Incompatibility,
    Job,
    User,
)
from mycomfy.database.session import db_now
from mycomfy.engine.adapter import ComfyAdapter
from mycomfy.generation.jobs import cancel_job, retry_job
from mycomfy.generation.state import effective_priority
from mycomfy.generation.strategy import sql_publish
from mycomfy.generation.worker import Worker, coordinator_lock


class EngineDouble:
    def __init__(self):
        """记录所有到达替身的请求，分别控制提交回执、队列状态与终态。"""
        self.posts = []
        self.interrupts = []
        self.running = set()
        self.pending = set()
        self.history = {}
        self.history_error = None
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
            return httpx.Response(
                200,
                json={
                    "queue_running": [[0, p] for p in self.running],
                    "queue_pending": [[0, p] for p in self.pending],
                },
            )
        if path.startswith("/history/"):
            if self.history_error is not None:
                raise self.history_error
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
def test_claim_uses_catalog_of_one_engine(setup_worker):
    """相同清单的两台引擎各自按实际注册节点领取，另一实例缺节点不会占用任务。"""
    worker, _ = setup_worker
    with worker.sessions.begin() as db:
        first = db.get(EngineInstance, "engine")
        second = EngineInstance(
            id="gpu1",
            generation=7,
            manifest=first.manifest,
            runtime=first.runtime,
            accepting=True,
            heartbeat_at=first.heartbeat_at,
        )
        db.add(second)
        db.flush()
        db.add_all(
            [
                EngineNodeCatalog(instance_id=first.id, generation=7, nodes={"LoadImage": "nodes"}),
                EngineNodeCatalog(
                    instance_id=second.id, generation=7, nodes={"SaveVideo": "nodes"}
                ),
            ]
        )

    worker.refresh_queue()
    assert worker.claim("engine") is None
    with worker.sessions() as db:
        job = db.get(Job, "job")
        assert job.attempt_count == 0 and job.blocked_reason is None
    assert worker.claim("gpu1")


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
    """自动重试耗尽后可手动开始新周期，旧执行和连续的尝试序号仍可追溯。"""
    worker, engine = setup_worker
    worker.tick()
    for _ in range(3):
        engine.complete(worker.settings.data_root / "output", failure=True)
        worker.tick()
    worker.tick()
    with worker.sessions.begin() as db:
        job = db.get(Job, "job")
        assert (job.state, job.budget_used, job.manual_retry_count) == ("FAILED", 3, 0)
        old_attempts = db.scalars(select(Execution.id).where(Execution.job_id == job.id)).all()
        retry_job(db, job.id, job.user_id, job.version, 1, worker.store, worker.capacity)
        assert (job.state, job.budget_used, job.manual_retry_count) == ("QUEUED", 0, 1)
        assert job.current_execution_id is None and job.failure is None
    assert len(engine.posts) == 3
    worker.tick()
    engine.complete(worker.settings.data_root / "output", failure=True)
    worker.tick()
    engine.complete(worker.settings.data_root / "output")
    worker.tick()
    with worker.sessions() as db:
        job = db.get(Job, "job")
        assert (job.state, job.budget_used, job.manual_retry_count) == ("SUCCEEDED", 2, 1)
        executions = db.scalars(
            select(Execution).where(Execution.job_id == job.id).order_by(Execution.attempt_no)
        ).all()
        assert {execution.id for execution in executions[:3]} == set(old_attempts)
        assert [execution.attempt_no for execution in executions] == [1, 2, 3, 4, 5]


@pytest.mark.mysql
@pytest.mark.parametrize(("queue", "stage"), [("pending", "engine_queue"), ("running", "running")])
def test_queue_match_after_reconcile_deadline_recovers_execution(setup_worker, queue, stage):
    """历史超时后，用同一 prompt 的队列记录恢复执行，即使上次未确认已超过核对期限。"""
    worker, engine = setup_worker
    worker.tick()
    prompt = engine.posts[0]["prompt_id"]
    engine.running.clear()
    engine.history_error = httpx.ReadTimeout("simulated history outage")
    worker.tick()
    with worker.sessions.begin() as db:
        execution = db.scalars(select(Execution)).one()
        execution_id = execution.id
        assert execution.phase == "UNCONFIRMED"
        assert execution.result["transitions"][-1]["evidence"] == "ENGINE_LOOKUP_UNAVAILABLE"
        assert db.get(Job, "job").state == "RECOVERING"
        execution.updated_at = db_now(db) - timedelta(seconds=31)

    # 与本任务无关的记录并存，Worker 必须用本次提交的 prompt_id 判断归属。
    engine.pending.add("other-prompt")
    getattr(engine, queue).add(prompt)
    worker.tick()
    worker.tick()
    with worker.sessions() as db:
        execution = db.get(Execution, execution_id)
        assert (execution.phase, execution.stage) == ("RUNNING", stage)
        assert db.get(EngineInstance, "engine").active_execution_id == execution_id
        assert db.get(Job, "job").state == "EXECUTING"
        assert "reconciliation_trigger" not in execution.result
        assert [t["phase"] for t in execution.result["transitions"]].count("RUNNING") == 1
        lookup = execution.result["last_lookup"]
        assert lookup["at"]
        assert lookup["history"]["status"] == "error"
        assert lookup["history"]["error"] == "ReadTimeout"
        assert lookup["queue"]["status"] == f"matched_{queue}"
        assert lookup["queue"]["http_status"] == 200
        assert lookup["history"]["duration_ms"] >= 0 and lookup["queue"]["duration_ms"] >= 0
    assert len(engine.posts) == 1


@pytest.mark.mysql
def test_unconfirmed_exit_quarantines_instance(setup_worker, monkeypatch):
    """R02 重复核对的诊断不延后截止时间；无法证明进程退出时保留占用和最后证据。"""
    worker, engine = setup_worker
    engine.drop_receipt = True
    worker.tick()
    engine.running.clear()
    with worker.sessions() as db:
        execution = db.scalars(select(Execution)).one()
        since = execution.updated_at
    # 两次观测到的故障类型不同；停机时必须留下触发该决定的最近一次查询结果。
    engine.history_error = httpx.ReadTimeout("simulated history outage")
    now = since + timedelta(seconds=20)
    monkeypatch.setattr("mycomfy.generation.worker.db_now", lambda db: now)
    worker.tick()
    with worker.sessions() as db:
        current = db.get(Execution, execution.id)
        assert current.phase == "UNCONFIRMED" and current.updated_at == since
        assert current.result["last_lookup"]["history"]["error"] == "ReadTimeout"
    now = since + timedelta(seconds=31)
    engine.history_error = httpx.ConnectError("simulated disconnect")
    worker.tick()
    worker.tick()
    with worker.sessions.begin() as db:
        instance = db.get(EngineInstance, "engine")
        assert instance.quarantine_reason == "PROCESS_EXIT_UNCONFIRMED"
        assert instance.active_execution_id == execution.id
        result = db.get(Execution, execution.id).result
        assert result["reconciliation_trigger"]["reason"] == "RECONCILE_TIME_LIMIT"
        assert result["last_lookup"]["history"]["error"] == "ConnectError"
        assert result["last_lookup"]["queue"]["status"] == "missing"
        assert result["last_lookup"]["queue"]["http_status"] == 200
        assert db.get(Job, "job").state == "RECOVERING"
    assert len(engine.posts) == 1


@pytest.mark.mysql
@pytest.mark.parametrize("already_ended", [False, True])
def test_reconciliation_records_first_trigger_before_stop(setup_worker, already_ended):
    """区分已退出与总时限；模拟停机后 Worker 重启也保留首次触发原因。"""
    worker, engine = setup_worker
    worker.tick()
    worker.tick()
    run_dir = worker.settings.data_root
    with worker.sessions.begin() as db:
        execution = db.scalars(select(Execution)).one()
        execution_id = execution.id
        instance = db.get(EngineInstance, "engine")
        instance.runtime = {**instance.runtime, "run_dir": str(run_dir)}
        if not already_ended:
            execution.authorized_at = db_now(db) - timedelta(seconds=121)

    ended = run_dir / "ended.json"
    if already_ended:
        ended.write_text('{"generation": 7}')
    expected = "GENERATION_ALREADY_ENDED" if already_ended else "TOTAL_TIME_LIMIT"

    def stop_then_restart(sessions, instance_id, generation):
        """核查停止调用前已提交诊断记录，模拟退出凭据落盘后协调器被重启。"""
        with sessions() as db:
            trigger = db.get(Execution, execution_id).result["reconciliation_trigger"]
            assert trigger["reason"] == expected
        ended.write_text(json.dumps({"generation": generation}))
        raise RuntimeError("simulated worker restart")

    worker.stop_generation = stop_then_restart
    with pytest.raises(RuntimeError, match="simulated worker restart"):
        worker.process("engine", execution_id)
    worker.stop_generation = lambda *args: True
    worker.process("engine", execution_id)
    with worker.sessions() as db:
        execution = db.get(Execution, execution_id)
        assert execution.phase == "LOST"
        assert execution.result["reconciliation_trigger"]["reason"] == expected
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
@pytest.mark.parametrize("completed", [True, False], ids=["complete-output", "missing-output"])
def test_host_reboot_keeps_authorized_slot_until_worker_reconciles(
    setup_worker, monkeypatch, completed
):
    """旧系统重启后，管理器只证明进程已退出；原提交须先按落盘产物结算一次。"""
    from unittest.mock import Mock
    from uuid import uuid4

    from mycomfy.engine.runtime import EngineConfig, Manager, config_hash, stop_generation

    worker, engine = setup_worker
    root = worker.settings.data_root / "gpu0"
    directory = root / f"g7-{uuid4().hex}"
    for name in ("input", "output"):
        (directory / name).mkdir(parents=True)
    config = EngineConfig(
        id="engine",
        gpu="0",
        port=8188,
        root=str(root),
        python="/usr/bin/python3",
        entrypoint=str(root / "main.py"),
        manifest={},
        observation_seconds=10,
        timeout_seconds=120,
        reconcile_seconds=30,
    )
    (directory / "launch.json").write_text(json.dumps({"generation": 7}))
    (directory / "identity.json").write_text(
        json.dumps({"pid": 41, "ticks": 42, "boot": str(uuid4())})
    )
    with worker.sessions.begin() as db:
        row = db.get(EngineInstance, "engine")
        row.runtime = {
            **row.runtime,
            "run_dir": str(directory),
            "config_hash": config_hash(config),
            "input_dir": str(directory / "input"),
            "output_dir": str(directory / "output"),
            "launched_at": 1,
        }

    # 只发送一次真实协议 POST；模拟掉电后引擎内存中的队列和历史全部消失。
    worker.tick()
    execution_id = engine.posts[0]["extra_data"]["mycomfy_execution_id"]
    if completed:
        engine.complete(directory / "output")
    engine.running.clear()
    engine.history.clear()
    with worker.sessions.begin() as db:
        row = db.get(EngineInstance, "engine")
        row.accepting = False
        row.quarantine_reason = "PROCESS_IDENTITY_UNCONFIRMED"

    manager = Manager(worker.sessions)
    launch = Mock()
    monkeypatch.setattr(manager, "launch", launch)
    configs = {config.id: config}
    manager.tick(configs)
    manager.tick(configs)
    launch.assert_not_called()
    evidence = json.loads((directory / "ended.json").read_text())
    assert (evidence["generation"], evidence["reason"]) == (7, "HOST_REBOOT_CONFIRMED")
    with worker.sessions() as db:
        row = db.get(EngineInstance, "engine")
        assert not row.accepting and row.quarantine_reason is None
        assert (row.generation, row.active_execution_id) == (7, execution_id)

    # 用正式的退出凭据检查恢复。即使 Worker 再轮询一遍，也不会再次提交旧执行。
    worker.stop_generation = stop_generation
    worker.tick(claim=False)
    worker.tick(claim=False)
    with worker.sessions() as db:
        execution = db.get(Execution, execution_id)
        assert execution.phase == ("SUCCEEDED" if completed else "LOST")
        assert execution.result["reconciliation_trigger"]["reason"] == "GENERATION_ALREADY_ENDED"
        assert db.get(EngineInstance, "engine").active_execution_id is None
        assert db.get(Job, "job").budget_used == 1
        if completed:
            assert db.scalars(select(Asset)).one().complete
    assert len(engine.posts) == 1
    manager.tick(configs)
    launch.assert_called_once_with(config, 7)


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
    # 回执已确认；一次队列和历史同时未命中时，停机循环应保留槽位并继续核对。
    prompt_id = engine.running.pop()
    worker.tick()
    assert not worker.drained()
    with worker.sessions() as db:
        assert db.get(Job, "job").state == "RECOVERING"
    engine.running.add(prompt_id)
    worker.tick()
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
