"""在隔离的业务部署上核对临时外网入口编排，全程使用替身容器和本地客户端。"""

import importlib
import json
import os
import runpy
import shutil
import subprocess
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def checkout(tmp_path):
    """复制 CLI 的正式入口和资源，避免读写工作区里的个人配置与运行状态。"""
    source = Path(__file__).resolve().parents[2]
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copytree(
        source / "scripts", repo / "scripts", ignore=shutil.ignore_patterns("__pycache__")
    )
    for name in (
        "compose.yaml",
        "deploy.config.example.yaml",
        "deploy.test.example.yaml",
    ):
        shutil.copy(source / name, repo / name)
    module = Path("src/mycomfy/origins.py")
    (repo / module.parent).mkdir(parents=True)
    shutil.copy(source / module, repo / module)
    return repo


def test_normal_compose_never_reads_tunnel_config(checkout, tmp_path):
    """普通命令只解析业务设置，即使存在损坏的个人测试配置也照常执行。"""
    bad = checkout / "deploy.test.yaml"
    bad.write_text("machine_name: [\n")
    binary = tmp_path / "docker"
    binary.write_text(
        f"#!{sys.executable}\nimport sys\nassert sys.argv[1] == 'compose' and sys.argv[-1] == 'ps'\n"
        "print('compose-ps-ok')\n"
    )
    binary.chmod(0o700)
    result = subprocess.run(
        [sys.executable, str(checkout / "scripts/deploy.py"), "--compose", "ps"],
        cwd=tmp_path,
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "compose-ps-ok"
    assert bad.read_text() == "machine_name: [\n"


def test_cli_test_uses_explicit_base_without_bootstrapping_gpu(checkout, tmp_path, monkeypatch):
    """从正式 CLI 验证两种配置的分工，已有业务端无需填写任何 GPU 安装配方。"""
    base = tmp_path / "business.yaml"
    base.write_text(
        "mysql: {user: studio, password: test}\nredis: {user: cache, password: test}\n"
        "ports: {api: 18300, admin: 18310}\n"
    )
    private = tmp_path / "tunnels.yaml"
    private.write_text("machine_name: auto\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.syspath_prepend(str(checkout / "scripts"))
    orchestrator = importlib.import_module("deploy_tunnel")
    observed = []

    def record(repo, config_path, requested, run_compose, process_env):
        """记录 CLI 解析的端口与测试设置路径，不发起实际隧道或部署操作。"""
        observed.append(
            (
                repo,
                config_path,
                requested,
                process_env["MYCOMFY_LOCAL_PORT"],
                process_env["MYCOMFY_API_PORT"],
            )
        )

    monkeypatch.setattr(orchestrator, "run_test_session", record)
    monkeypatch.setattr(
        sys, "argv", ["deploy.py", "--test", "--config", str(base), "--test-config", str(private)]
    )
    runpy.run_path(str(checkout / "scripts/deploy.py"), run_name="__main__")
    assert observed == [(checkout, base, private, "18310", "18300")]
    assert not (checkout / "var").exists() and not (checkout / ".env").exists()


def test_missing_explicit_business_config_prevents_test_session(checkout, tmp_path):
    """测试模式使用指定业务配置，拼错文件名不会回退到模板并切换其他业务入口。"""
    missing = tmp_path / "missing-production.yaml"
    result = subprocess.run(
        [sys.executable, str(checkout / "scripts/deploy.py"), "--test", "--config", str(missing)],
        cwd=tmp_path,
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0 and str(missing) in result.stderr
    assert not (checkout / "var").exists()


@pytest.fixture
def session(checkout, tmp_path, monkeypatch):
    """真实渲染两个 Compose 服务，仅模拟已有容器和两条对外转发的生命周期。"""
    monkeypatch.syspath_prepend(str(checkout / "scripts"))
    deploy = importlib.import_module("deploy_tunnel")
    settings = importlib.import_module("deployment_config")
    base = checkout / "deploy.config.yaml"
    base.write_text(
        """mysql: {user: db, password: example-mysql}
redis: {user: cache, password: example-redis}
ports: {api: 18232, admin: 18233}
public:
  host: 0.0.0.0
  origin: "https://business.example"
  admin_origin: "https://management.example"
  cookie_secure: false
  trusted_proxy_cidrs: ["192.0.2.33"]
"""
    )
    # 使用显式的配置路径和非默认的两个外部端口，校验 CLI 没有写死默认模板。
    private = tmp_path / "personal-test.yaml"
    private.write_text("machine_name: auto\npublic_https_port: 10000\nadmin_https_port: 443\n")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MYSQL_", "REDIS_", "MYCOMFY_", "COMPOSE_"))
    }
    env.update(settings.read_config(base, checkout, core_only=True)[0])
    command = ["docker", "compose", "--env-file", os.devnull]

    def compose_config(*args, **kwargs):
        """使用本地 Compose CLI 计算配置，始终不对 Docker 守护进程发起写入。"""
        return subprocess.run(
            [*command, "config", *args], env=env, cwd=checkout, check=True, **kwargs
        )

    def current():
        """用正式 Compose 输出模拟本次 up 产生的容器环境和配置摘要。"""
        config = json.loads(compose_config("--format", "json", capture_output=True).stdout)
        rows = compose_config(
            "--hash=core,gateway", capture_output=True, text=True
        ).stdout.splitlines()
        hashes = dict(row.split() for row in rows)
        gateway_ports = {
            entry["target"]: (entry["host_ip"], int(entry["published"]))
            for entry in config["services"]["gateway"]["ports"]
        }
        return {
            "core": {"env": config["services"]["core"]["environment"], "hash": hashes["core"]},
            "gateway": {"ports": gateway_ports, "hash": hashes["gateway"]},
        }

    initial = current()
    state = SimpleNamespace(
        live=deepcopy(initial),
        initial=initial,
        updates=[],
        forwards=[],
        occupied=set(),
        during=None,
        fail_up=False,
        fail_forward=False,
        base=base,
        private=private,
        repo=checkout,
        env=env,
    )

    def run_compose(*args, **kwargs):
        """记录真实 apply/restore 命令，把每次已生效的服务环境保存到测试现场。"""
        if args[0] == "config":
            return compose_config(*args[1:], **kwargs)
        assert args[0] == "up", args
        assert set(args[1:]) == {
            "-d",
            "--no-build",
            "--no-deps",
            "--pull",
            "never",
            "--wait",
            "core",
            "gateway",
        }
        state.updates.append(current())
        if state.fail_up:
            # 模拟 Core 已重建，网关启动失败。恢复不可只依赖成功的 up 回执。
            state.live["core"] = state.updates[-1]["core"]
            state.fail_up = False
            raise subprocess.CalledProcessError(42, list(args))
        state.live = deepcopy(state.updates[-1])
        return subprocess.CompletedProcess(args, 0)

    class Client:
        """提供离线客户端，记录进入测试时的映射并允许在会话进行中核对状态。"""

        hostname = "studio.device.ts.net"

        def occupied_ports(self):
            """返回测试开始前已被其他转发占用的端口。"""
            return state.occupied

        @contextmanager
        def forward(self, mapping):
            """记录请求的两条转发，必要时模拟第二条转发启动失败。"""
            state.forwards.append(dict(mapping))
            if state.fail_forward:
                raise RuntimeError("second test forward failed")
            yield self

        def wait(self):
            """在两条转发已经可用时检查业务设置和入口，随后模拟正常结束。"""
            if state.during:
                state.during()

    monkeypatch.setattr(
        deploy, "_live_hashes", lambda _: {name: row["hash"] for name, row in state.live.items()}
    )
    monkeypatch.setattr(deploy, "FunnelClient", SimpleNamespace(discover=Client))
    state.run = lambda requested=private: deploy.run_test_session(
        checkout, base, requested, run_compose, env
    )
    return state


@pytest.mark.parametrize("interrupt", [False, True], ids=["normal", "ctrl-c"])
def test_temporary_links_restore_live_services(session, capsys, interrupt):
    """两条来源及本地目标在会话中同时生效，退出后两份容器配置、业务文件均还原。"""
    original_files = {path: path.read_bytes() for path in (session.base, session.private)}
    observed = []

    def verify_live():
        """在会话存活而非已经清理之后核对 HTTPS 来源与防降级 Cookie。"""
        core = session.live["core"]["env"]
        assert core["MYCOMFY_PUBLIC_ORIGIN"] == "https://studio.device.ts.net:10000"
        assert core["MYCOMFY_ADMIN_ORIGIN"] == "https://studio.device.ts.net"
        assert core["MYCOMFY_COOKIE_SECURE"] == "true"
        assert session.live["gateway"]["ports"] == {
            8000: ("127.0.0.1", 18232),
            8001: ("127.0.0.1", 18233),
        }
        assert session.forwards == [{10000: 18232, 443: 18233}]
        observed.append(True)
        if interrupt:
            raise KeyboardInterrupt

    assert session.initial["gateway"]["ports"] == {
        8000: ("0.0.0.0", 18232),
        8001: ("127.0.0.1", 18233),
    }
    session.during = verify_live
    try:
        session.run()
    except KeyboardInterrupt:
        pytest.fail("交互式会话应在恢复原入口后正常处理 Ctrl+C")
    assert observed and len(session.updates) == 2
    assert session.live == session.initial
    assert all(path.read_bytes() == original for path, original in original_files.items())
    output = capsys.readouterr()
    assert (
        "https://studio.device.ts.net:10000/" in output.out
        and "https://studio.device.ts.net/" in output.out
    )
    assert "example-mysql" not in output.out + output.err


@pytest.mark.parametrize("failure", ["fail_up", "fail_forward"])
def test_test_session_recovers_partial_start(session, failure):
    """即使只切换了一个服务或只打开了第一个链接，已有两个业务服务也须还原。"""
    setattr(session, failure, True)
    with pytest.raises((RuntimeError, subprocess.CalledProcessError, ValueError)):
        session.run()
    assert session.live == session.initial
    # 可重试证明异常没有留下会话锁或妨碍后续恢复的现场。
    session.fail_forward = False
    session.run()
    assert session.live == session.initial


def test_first_session_records_device_after_both_forwards(session):
    """首次启动失败不产生个人配置，两条入口就绪后才保存实际使用的设备与外部端口。"""
    personal = session.repo / "deploy.test.yaml"
    session.fail_forward = True
    with pytest.raises((RuntimeError, ValueError)):
        session.run(None)
    assert not personal.exists()
    session.fail_forward = False
    session.run(None)
    config = runpy.run_path(str(session.repo / "scripts/config_document.py"))["load_document"]
    assert config(personal) == {
        "machine_name": "studio.device.ts.net",
        "public_https_port": 443,
        "admin_https_port": 8443,
    }
    assert session.live == session.initial


def test_preflight_refuses_busy_ports_or_drift(session):
    """发现外部端口已有所有者或 Compose 配置偏离当前容器时不改动业务。"""
    session.occupied.add(443)
    with pytest.raises((RuntimeError, ValueError)):
        session.run()
    assert not session.updates and not session.forwards
    session.occupied.clear()
    session.live["core"]["hash"] = "0" * 64
    with pytest.raises((RuntimeError, ValueError)):
        session.run()
    assert not session.updates and not session.forwards


def test_prior_single_host_deployment_keeps_its_local_database(session):
    """当初部署使用命令行 --single-host 时，临时会话仍沿用现有容器实际连接的本地数据库。"""
    # 模拟业务文件写的是远端地址，已运行服务则由此前的单机部署固定到了回环地址。
    env = session.env
    remote = session.base.read_text().replace("user: db,", "user: db, host: private-db,")
    session.base.write_text(remote.replace("user: cache,", "user: cache, host: private-cache,"))
    config = importlib.import_module("deployment_config")
    env.update(config.read_config(session.base, session.repo, core_only=True)[0])
    assert "@private-db:" in env["MYCOMFY_DATABASE_URL"]
    assert "@private-cache:" in env["MYCOMFY_REDIS_URL"]
    original = dict(env)
    session.run()
    for snapshot in session.updates:
        core = snapshot["core"]["env"]
        for key in ("MYCOMFY_DATABASE_URL", "MYCOMFY_REDIS_URL"):
            assert core[key] == session.initial["core"]["env"][key]
    assert env == original and session.live == session.initial


def test_second_session_cannot_change_running_one(session):
    """同一仓库的另一会话应在取得控制权前退出，原会话继续保持原本的两个链接。"""

    def try_another_session():
        """在第一会话保持打开时尝试第二次启动，通过变更记录检查是否发生干扰。"""
        before = deepcopy(session.live)
        session.during = None
        with pytest.raises((RuntimeError, ValueError, BlockingIOError)):
            session.run()
        assert session.live == before and len(session.updates) == 1 and len(session.forwards) == 1

    session.during = try_another_session
    session.run()
    assert session.live == session.initial and len(session.updates) == 2
