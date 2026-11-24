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

from deployment_config import legacy_single_host_environment, read_config


def select_config(repo, requested):
    """优先使用显式配置，否则依次查找本机 YAML、YML 和旧 JSON。"""
    if requested is not None:
        candidates = [requested.resolve()]
    else:
        candidates = [
            repo / name
            for name in ("deploy.config.yaml", "deploy.config.yml", "deploy.config.json")
        ]
    for path in candidates:
        if path.exists():
            return path

    if requested is None:
        # 已有部署可能只在数据库机保存 .env，或在 GPU 机使用预制引擎清单。
        deployment = repo / (os.environ.get("MYCOMFY_DEPLOYMENT_DIR") or "var/deployment")
        if (repo / ".env").is_file() or (deployment / "deployment.json").is_file():
            return None

    # 模板相对路径以仓库为基准。提示写到 stderr，保持 Compose 的标准输出可供解析。
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
    edge_ports = [
        int(gateway["environment"][name]) for name in ("MYCOMFY_API_PORT", "MYCOMFY_LOCAL_PORT")
    ]
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
        repo / ".env",
    ]
    if config_path is not None:
        private.append(config_path)
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
        # 使用已部署镜像的实例配置快照，兼容尚未升级停机处理的旧引擎。
        probe = Path(__file__).with_name("stop_snapshot.py").read_text()
        result = run_compose(
            "exec", "-T", "engine", "python", "-", input=probe, stdout=subprocess.PIPE, text=True
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
        help="部署参数文件；自动查找 deploy.config.yaml、.yml、.json，缺失时使用仓库 YAML 模板",
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
        "--compose", nargs=argparse.REMAINDER, help="用同一份配置执行 Compose 命令，如 ps 或 logs"
    )
    parser.add_argument(
        "--stop", action="store_true", help="排空在途任务后停止本项目服务，可指定机器角色"
    )
    args = parser.parse_args()
    if args.stop and args.compose is not None:
        parser.error("--stop 不能与 --compose 一起使用")
    repo = Path(__file__).resolve().parents[1]
    config_path = select_config(repo, args.config)
    process_env = {**os.environ, "PWD": str(repo)}
    compose = ["docker", "compose"]
    recipe = None
    single_host = args.single_host

    if args.stop:
        # stop 只按项目和服务名操作已有容器。无需生成密码、安装模型或解析部署参数。
        if config_path is not None:
            compose += ["--env-file", os.devnull]
    elif config_path is not None:
        # 配置文件提供 Compose 凭据，单独的引擎参数通过 stdin 传给安装器。
        settings, recipe, single_host = read_config(
            config_path,
            repo,
            core_only=args.db_only or args.core_only or args.compose is not None,
            database_init=args.db_only,
            redis_optional=args.gpu_only or args.compose is not None,
            single_host=single_host,
        )
        process_env.update(settings)
        compose += ["--env-file", os.devnull]
    elif args.compose is None:
        # 已部署的项目仍可沿用原有 .env、deployment.json 和离线锁文件。
        subprocess.run(
            ["sh", "scripts/init-env.sh", "--generate-passwords"],
            check=True,
            cwd=repo,
            env=process_env,
        )
    if single_host or args.db_only or (args.stop and not (args.core_only or args.gpu_only)):
        # 两阶段复用同一 Compose 项目和网络，后续运维也可用配置开关查看数据库。
        compose += ["--profile", "database"]

    def run_compose(*arguments, **options):
        """让部署和日常 Compose 命令使用相同的工作目录和配置快照。"""
        return subprocess.run(
            [*compose, *arguments], cwd=repo, env=process_env, check=True, **options
        )

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
    if deploy_local_stack and config_path is None:
        # 旧版 .env 也可切换到单机，连接串使用随附数据库的实际账号和发布端口。
        process_env.update(legacy_single_host_environment(services))
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
        if recipe is None and not (deployment / "deployment.json").is_file():
            raise ValueError(
                f"旧版部署缺少 {deployment / 'deployment.json'}。"
                "使用默认 H3 模板可在部署命令中指定 --config deploy.config.example.yaml。"
            )
        build += ["engine", "worker"]
        start += ["engine", "worker"]
    if deploy_local_stack:
        # 单机全栈在配置检查完成后启动数据层。显式角色仍只操作各自的服务。
        start_database()
    run_compose("build", *build)

    # 在具备 GPU 和持久卷的临时 engine 容器中安装，失败时保留已有服务和数据。
    if not args.core_only:
        install = [
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "mycomfy-deploy-engine",
            "engine",
        ]
        if recipe is None:
            run_compose(*install)
        else:
            run_compose(*install, "--recipe", "-", input=json.dumps(recipe).encode("utf-8"))
    # 分机时业务机已完成迁移。GPU 机同时指定 engine 和 worker，只启动这两个服务。
    options = ["--no-deps"] if args.gpu_only else []
    run_compose("up", "-d", "--wait", "--no-build", *options, *start)
    if args.gpu_only:
        print("GPU 引擎和 Worker 已启动。", flush=True)
    print("工作台地址", owner["environment"]["MYCOMFY_PUBLIC_ORIGIN"], flush=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
