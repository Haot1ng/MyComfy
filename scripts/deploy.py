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

from deployment_config import read_config


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
        "--config", type=Path, help="部署参数文件，默认使用仓库根目录的 deploy.config.json"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--core-only", action="store_true", help="只启动网页、API、MySQL 和 Redis")
    mode.add_argument(
        "--compose", nargs=argparse.REMAINDER, help="用同一份配置执行 Compose 命令，如 ps 或 logs"
    )
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    config_path = args.config.resolve() if args.config else repo / "deploy.config.json"
    process_env = {**os.environ, "PWD": str(repo)}
    compose = ["docker", "compose"]
    recipe = None

    if args.config or config_path.exists():
        # 配置文件提供 Compose 凭据，单独的引擎参数通过 stdin 传给安装器。
        settings, recipe = read_config(
            config_path, repo, core_only=args.core_only or args.compose is not None
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
    config = run_compose("config", "--format", "json", stdout=subprocess.PIPE).stdout
    services = json.loads(config)["services"]
    core = services["core"]
    data = bind_source(core, core["environment"]["MYCOMFY_DATA_ROOT"])
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    prepare_token(bind_source(core, "/run/mycomfy"))

    build = ["migrate", "core"]
    if not args.core_only:
        deployment = bind_source(services["engine"], "/deployment")
        deployment.mkdir(mode=0o700, parents=True, exist_ok=True)
        if recipe is None and not (deployment / "deployment.json").is_file():
            raise ValueError(
                f"新部署请填写 {repo / 'deploy.config.json'}，模板是 deploy.config.example.json。"
                f"已有项目可继续使用 {deployment / 'deployment.json'} 及其依赖锁。"
            )
        build += ["engine", "worker"]
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
    start = ["core"] if args.core_only else ["core", "engine", "worker"]
    run_compose("up", "-d", "--wait", "--no-build", *start)
    print("部署完成，工作台地址", core["environment"]["MYCOMFY_PUBLIC_ORIGIN"], flush=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
