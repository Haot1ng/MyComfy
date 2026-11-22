"""验证隧道后端收到的 HTTP 请求与公开 HTTPS 页面之间的访问边界。"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from mycomfy.api.app import create_app
from mycomfy.config import Settings
from mycomfy.database.models import Audit, Base
from mycomfy.database.session import make_sessions

HOST = "test-node.example.ts.net:8443"
ORIGIN = f"https://{HOST}"
PUBLISH = "/local/v1/strategy-versions"
PROXY = {"Host": HOST, "Tailscale-Funnel-Request": "?1"}
PAGE = {**PROXY, "Origin": ORIGIN, "X-MyComfy-Admin": "templates"}
BEARER = {"Authorization": "Bearer fixture-maintenance"}


@pytest.fixture
def backend(tmp_path):
    """构造带有效本机维护令牌的临时数据库，隔离各项读写测试。"""
    token = tmp_path / "credential"
    token.write_text("fixture-maintenance")
    token.chmod(0o600)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.sqlite'}",
        data_root=tmp_path / "data",
        maintenance_token_file=token,
        public_origin="https://test-node.example.ts.net",
        admin_test_origin=ORIGIN,
    )
    factory = make_sessions(settings.database_url)
    Base.metadata.create_all(factory.kw["bind"])
    try:
        yield settings, factory
    finally:
        factory.kw["bind"].dispose()


def listener(settings, factory, *, local=True, peer="127.0.0.1"):
    """以明文 HTTP 连接真实应用路由，可独立指定直连对端和监听器。"""
    return TestClient(
        create_app(settings, sessions=factory, local=local),
        base_url=f"http://127.0.0.1:{8001 if local else 8000}",
        client=(peer, 45555),
    )


def test_anonymous_admin_browser_flow(backend):
    """访客能查看监控、预览并发布内置模板，数据库按测试身份记录发布。"""
    settings, factory = backend
    # 删除凭据文件，覆盖实际匿名测试部署；隧道会保留 Host 并标记公网流量。
    settings.maintenance_token_file.unlink()
    with listener(settings, factory) as admin:
        assert admin.get("/", headers=PROXY).status_code == 200
        assert admin.head("/workflow-graph.js", headers=PROXY).status_code == 200
        page = admin.get("/templates.html", headers=PROXY).raise_for_status()
        assert page.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
        assert admin.get("/local/v1/monitor/overview", headers=PROXY).status_code == 200
        preset = admin.get("/presets/h3-t2v.strategy.json", headers=PROXY).raise_for_status().json()
        published = admin.post(
            PUBLISH + "?create_only=true",
            headers={**PAGE, "Sec-Fetch-Site": "same-origin"},
            json=preset,
        )
        assert published.status_code == 201, published.text
        version = published.json()["strategy_version_id"]
        listed = admin.get("/local/v1/strategies", headers=PROXY).raise_for_status()
        assert listed.json()["items"][0]["strategy_version_id"] == version
        with factory() as db:
            audit = db.get(Audit, published.headers["X-Request-ID"])
            assert audit.actor == "test-template-admin" and audit.target == version


def test_tunnel_cannot_use_maintenance_or_other_routes(backend):
    """测试入口仅接受管理界面所需读写，即使调用方知道本机令牌也不能越权。"""
    with listener(*backend) as admin:
        for path in ("/local/v1/users", "/local/v1/groups", "/api/v1/me", "/app.js"):
            assert admin.get(path, headers=PROXY).status_code == 403
            assert admin.get(path, headers={**PROXY, **BEARER}).status_code == 403
        assert admin.get("/templates.html", headers={**PROXY, **BEARER}).status_code == 403
        for method, path in (
            ("POST", "/local/v1/users/user/password-reset"),
            ("POST", "/local/v1/groups"),
            ("DELETE", "/local/v1/groups/group"),
            ("POST", "/local/v1/monitor/overview"),
            ("PUT", PUBLISH),
            ("OPTIONS", PUBLISH),
        ):
            assert admin.request(method, path, headers=PAGE).status_code == 403
            assert admin.request(method, path, headers={**PAGE, **BEARER}).status_code == 403
        # 直连路径继续支持现有程序凭据，隧道请求不能用回环 Host 模拟它。
        assert admin.get("/local/v1/users", headers=BEARER).status_code == 200
        assert (
            admin.get(
                "/local/v1/users", headers={**BEARER, "Tailscale-Funnel-Request": "?1"}
            ).status_code
            == 403
        )


def test_browser_publication_requires_same_origin_json_and_page_header(backend):
    """外网模板发布要求明确的浏览器来源、专用请求头以及 JSON 载荷。"""
    with listener(*backend) as admin:
        for headers in (
            PROXY,
            {**PROXY, "X-MyComfy-Admin": "templates"},
            {**PROXY, "Origin": ORIGIN},
            {**PAGE, "Origin": "http://" + HOST},
            {**PAGE, "Origin": "https://test-node.example.ts.net"},
            {**PAGE, "X-MyComfy-Admin": "groups"},
            {**PAGE, "Sec-Fetch-Site": "same-site"},
            {**PAGE, "Sec-Fetch-Site": "cross-site"},
            {**PAGE, **BEARER},
        ):
            assert admin.post(PUBLISH, headers=headers, json={}).status_code == 403
        assert admin.post(PUBLISH, headers=PAGE, content=b"{}").status_code == 415
        assert (
            admin.post(PUBLISH, headers={**PAGE, "Content-Type": "text/plain"}).status_code == 415
        )


def test_tunnel_requires_enabled_exact_host_and_physical_loopback(backend):
    """物理对端、目标端口、原始 Host 和开关共同决定管理入口，转发头不能授权。"""
    settings, factory = backend
    forged = {
        "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Host": HOST,
        "X-Forwarded-Proto": "https",
    }
    with (
        listener(replace(settings, admin_test_origin=None), factory) as disabled,
        listener(settings, factory) as admin,
        listener(settings, factory, peer="192.0.2.18") as remote,
        listener(settings, factory, local=False) as public,
    ):
        assert disabled.get("/templates.html", headers={**PROXY, **forged}).status_code == 403
        for host in ("test-node.example.ts.net", "unrelated.example:8443", "127.0.0.1:8001"):
            headers = {**PROXY, **forged, "Host": host}
            assert admin.get("/templates.html", headers=headers).status_code == 403
        assert (
            admin.get("/", headers={**PROXY, "Origin": "https://other.example"}).status_code == 403
        )
        assert remote.get("/templates.html", headers={**PROXY, **forged}).status_code == 403
        assert public.get("/templates.html", headers={**PROXY, **forged}).status_code == 404
        assert public.post(PUBLISH, headers=PAGE, json={}).status_code == 404
