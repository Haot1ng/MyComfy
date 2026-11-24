"""在同源管理页面查看接口流量并动态保存或重置限流规则。"""

from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from mycomfy.database.session import db_now, timestamp
from mycomfy.errors import ApiError, not_found
from mycomfy.ops.traffic_metrics import core_window, edge_window, gateway_state, route_metrics
from mycomfy.ops.traffic_store import effective_rules, load_rule_overrides, route_key, store_rule

from .traffic_catalog import default_rule, gateway_snapshot, publish_gateway_rules, route_catalog

PREFIX = "/local/v1/traffic"


class RuleValues(BaseModel):
    """完整替换的非负整数限额；0 关闭对应额度，突发量为 0 表示不额外放行。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    edge_rpm: int = Field(ge=0, le=60000)
    edge_burst: int = Field(ge=0, le=10000)
    user_rpm: int = Field(ge=0, le=60000)
    provider_concurrency: int = Field(ge=0, le=1024)


class RuleUpdate(BaseModel):
    """用监听器、方法和固定路径选择接口，显式传 null 恢复内置默认值。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    listener: Literal["public", "local"]
    method: str = Field(pattern=r"^[A-Z]{1,10}$")
    path: str = Field(min_length=1, max_length=255)
    rule: RuleValues | None


def is_traffic_admin_operation(request):
    """仅允许流量管理页面通过已配置的同源维护通道查看和写入规则。"""
    return (request.method, request.scope["path"]) in {("GET", PREFIX), ("PUT", PREFIX + "/rules")}


def _item(route, override):
    """为列表与保存响应统一返回适用范围、基准规则及实际生效的限额。"""
    defaults = default_rule(route)
    return {
        **route,
        "defaults": defaults,
        "rule": override if override is not None else defaults,
        "overridden": override is not None,
    }


def build_traffic_admin_router(sessions, audit):
    """构建管理端路由，并保证限流配置和操作审计属于同一笔写入。"""
    router = APIRouter(prefix=PREFIX)

    @router.get("")
    def list_traffic(request: Request, window_minutes: Annotated[int, Query(ge=1, le=1440)] = 15):
        """列出所有注册接口（含零流量），汇总同一组分钟桶中的入口或核心请求。"""
        routes = route_catalog(request.app)
        directory = request.app.state.gateway_dir
        with sessions() as db:
            now = db_now(db)
            # 包含当前正在累积的一分钟；两个来源使用同一时间边界和配置的窗口分钟数。
            start = now.replace(second=0, microsecond=0) - timedelta(minutes=window_minutes - 1)
            overrides = load_rule_overrides(db)
            edge = edge_window(directory, start, now)
            core = core_window(db, routes, start, now, rejections_only=edge is not None)
        items = []
        for route in routes:
            key = route_key(route)
            items.append(
                {
                    **_item(route, overrides.get(key)),
                    "metrics": route_metrics(key, core, edge, window_minutes),
                }
            )
        # 期望修订来自此页显示的数据库规则，即使上一次写 rules.json 失败仍然准确。
        rules = {route_key(item): item["rule"] for item in items}
        desired = gateway_snapshot(routes, rules)["revision"] if directory else None
        return {
            "window_minutes": window_minutes,
            "window_start": timestamp(start),
            "as_of": timestamp(now),
            "metrics_source": "gateway" if edge is not None else "core",
            "gateway": gateway_state(directory, desired, now, edge is not None),
            "items": items,
        }

    @router.put("/rules")
    def update_rule(request: Request, body: RuleUpdate):
        """仅接受已注册的接口；先提交整条规则，再要求网关控制器读取最新快照。"""
        key = (body.listener, body.method, body.path)
        routes = route_catalog(request.app)
        route = next((item for item in routes if route_key(item) == key), None)
        if route is None:
            raise not_found()
        rule = body.rule.model_dump() if body.rule is not None else None
        if rule and (
            (rule["user_rpm"] and not route["user_scoped"])
            or (rule["provider_concurrency"] and not route["provider_scoped"])
        ):
            raise ApiError(
                422, "TRAFFIC_RULE_UNSUPPORTED", "该接口不适用所选的用户限流或服务商并发规则。"
            )
        directory = request.app.state.gateway_dir
        with sessions.begin() as db:
            store_rule(db, route, rule)
            audit(db, request, 200)
            now = db_now(db)
            desired = (
                gateway_snapshot(routes, effective_rules(db, routes))["revision"]
                if directory
                else None
            )
        available = edge_window(directory, now, now) is not None
        try:
            published = publish_gateway_rules(request.app)
        except (OSError, SQLAlchemyError):
            # 业务规则已经提交。周期同步器将重试，页面此时不得显示旧快照已经生效。
            status = gateway_state(directory, desired, now, available)
            status["state"] = "error"
        else:
            status = gateway_state(directory, published, now, available)
        return {**_item(route, rule), "gateway": status}

    return router
