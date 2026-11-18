"""在同一 API 进程中分别监听用户端口和回环维护端口。"""

import os
import socket
from contextlib import asynccontextmanager, contextmanager

import uvicorn
from starlette.routing import Router
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from mycomfy.database.session import make_sessions

from .app import create_app


@contextmanager
def api_server(settings, host, port, local_port, *, sessions=None):
    """绑定两个独立的系统套接字，共用数据库连接池及一个 Uvicorn 生命周期。"""
    if port == local_port and port != 0:
        raise ValueError("公开端口与本机端口必须不同")
    public_family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with (
        socket.create_server((host, port), family=public_family) as public_socket,
        socket.create_server(("127.0.0.1", local_port)) as local_socket,
    ):
        sockets = [public_socket, local_socket]
        shared = sessions if sessions is not None else make_sessions(settings.database_url)
        try:
            public = create_app(settings, sessions=shared)
            local = create_app(settings, local=True, sessions=shared)

            @asynccontextmanager
            async def lifespan(app):
                """同时管理两个子应用的启停，只有公开子应用运行事件分发器。"""
                async with (
                    public.router.lifespan_context(public),
                    local.router.lifespan_context(local),
                ):
                    yield

            lifecycle = Router(lifespan=lifespan)
            public_proxy = ProxyHeadersMiddleware(
                public, trusted_hosts=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
            )
            handlers = {
                public_socket.getsockname()[1]: public_proxy,
                local_socket.getsockname()[1]: local,
            }

            async def dispatch(scope, receive, send):
                """按传输层本地端口选择处理器，转发头只在选定公开应用后处理。"""
                if scope["type"] == "lifespan":
                    await lifecycle.lifespan(scope, receive, send)
                    return
                # Uvicorn 从已接收连接的 getsockname 填充 server，HTTP 请求头不能指定这个值。
                address = scope.get("server")
                if not address or address[1] not in handlers:
                    raise RuntimeError("请求未关联已登记的监听套接字")
                await handlers[address[1]](scope, receive, send)

            config = uvicorn.Config(dispatch, proxy_headers=False, access_log=False, lifespan="on")
            yield uvicorn.Server(config), sockets
        finally:
            if sessions is None:
                shared.kw["bind"].dispose()
