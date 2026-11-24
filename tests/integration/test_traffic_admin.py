"""从本机管理入口验证路由规则、真实请求计数和网关状态。"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from mycomfy.api.app import create_app
from mycomfy.api.traffic_catalog import PROMPT_PATH
from mycomfy.config import Settings
from mycomfy.database.models import Audit
from mycomfy.database.schema import migrate
from mycomfy.database.session import db_now, make_sessions, new_id, timestamp
from mycomfy.ops.traffic_store import effective_rule, route_key

LOCAL = "http://127.0.0.1:8001"
PAGE = {"X-MyComfy-Admin": "traffic", "Origin": LOCAL}
URL = "/local/v1/traffic"
TARGET = {"listener": "public", "method": "GET", "path": "/api/v1/me"}


@pytest.fixture
def factory(tmp_path):
    """迁移独立业务库，同时支持多个监听器和应用重建。"""
    url = f"sqlite:///{tmp_path / 'traffic.sqlite'}"
    migrate(url)
    sessions = make_sessions(url)
    try:
        yield sessions
    finally:
        sessions.kw["bind"].dispose()


def local_client(sessions, tmp_path):
    """构造本机维护监听器，让测试请求经历真实授权、路由和审计中间件。"""
    app = create_app(
        Settings(database_url="unused", data_root=tmp_path), sessions=sessions, local=True
    )
    return TestClient(app, base_url=LOCAL, client=("127.0.0.1", 9000))


def report(client, target=TARGET):
    """读取五分钟统计和指定固定路由，避免依赖目录排序。"""
    result = client.get(URL, headers=PAGE, params={"window_minutes": 5})
    assert result.status_code == 200, result.text
    payload = result.json()
    return payload, next(
        route for route in payload["items"] if route_key(route) == route_key(target)
    )


def audit_row(db, status, duration, *, listener="public", layer=None, minutes=1):
    """添加窗口内外新旧格式的审计事实，区分 HTTP 429 和核心限流器实际拒绝。"""
    db.add(
        Audit(
            id=new_id("r"),
            action="GET /api/v1/me",
            result=status,
            duration_ms=duration,
            created_at=db_now(db) - timedelta(minutes=minutes),
            traffic_listener=listener,
            rate_limit_layer=layer,
        )
    )


def test_admin_rules_and_core_only_metrics(factory, tmp_path, monkeypatch):
    """规则可持久保存、清空，未安装网关也能观察核心指标和真实拒绝次数。"""
    monkeypatch.delenv("MYCOMFY_GATEWAY_DIR", raising=False)
    with factory.begin() as db:
        audit_row(db, 429, 10, layer="core")
        audit_row(db, 429, 20)
        audit_row(db, 503, 30, listener=None)
        audit_row(db, 200, 999, minutes=20)
    with local_client(factory, tmp_path) as client:
        assert client.get(URL).status_code in {403, 503}
        assert client.get(URL, headers=PAGE, params={"window_minutes": 1441}).status_code == 422
        payload, row = report(client)
        assert payload["metrics_source"] == "core" and payload["gateway"]["state"] == "unavailable"
        assert row["metrics"] == {
            "requests": 3,
            "errors_4xx": 2,
            "errors_5xx": 1,
            "requests_per_minute": 0.6,
            "avg_latency_ms": 20.0,
            "max_latency_ms": 30,
            "core_rejections": 1,
            "edge_rejections": None,
            "gateway_failures": None,
        }
        assert row["rule"] == row["defaults"] and not row["overridden"]
        data = {**TARGET, "rule": {**row["defaults"], "user_rpm": 7, "edge_burst": 0}}
        assert (
            client.put(
                URL + "/rules", json=data, headers={**PAGE, "Origin": "http://elsewhere"}
            ).status_code
            == 403
        )
        assert (
            client.put(
                URL + "/rules",
                json={**data, "rule": {**data["rule"], "provider_concurrency": 3}},
                headers=PAGE,
            ).status_code
            == 422
        )
        assert (
            client.put(
                URL + "/rules", json={**data, "path": "/api/v1/jobs/some-id"}, headers=PAGE
            ).status_code
            == 404
        )
        changed = client.put(URL + "/rules", json=data, headers=PAGE)
        assert changed.status_code == 200 and changed.json()["overridden"]
        with factory() as db:
            assert effective_rule(db, row) == data["rule"]
    with local_client(factory, tmp_path) as restarted:
        assert report(restarted)[1]["rule"] == data["rule"]
        reset = restarted.put(URL + "/rules", json={**TARGET, "rule": None}, headers=PAGE)
        assert not reset.json()["overridden"] and reset.json()["rule"] == row["defaults"]


def test_gateway_authoritative_metrics_and_sync(factory, tmp_path, monkeypatch):
    """入口记录已包含核心响应；统计不能把同一请求加两次，也不能将旧心跳显示为生效。"""
    directory = tmp_path / "gateway"
    monkeypatch.setenv("MYCOMFY_GATEWAY_DIR", str(directory))
    with factory.begin() as db:
        audit_row(db, 429, 10, layer="core")
        now = db_now(db)
    with local_client(factory, tmp_path) as client:
        desired = json.loads((directory / "rules.json").read_text())["revision"]
        heartbeat = {"revision": desired, "updated_at": timestamp(now), "error": None}
        status = directory / "status.json"
        status.write_text(json.dumps(heartbeat))
        with sqlite3.connect(directory / "edge.sqlite3") as edge:
            edge.execute(
                """CREATE TABLE edge_minutes(minute INTEGER, listener TEXT, method TEXT, path TEXT, requests INTEGER, status_4xx INTEGER, status_5xx INTEGER, duration_ms INTEGER, max_duration_ms INTEGER, rejected INTEGER, gateway_failures INTEGER)"""
            )
            minute = int(now.replace(second=0, microsecond=0, tzinfo=UTC).timestamp())
            edge.execute(
                "INSERT INTO edge_minutes VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (minute, *route_key(TARGET), 8, 2, 3, 100, 45, 1, 2),
            )
        payload, row = report(client)
        assert payload["gateway"]["state"] == "applied" and payload["metrics_source"] == "gateway"
        assert row["metrics"] == {
            "requests": 8,
            "errors_4xx": 2,
            "errors_5xx": 3,
            "requests_per_minute": 1.6,
            "avg_latency_ms": 12.5,
            "max_latency_ms": 45,
            "core_rejections": 1,
            "edge_rejections": 1,
            "gateway_failures": 2,
        }
        changed = client.put(
            URL + "/rules", headers=PAGE, json={**TARGET, "rule": {**row["rule"], "edge_rpm": 1}}
        )
        assert changed.json()["gateway"]["state"] == "pending"
        assert report(client)[0]["gateway"]["applied_revision"] == desired
        status.write_text(json.dumps({**heartbeat, "error": "reload failed"}))
        assert report(client)[0]["gateway"]["state"] == "error"
        status.write_text(
            json.dumps({**heartbeat, "updated_at": timestamp(now - timedelta(minutes=5))})
        )
        assert report(client)[0]["gateway"]["state"] == "unavailable"

        def fail_snapshot(_app):
            """模拟数据库提交后，共享目录因临时写入错误未能发布新修订。"""
            raise OSError("fixture")

        previous = json.loads((directory / "rules.json").read_text())["revision"]
        status.write_text(json.dumps({**heartbeat, "revision": previous}))
        monkeypatch.setattr("mycomfy.api.traffic_admin.publish_gateway_rules", fail_snapshot)
        response = client.put(URL + "/rules", headers=PAGE, json={**TARGET, "rule": None})
        assert response.status_code == 200 and response.json()["gateway"]["state"] == "error"
        gateway = report(client)[0]["gateway"]
        assert gateway["desired_revision"] == desired and gateway["applied_revision"] == previous
        assert gateway["state"] == "pending"


@pytest.mark.mysql
def test_concurrent_initial_rules(mysql_sessions, tmp_path, monkeypatch):
    """两个管理员同时新增同一条路由覆盖时，两次写均完整提交且得到可读取的最终配置。"""
    monkeypatch.delenv("MYCOMFY_GATEWAY_DIR", raising=False)
    with local_client(mysql_sessions, tmp_path) as client:
        target = {"listener": "public", "method": "POST", "path": PROMPT_PATH}
        _, row = report(client, target)
        barrier = Barrier(2)

        def save(limit):
            """在同一时刻发出不同的全量规则以检验 MySQL upsert。"""
            barrier.wait(timeout=5)
            return client.put(
                URL + "/rules",
                headers=PAGE,
                json={**target, "rule": {**row["rule"], "provider_concurrency": limit}},
            ).status_code

        with ThreadPoolExecutor(2) as pool:
            assert list(pool.map(save, [1, 2])) == [200, 200]
        assert report(client, target)[1]["rule"]["provider_concurrency"] in {1, 2}
