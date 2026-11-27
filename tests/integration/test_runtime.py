"""使用真实 Linux 子进程验证启动、接管、日志和停机证据。"""

import json
import os
import pwd
import signal
import socket
import subprocess
import sys
import time
from datetime import timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from mycomfy.database.models import Base, EngineInstance, EngineNodeCatalog
from mycomfy.database.schema import migrate
from mycomfy.engine import runtime
from mycomfy.engine.runtime import (
    EngineConfig,
    Manager,
    administrative_action,
    runtime_ready,
    sample_gpu,
    stop_generation,
)
from mycomfy.engine.supervisor import process_identity


@pytest.mark.parametrize("account_name", [None, "engine-user"])
def test_engine_child_resolves_container_username(tmp_path, monkeypatch, account_name):
    """验证引擎依赖可按运行 UID 取用户名，包括容器中无同 UID 账号的情况。"""
    uid = os.getuid()
    accounts = {} if account_name is None else {uid: SimpleNamespace(pw_name=account_name)}
    monkeypatch.setattr(pwd, "getpwuid", accounts.__getitem__)
    monkeypatch.setenv("LOGNAME", "host-login")
    monkeypatch.setenv("USER", "host-user")

    # 从受管子进程调用 PyTorch 使用的标准库接口，覆盖启动环境的筛选和传递。
    script = tmp_path / "engine.py"
    script.write_text(
        'import getpass\nfrom pathlib import Path\nPath("username.txt").write_text(getpass.getuser())\n'
    )
    cfg = EngineConfig(
        id="gpu0",
        gpu="0",
        port=8188,
        root=str(tmp_path / "instance"),
        python=sys.executable,
        entrypoint=str(script),
        manifest={},
        observation_seconds=2,
        timeout_seconds=10,
        reconcile_seconds=3,
    )
    sessions = sessionmaker(create_engine("sqlite://"), expire_on_commit=False)
    Base.metadata.create_all(sessions.kw["bind"])
    manager = Manager(sessions)
    manager.tick({cfg.id: cfg})
    assert manager.children[cfg.id].wait(timeout=10) == 0
    assert (tmp_path / "username.txt").read_text() == (account_name or str(uid))


def test_container_shutdown_records_exit_and_allows_restart(tmp_path):
    """验证容器服务收到 TERM 后会等待监护程序收割引擎，并可从持久状态启动下一代。"""
    url = f"sqlite:///{tmp_path}/db.sqlite"
    migrate(url)
    sessions = sessionmaker(create_engine(url), expire_on_commit=False)
    engine = tmp_path / "dummy.py"
    engine.write_text("import time\ntime.sleep(60)\n")
    config = EngineConfig(
        id="gpu0",
        gpu="0",
        port=8188,
        root=str(tmp_path / "instance"),
        python=sys.executable,
        entrypoint=str(engine),
        manifest={},
        observation_seconds=2,
        timeout_seconds=10,
        reconcile_seconds=3,
    )
    path = tmp_path / "engines.json"
    path.write_text(json.dumps({"instances": [config.model_dump()]}))
    env = {
        **os.environ,
        "MYCOMFY_DATABASE_URL": url,
        "MYCOMFY_DATA_ROOT": str(tmp_path),
        "MYCOMFY_ENGINE_CONFIG": str(path),
    }
    try:
        for generation in (1, 2):
            manager = subprocess.Popen(
                [sys.executable, "-m", "mycomfy.cli", "runtime", "serve", "--stop-engines-on-exit"],
                env=env,
            )
            try:
                # 等到监护程序实际创建了该代引擎子进程，再模拟 docker stop。
                deadline = time.monotonic() + 12
                while not list((tmp_path / "instance").glob(f"g{generation}-*/child.json")):
                    assert manager.poll() is None and time.monotonic() < deadline
                    time.sleep(0.05)
                # 从部署器使用的镜像命令读取现场，确保 serve 持锁时也可取到本代快照。
                snapshot = subprocess.check_output(
                    [sys.executable, "-m", "mycomfy.cli", "runtime", "stop-snapshot"], env=env
                )
            finally:
                manager.terminate()
                assert manager.wait(timeout=18) == 0
            with sessions() as db:
                row = db.get(EngineInstance, "gpu0")
                assert row.generation == generation and not row.accepting
                assert not row.quarantine_reason
                directory = Path(row.runtime["run_dir"])
            assert json.loads(snapshot) == [{"generation": generation, "run_dir": str(directory)}]
            proof = json.loads((directory / "ended.json").read_text())
            child = json.loads((directory / "child.json").read_text())
            assert proof["generation"] == generation and proof["reason"] == "requested stop"
            assert process_identity(child["pid"]) != child
    finally:
        sessions.kw["bind"].dispose()


@pytest.mark.parametrize(("launched", "receipt_generation"), [(False, None), (True, 7), (True, 6)])
def test_service_exit_requires_matching_receipts(
    tmp_path, monkeypatch, launched, receipt_generation
):
    """验证空部署和已停止代次可正常停机，缺少本代退出凭据时报告失败。"""
    url = f"sqlite:///{tmp_path}/db.sqlite"
    migrate(url)
    sessions = sessionmaker(create_engine(url), expire_on_commit=False)
    config = tmp_path / "engines.json"
    config.write_text('{"instances": []}')
    monkeypatch.setenv("MYCOMFY_DATABASE_URL", url)
    monkeypatch.setenv("MYCOMFY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("MYCOMFY_ENGINE_CONFIG", str(config))
    if launched:
        with sessions.begin() as db:
            db.add(
                EngineInstance(
                    id="gpu0", generation=7, manifest={}, runtime={"run_dir": str(tmp_path)}
                )
            )
        (tmp_path / "ended.json").write_text(json.dumps({"generation": receipt_generation}))

    # 让 serve 在收到真实 TERM 的路径走完最终核对，缩短等待缺失凭据的时间。
    monkeypatch.setattr(Manager, "tick", lambda *_: signal.raise_signal(signal.SIGTERM))
    monkeypatch.setattr(runtime, "_stop_generations", partial(runtime._stop_generations, timeout=0))
    try:
        if launched and receipt_generation != 7:
            with pytest.raises(SystemExit, match="退出凭据不完整"):
                runtime.main(["serve", "--stop-engines-on-exit"])
            with sessions() as db:
                assert (
                    db.get(EngineInstance, "gpu0").quarantine_reason
                    == "PROCESS_TREE_EXIT_UNCONFIRMED"
                )
        else:
            runtime.main(["serve", "--stop-engines-on-exit"])
    finally:
        sessions.kw["bind"].dispose()


def test_gpu_sample_retains_supported_wsl_metrics(monkeypatch):
    """部分驱动不提供利用率时仍然记录本次显存和本代峰值。"""
    monkeypatch.setattr(
        subprocess, "check_output", lambda *args, **kwargs: "0, GPU, 32, 128, [N/A]"
    )
    sample = sample_gpu("0", {"gpu_memory_peak_mib": 45, "gpu_utilization_percent": 12})
    assert sample["gpu_memory_used_mib"] == 32 and sample["gpu_memory_total_mib"] == 128
    assert sample["gpu_memory_peak_mib"] == 45 and "gpu_utilization_percent" not in sample


@pytest.mark.parametrize(
    ("status", "payload", "reachable"),
    [
        (200, {"exec_info": {"queue_remaining": 0}}, True),
        (200, {"exec_info": {"queue_remaining": 2}}, True),
        (200, {}, False),
        (200, {"exec_info": None}, False),
        (200, {"exec_info": {"queue_remaining": False}}, False),
        (200, {"exec_info": {"queue_remaining": -1}}, False),
        (503, {"exec_info": {"queue_remaining": 0}}, False),
    ],
)
def test_probe_generation_checks_comfy_queue_summary(
    tmp_path, monkeypatch, status, payload, reachable
):
    """核对轻量探活只读取 /prompt，兼容空闲、忙碌，并识别异常响应。"""
    identity = process_identity(os.getpid())
    (tmp_path / "identity.json").write_text(json.dumps(identity))
    monkeypatch.setattr(runtime, "owns_port", lambda pid, port: True)

    def respond(request):
        """检查两条 HTTP 路径不会混淆，记录队列信息时也不会向引擎提交任务。"""
        assert request.method == "GET" and request.url.path == "/prompt"
        return httpx.Response(status, json=payload)

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(runtime.httpx, "Client", partial(httpx.Client, transport=transport))
    instance = {"run_dir": str(tmp_path), "base_url": "http://127.0.0.1:8188"}
    assert runtime.probe_generation(instance) == (identity, True, reachable)


def test_manager_adopts_generation_and_stops_detached_descendant(tmp_path):
    """验证管理程序重建、旧代次迟到停机、脱离会话的后代退出和日志脱敏。"""
    sessions = sessionmaker(create_engine("sqlite://"), expire_on_commit=False)
    Base.metadata.create_all(sessions.kw["bind"])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    script = tmp_path / "engine.py"
    script.write_text('''
import http.server, pathlib, subprocess, sys
port = int(sys.argv[sys.argv.index("--port") + 1])
state = pathlib.Path(sys.argv[sys.argv.index("--user-directory") + 1])
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"], start_new_session=True)
(state / "child-pid").write_text(str(child.pid))
print("Cookie: highly-private-value", flush=True)
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        """模拟轻量探活与真实注册节点两个独立接口。"""
        if self.path == "/prompt":
            data = b'{"exec_info": {"queue_remaining": 2}}'
        elif self.path == "/object_info":
            (state / "catalog-read").touch()
            data = b'{"SaveVideo":{"input":{},"python_module":"nodes"},"Director":{"input":{},"python_module":"custom_nodes.Director"},"Legacy":{"input":{}}}'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(data)
http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
''')
    cfg = EngineConfig(
        id="local",
        gpu="0",
        port=port,
        root=str(tmp_path / "instance"),
        python=sys.executable,
        entrypoint=str(script),
        manifest={},
        observation_seconds=2,
        timeout_seconds=10,
        reconcile_seconds=3,
    )
    configs = {cfg.id: cfg}
    original = Manager(sessions)
    original.tick(configs)
    manager = Manager(sessions)
    try:
        for _ in range(100):
            manager.tick(configs)
            with sessions.begin() as db:
                row = db.get(EngineInstance, "local")
                catalog = db.get(EngineNodeCatalog, "local")
                if row.accepting and catalog:
                    break
            time.sleep(0.05)
        assert row.accepting and row.generation == 1
        assert catalog.generation == row.generation
        assert catalog.nodes == {
            "SaveVideo": "nodes",
            "Director": "custom_nodes.Director",
            "Legacy": None,
        }
        # 管理器重启后复用同代清单，Docker 健康探测也独立于 /object_info。
        marker = Path(row.runtime["state_dir"]) / "catalog-read"
        marker.unlink()
        manager = Manager(sessions)
        manager.tick(configs)
        assert not manager.node_catalog.pending
        assert row.runtime["pid"] == original.children["local"].pid
        assert runtime_ready(sessions, configs)
        assert not marker.exists()
        assert not runtime_ready(sessions, {})
        assert not stop_generation(sessions, "local", 0)
        descendant = int((Path(row.runtime["state_dir"]) / "child-pid").read_text())
        identity = process_identity(descendant)
        assert identity
        assert "highly-private-value" not in Path(row.runtime["log_path"]).read_text()
        assert "已脱敏" in Path(row.runtime["log_path"]).read_text()
        with sessions.begin() as db:
            instance = db.get(EngineInstance, "local")
            instance.active_execution_id = "active"
            instance.heartbeat_at -= timedelta(minutes=1)
        assert not runtime_ready(sessions, configs)
        manager.tick(configs)
        assert runtime_ready(sessions, configs)
        administrative_action(sessions, "drain", "local", 1, None)
        manager.tick(configs)
        with sessions.begin() as db:
            row = db.get(EngineInstance, "local")
            assert not row.accepting and row.runtime["process_alive"]
        assert stop_generation(sessions, "local", 1)
        assert not runtime_ready(sessions, configs)
        assert process_identity(descendant) != identity
        manager.tick(configs)
        with sessions.begin() as db:
            row = db.get(EngineInstance, "local")
            assert row.generation == 1 and row.active_execution_id == "active"
    finally:
        stop_generation(sessions, "local", 1)
        original.children["local"].wait(timeout=5)


def test_stop_unknown_tree_requires_evidence(tmp_path):
    """验证缺少退出凭据时保持槽位并隔离，人工确认后可继续核对。"""
    sessions = sessionmaker(create_engine("sqlite://"), expire_on_commit=False)
    Base.metadata.create_all(sessions.kw["bind"])
    with sessions.begin() as db:
        db.add(
            EngineInstance(
                id="lost",
                generation=9,
                manifest={},
                active_execution_id="active",
                accepting=True,
                runtime={"run_dir": str(tmp_path)},
            )
        )
    with pytest.raises(SystemExit, match="仍有未核实的执行 active"):
        runtime.stop_snapshot(sessions, ["lost"])
    assert runtime.stop_snapshot(sessions, ["other-machine"]) == []
    assert not stop_generation(sessions, "lost", 9, timeout=0)
    with sessions.begin() as db:
        row = db.get(EngineInstance, "lost")
        assert row.quarantine_reason and not row.accepting and row.active_execution_id == "active"
    administrative_action(sessions, "resolve", "lost", 9, "核实进程树和产物，旧发送已结束")
    assert runtime.stop_snapshot(sessions, ["lost"]) == [
        {"generation": 9, "run_dir": str(tmp_path)}
    ]
    assert stop_generation(sessions, "lost", 9, timeout=0)


@pytest.fixture
def power_loss(tmp_path):
    """重建旧版本在 GPU 主机掉电后留下的 4071 代数据库行和进程控制文件。"""
    script = tmp_path / "engine.py"
    script.write_text("pass\n")
    cfg = EngineConfig(
        id="gpu0",
        gpu="0",
        port=8188,
        root=str(tmp_path / "instance"),
        python=sys.executable,
        entrypoint=str(script),
        manifest={},
        observation_seconds=2,
        timeout_seconds=10,
        reconcile_seconds=3,
    )
    directory = Path(cfg.root) / f"g4071-{uuid4().hex}"
    directory.mkdir(parents=True)
    old_boot = str(uuid4())
    previous_identity = {"pid": 2147483647, "ticks": 17, "boot": old_boot}
    (directory / "identity.json").write_text(json.dumps(previous_identity))
    (directory / "launch.json").write_text(json.dumps({"generation": 4071}))
    (directory / "child.json").write_text(json.dumps({**previous_identity, "pid": 2147483646}))

    sessions = sessionmaker(create_engine("sqlite://"), expire_on_commit=False)
    Base.metadata.create_all(sessions.kw["bind"])
    with sessions.begin() as db:
        db.add(
            EngineInstance(
                id=cfg.id,
                generation=4071,
                manifest={},
                accepting=False,
                quarantine_reason="PROCESS_IDENTITY_UNCONFIRMED",
                runtime={
                    "run_dir": str(directory),
                    "base_url": f"http://127.0.0.1:{cfg.port}",
                    "config_hash": runtime.config_hash(cfg),
                    "launched_at": time.time() - 86400,
                },
            )
        )
    try:
        yield sessions, cfg, directory, old_boot
    finally:
        sessions.kw["bind"].dispose()


@pytest.mark.parametrize("already_quarantined", [False, True])
def test_power_loss_restarts_idle_instance(power_loss, already_quarantined):
    """旧版未写退出凭据时，首次发现掉电和升级前已隔离的代次都可自动重启。"""
    sessions, cfg, directory, old_boot = power_loss
    if not already_quarantined:
        with sessions.begin() as db:
            db.get(EngineInstance, cfg.id).quarantine_reason = None
    # 监护程序写身份后、启动 ComfyUI 前同样可能掉电。
    (directory / "child.json").unlink()
    manager = Manager(sessions)
    for _ in range(3):
        manager.tick({cfg.id: cfg})
        if cfg.id in manager.children:
            break
    receipt = json.loads((directory / "ended.json").read_text())
    assert receipt["reason"] == "HOST_REBOOT_CONFIRMED" and receipt["generation"] == 4071
    assert receipt["previous_boot_id"] == old_boot
    assert receipt["current_boot_id"] == process_identity(os.getpid())["boot"]
    assert manager.children[cfg.id].wait(timeout=10) == 0
    with sessions() as db:
        row = db.get(EngineInstance, cfg.id)
        assert row.generation == 4072 and row.quarantine_reason is None
        assert not row.accepting


def test_power_loss_replays_exit_receipt_while_paused(power_loss):
    """退出凭据已发布但数据库更新未提交时，重试能修复隔离且尊重暂停。"""
    sessions, cfg, directory, _ = power_loss
    with sessions.begin() as db:
        row = db.get(EngineInstance, cfg.id)
        row.runtime = {**row.runtime, "paused": True}
    receipt_file = directory / "ended.json"

    def fail_after_receipt(_db):
        """仅中断已写入退出凭据的事务，保留文件并回滚数据库变更。"""
        if receipt_file.exists():
            raise RuntimeError("模拟数据库提交前中断")

    event.listen(sessions, "before_commit", fail_after_receipt)
    try:
        with pytest.raises(RuntimeError, match="模拟数据库提交前中断"):
            Manager(sessions).tick({cfg.id: cfg})
    finally:
        event.remove(sessions, "before_commit", fail_after_receipt)
    receipt = receipt_file.read_bytes()
    with sessions() as db:
        assert db.get(EngineInstance, cfg.id).quarantine_reason == "PROCESS_IDENTITY_UNCONFIRMED"
    # 新管理器看到的是旧数据库行和上次发布的退出凭据。
    Manager(sessions).tick({cfg.id: cfg})
    with sessions() as db:
        row = db.get(EngineInstance, cfg.id)
        assert row.quarantine_reason is None and row.generation == 4071
        assert row.runtime["paused"] and not row.accepting
    assert receipt_file.read_bytes() == receipt


@pytest.mark.parametrize(
    ("filename", "update"),
    [
        ("identity.json", None),
        ("identity.json", {"boot": "malformed"}),
        ("launch.json", None),
        ("launch.json", {"generation": 4070}),
        ("identity.json", {"boot": "current"}),
        ("child.json", {"boot": "current"}),
    ],
)
def test_inconclusive_power_loss_preserves_quarantine(power_loss, filename, update):
    """证据缺失、代次不符、同内核丢进程、父子身份冲突时均不能启动新代次。"""
    sessions, cfg, directory, _ = power_loss
    path = directory / filename
    if update is None:
        path.unlink()
    else:
        if update.get("boot") == "current":
            update = {"boot": process_identity(os.getpid())["boot"]}
            if filename == "identity.json":
                child_file = directory / "child.json"
                child_file.write_text(json.dumps({**json.loads(child_file.read_text()), **update}))
        path.write_text(json.dumps({**json.loads(path.read_text()), **update}))
    Manager(sessions).tick({cfg.id: cfg})
    with sessions() as db:
        row = db.get(EngineInstance, cfg.id)
        assert row.quarantine_reason == "PROCESS_IDENTITY_UNCONFIRMED"
        assert row.generation == 4071 and not row.accepting
    assert not (directory / "ended.json").exists()


def test_power_loss_does_not_confirm_another_instance_directory(power_loss):
    """同一数据库中的其他 GPU 主机可能有不同的 boot ID，目录归属须与配置相符。"""
    sessions, cfg, directory, _ = power_loss
    # 配置未改动，但数据库中的 run_dir 引用了另一实例的代次目录。
    other_root = directory.parent.parent / "other-instance"
    other_root.mkdir()
    other_directory = directory.rename(other_root / directory.name)
    with sessions.begin() as db:
        row = db.get(EngineInstance, cfg.id)
        row.runtime = {**row.runtime, "run_dir": str(other_directory)}
    Manager(sessions).tick({cfg.id: cfg})
    with sessions() as db:
        row = db.get(EngineInstance, cfg.id)
        assert row.quarantine_reason == "PROCESS_IDENTITY_UNCONFIRMED"
        assert row.generation == 4071 and not row.accepting
    assert not (other_directory / "ended.json").exists()


def test_power_loss_preserves_unrelated_quarantine(power_loss):
    """主机重启不能解除等待核实工作目录等独立问题引发的隔离。"""
    sessions, cfg, _, _ = power_loss
    with sessions.begin() as db:
        db.get(EngineInstance, cfg.id).quarantine_reason = "STOPPED_SOURCE_UNAVAILABLE"
    manager = Manager(sessions)
    for _ in range(2):
        manager.tick({cfg.id: cfg})
    with sessions() as db:
        row = db.get(EngineInstance, cfg.id)
        assert row.quarantine_reason == "STOPPED_SOURCE_UNAVAILABLE"
        assert row.generation == 4071 and not row.accepting


def test_deployment_change_preserves_active_generation_manifest(tmp_path):
    """验证替换部署配置时排空旧实例，并分别保存待接单和实际运行的环境清单。"""
    config = EngineConfig(
        id="i",
        gpu="0",
        port=8188,
        root=str(tmp_path),
        python=sys.executable,
        entrypoint=str(tmp_path / "main.py"),
        manifest={"comfyui": "new"},
        observation_seconds=2,
        timeout_seconds=8,
        reconcile_seconds=2,
    )
    sessions = sessionmaker(create_engine("sqlite://"), expire_on_commit=False)
    Base.metadata.create_all(sessions.kw["bind"])
    with sessions.begin() as db:
        db.add(
            EngineInstance(
                id="i",
                generation=1,
                manifest={"comfyui": "old"},
                active_execution_id="old-execution",
                accepting=True,
                runtime={"run_dir": str(tmp_path), "config_hash": "old-config"},
            )
        )
    Manager(sessions).configure({"i": config})
    with sessions.begin() as db:
        instance = db.get(EngineInstance, "i")
        assert instance.manifest == {"comfyui": "old"}
        assert instance.runtime["configured_manifest"] == {"comfyui": "new"}
        assert not instance.accepting
        assert instance.active_execution_id == "old-execution"
