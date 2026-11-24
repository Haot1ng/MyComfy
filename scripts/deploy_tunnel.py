"""在已有业务部署上临时切换 HTTPS 来源，并管理本次测试的两条转发。"""

import fcntl
import json
import os
import signal
import subprocess
import threading
from contextlib import contextmanager
from urllib.parse import urlsplit

from deployment_config import legacy_single_host_environment
from funnel_client import FunnelClient, FunnelError
from tunnel_settings import load_tunnel_settings, remember_machine_name


@contextmanager
def _exclusive_session(repo):
    """通过仓库目录上的内核锁避免两个会话交错切换和恢复网关。"""
    fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("该仓库已有测试会话，请先在原终端按 Ctrl+C 结束。") from None
        yield
    finally:
        os.close(fd)


@contextmanager
def _handle_shutdown():
    """让终端中断、正常关机信号都沿上下文退出路径关闭转发并恢复业务。"""
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {}

    def protect_cleanup():
        """避免关闭转发后收到的新信号打断原业务入口的恢复。"""
        for item in previous:
            signal.signal(item, signal.SIG_IGN)

    def stop_session(signum, frame):
        """第一次退出请求开始清理，重复信号交给现有清理流程处理。"""
        protect_cleanup()
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        for item in signals:
            previous[item] = signal.signal(item, stop_session)
    try:
        yield protect_cleanup
    finally:
        for item, handler in previous.items():
            signal.signal(item, handler)


def _live_services(run_compose):
    """读取本项目正在运行的 Core、网关的环境和配置摘要，以核实切换前的现场。"""
    ids = []
    for name in ("core", "gateway"):
        output = run_compose(
            "ps", "--status", "running", "--quiet", name, stdout=subprocess.PIPE, text=True
        ).stdout.split()
        if len(output) != 1:
            raise ValueError(f"需要一个运行中的 {name} 容器，请在已启动本项目的业务机执行 --test。")
        ids.extend(output)

    # 只读取容器配置，凭据留在进程内，不能作为命令参数或诊断信息输出。
    records = subprocess.run(
        ["docker", "inspect", "--type", "container", "--format", "{{json .Config}}", *ids],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    live = {}
    for line in records:
        config = json.loads(line)
        labels = config["Labels"]
        name = labels["com.docker.compose.service"]
        live[name] = {
            "hash": labels.get("com.docker.compose.config-hash"),
            "env": dict(entry.split("=", 1) for entry in config["Env"] if "=" in entry),
        }
    if set(live) != {"core", "gateway"}:
        raise ValueError("未能确认两个运行中的业务容器，请检查 docker compose ps。")
    return live


def _compose_hashes(run_compose):
    """读取 Compose 用于判断 Core 和网关是否需要重建的配置摘要。"""
    result = run_compose("config", "--hash=core,gateway", stdout=subprocess.PIPE, text=True)
    return dict(line.split() for line in result.stdout.splitlines() if line.strip())


def _match_baseline(run_compose, process_env, live):
    """只允许使用能复现当前容器的配置，兼容曾通过 --single-host 启动的部署。"""
    expected = {name: row["hash"] for name, row in live.items()}
    if _compose_hashes(run_compose) == expected:
        return

    # 单机启动标记可能只在当时的命令行出现。用业务配置里的凭据和内置端口重算，
    # 并再次比对完整服务摘要，防止把原本连接外部数据库的 Core 切到其他数据库。
    current = live["core"]["env"]
    if all(
        urlsplit(current.get(key, "")).hostname == "127.0.0.1"
        for key in ("MYCOMFY_DATABASE_URL", "MYCOMFY_REDIS_URL")
    ):
        profiles = process_env.get("COMPOSE_PROFILES", "")
        process_env["COMPOSE_PROFILES"] = ",".join(filter(None, [profiles, "database"]))
        try:
            config = run_compose("config", "--format", "json", stdout=subprocess.PIPE, text=True)
            services = json.loads(config.stdout)["services"]
            process_env.update(legacy_single_host_environment(services))
        finally:
            if profiles:
                process_env["COMPOSE_PROFILES"] = profiles
            else:
                process_env.pop("COMPOSE_PROFILES", None)
        if _compose_hashes(run_compose) == expected:
            return
    raise ValueError("业务配置与运行中的 Core 或网关不一致，请用 --config 指定原部署文件。")


def _origin(machine_name, port):
    """生成浏览器实际使用的 HTTPS 来源，标准 443 端口省略。"""
    return f"https://{machine_name}" + ("" if port == 443 else f":{port}")


def _apply_gateway(run_compose):
    """只用本地已有镜像切换 Core、网关，等待健康检查完成。"""
    run_compose(
        "up", "-d", "--no-build", "--no-deps", "--pull", "never", "--wait", "core", "gateway"
    )


def _share(repo, requested, run_compose, process_env, protect_cleanup):
    """预检设备及部署，在可还原的业务配置上公布本次测试会话的入口。"""
    settings = load_tunnel_settings(repo, requested)
    client = FunnelClient.discover()
    machine = settings.resolve_machine_name(client.hostname)
    public, admin = settings.public_https_port, settings.admin_https_port
    occupied = client.occupied_ports() & {public, admin}
    if occupied:
        raise ValueError(
            f"Tailscale 端口 {sorted(occupied)} 已有转发，请在测试配置里选择空闲端口。"
        )
    live = _live_services(run_compose)
    _match_baseline(run_compose, process_env, live)
    gateway = live["gateway"]["env"]
    mapping = {public: int(gateway["MYCOMFY_API_PORT"]), admin: int(gateway["MYCOMFY_LOCAL_PORT"])}
    origins = [_origin(machine, port) for port in (public, admin)]
    baseline = process_env.copy()
    process_env.update(
        MYCOMFY_PUBLIC_ORIGIN=origins[0],
        MYCOMFY_ADMIN_ORIGIN=origins[1],
        MYCOMFY_COOKIE_SECURE="true",
        MYCOMFY_API_HOST="127.0.0.1",
    )
    print("正在为临时 HTTPS 地址切换业务入口。", flush=True)
    try:
        _apply_gateway(run_compose)
        with client.forward(mapping) as session:
            if remember_machine_name(repo, requested, settings, machine):
                print("已将当前设备名保存到 deploy.test.yaml。", flush=True)
            print(f"Workbench: {origins[0]}/\nAdmin:     {origins[1]}/", flush=True)
            print(
                "管理链接允许免登录修改用户分组等设置。按 Ctrl+C 结束测试并恢复业务入口。",
                flush=True,
            )
            session.wait()
    finally:
        # up 有可能只替换了一个容器；从第一次尝试开始，就必须恢复两份原始配置。
        protect_cleanup()
        process_env.clear()
        process_env.update(baseline)
        print("正在恢复原业务入口。", flush=True)
        try:
            _apply_gateway(run_compose)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError(
                "业务入口恢复失败，请检查 Docker 后用原部署命令及其 --config 恢复业务入口。"
            ) from exc
        print("原业务入口已恢复。", flush=True)


def run_test_session(repo, requested, run_compose, process_env):
    """在同一仓库串行执行临时测试，退出时释放会话锁并还原调用方的环境。"""
    original = process_env.copy()
    try:
        with _exclusive_session(repo), _handle_shutdown() as protect_cleanup:
            _share(repo, requested, run_compose, process_env, protect_cleanup)
    except FunnelError as exc:
        raise ValueError(str(exc)) from exc
    except KeyboardInterrupt:
        print("已结束临时测试。", flush=True)
    finally:
        process_env.clear()
        process_env.update(original)
