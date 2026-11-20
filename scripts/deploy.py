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


def bind_source(service, target):
    """按容器内的挂载点找到 Compose 实际使用的宿主目录。"""
    for mount in service["volumes"]:
        if mount["type"] == "bind" and mount["target"] == target:
            return Path(mount["source"])
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
    """初始化配置，按所选模式构建镜像、部署引擎并等待服务启动。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-only", action="store_true", help="只启动网页、API、MySQL 和 Redis")
    args = parser.parse_args()
    os.chdir(Path(__file__).resolve().parents[1])
    # Compose 展开模板中的 ${PWD} 时使用环境变量，而 Python 的 chdir 不会更新它。
    os.environ["PWD"] = str(Path.cwd())
    compose = ["docker", "compose"]

    # 首次运行生成独立密码，已有 .env 由部署者管理。由 Compose 自己解析变量和覆盖文件。
    subprocess.run(["sh", "scripts/init-env.sh", "--generate-passwords"], check=True)
    config = subprocess.check_output([*compose, "config", "--format", "json"])
    services = json.loads(config)["services"]
    core = services["core"]
    data = bind_source(core, core["environment"]["MYCOMFY_DATA_ROOT"])
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    prepare_token(bind_source(core, "/run/mycomfy"))

    build = ["migrate", "core"]
    if not args.core_only:
        deployment = bind_source(services["engine"], "/deployment")
        deployment.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not (deployment / "deployment.json").is_file():
            raise ValueError(
                f"请准备 {deployment / 'deployment.json'} 及其依赖锁和模型，"
                "或用 --core-only 先启动工作台"
            )
        build += ["engine", "worker"]
    subprocess.run([*compose, "build", *build], check=True)

    # 在具备 GPU 和持久卷的临时 engine 容器中安装，失败时保留已有服务和数据。
    if not args.core_only:
        subprocess.run(
            [
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--entrypoint",
                "mycomfy-deploy-engine",
                "engine",
            ],
            check=True,
        )
    start = ["core"] if args.core_only else ["core", "engine", "worker"]
    subprocess.run([*compose, "up", "-d", "--wait", "--no-build", *start], check=True)
    print("部署完成，工作台地址", core["environment"]["MYCOMFY_PUBLIC_ORIGIN"], flush=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
