"""验证目录来自实际注册的处理器，网关快照能从数据库重建且没有资源 ID 或业务密钥。"""

import json

from fastapi.testclient import TestClient

from mycomfy.api.app import create_app
from mycomfy.api.traffic_catalog import (
    default_rule,
    match_traffic_route,
    publish_gateway_rules,
    route_catalog,
)
from mycomfy.config import Settings
from mycomfy.database.schema import migrate
from mycomfy.database.session import make_sessions


def test_catalog_on_both_listeners_and_startup_snapshot(tmp_path, monkeypatch):
    """公开与管理目录保持一致，未访问的路由已存在，网关重启后使用同一份规范化快照。"""
    directory = tmp_path / "gateway"
    monkeypatch.setenv("MYCOMFY_GATEWAY_DIR", str(directory))
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'traffic.db'}", data_root=tmp_path / "data"
    )
    migrate(settings.database_url)
    sessions = make_sessions(settings.database_url)
    try:
        public = create_app(settings, sessions=sessions)
        local = create_app(settings, local=True, sessions=sessions)
        routes = route_catalog(local)
        assert routes == route_catalog(public)
        keys = {(r["listener"], r["method"], r["path"]) for r in routes}
        assert len(keys) == len(routes)
        assert {
            ("public", "GET", "/api/v1/events/cursor"),
            ("public", "POST", "/api/v1/batches"),
            ("local", "GET", "/local/v1/monitor/instances/{row_id}"),
            ("local", "GET", "/local/v1/monitor/overview"),
            ("local", "PUT", "/local/v1/traffic/rules"),
        } <= keys
        # 查询、静态文件和资源标识不能新增监控维度；同一处理器的不同请求落在同一行。
        first = match_traffic_route(public, "GET", "/api/v1/jobs/job_one/original")
        assert first == match_traffic_route(public, "GET", "/api/v1/jobs/job_two/original")
        assert match_traffic_route(local, "GET", "/api/v1/jobs/job_one/original") is None
        assert match_traffic_route(local, "GET", "/traffic.html") is None
        assert not match_traffic_route(public, "POST", "/api/v1/accounts")["user_scoped"]
        assist = match_traffic_route(public, "POST", "/api/v1/strategy-versions/old/prompt-assist")
        assert default_rule(assist)["provider_concurrency"] > 0

        with TestClient(local):
            path = directory / "rules.json"
            original = path.read_bytes()
            snapshot = json.loads(original)
            assert len(snapshot["routes"]) == len(keys)
            assert all(
                set(row) == {"listener", "method", "path", "edge_rpm", "edge_burst"}
                for row in snapshot["routes"]
            )
            # 修复过程中重新查库，不会用请求线程缓存的旧配置反向覆盖较新的提交。
            path.unlink()
            assert publish_gateway_rules(local) == snapshot["revision"]
            assert path.read_bytes() == original
    finally:
        sessions.kw["bind"].dispose()
