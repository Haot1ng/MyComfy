"""从已注册的 API 路由生成监控目录，并把入口规则原子发布给 Nginx。"""

import asyncio
import contextlib
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import SQLAlchemyError
from starlette.routing import compile_path

from mycomfy.generation.state import canonical_digest
from mycomfy.ops.monitor import MODELS as MONITOR_COLLECTIONS

PROMPT_PATH = "/api/v1/strategy-versions/{version_id}/prompt-assist"
ANONYMOUS = {("POST", "/api/v1/accounts"), ("POST", "/api/v1/sessions")}
LOG = logging.getLogger(__name__)


def _paths(path):
    """展开监控接口支持的集合名，让任务列表和 GPU 状态可以独立观测与限流。"""
    if path == "/local/v1/monitor/{collection}":
        return [path.replace("{collection}", key) for key in ("overview", *MONITOR_COLLECTIONS)]
    if path == "/local/v1/monitor/{collection}/{row_id}":
        return [path.replace("{collection}", key) for key in MONITOR_COLLECTIONS]
    return [path]


def register_catalog(app, routers, listener):
    """遍历两个监听器使用的路由，按真实注册顺序构建目录及本监听器的匹配索引。"""
    catalog = []
    matchers = {}
    for side, group in routers.items():
        for router in group:
            for endpoint in router.routes:
                for path in _paths(endpoint.path):
                    for method in sorted(endpoint.methods):
                        user_scoped = (
                            side == "public"
                            and (method, path) not in ANONYMOUS
                            and (method, path) != ("DELETE", "/api/v1/sessions/current")
                        )
                        route = {
                            "listener": side,
                            "method": method,
                            "path": path,
                            "user_scoped": user_scoped,
                            "provider_scoped": path == PROMPT_PATH and method == "POST",
                        }
                        catalog.append(route)
                        if side == listener:
                            regex, _, _ = compile_path(path)
                            matchers.setdefault(method, []).append((regex, route))
    app.state.traffic_catalog = catalog
    app.state.traffic_matchers = matchers


def route_catalog(app):
    """返回本次部署提供的所有业务和管理 API，包括未产生流量的入口。"""
    return app.state.traffic_catalog


def match_traffic_route(app, method, path):
    """在解析请求体和鉴权之前取得固定模板，丢弃查询串及资源 ID 等高基数内容。"""
    for pattern, route in app.state.traffic_matchers.get(method, ()):
        if pattern.fullmatch(path):
            return route
    return None


def default_rule(route):
    """定义首次部署和恢复默认值时每个入口实际使用的两个限流层级。"""
    # 一般读写与浏览器轮询留足突发余量；注册和付费模型调用使用较低的独立额度。
    edge_rpm, burst = 600, 60
    user_rpm = 120 if route["user_scoped"] else 0
    if route["path"] == "/api/v1/accounts":
        edge_rpm, burst = 30, 5
    if route["provider_scoped"]:
        edge_rpm, burst, user_rpm = 60, 10, 12
    return {
        "edge_rpm": edge_rpm,
        "edge_burst": burst,
        "user_rpm": user_rpm,
        "provider_concurrency": 4 if route["provider_scoped"] else 0,
    }


def gateway_snapshot(routes, rules):
    """从数据库有效规则生成网关期望的修订号，让界面和发布器使用同一份计算。"""
    snapshot = []
    for route in routes:
        side, method, path = route["listener"], route["method"], route["path"]
        rule = rules[side, method, path]
        snapshot.append(
            {
                "listener": side,
                "method": method,
                "path": path,
                "edge_rpm": rule["edge_rpm"],
                "edge_burst": rule["edge_burst"],
            }
        )
    revision = canonical_digest(snapshot)
    return {"revision": revision, "routes": snapshot}


def publish_gateway_rules(app):
    """持锁重新读取已提交的规则，并原子替换仅含入口限流参数的快照。"""
    from mycomfy.ops.traffic_store import effective_rules

    directory = app.state.gateway_dir
    if directory is None:
        return None
    import fcntl

    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "publish.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        routes = route_catalog(app)
        with app.state.sessions() as db:
            rules = effective_rules(db, routes)
        snapshot = gateway_snapshot(routes, rules)
        revision = snapshot["revision"]
        payload = json.dumps(snapshot, separators=(",", ":")).encode()
        destination = directory / "rules.json"
        try:
            if destination.read_bytes() == payload:
                return revision
        except FileNotFoundError:
            pass
        # 唯一临时文件只在落盘后发布；网关读到的是旧快照或完整的新快照。
        with NamedTemporaryFile(dir=directory, prefix=".rules-", delete=False) as temp:
            try:
                temp.write(payload)
                temp.flush()
                os.fsync(temp.fileno())
                os.replace(temp.name, destination)
            finally:
                Path(temp.name).unlink(missing_ok=True)
        return revision


@asynccontextmanager
async def gateway_sync(app):
    """在业务机启动时导出完整目录，定期修复数据库提交后未及时发布的入口快照。"""
    if app.state.gateway_dir is None:
        yield
        return
    await run_in_threadpool(publish_gateway_rules, app)

    async def reconcile():
        """处理服务重启、文件被清理或一次规则发布失败后的重建。"""
        while True:
            await asyncio.sleep(15)
            try:
                await run_in_threadpool(publish_gateway_rules, app)
            except (OSError, SQLAlchemyError) as error:
                LOG.error("Gateway rule sync failed (%s)", type(error).__name__)

    task = asyncio.create_task(reconcile())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
