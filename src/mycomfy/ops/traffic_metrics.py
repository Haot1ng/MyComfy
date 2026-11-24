"""按固定路由和分钟窗口统计流量，网关可用时以实际完成的入口请求为准。"""

import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, func, select

from mycomfy.database.models import Audit


@dataclass
class TrafficCounts:
    """保留可相加的事件与耗时，便于将迁移前后的分组汇总为一个窗口。"""

    requests: int = 0
    client_errors: int = 0
    server_errors: int = 0
    total_ms: int = 0
    max_ms: int = 0
    rejected: int = 0
    gateway_failures: int = 0


def core_window(db, routes, start, now, *, rejections_only=False):
    """在数据库中分组汇总核心审计，包括限流标记；历史请求仅在路由归属唯一时计入。"""
    locations = defaultdict(list)
    for route in routes:
        locations[(route["method"], route["path"])].append(route["listener"])
    # 网关提供总量时只访问真正的核心限流记录，避免扫描窗口内所有成功请求。
    groups = (Audit.traffic_listener, Audit.action)
    if rejections_only:
        statement = select(*groups, func.count().label("rejected")).where(
            Audit.rate_limit_layer == "core"
        )
    else:
        statement = select(
            *groups,
            func.count().label("requests"),
            func.sum(case((Audit.result.between(400, 499), 1), else_=0)).label("client_errors"),
            func.sum(case((Audit.result.between(500, 599), 1), else_=0)).label("server_errors"),
            func.sum(Audit.duration_ms).label("duration"),
            func.max(Audit.duration_ms).label("peak"),
            func.sum(case((Audit.rate_limit_layer == "core", 1), else_=0)).label("rejected"),
        )
    statement = statement.where(Audit.created_at >= start, Audit.created_at <= now).group_by(
        *groups
    )
    result = {}
    for row in db.execute(statement):
        side = row.traffic_listener
        method, _, path = row.action.partition(" ")
        sides = locations.get((method, path), ())
        if side is None and len(sides) == 1:
            side = sides[0]
        if side not in sides:
            continue
        key = (side, method, path)
        # 迁移前后的同一接口可以出现在两个分组中，延迟均值须按总数加权。
        bucket = result.setdefault(key, TrafficCounts())
        bucket.rejected += int(row.rejected)
        if not rejections_only:
            bucket.requests += row.requests
            bucket.client_errors += int(row.client_errors)
            bucket.server_errors += int(row.server_errors)
            bucket.total_ms += int(row.duration)
            bucket.max_ms = max(bucket.max_ms, row.peak)
    return result


def edge_window(directory, start, now):
    """只读查询网关按分钟聚合的总流量，区分空窗口与无权限、缺表或尚未启动。"""
    if directory is None:
        return None
    path = directory / "edge.sqlite3"
    try:
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)
        ) as db:
            rows = db.execute(
                """SELECT listener, method, path, SUM(requests), SUM(status_4xx), SUM(status_5xx),
                          SUM(duration_ms), MAX(max_duration_ms), SUM(rejected), SUM(gateway_failures)
                   FROM edge_minutes WHERE minute BETWEEN ? AND ? GROUP BY listener, method, path""",
                (
                    int(start.replace(tzinfo=UTC).timestamp()),
                    int(now.replace(tzinfo=UTC).timestamp()),
                ),
            )
            return {
                (side, method, path): TrafficCounts(*(int(v) for v in values))
                for side, method, path, *values in rows
            }
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None


def _gateway_json(path):
    """读取网关共享目录中的原子 JSON 快照，缺失或不完整时返回空对象。"""
    try:
        with path.open("rb") as stream:
            data = json.loads(stream.read(1024 * 1024 + 1))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def gateway_state(directory, desired, now, metrics_available):
    """通过新鲜心跳和期望/生效修订判断入口应用状态，仅返回可公开的固定状态。"""
    status = _gateway_json(directory / "status.json") if directory else {}
    applied = status.get("revision")
    if not isinstance(desired, str) or not re.fullmatch("[0-9a-f]{64}", desired):
        desired = None
    if not isinstance(applied, str) or not re.fullmatch("[0-9a-f]{64}", applied):
        applied = None
    try:
        stamp = datetime.fromisoformat(status["updated_at"])
        if stamp.tzinfo is None:
            raise ValueError
        fresh = -30 <= (now.replace(tzinfo=UTC) - stamp).total_seconds() <= 90
    except (KeyError, TypeError, ValueError, OverflowError):
        stamp, fresh = None, False
    state = "unavailable"
    if fresh:
        if status.get("error"):
            state = "error"
        else:
            state = "applied" if desired and desired == applied else "pending"
    return {
        "state": state,
        "desired_revision": desired,
        "applied_revision": applied,
        "updated_at": stamp.astimezone(UTC).isoformat() if stamp else None,
        "metrics_available": metrics_available,
    }


def route_metrics(key, core, edge, minutes):
    """从一个权威来源计算请求、错误和延迟，另计 core 限流，避免叠加两层观测。"""
    core_counts = core.get(key, TrafficCounts())
    counts = edge.get(key, TrafficCounts()) if edge is not None else core_counts
    return {
        "requests": counts.requests,
        "errors_4xx": counts.client_errors,
        "errors_5xx": counts.server_errors,
        "requests_per_minute": round(counts.requests / minutes, 2),
        "core_rejections": core_counts.rejected,
        "edge_rejections": counts.rejected if edge is not None else None,
        "gateway_failures": counts.gateway_failures if edge is not None else None,
        "avg_latency_ms": round(counts.total_ms / counts.requests, 2) if counts.requests else None,
        "max_latency_ms": counts.max_ms if counts.requests else None,
    }
