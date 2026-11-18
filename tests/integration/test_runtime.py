"""使用真实 Linux 子进程验证启动、接管、日志和停机证据。"""

import socket
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mycomfy.database.models import Base, EngineInstance
from mycomfy.engine.runtime import EngineConfig, Manager, administrative_action, stop_generation
from mycomfy.engine.supervisor import process_identity


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
        assert not stop_generation(sessions, "local", 0)
        descendant = int((Path(row.runtime["state_dir"]) / "child-pid").read_text())
        identity = process_identity(descendant)
        assert identity
        assert "highly-private-value" not in Path(row.runtime["log_path"]).read_text()
        assert "已脱敏" in Path(row.runtime["log_path"]).read_text()
        with sessions.begin() as db:
            db.get(EngineInstance, "local").active_execution_id = "active"
        administrative_action(sessions, "drain", "local", 1, None)
        manager.tick(configs)
        with sessions.begin() as db:
            row = db.get(EngineInstance, "local")
            assert not row.accepting and row.runtime["process_alive"]
        assert stop_generation(sessions, "local", 1)
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
