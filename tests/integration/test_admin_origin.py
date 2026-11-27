"""验证管理应用的物理监听边界、HTTPS 来源与同源管理授权。"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from mycomfy.api.app import create_app
from mycomfy.config import Settings
from mycomfy.database.models import Audit, Base, EngineInstance, EngineNodeCatalog, User
from mycomfy.database.session import db_now, make_sessions
from mycomfy.generation.state import canonical_digest

HOST = "admin.example:8443"
ORIGIN = f"https://{HOST}"
PUBLISH = "/local/v1/strategy-versions"
NODE_CHECK = "/local/v1/workflow-node-check"
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


@pytest.mark.parametrize("path", [PUBLISH, NODE_CHECK])
def test_browser_publication_requires_same_origin_json_and_page_header(backend, path):
    """外网模板预检与发布要求明确的浏览器来源、专用请求头以及 JSON 载荷。"""
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
            assert admin.post(path, headers=headers, json={}).status_code == 403
        assert admin.post(path, headers=PAGE, content=b"{}").status_code == 415
        assert admin.post(path, headers={**PAGE, "Content-Type": "text/plain"}).status_code == 415


def register_engine(db, instance_id, nodes=None, *, configured=True, catalog_generation=5):
    """登记测试引擎及其真实节点清单，可模拟升级后新代次尚未采集清单。"""
    db.add(
        EngineInstance(
            id=instance_id,
            generation=5,
            manifest={},
            configured=configured,
            heartbeat_at=db_now(db),
        )
    )
    db.flush()
    if nodes is not None:
        db.add(
            EngineNodeCatalog(instance_id=instance_id, generation=catalog_generation, nodes=nodes)
        )


def director_strategy():
    """构造由导演台规划、采样、官方节点写出视频组成的最小可发布策略。"""
    return {
        "strategy_key": "director",
        "name": "人物替换导演台",
        "workflow": {
            "7": {"class_type": "DirectorPlan", "inputs": {}},
            "40": {"class_type": "DirectorSample", "inputs": {"plan": ["7", 0]}},
            "10": {
                "class_type": "SaveVideo",
                "inputs": {"video": ["40", 0], "filename_prefix": "v"},
            },
        },
        "input_schema": {"type": "object", "properties": {}},
        "bindings": [],
        "requirements": {},
        "progress_mapping": [],
        "output_contract": [{"role": "main", "node_id": "10", "media_types": ["video/mp4"]}],
        "scheduling": {},
        "retry": {},
        "artifacts": {},
    }


def test_node_check_requires_one_engine_to_support_all_nodes(backend):
    """预检检查每台已配置引擎，发布会重验，不能拼凑两台机器的插件清单。"""
    settings, factory = backend
    plan = {"DirectorPlan": "custom_nodes.Director"}
    sampler = {"DirectorSample": "custom_nodes.Director"}
    base = {"SaveVideo": "comfy_extras.nodes_video"}
    strategy = director_strategy()
    with factory.begin() as db:
        register_engine(db, "gpu0", base | plan | sampler)
        register_engine(db, "gpu1", base | sampler)
        register_engine(db, "removed", base | plan | sampler, configured=False)

    with listener(settings, factory) as admin:
        preview = admin.post(NODE_CHECK, headers=PAGE, json=strategy["workflow"])
        assert preview.status_code == 200, preview.text
        report = preview.json()
        assert report["status"] == "ok"
        sources = {item["class_type"]: item["source"] for item in report["nodes"]}
        assert sources == {
            "DirectorPlan": "custom",
            "DirectorSample": "custom",
            "SaveVideo": "official",
        }

        # 预览后第一台引擎的版本变化。另一台有采样器，但缺导演台，停用的引擎不参与。
        with factory.begin() as db:
            db.get(EngineInstance, "gpu0").generation = 6
            catalog = db.get(EngineNodeCatalog, "gpu0")
            catalog.generation, catalog.nodes = 6, base | plan
        rejected = admin.post(PUBLISH, headers=PAGE, json=strategy)
        assert rejected.status_code == 422, rejected.text
        error = rejected.json()["error"]
        assert error["code"] == "WORKFLOW_NODES_MISSING"
        assert "DirectorPlan" in str(error["details"]) and "DirectorSample" in str(error["details"])
        assert admin.get("/local/v1/strategies", headers=PROXY).json()["items"] == []

        # 完整策略上传与直接 API 发布共用相同的代次检查结果。
        report = admin.post(NODE_CHECK, headers=PAGE, json=strategy).raise_for_status().json()
        assert report["status"] == "missing"
        assert {item["id"]: item["missing"] for item in report["instances"]} == {
            "gpu0": ["DirectorSample"],
            "gpu1": ["DirectorPlan"],
        }


def test_canvas_node_check_ignores_claimed_official_origin_when_class_is_absent(backend):
    """画布自称官方的节点也需匹配注册表，报错能定位同类节点的所有 ID。"""
    settings, factory = backend
    with factory.begin() as db:
        register_engine(db, "gpu0", {"SaveVideo": "comfy_extras.nodes_video"})
    nodes = [
        {"id": node_id, "type": "UnregisteredNode", "properties": {"cnr_id": "comfy-core"}}
        for node_id in (40, 42)
    ]
    with listener(settings, factory) as admin:
        report = (
            admin.post(NODE_CHECK, headers=PAGE, json={"nodes": nodes, "links": [], "version": 0.4})
            .raise_for_status()
            .json()
        )
        assert report["status"] == "missing"
        assert report["instances"][0]["missing"] == ["UnregisteredNode"]
        assert set(report["nodes"][0]["node_ids"]) == {"40", "42"}


@pytest.mark.parametrize("catalog_generation", [None, 4])
def test_unknown_node_catalog_allows_offline_publication(backend, catalog_generation):
    """离线预配和旧代次清单不能证明当前缺少节点，保留离线发布能力。"""
    settings, factory = backend
    base = {"SaveVideo": "comfy_extras.nodes_video"}
    with factory.begin() as db:
        register_engine(db, "known", base)
        if catalog_generation is None:
            register_engine(db, "upgrading")
        else:
            register_engine(db, "upgrading", base, catalog_generation=catalog_generation)
    with listener(settings, factory) as admin:
        strategy = director_strategy()
        report = admin.post(NODE_CHECK, headers=PAGE, json=strategy).raise_for_status().json()
        assert report["status"] == "unknown"
        assert {item["id"]: item["status"] for item in report["instances"]} == {
            "known": "missing",
            "upgrading": "unknown",
        }
        published = admin.post(PUBLISH, headers=PAGE, json=strategy)
        assert published.status_code == 201, published.text


def test_node_check_matches_pinned_and_pending_deployment(backend):
    """策略只检查满足版本要求的部署，切换期间保留未知直到新环境上报。"""
    settings, factory = backend
    base = {"SaveVideo": "comfy_extras.nodes_video"}
    installed = {**base, "DirectorPlan": "custom_nodes.director", "DirectorSample": None}
    strategy = director_strategy()
    strategy["requirements"] = {"comfyui": "pinned"}
    with factory.begin() as db:
        register_engine(db, "older", installed)
        register_engine(db, "pinned", base)
        db.get(EngineInstance, "pinned").manifest = {"comfyui": "pinned"}
    with listener(settings, factory) as admin:
        report = admin.post(NODE_CHECK, headers=PAGE, json=strategy).raise_for_status().json()
        assert report["status"] == "missing"
        assert [item["id"] for item in report["instances"]] == ["pinned"]
        # 另一台配置已切换，但其旧代次支持全部节点不能作为新环境就绪的依据。
        with factory.begin() as db:
            db.get(EngineInstance, "older").runtime = {"configured_manifest": {"comfyui": "pinned"}}
        report = admin.post(NODE_CHECK, headers=PAGE, json=strategy).raise_for_status().json()
        assert report["status"] == "unknown"
        assert {item["id"]: item["status"] for item in report["instances"]} == {
            "older": "unknown",
            "pinned": "missing",
        }


def test_pinned_package_absent_distinguishes_missing_class_and_version(backend):
    """版本要求排除全部实例时，所有现有引擎均缺节点才能得出缺少节点类型的结论。"""
    settings, factory = backend
    strategy = director_strategy()
    strategy["requirements"] = {"nodes": {"director": "new-revision"}}
    with factory.begin() as db:
        register_engine(db, "upgrading", {"SaveVideo": "nodes"})
        db.get(EngineInstance, "upgrading").manifest = {"nodes": {"director": "old-revision"}}
    with listener(settings, factory) as admin:
        report = admin.post(NODE_CHECK, headers=PAGE, json=strategy).raise_for_status().json()
        assert report["status"] == "missing"
        rejected = admin.post(PUBLISH, headers=PAGE, json=strategy)
        assert (
            rejected.status_code == 422
            and rejected.json()["error"]["code"] == "WORKFLOW_NODES_MISSING"
        )
        # 已加载所有类型，但安装的并非模板锁定版本，不能断言缺插件或伪报兼容。
        with factory.begin() as db:
            catalog = db.get(EngineNodeCatalog, "upgrading")
            catalog.nodes = {
                **catalog.nodes,
                "DirectorPlan": "custom_nodes.director",
                "DirectorSample": "custom_nodes.director",
            }
        report = admin.post(NODE_CHECK, headers=PAGE, json=strategy).raise_for_status().json()
        assert report["status"] == "unknown"
        assert admin.post(PUBLISH, headers=PAGE, json=strategy).status_code == 201


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
