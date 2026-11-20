"""为两个监听器分别注册各自网页的静态入口。"""

from pathlib import Path

from fastapi import Request
from starlette.staticfiles import StaticFiles

CLIENT_FILES = (
    "app.js",
    "session.js",
    "client.css",
    "composer.js",
    "composer.css",
    "history.js",
    "history.css",
    "studio.webp",
)
ADMIN_FILES = ("admin.js", "admin.css", "overview.js", "overview.css")


def install_web_routes(app, *, local):
    """从源码或安装包提供当前入口的页面、脚本和样式。"""
    package = Path(__file__).resolve().parents[1]
    web = package / "web"
    if not web.is_dir():
        web = package.parents[1] / "web"
    static = StaticFiles(directory=web / ("admin" if local else "client"))

    async def web_file(request: Request):
        """根据已匹配的固定路由返回资源，支持条件请求和 HEAD。"""
        name = request.scope["route"].path.lstrip("/") or "index.html"
        response = await static.get_response(name, request.scope)
        request.state.web_asset = True
        return response

    for name in ("", "index.html", *(ADMIN_FILES if local else CLIENT_FILES)):
        app.add_api_route("/" + name, web_file, methods=["GET", "HEAD"], include_in_schema=False)
