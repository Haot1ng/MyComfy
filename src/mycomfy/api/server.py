"""在同一 API 进程中分别监听用户端口和回环维护端口。"""

import asyncio
import os
import socket
from contextlib import ExitStack, asynccontextmanager, contextmanager
from pathlib import Path

import uvicorn
from starlette.routing import Router
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from mycomfy.database.session import make_sessions

from .app import create_app


class ApiServer(uvicorn.Server):
    """在 HTTP 连接排空前结束长连接订阅。"""

    def __init__(self, config, stop_streams):
        """保存当前服务实例与 SSE 订阅共享的停机信号。"""
        super().__init__(config)
        self.stop_streams = stop_streams

    async def shutdown(self, sockets=None):
        """唤醒所有 SSE 订阅，然后使用 Uvicorn 的优雅退出流程。"""
        # lifespan.shutdown 要等到活动 HTTP 请求结束才会触发。
        self.stop_streams.set()
        await super().shutdown(sockets=sockets)


@contextmanager
def _listeners(host, port, local_port, socket_dir):
    """直启时监听 TCP，网关部署时只在共享私有卷创建当前用户可连接的两个 Unix 套接字。"""
    with ExitStack() as stack:
        if socket_dir is None:
            if port == local_port and port != 0:
                raise ValueError("公开端口与本机端口必须不同")
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            yield [
                stack.enter_context(socket.create_server((host, port), family=family)),
                stack.enter_context(socket.create_server(("127.0.0.1", local_port))),
            ]
            return

        import fcntl

        directory = Path(socket_dir).resolve()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # 对目录 inode 加锁，异常退出会释放锁。只有锁持有人可清理上次崩溃留下的 socket。
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sockets = []
        for name in ("public.sock", "admin.sock"):
            path = directory / name
            if path.is_socket():
                path.unlink()
            listener = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            listener.bind(str(path))
            stack.callback(path.unlink, missing_ok=True)
            path.chmod(0o600)
            listener.listen()
            sockets.append(listener)
        yield sockets


@contextmanager
def api_server(settings, host, port, local_port, *, sessions=None, socket_dir=None):
    """在同一进程中运行公开、本机接口，按物理监听套接字区分两个应用。"""
    with _listeners(host, port, local_port, socket_dir) as sockets:
        shared = sessions if sessions is not None else make_sessions(settings.database_url)
        try:
            stop_streams = asyncio.Event()
            public = create_app(settings, sessions=shared, stop_streams=stop_streams)
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
            uds = socket_dir is not None
            trusted = "*" if uds else os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
            public_proxy = ProxyHeadersMiddleware(public, trusted_hosts=trusted)
            addresses = [listener.getsockname() for listener in sockets]
            # TCP 通配地址接受的连接具有不同的本机 IP，以端口区分；UDS 使用完整文件路径。
            keys = addresses if uds else [address[1] for address in addresses]
            handlers = dict(zip(keys, [public_proxy, local], strict=True))

            async def dispatch(scope, receive, send):
                """按传输层监听地址选取应用，仅在私有 UDS 上赋予网关本机代理身份。"""
                if scope["type"] == "lifespan":
                    await lifecycle.lifespan(scope, receive, send)
                    return
                # Uvicorn 从已接收连接的 getsockname 填充 server，HTTP 请求头不能指定这个值。
                address = scope.get("server")
                key = address[0 if uds else 1] if address else None
                if key not in handlers:
                    raise RuntimeError("请求未关联已登记的监听套接字")
                if uds:
                    # Unix 对端没有 TCP 地址；只有同 UID 的网关能连接这些 0600 套接字。
                    scope = {**scope, "client": ("127.0.0.1", 0)}
                await handlers[key](scope, receive, send)

            config = uvicorn.Config(dispatch, proxy_headers=False, access_log=False, lifespan="on")
            yield ApiServer(config, stop_streams), sockets
        finally:
            if sessions is None:
                shared.kw["bind"].dispose()
