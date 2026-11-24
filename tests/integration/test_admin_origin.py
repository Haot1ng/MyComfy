"""验证管理应用的物理监听边界、HTTPS 来源与同源管理授权。"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from mycomfy.api.app import create_app
from mycomfy.config import Settings
from mycomfy.database.models import Audit, Base, User
from mycomfy.database.session import make_sessions
from mycomfy.generation.state import canonical_digest

HOST = "admin.example:8443"
ORIGIN = f"https://{HOST}"
PUBLISH = "/local/v1/strategy-versions"
PROXY = {"Host": HOST}
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
        public_origin="https://admin.example",
        admin_origin=ORIGIN,
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
    """访客能查看监控、预览并发布内置模板，数据库记录 HTTPS 管理入口的操作主体。"""
    settings, factory = backend
    # 覆盖未配置维护令牌的浏览器用法，HTTPS 入口按原始 Host 识别。
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
            assert audit.actor == "remote-template-admin" and audit.target == version


def test_admin_template_preview_returns_exact_published_version(backend):
    """打开同一模板的两个版本，验证完整策略与发布摘要一致，用户监听器无法读取。"""
    settings, factory = backend
    with listener(settings, factory) as admin, listener(settings, factory, local=False) as public:
        path = "/presets/h3-t2v.strategy.json"
        strategy = admin.get(path, headers=PROXY).raise_for_status().json()
        strategy["strategy_key"] = "imported-workflow"
        # 种子超出 JavaScript 安全整数范围，验证 API 保留可供界面直接使用的精确原文。
        seed = 2**53 + 1
        strategy["workflow"]["129"]["inputs"]["noise_seed"] = seed
        v1 = admin.post(PUBLISH, headers=PAGE, json=strategy).raise_for_status().json()
        v2 = (
            admin.post(PUBLISH, headers=PAGE, json={**strategy, "name": "第二版"})
            .raise_for_status()
            .json()
        )
        for published, name in ((v1, strategy["name"]), (v2, "第二版")):
            detail_url = f"{PUBLISH}/{published['strategy_version_id']}"
            response = admin.get(detail_url, headers=PROXY).raise_for_status()
            content = response.json()
            assert content["name"] == name
            assert content["workflow"]["129"]["inputs"]["noise_seed"] == seed
            assert str(seed) in response.text
            assert canonical_digest(content) == published["content_digest"]
            assert public.get(detail_url).status_code == 404
        assert admin.get(f"{PUBLISH}/unknown", headers=PROXY).status_code == 404


@pytest.mark.parametrize(
    ("entry", "origin", "actor"),
    [({}, "http://127.0.0.1:8001", "local-user-admin"), (PROXY, ORIGIN, "remote-user-admin")],
    ids=["direct", "https-origin"],
)
def test_same_user_management_and_maintenance_policy(backend, entry, origin, actor):
    """两个后台入口使用相同的用户管理、程序维护和未知路由规则。"""
    settings, factory = backend
    # 故意使用乱序 ID 和一个无组用户，核对续页不会漏掉或重复记录。
    with factory.begin() as db:
        db.add_all(
            User(id=id_, username=name, password_hash="sensitive-hash")
            for id_, name in [("u-c", "陈"), ("u-a", "安"), ("u-b", "白")]
        )
    user_list = "/local/v1/users"
    group_list = "/local/v1/groups"
    memberships = user_list + "/u-a/groups"
    reset = user_list + "/u-a/password-reset"
    read = {**entry, "X-MyComfy-Admin": "users", "Sec-Fetch-Site": "same-origin"}
    write = {**read, "Origin": origin}
    bearer = {**entry, **BEARER}

    with listener(settings, factory) as admin:
        for path in ("/api/v1/me", "/app.js", "/local/v1/unknown"):
            assert admin.get(path, headers=entry).status_code == 404
            assert admin.get(path, headers=bearer).status_code == 404
        for path in ("/", "/index.html", "/templates.html", "/users.html"):
            response = admin.get(path, headers=bearer).raise_for_status()
            assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
        # 维护凭据与浏览器页面身份权限不同，在两个后台地址上的核验一致。
        for path in (user_list, group_list, memberships):
            assert admin.get(path, headers=entry).status_code == 403
            assert (
                admin.get(path, headers={**read, "Authorization": "Bearer wrong"}).status_code
                == 403
            )
            assert admin.get(path, headers=bearer).status_code == 200
        for code in ("viewer", "editor", "empty"):
            perms = [] if code == "empty" else ["jobs.view"]
            body = {"key": code, "name": code, "permissions": perms}
            assert admin.post(group_list, headers=write, json=body).status_code == 403
            assert admin.post(group_list, headers=bearer, json=body).status_code == 201
        updated = {"name": "编辑", "permissions": ["jobs.view", "jobs.submit"]}
        assert admin.put(group_list + "/editor", headers=write, json=updated).status_code == 403
        assert admin.put(group_list + "/editor", headers=bearer, json=updated).status_code == 200
        assert len(admin.get(group_list, headers=read).json()["items"]) == 3
        password = {"new_password": "fixture-password"}
        assert admin.post(reset, headers=write, json=password).status_code == 403
        reset_result = admin.post(reset, headers=bearer, json=password)
        assert reset_result.status_code == 204
        with factory() as db:
            audit = db.get(Audit, reset_result.headers["X-Request-ID"])
            assert audit.actor == ("remote-maintenance" if entry else "local-maintenance")
            assert audit.target == "u-a"
        assert (
            admin.get(user_list, headers={**bearer, "Origin": "https://invalid.test"}).status_code
            == 403
        )

        desired = {"group_keys": ["viewer", "editor", "empty"]}
        expected = {
            "groups": [
                {"key": "editor", "name": "编辑"},
                {"key": "empty", "name": "empty"},
                {"key": "viewer", "name": "viewer"},
            ],
            "permissions": ["jobs.submit", "jobs.view"],
        }
        # GET 的浏览器可省 Origin，但跨站标记、缺少页面标识或重复来源均须拒绝。
        for bad in (
            {"X-MyComfy-Admin": "users"},
            {"Sec-Fetch-Site": "same-origin"},
            {"X-MyComfy-Admin": "users", "Origin": "https://invalid.test"},
            {**write, "Sec-Fetch-Site": "cross-site"},
        ):
            assert admin.get(user_list, headers={**entry, **bad}).status_code == 403
        assert (
            admin.get(
                user_list, headers={**entry, "Origin": origin, "X-MyComfy-Admin": "users"}
            ).status_code
            == 200
        )
        assert admin.put(memberships, headers=read, json=desired).status_code == 403
        assert (
            admin.put(
                memberships, headers={**write, "Sec-Fetch-Site": "same-site"}, json=desired
            ).status_code
            == 403
        )
        assert admin.put(memberships, headers=write, content=b"{}").status_code == 415
        assert (
            admin.put(
                memberships, headers={**write, "Content-Type": "text/plain"}, content=b"{}"
            ).status_code
            == 415
        )
        saved = admin.put(memberships, headers=write, json=desired)
        assert saved.status_code == 200 and saved.json() == expected
        assert admin.get(memberships, headers=read).json() == expected
        with factory() as db:
            audit = db.get(Audit, saved.headers["X-Request-ID"])
            assert audit.actor == actor and audit.target == "u-a"

        empty = {"groups": [], "permissions": []}
        first = admin.get(user_list, headers=read, params={"limit": 2}).json()
        assert first["items"] == [
            {"user_id": "u-a", "username": "安", **expected},
            {"user_id": "u-b", "username": "白", **empty},
        ]
        second = admin.get(
            user_list, headers=read, params={"limit": 2, "cursor": first["next_cursor"]}
        ).json()
        assert second == {
            "items": [{"user_id": "u-c", "username": "陈", **empty}],
            "next_cursor": None,
        }
        assert admin.get(user_list, headers=read, params={"cursor": "invalid"}).status_code == 400
        assert (
            admin.put(memberships, headers=write, json={"group_keys": ["missing"]}).status_code
            == 422
        )
        assert admin.get(memberships, headers=read).json() == expected
        assert admin.put(memberships, headers=write, json={"group_keys": []}).json() == empty


def test_browser_publication_requires_same_origin_json_and_page_header(backend):
    """外网模板发布要求明确的浏览器来源、专用请求头以及 JSON 载荷。"""
    with listener(*backend) as admin:
        for headers in (
            PROXY,
            {**PROXY, "X-MyComfy-Admin": "templates"},
            {**PROXY, "Origin": ORIGIN},
            {**PAGE, "Origin": "http://" + HOST},
            {**PAGE, "Origin": "https://admin.example"},
            {**PAGE, "X-MyComfy-Admin": "groups"},
            {**PAGE, "Sec-Fetch-Site": "same-site"},
            {**PAGE, "Sec-Fetch-Site": "cross-site"},
            {**PAGE, "Authorization": "Bearer incorrect"},
        ):
            assert admin.post(PUBLISH, headers=headers, json={}).status_code == 403
        assert admin.post(PUBLISH, headers=PAGE, content=b"{}").status_code == 415
        assert (
            admin.post(PUBLISH, headers={**PAGE, "Content-Type": "text/plain"}).status_code == 415
        )


def test_admin_requires_configured_host_and_physical_loopback(backend):
    """物理对端、目标端口、原始 Host 和开关共同决定管理入口，转发头不能授权。"""
    settings, factory = backend
    forged = {
        "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Host": HOST,
        "X-Forwarded-Proto": "https",
    }
    with (
        listener(replace(settings, admin_origin=None), factory) as disabled,
        listener(settings, factory) as admin,
        listener(settings, factory, peer="192.0.2.18") as remote,
        listener(settings, factory, local=False) as public,
    ):
        assert disabled.get("/templates.html", headers={**PROXY, **forged}).status_code == 403
        for host in ("admin.example", "unrelated.example:8443", "127.0.0.1:8001"):
            headers = {**PROXY, **forged, "Host": host}
            assert admin.get("/templates.html", headers=headers).status_code == 403
        assert (
            admin.get("/", headers={**PROXY, "Origin": "https://other.example"}).status_code == 403
        )
        assert remote.get("/templates.html", headers={**PROXY, **forged}).status_code == 403
        assert public.get("/templates.html", headers={**PROXY, **forged}).status_code == 404
        assert public.post(PUBLISH, headers=PAGE, json={}).status_code == 404


def test_proxy_metadata_cannot_select_local_administration(backend):
    """经代理访问须保留配置的管理 Host；来源头仅用于拒绝降级，不授予机器凭据。"""
    path = "/local/v1/groups"
    with listener(*backend) as admin:
        for name in (
            "Forwarded",
            "X-Forwarded-For",
            "X-Forwarded-Host",
            "X-Forwarded-Proto",
            "X-Forwarded-Port",
            "X-Real-IP",
        ):
            assert admin.get(path, headers={name: "for=unknown", **BEARER}).status_code == 403
        proxy = {**PROXY, "Forwarded": "for=unknown"}
        assert admin.get(path, headers=proxy).status_code == 403
        assert admin.get(path, headers={**proxy, **BEARER}).status_code == 200
        assert admin.get(path, headers=[("Host", HOST), ("Host", "localhost")]).status_code == 403
        for malformed in ("localhost?", "@localhost", "[::1]extra:8001"):
            assert admin.get(path, headers={"Host": malformed, **BEARER}).status_code == 403
