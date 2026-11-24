#!/usr/bin/env python3
"""在 Linux 或 WSL 上准备挂载目录，用 Docker Compose 部署 MyComfy。"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from deployment_config import http_origin, read_config


def select_config(repo, requested, *, testing):
    """选择个人 YAML 或示例模板，测试现有服务时必须使用确实存在的显式配置。"""
    if requested is None:
        candidates = [repo / "deploy.config.yaml", repo / "deploy.config.yml"]
    else:
        if requested.suffix.lower() not in {".yaml", ".yml"}:
            raise ValueError(f"--config 需要指定 .yaml 或 .yml 文件：{requested}")
        candidates = [requested.absolute()]
    for path in candidates:
        if path.is_file():
            return path.resolve()
        if path.exists() or path.is_symlink():
            raise ValueError(f"部署配置不是可读文件：{path}")
    if testing and requested is not None:
        raise ValueError(f"找不到已有业务部署的配置 {requested}")

    # 把提示写入 stderr，保留 Compose 标准输出供调用方解析。
    template = repo / "deploy.config.example.yaml"
    missing = "、".join(str(path) for path in candidates)
    print(f"未找到配置 {missing}，使用默认模板 {template}。", file=sys.stderr)
    return template


def bind_source(service, target):
    """按容器内的挂载点找到 Compose 实际使用的宿主目录。"""
    for mount in service["volumes"]:
        if mount["type"] == "bind" and mount["target"] == target:
            # Compose 序列化配置时将路径内的字面量 $ 写成 $$，文件系统使用原字符。
            return Path(mount["source"].replace("$$", "$"))
    raise ValueError(f"Compose 配置中缺少目录挂载 {target}")


def prepare_gateway(services, repo, config_path, deploy_local_stack):
    """核对本机监听端口和所选挂载范围，给网关准备独立的持久状态目录。"""
    gateway = services["gateway"]
    # 端口冲突发生在宿主机，容器内固定的 Nginx 端口可与其他服务重复。
    edge_ports = [int(binding["published"]) for binding in gateway["ports"]]
    if len(set(edge_ports)) != len(edge_ports):
        raise ValueError("ports.api 和 ports.admin 必须不同")
    if deploy_local_stack:
        local_ports = edge_ports + [
            int(services[name]["ports"][0]["published"]) for name in ("mysql", "redis")
        ]
        if len(set(local_ports)) != len(local_ports):
            raise ValueError("单机部署的 ports 监听端口必须分别配置")
    state = bind_source(gateway, "/var/lib/mycomfy-gateway").resolve()
    core = services["core"]
    private = [
        bind_source(core, "/run/mycomfy"),
        bind_source(core, core["environment"]["MYCOMFY_DATA_ROOT"]),
        bind_source(services["engine"], "/deployment"),
        config_path,
        repo / ".env",  # 即使部署不加载这个文件，也不能把现场可能留存的凭据挂给网关。
    ]
    if any(path.resolve().is_relative_to(state) for path in private):
        raise ValueError("网关状态须使用独立目录，不能包含维护凭据、部署文件或业务数据")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)


def prepare_token(directory):
    """首次部署生成本机维护令牌，保留已有令牌及并发部署先发布的令牌。"""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = directory / "maintenance-token"
    if not token.exists():
        # 临时文件默认权限为 0600，写完后才使令牌对 core 可见。
        with tempfile.NamedTemporaryFile(dir=directory) as draft:
            draft.write((secrets.token_hex(32) + "\n").encode("ascii"))
            draft.flush()
            try:
                os.link(draft.name, token)
            except FileExistsError:
                pass
    if not token.is_file() or not token.stat().st_size or token.stat().st_mode & 0o077:
        raise ValueError(f"请检查 {token}，需要保存非空令牌并设置权限 0600")


def container_states(run_compose, service):
    """读取 Compose 常驻容器状态，兼容 JSON 数组与逐行 JSON。"""
    output = run_compose(
        "ps", "--all", "--format", "json", service, stdout=subprocess.PIPE, text=True
    ).stdout.strip()
    rows = (
        json.loads(output)
        if output.startswith("[")
        else [json.loads(line) for line in output.splitlines()]
    )
    return [
        row
        for row in rows
        if "com.docker.compose.oneoff=true" not in row.get("Labels", "").lower().split(",")
    ]


def stop_process_service(run_compose, service):
    """请求容器无限期优雅退出，再检查实际进程退出状态。"""
    run_compose("stop", "--timeout", "-1", service)
    for row in container_states(run_compose, service):
        if row["State"] != "created" and (row["State"] != "exited" or int(row["ExitCode"]) != 0):
            raise ValueError(f"{service} 未正常退出，请检查容器日志。")


def stop_engine(run_compose, repo):
    """记录正在运行的代次，仅在每一份退出凭据都核实后清除停机检查记录。"""
    rows = container_states(run_compose, "engine")
    project = rows[0].get("Project") if rows else None
    directory = repo / "var" / "stop"
    if not project and not rows and directory.exists():
        # 容器可能已在失败后被移除，仍须使用 Compose 项目名找到未核实的凭据。
        config = run_compose("config", "--format", "json", stdout=subprocess.PIPE, text=True)
        project = json.loads(config.stdout).get("name")
    checkpoint = directory / f"{project or repo.name}.json"
    targets = json.loads(checkpoint.read_text()) if checkpoint.exists() else []
    if any(row["State"] == "running" for row in rows):
        # 在运行中的引擎镜像内核对任务与代次，再保存关机后仍需验证的凭据。
        result = run_compose(
            "exec",
            "-T",
            "engine",
            "python",
            "-m",
            "mycomfy.cli",
            "runtime",
            "stop-snapshot",
            stdout=subprocess.PIPE,
            text=True,
        )
        targets.extend(target for target in json.loads(result.stdout) if target not in targets)
        if targets:
            # 退出后无法再 exec。先原子保存现场，允许中断后重复核验同一个代次。
            checkpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=checkpoint.parent, mode="w", delete=False
            ) as draft:
                json.dump(targets, draft)
            os.replace(draft.name, checkpoint)

    stop_process_service(run_compose, "engine")
    for target in targets:
        receipt = Path(target["run_dir"]) / "ended.json"
        if (
            not receipt.is_file()
            or json.loads(receipt.read_text()).get("generation") != target["generation"]
        ):
            raise ValueError(f"引擎代次 {target['generation']} 缺少匹配的退出凭据 {receipt}")
    checkpoint.unlink(missing_ok=True)


def graceful_stop(run_compose, args, repo):
    """依次排空 Worker、确认引擎退出，再关闭本次选择的业务和数据服务。"""
    if not (args.core_only or args.db_only):
        print("正在排空 Worker；运行中的任务可能需要较长时间。", flush=True)
        stop_process_service(run_compose, "worker")
        stop_engine(run_compose, repo)

    if not (args.db_only or args.gpu_only):
        # Core 先关闭长连接，让网关在没有活动 SSE 的情况下排空客户端。
        run_compose("stop", "--timeout", "-1", "core")
        run_compose("stop", "--timeout", "-1", "gateway")
    if not (args.core_only or args.gpu_only):
        run_compose("stop", "--timeout", "-1", "mysql", "redis")
    print("所选服务已停止。", flush=True)


def main():
    """读取一份部署配置，按所选模式构建镜像、部署引擎并等待服务就绪。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        help="部署 YAML；默认查找 deploy.config.yaml 或 .yml，均缺失时使用仓库模板",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--single-host", action="store_true", help="在同一台 GPU 主机上部署内置数据库和全部应用"
    )
    mode.add_argument("--db-only", action="store_true", help="只启动内置 MySQL 和 Redis 并等待就绪")
    mode.add_argument(
        "--core-only", action="store_true", help="连接现有数据库，执行迁移并启动网页和 API"
    )
    mode.add_argument(
        "--gpu-only", action="store_true", help="连接已迁移的数据库，只部署 GPU 引擎和 Worker"
    )
    mode.add_argument(
        "--test", action="store_true", help="在已运行的业务机临时分享工作台和后台，Ctrl+C 恢复"
    )
    parser.add_argument(
        "--test-config", type=Path, help="测试专用 YAML，配置设备名和两个 HTTPS 端口"
    )
    mode.add_argument(
        "--compose", nargs=argparse.REMAINDER, help="用同一份配置执行 Compose 命令，如 ps 或 logs"
    )
    parser.add_argument(
        "--stop", action="store_true", help="排空在途任务后停止本项目服务，可指定机器角色"
    )
    args = parser.parse_args()
    if args.stop and args.compose is not None:
        parser.error("--stop 不能与 --compose 一起使用")
    if args.stop and args.test:
        parser.error("--test 会在 Ctrl+C 后结束，请单独执行 --stop 停机")
    if args.test_config is not None and not args.test:
        parser.error("--test-config 需要与 --test 一起使用")
    repo = Path(__file__).resolve().parents[1]
    config_path = select_config(repo, args.config, testing=args.test)
    process_env = {**os.environ, "PWD": str(repo)}
    # 显式屏蔽 Compose 自动读取仓库 .env，凭据和路径均从所选 YAML 提供。
    compose = ["docker", "compose", "--env-file", os.devnull]
    recipe = None
    single_host = args.single_host

    if not args.stop:
        # 停机只按项目和服务名操作已有容器。其余命令按角色读取所需的凭据和引擎参数。
        settings, recipe, single_host = read_config(
            config_path,
            repo,
            core_only=args.db_only or args.core_only or args.test or args.compose is not None,
            database_init=args.db_only,
            redis_optional=args.gpu_only or args.compose is not None,
            gateway_required=not (args.db_only or args.gpu_only),
            single_host=single_host,
        )
        process_env.update(settings)
    if single_host or args.db_only or (args.stop and not (args.core_only or args.gpu_only)):
        # 两阶段复用同一 Compose 项目和网络，后续运维也可用配置开关查看数据库。
        compose += ["--profile", "database"]

    def run_compose(*arguments, **options):
        """让部署和日常 Compose 命令使用相同的工作目录和配置快照。"""
        return subprocess.run(
            [*compose, *arguments], cwd=repo, env=process_env, check=True, **options
        )

    if args.test:
        # 仅显式测试时加载 Tailscale 适配器，常规部署不依赖本机的隧道客户端。
        from deploy_tunnel import run_test_session

        run_test_session(repo, config_path, args.test_config, run_compose, process_env)
        return
    if args.stop:
        graceful_stop(run_compose, args, repo)
        return
    if args.compose is not None:
        if not args.compose:
            parser.error("--compose 后需要命令，例如 ps")
        run_compose(*args.compose)
        return

    def start_database():
        """等待两个内置数据服务均健康，失败时终止本次部署。"""
        run_compose("up", "-d", "--wait", "mysql", "redis")
        print("MySQL 和 Redis 已就绪。", flush=True)

    if args.db_only:
        start_database()
        return
    config = run_compose("config", "--format", "json", stdout=subprocess.PIPE).stdout
    services = json.loads(config)["services"]
    deploy_local_stack = single_host and not (args.gpu_only or args.core_only)
    if not args.gpu_only:
        prepare_gateway(services, repo, config_path, deploy_local_stack)
    owner = services["engine" if args.gpu_only else "core"]
    data = bind_source(owner, owner["environment"]["MYCOMFY_DATA_ROOT"])
    data.mkdir(mode=0o700, parents=True, exist_ok=True)

    build = []
    start = []
    if not args.gpu_only:
        prepare_token(bind_source(services["core"], "/run/mycomfy"))
        build.extend(["migrate", "core", "gateway"])
        start.extend(["core", "gateway"])
    if not args.core_only:
        deployment = bind_source(services["engine"], "/deployment")
        deployment.mkdir(mode=0o700, parents=True, exist_ok=True)
        build += ["engine", "worker"]
        start += ["engine", "worker"]
    if deploy_local_stack:
        # 单机全栈在配置检查完成后启动数据层。显式角色仍只操作各自的服务。
        start_database()
    run_compose("build", *build)

    # 在具备 GPU 和持久卷的临时 engine 容器中安装，失败时保留已有服务和数据。
    if not args.core_only:
        run_compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "engine",
            "deploy",
            "-",
            "--base",
            "/deployment",
            "--root",
            str(Path(process_env["MYCOMFY_DATA_ROOT"]) / "engines"),
            input=json.dumps(recipe).encode("utf-8"),
        )
    # 分机时业务机已完成迁移。GPU 机同时指定 engine 和 worker，只启动这两个服务。
    options = ["--no-deps"] if args.gpu_only else []
    run_compose("up", "-d", "--wait", "--no-build", *options, *start)
    if args.gpu_only:
        print("GPU 引擎和 Worker 已启动。", flush=True)
    print("工作台地址", process_env["MYCOMFY_PUBLIC_ORIGIN"], flush=True)
    if not args.gpu_only:
        # 管理入口可由 HTTPS 代理提供，直连时只发布在业务机回环地址。
        admin = process_env["MYCOMFY_ADMIN_ORIGIN"] or http_origin(
            "127.0.0.1", int(process_env["MYCOMFY_LOCAL_PORT"])
        )
        print("管理后台地址", admin, flush=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
