"""使用真实 Linux 子进程验证启动、接管、日志和停机证据。"""

import json
import os
import pwd
import socket
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mycomfy.database.models import Base, EngineInstance
from mycomfy.database.schema import migrate
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
                [sys.executable, "-m", "mycomfy.engine.runtime", "serve", "--stop-engines-on-exit"],
                env=env,
            )
            try:
                # 等到监护程序实际创建了该代引擎子进程，再模拟 docker stop。
                deadline = time.monotonic() + 12
                while not list((tmp_path / "instance").glob(f"g{generation}-*/child.json")):
                    assert manager.poll() is None and time.monotonic() < deadline
                    time.sleep(0.05)
            finally:
                manager.terminate()
                assert manager.wait(timeout=18) == 0
            with sessions() as db:
                row = db.get(EngineInstance, "gpu0")
                assert row.generation == generation and not row.accepting
                assert not row.quarantine_reason
                directory = Path(row.runtime["run_dir"])
            proof = json.loads((directory / "ended.json").read_text())
            child = json.loads((directory / "child.json").read_text())
            assert proof["generation"] == generation and proof["reason"] == "requested stop"
            assert process_identity(child["pid"]) != child
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
        """返回用于测试的引擎健康快照。"""
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")
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
                if row.accepting:
                    break
            time.sleep(0.05)
        assert row.accepting and row.generation == 1
        assert row.runtime["pid"] == original.children["local"].pid
        assert runtime_ready(sessions, configs)
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
    assert not stop_generation(sessions, "lost", 9, timeout=0)
    with sessions.begin() as db:
        row = db.get(EngineInstance, "lost")
        assert row.quarantine_reason and not row.accepting and row.active_execution_id == "active"
    administrative_action(sessions, "resolve", "lost", 9, "核实进程树和产物，旧发送已结束")
    assert stop_generation(sessions, "lost", 9, timeout=0)


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
