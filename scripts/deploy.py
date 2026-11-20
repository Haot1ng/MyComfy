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
    """选择指定或本机 JSON。新部署使用仓库模板，已有旧版部署返回 None。"""
    path = requested.resolve() if requested is not None else repo / "deploy.config.json"
    if path.exists():
        return path

    if requested is None:
        # 已有部署可能只在数据库机保存 .env，或在 GPU 机使用预制引擎清单。
        deployment = repo / (os.environ.get("MYCOMFY_DEPLOYMENT_DIR") or "var/deployment")
        if (repo / ".env").is_file() or (deployment / "deployment.json").is_file():
            return None

    # 模板相对路径以仓库为基准。提示写到 stderr，保持 Compose 的标准输出可供解析。
    template = repo / "deploy.config.example.json"
    print(f"未找到配置 {path}，使用默认模板 {template}。", file=sys.stderr)
    return template


def bind_source(service, target):
    """按容器内的挂载点找到 Compose 实际使用的宿主目录。"""
    for mount in service["volumes"]:
        if mount["type"] == "bind" and mount["target"] == target:
            # Compose 序列化配置时将路径内的字面量 $ 写成 $$，文件系统使用原字符。
            return Path(mount["source"].replace("$$", "$"))
    raise ValueError(f"Compose 配置中缺少目录挂载 {target}")


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


def main():
    """读取一份部署配置，按所选模式构建镜像、部署引擎并等待服务就绪。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, help="部署参数文件，默认查找 deploy.config.json，缺失时使用仓库模板"
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
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    config_path = select_config(repo, args.config)
    process_env = {**os.environ, "PWD": str(repo)}
    compose = ["docker", "compose"]
    recipe = None
    single_host = args.single_host

    if config_path is not None:
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
    if single_host or args.db_only:
        # 两阶段复用同一 Compose 项目和网络，后续运维也可用配置开关查看数据库。
        compose += ["--profile", "database"]

    def run_compose(*arguments, **options):
        """让部署和日常 Compose 命令使用相同的工作目录和配置快照。"""
        return subprocess.run(
            [*compose, *arguments], cwd=repo, env=process_env, check=True, **options
        )

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
    if deploy_local_stack and recipe is None:
        # 旧版 .env 也可切换到单机，连接串使用随附数据库的实际账号和发布端口。
        process_env.update(legacy_single_host_environment(services))
    owner = services["engine" if args.gpu_only else "core"]
    data = bind_source(owner, owner["environment"]["MYCOMFY_DATA_ROOT"])
    data.mkdir(mode=0o700, parents=True, exist_ok=True)

    build = []
    start = []
    if not args.gpu_only:
        prepare_token(bind_source(services["core"], "/run/mycomfy"))
        build.extend(["migrate", "core"])
        start.append("core")
    if not args.core_only:
        deployment = bind_source(services["engine"], "/deployment")
        deployment.mkdir(mode=0o700, parents=True, exist_ok=True)
        if recipe is None and not (deployment / "deployment.json").is_file():
            raise ValueError(
                f"旧版部署缺少 {deployment / 'deployment.json'}。"
                "使用默认 H3 模板可在部署命令中指定 --config deploy.config.example.json。"
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
