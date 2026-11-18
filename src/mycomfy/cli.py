"""命令行入口，独立运行数据库迁移、用户 API、本机 API 和 Worker。"""

import argparse
import sys

from .config import Settings
from .schema import migrate, require_current_schema


def main(argv=None):
    """解析服务子命令，检查数据库版本，并启动对应进程。"""
    args = list(sys.argv[1:] if argv is None else argv)
    # 部署与引擎管理有自己的子命令，原样传递参数以保留各自的帮助信息。
    if args and args[0] == "deploy":
        from .deploy import main as deploy_main

        return deploy_main(args[1:])
    if args and args[0] == "runtime":
        from .runtime import main as runtime_main

        return runtime_main(args[1:])

    parser = argparse.ArgumentParser(prog="mycomfy")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("deploy", help="按清单安装 ComfyUI、依赖、节点和模型")
    commands.add_parser("runtime", help="管理本机引擎实例与运行代次")
    commands.add_parser("migrate", help="升级 MySQL 结构")
    public = commands.add_parser("api", help="在一个进程运行用户及本机 API")
    public.add_argument("--host", default="127.0.0.1")
    public.add_argument("--port", type=int, default=8000)
    public.add_argument("--local-port", type=int, default=8001, help="本机监控与维护端口")
    local = commands.add_parser("local-api", help="仅运行回环诊断入口")
    local.add_argument("--port", type=int, default=8001)
    worker = commands.add_parser("worker", help="运行单机 Worker 协调进程")
    worker.add_argument("--once", action="store_true", help="只运行一次协调循环")
    selected = parser.parse_args(args)
    settings = Settings.from_env()
    if selected.command == "migrate":
        migrate(settings.database_url)
        return
    require_current_schema(settings.database_url)
    if selected.command == "worker":
        from .worker import run_worker

        return run_worker(settings, once=selected.once)

    if selected.command == "api":
        from .serve import api_server

        with api_server(settings, selected.host, selected.port, selected.local_port) as (
            server,
            sockets,
        ):
            server.run(sockets=sockets)
            if not server.started:
                # 将生命周期初始化失败反馈给守护进程。
                raise SystemExit(3)
        return

    import uvicorn

    from .api import create_app

    # 单独诊断入口只接受本机直连，端口选择不改变其绑定地址。
    uvicorn.run(
        create_app(settings, local=True),
        host="127.0.0.1",
        port=selected.port,
        proxy_headers=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
