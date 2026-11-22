"""用两个真实监听端口验证单进程 API 的物理访问边界和启停行为。"""

from contextlib import asynccontextmanager
from threading import Thread
from time import monotonic, sleep

import httpx
import pytest

from mycomfy.api import app as api
from mycomfy.api.server import api_server
from mycomfy.config import Settings
from mycomfy.database.models import Base
from mycomfy.database.session import make_sessions


def test_single_process_binds_public_and_local_to_different_ports(tmp_path, monkeypatch):
    """从公开端口伪造 Host 及转发头，验证本机读写路由仍仅在回环专用端口可达。"""
    token = tmp_path / "maintenance-token"
    token.write_text("fixture-local-token")
    token.chmod(0o600)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'serve.sqlite'}",
        data_root=tmp_path / "files",
        maintenance_token_file=token,
        poll_seconds=0.1,
    )
    factory = make_sessions(settings.database_url)
    Base.metadata.create_all(factory.kw["bind"])
    original = api.event_dispatcher
    lifecycle = []

    @asynccontextmanager
    async def counted(settings, sessions):
        """记录公开应用的真实分发器何时进入与退出。"""
        assert sessions is factory
        lifecycle.append("start")
        async with original(settings, sessions):
            yield
        lifecycle.append("stop")

    monkeypatch.setattr(api, "event_dispatcher", counted)
    try:
        with api_server(settings, "127.0.0.1", 0, 0, sessions=factory) as (server, sockets):
            public_port, local_port = [s.getsockname()[1] for s in sockets]
            assert public_port != local_port and sockets[1].getsockname()[0] == "127.0.0.1"
            thread = Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
            thread.start()
            try:
                deadline = monotonic() + 10
                while not server.started:
                    assert thread.is_alive() and monotonic() < deadline
                    sleep(0.01)
                assert lifecycle == ["start"]
                public = f"http://127.0.0.1:{public_port}"
                local = f"http://127.0.0.1:{local_port}"
                forged = {
                    "Host": f"127.0.0.1:{local_port}",
                    "X-Forwarded-Host": f"127.0.0.1:{local_port}",
                    "X-Forwarded-Port": str(local_port),
                    "X-Forwarded-For": "127.0.0.1",
                    "X-Forwarded-Proto": "http",
                    "Origin": local,
                    "Authorization": "Bearer fixture-local-token",
                }
                with httpx.Client(timeout=3, trust_env=False) as client:
                    monitor = "/local/v1/monitor/overview"
                    publish = "/local/v1/strategy-versions"
                    user_page = client.get(public + "/", headers=forged)
                    local_page = client.get(local + "/")
                    assert user_page.status_code == local_page.status_code == 200
                    assert user_page.content != local_page.content
                    assert client.get(public + "/index.html").content == user_page.content
                    assert client.get(local + "/index.html").content == local_page.content
                    assert user_page.headers["content-type"].startswith("text/html")
                    assert "javascript" in client.get(public + "/app.js").headers["content-type"]
                    assert (
                        "javascript" in client.get(public + "/access.mjs").headers["content-type"]
                    )
                    assert "javascript" in client.get(local + "/admin.js").headers["content-type"]
                    assert client.head(public + "/").headers["etag"] == user_page.headers["etag"]
                    assert (
                        client.get(
                            public + "/", headers={"If-None-Match": user_page.headers["etag"]}
                        ).status_code
                        == 304
                    )
                    assert client.get(public + "/admin.js", headers=forged).status_code == 404
                    assert client.get(local + "/app.js").status_code == 404
                    assert client.get(local + "/access.mjs").status_code == 404
                    assert client.get(local + "/README.md").status_code == 404
                    assert (
                        client.get(local + "/", headers={"Host": "elsewhere.example"}).status_code
                        == 403
                    )
                    assert client.get(public + monitor, headers=forged).status_code == 404
                    assert client.post(public + publish, headers=forged, json={}).status_code == 404
                    assert client.get(public + "/api/v1/me").status_code == 401
                    assert client.get(local + "/api/v1/me").status_code == 404
                    assert client.get(local + monitor).status_code == 200
                    assert (
                        client.get(local + monitor, headers={"Host": "other.invalid"}).status_code
                        == 403
                    )
                    assert client.post(local + publish, json={}).status_code == 403
                    assert client.post(local + publish, headers=forged, json={}).status_code == 422
                    # 回环维护页包含模板发布入口，并禁止其他页面把它嵌入框架。
                    template_page = client.get(local + "/templates.html").raise_for_status()
                    assert (
                        "frame-ancestors 'none'" in template_page.headers["Content-Security-Policy"]
                    )
                    for asset in ("templates.html", "templates.js", "workflow-graph.js"):
                        assert client.get(local + "/" + asset).status_code == 200
                        assert client.get(public + "/" + asset, headers=forged).status_code == 404
                    for kind in ("t2v", "i2v"):
                        asset = f"/presets/h3-{kind}.strategy.json"
                        assert client.get(public + asset, headers=forged).status_code == 404
                        template = client.get(local + asset).raise_for_status().json()
                        assert client.post(local + publish, json=template).status_code == 403
                        result = client.post(local + publish, headers=forged, json=template)
                        assert result.status_code == 201
                        assert result.json()["strategy_key"] == f"minimax-h3-{kind}"
                        assert result.json()["version"] == 1
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                assert not thread.is_alive()
        assert lifecycle == ["start", "stop"]
    finally:
        factory.kw["bind"].dispose()


def test_configuration_rejects_coincident_ports(tmp_path):
    """在接单前拒绝公开端口与本机端口使用同一个固定值。"""
    settings = Settings(database_url="unused", data_root=tmp_path)
    with (
        pytest.raises(ValueError, match="端口必须不同"),
        api_server(settings, "127.0.0.1", 8000, 8000),
    ):
        pytest.fail("端口冲突时不应启动")
