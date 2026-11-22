"""用真实 HTTP 入口和独立 MySQL 检验用户、维护与批次契约。"""

import re
import struct
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import event, func, select

from mycomfy.api import auth
from mycomfy.api.app import create_app
from mycomfy.api.auth import COOKIE
from mycomfy.api.permissions import Permission
from mycomfy.api.session_cache import KEY_PREFIX
from mycomfy.artifacts.storage import LocalStore
from mycomfy.config import Settings
from mycomfy.database.models import (
    Audit,
    Batch,
    EngineInstance,
    Incompatibility,
    Job,
    SourceEvent,
    User,
)
from mycomfy.errors import ApiError
from mycomfy.generation.jobs import BatchRequest, admit_batch
from mycomfy.generation.state import canonical_digest

pytestmark = pytest.mark.mysql
PUBLIC = "https://video.example"
LOCAL = "http://127.0.0.1:8001"
ORIGIN = {"Origin": PUBLIC}
MAINTENANCE = {"Authorization": "Bearer test-maintenance"}
TEMPLATE_PAGE = {"Origin": LOCAL, "X-MyComfy-Admin": "templates"}


@pytest.fixture
def clients(mysql_sessions, tmp_path, request):
    """以独立监听地址启动用户和本机应用，并注册离线兼容的引擎配置。"""
    redis_url = getattr(request, "param", None)
    if redis_url:
        request.getfixturevalue("redis_probe")
    secret = tmp_path / "maintenance-token"
    secret.write_text("test-maintenance")
    secret.chmod(0o600)
    settings = Settings(
        database_url="unused",
        data_root=tmp_path / "data",
        public_origin=PUBLIC,
        maintenance_token_file=secret,
        max_upload_bytes=2048,
        poll_seconds=0.1,
        redis_url=redis_url,
    )
    with mysql_sessions.begin() as db:
        db.add(EngineInstance(id="engine", generation=1, manifest={}, runtime={}, accepting=False))
    public = create_app(settings, sessions=mysql_sessions)
    local = create_app(settings, sessions=mysql_sessions, local=True)
    with (
        TestClient(public, base_url=PUBLIC) as user,
        TestClient(local, base_url=LOCAL, client=("127.0.0.1", 44444)) as maintenance,
    ):
        yield user, maintenance


def login(user, username, password="test-password", *, register=True):
    """返回当前页面必须保存的公开会话代次与写请求令牌。"""
    credentials = {"username": username, "password": password}
    if register:
        result = user.post("/api/v1/accounts", json=credentials, headers=ORIGIN)
        assert result.status_code == 201, result.text
    response = user.post("/api/v1/sessions", json=credentials, headers=ORIGIN)
    assert response.status_code == 200, response.text
    payload = response.json()
    return {
        **ORIGIN,
        "X-CSRF-Token": payload["csrf_token"],
        "X-Session-Generation": payload["session_generation"],
    }


def strategy():
    """构造一个接受文字和可选图片、生成一个必需视频的策略版本。"""
    return {
        "strategy_key": "clip",
        "name": "演示",
        "workflow": {
            "1": {"class_type": "TestSource", "inputs": {"text": "", "image": ""}},
            "2": {"class_type": "TestVideo", "inputs": {"filename_prefix": "video"}},
        },
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"text": {"type": "string"}, "image": {"type": "string"}},
            "required": ["text"],
        },
        "bindings": [
            {"input": "text", "node_id": "1", "node_input": "text", "kind": "value"},
            {"input": "image", "node_id": "1", "node_input": "image", "kind": "asset"},
        ],
        "requirements": {},
        "progress_mapping": [],
        "output_contract": [{"role": "final_video", "node_id": "2", "media_types": ["video/mp4"]}],
        "scheduling": {},
        "retry": {},
        "artifacts": {},
    }


def publish(local):
    """经维护入口发布策略，返回本次不可变版本的响应。"""
    return local.post("/local/v1/strategy-versions", json=strategy(), headers=MAINTENANCE)


def png_sample():
    """构造内容和 CRC 都有效的单像素 PNG，用来验证按真实媒体上传及 Range。"""

    def chunk(kind, data):
        """编码带长度和校验和的 PNG 数据块。"""
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff"))
        + chunk(b"IEND", b"")
    )


def test_session_replacement_and_password_reset(clients, mysql_sessions):
    """旧页即使共享新 Cookie 也无法退出新会话，改密和维护重置可撤销旧凭据。"""
    user, local = clients
    old = login(user, "Alice")
    old_cookie = user.cookies[COOKIE]
    current = login(user, "ALICE", register=False)
    new_cookie = user.cookies[COOKIE]
    assert current["X-Session-Generation"] != old["X-Session-Generation"]
    stale = user.delete("/api/v1/sessions/current", headers=old)
    assert stale.status_code == 401
    replay = user.delete(
        "/api/v1/sessions/current", headers={**old, "Cookie": f"{COOKIE}={old_cookie}"}
    )
    assert replay.status_code == 204
    assert "Set-Cookie" not in replay.headers and user.cookies[COOKIE] == new_cookie
    me = user.get("/api/v1/me").json()
    assert me["session_generation"] == current["X-Session-Generation"]
    assert (
        user.post(
            "/api/v1/me/password",
            headers=ORIGIN,
            json={"current_password": "test-password", "new_password": "new-password"},
        ).status_code
        == 401
    )
    changed = user.post(
        "/api/v1/me/password",
        headers=current,
        json={"current_password": "test-password", "new_password": "new-password"},
    )
    assert changed.status_code == 200 and user.cookies[COOKIE] != new_cookie
    reset = local.post(
        f"/local/v1/users/{me['user_id']}/password-reset",
        headers=MAINTENANCE,
        json={"new_password": "reset-password"},
    )
    assert reset.status_code == 204
    assert user.get("/api/v1/me").status_code == 401
    failed = user.post(
        "/api/v1/sessions", headers=ORIGIN, json={"username": "alice", "password": "new-password"}
    )
    assert failed.status_code == 401
    login(user, "alice", "reset-password", register=False)
    with mysql_sessions() as db:
        audit = db.get(Audit, reset.headers["X-Request-ID"])
        assert audit.actor == "local-maintenance" and audit.target == me["user_id"]


@pytest.fixture
def redis_probe(monkeypatch):
    """模拟 Redis 命中和删除超时，保留实际会话缓存代码。"""
    entries = {}
    client = Mock(spec=Redis)
    client.get.side_effect = entries.get
    client.set.side_effect = lambda key, value, *, ex: entries.__setitem__(key, value)
    client.delete.side_effect = lambda key: entries.pop(key, None)
    monkeypatch.setattr(Redis, "from_url", Mock(return_value=client))
    return client, entries


@pytest.mark.parametrize("clients", ["redis://localhost/0"], indirect=True)
def test_session_cache_precheck_and_read_repair(clients, redis_probe):
    """缓存命中只省去预检查库，撤销后仍由 MySQL 拒绝并回删旧键。"""
    user, _ = clients
    redis, entries = redis_probe
    old = login(user, "cache-user")
    old_cookie = user.cookies[COOKIE]
    old_key = KEY_PREFIX + auth.digest(old_cookie)
    body = {"client_batch_key": "cache-probe", "tasks": []}

    with patch.object(auth, "authenticate", wraps=auth.authenticate) as authenticate:
        # 空批次故意在业务校验时失败，以便只测到鉴权边界。
        assert user.post("/api/v1/batches", headers=old, json=body).status_code == 422
        assert authenticate.call_count == 2
        assert old_key in entries
        assert redis.set.call_args.kwargs["ex"] == 60
        assert old["X-CSRF-Token"] not in entries[old_key]
        authenticate.reset_mock()
        assert user.post("/api/v1/batches", headers=old, json=body).status_code == 422
        assert authenticate.call_count == 1
        authenticate.reset_mock()
        # 页签身份不同，即使缓存命中且代次和 CSRF 都匹配，也在预检阶段拒绝。
        wrong_page = {**old, "X-Page-User": "another-user"}
        assert user.post("/api/v1/batches", headers=wrong_page, json=body).status_code == 401
        assert authenticate.call_count == 0
        assert old_key in entries

    delete = redis.delete.side_effect
    redis.delete.side_effect = RedisConnectionError("simulated timeout after login")
    current = login(user, "cache-user", register=False)
    current_key = KEY_PREFIX + auth.digest(user.cookies[COOKIE])
    assert old_key in entries
    assert current_key not in entries
    redis.delete.side_effect = delete

    with patch.object(auth, "authenticate", wraps=auth.authenticate) as authenticate:
        # 旧缓存仍会命中，写事务必须可靠地拒绝旧 Cookie 并触发缓存修复。
        response = user.post(
            "/api/v1/batches", headers={**old, "Cookie": f"{COOKIE}={old_cookie}"}, json=body
        )
        assert response.status_code == 401
        assert authenticate.call_count == 1
        assert old_key not in entries

    assert user.post("/api/v1/batches", headers=current, json=body).status_code == 422
    assert current_key in entries
    # 旧页共享新 Cookie 时，代次错误不能误删当前会话对应的缓存键。
    assert user.post("/api/v1/batches", headers=old, json=body).status_code == 401
    assert current_key in entries


@pytest.mark.parametrize("clients", ["redis://localhost/0"], indirect=True)
def test_session_cache_unavailable_falls_back_to_mysql(clients, redis_probe):
    """Redis 访问失败时，写请求仍执行数据库预检和最终复核。"""
    user, _ = clients
    redis, _ = redis_probe
    redis.get.side_effect = RedisConnectionError("simulated outage")
    redis.set.side_effect = RedisConnectionError("simulated outage")
    headers = login(user, "cache-fallback")
    body = {"client_batch_key": "cache-fallback", "tasks": []}
    with patch.object(auth, "authenticate", wraps=auth.authenticate) as authenticate:
        assert user.post("/api/v1/batches", headers=headers, json=body).status_code == 422
        assert authenticate.call_count == 2


@pytest.mark.parametrize("clients", [None, "redis://localhost/0"], indirect=True)
def test_user_group_permissions_gate_the_same_actions_as_the_frontend(clients, mysql_sessions):
    """账户继承默认授权，可组合多个组，并在现有会话上立即执行权限变更。"""
    user, local = clients
    headers = login(user, "group-user")
    me = user.get("/api/v1/me").json()
    user_id = me["user_id"]
    membership = f"/local/v1/users/{user_id}/groups"
    assert me["groups"] == [{"key": "standard", "name": "标准用户"}]
    assert me["permissions"] == sorted(Permission)
    assert local.get("/local/v1/groups").status_code == 403
    assert local.put(membership, json={"group_keys": []}).status_code == 403
    assert user.get("/local/v1/groups", headers=MAINTENANCE).status_code == 404
    listed = local.get("/local/v1/users", headers=MAINTENANCE).json()["items"]
    assert {"user_id": user_id, "username": "group-user"} in listed

    for key, name, permissions in (
        ("viewer", "浏览组", ["jobs.view", "notifications.view"]),
        ("creator", "生成组", ["jobs.submit", "strategies.view", "assets.upload"]),
    ):
        group = {"key": key, "name": name, "permissions": permissions}
        assert local.post("/local/v1/groups", json=group, headers=MAINTENANCE).status_code == 201
    invalid = local.post(
        "/local/v1/groups",
        json={"key": "invalid", "name": "无效组", "permissions": ["users.manage"]},
        headers=MAINTENANCE,
    )
    assert invalid.status_code == 422

    version_id = publish(local).json()["strategy_version_id"]
    payload = {
        "client_batch_key": "group-permissions",
        "tasks": [
            {
                "client_task_key": "clip",
                "strategy_version_id": version_id,
                "inputs": {"text": "test"},
            }
        ],
    }
    response = local.put(membership, json={"group_keys": ["viewer"]}, headers=MAINTENANCE)
    assert response.status_code == 200
    assert user.get("/api/v1/me").json()["permissions"] == ["jobs.view", "notifications.view"]
    assert user.get("/api/v1/jobs").status_code == 200
    assert user.get("/api/v1/notifications").status_code == 200
    assert user.get("/api/v1/strategies").status_code == 403
    denied = user.post("/api/v1/batches", headers=headers, json=payload)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "PERMISSION_DENIED"
    assert (
        user.post(
            "/api/v1/assets", headers=headers, files={"file": ("sample.png", png_sample())}
        ).status_code
        == 403
    )
    with mysql_sessions() as db:
        assert db.scalar(select(func.count()).select_from(Batch)) == 0

    combined = local.put(
        membership, json={"group_keys": ["viewer", "creator"]}, headers=MAINTENANCE
    ).json()
    assert {group["key"] for group in combined["groups"]} == {"viewer", "creator"}
    assert combined["permissions"] == [
        "assets.upload",
        "jobs.submit",
        "jobs.view",
        "notifications.view",
        "strategies.view",
    ]
    admitted = user.post("/api/v1/batches", headers=headers, json=payload)
    assert admitted.status_code == 202
    job_id = admitted.json()["tasks"][0]["job_id"]
    assert user.get(f"/api/v1/jobs/{job_id}").status_code == 200
    assert user.post(f"/api/v1/jobs/{job_id}/cancel", headers=headers).status_code == 403
    upload = user.post(
        "/api/v1/assets", headers=headers, files={"file": ("sample.png", png_sample())}
    )
    assert upload.status_code == 201
    asset = upload.json()["asset_id"]
    assert user.get(f"/api/v1/assets/{asset}/content").status_code == 403

    updated = local.put(
        "/local/v1/groups/creator",
        json={"name": "暂停提交", "permissions": []},
        headers=MAINTENANCE,
    )
    assert updated.status_code == 200
    assert user.post("/api/v1/batches", headers=headers, json=payload).status_code == 403
    assert user.get("/api/v1/me").json()["permissions"] == ["jobs.view", "notifications.view"]
    missing = local.put(membership, json={"group_keys": ["missing"]}, headers=MAINTENANCE)
    assert missing.status_code == 422
    assert user.get("/api/v1/jobs").status_code == 200
    assert local.put(membership, json={"group_keys": []}, headers=MAINTENANCE).status_code == 200
    assert user.get("/api/v1/events/cursor").status_code == 403
    assert user.get("/api/v1/notifications").status_code == 403
    assert user.get("/api/v1/me").json()["permissions"] == []


def test_batch_validation_reads_each_metadata_table_once(clients, mysql_sessions, tmp_path):
    """重复使用策略、默认素材及被拒环境时，元数据查询按表聚合，错误仍按任务定位。"""
    user, local = clients
    headers = login(user, "batch-reader")
    user_id = user.get("/api/v1/me").json()["user_id"]
    upload = user.post("/api/v1/assets", headers=headers, files={"file": ("ok.png", png_sample())})
    assert upload.status_code == 201, upload.text
    manifest = {"comfyui": "configured"}
    versions = []
    uploaded_id = upload.json()["asset_id"]
    for asset_id in (uploaded_id, "missing-asset"):
        config = strategy()
        config["requirements"] = manifest
        config["input_schema"]["properties"]["image"]["default"] = asset_id
        result = local.post("/local/v1/strategy-versions", json=config, headers=MAINTENANCE)
        assert result.status_code == 201, result.text
        versions.append(result.json()["strategy_version_id"])

    with mysql_sessions.begin() as db:
        # 切换部署时按下次启动清单判断；相同清单只拒收第二个策略版本。
        db.get(EngineInstance, "engine").runtime = {"configured_manifest": manifest}
        db.add(
            Incompatibility(
                strategy_version_id=versions[1],
                environment_digest=canonical_digest(manifest),
                reason="test",
            )
        )
    tasks = [
        {
            "client_task_key": str(i),
            "strategy_version_id": versions[i % 2],
            "inputs": {"text": "风景"},
        }
        for i in range(8)
    ]
    # 同一批内两种大小写都能按数据库主键排序规则匹配到同一版本和素材。
    tasks[2]["strategy_version_id"] = versions[0].upper()
    tasks[2]["inputs"]["image"] = uploaded_id.upper()
    request = BatchRequest.model_validate({"client_batch_key": "bulk-metadata", "tasks": tasks})
    counts = Counter()
    with mysql_sessions() as db:
        # 只统计本次受理使用的连接，排除后台服务的定时查询。
        @event.listens_for(db.connection(), "before_cursor_execute")
        def count_selects(conn, cursor, sql, params, context, executemany):
            """记录实际执行的 SELECT 涉及的表和次数。"""
            if sql.lstrip().upper().startswith("SELECT"):
                counts.update(set(re.findall(r"\b(?:from|join)\s+`?(\w+)", sql.lower())))

        store = LocalStore(tmp_path / "data")
        with (
            patch.object(store, "readable", wraps=store.readable) as readable,
            pytest.raises(ApiError) as caught,
        ):
            admit_batch(db, db.get(User, user_id), request, 20, store)
        readable.assert_called_once()

    for table in ("strategy_versions", "engine_instances", "incompatibilities", "assets"):
        assert counts[table] == 1, counts
    assert caught.value.status == 422
    assert [(e["index"], e["path"], e["code"]) for e in caught.value.details] == [
        (i, f"/tasks/{i}/{field}", code)
        for i in (1, 3, 5, 7)
        for field, code in (
            ("strategy_version_id", "NO_COMPATIBLE_DEPLOYMENT"),
            ("inputs/image", "ASSET_UNAVAILABLE"),
        )
    ]


def test_batch_replays_ownership_and_partial_errors(clients, mysql_sessions):
    """覆盖整批校验、同键并发、授权字节读取、可用性变化与重复取消。"""
    user, local = clients
    response = publish(local)
    assert response.status_code == 201, response.text
    version = response.json()["strategy_version_id"]
    headers = login(user, "alice")
    sample = png_sample()
    upload = user.post("/api/v1/assets", headers=headers, files={"file": ("file.bin", sample)})
    assert upload.status_code == 201, upload.text
    asset_id = upload.json()["asset_id"]
    url = f"/api/v1/assets/{asset_id}/content"
    partial = user.get(url, headers={"Range": "bytes=1-4"})
    assert partial.status_code == 206 and partial.content == sample[1:5]
    assert user.get(url, headers={"Range": "bytes=1000-"}).status_code == 416
    owner = user.get("/api/v1/me").json()
    binding = {"page_user": owner["user_id"], "session_generation": owner["session_generation"]}
    assert user.get(url, params=binding).status_code == 200
    assert user.get(url, params={**binding, "session_generation": "0"}).status_code == 401

    bob = login(user, "bob")
    # 两个账号首个会话的代次相同。旧页签不得在 Cookie 变化后取得新账号的快照或事件。
    assert bob["X-Session-Generation"] == owner["session_generation"]
    assert user.get("/api/v1/jobs", headers={"X-Page-User": owner["user_id"]}).status_code == 401
    assert user.get("/api/v1/events", params={**binding, "after": "0"}).status_code == 401
    assert user.get(url, params=binding).status_code == 401
    assert user.get(url, headers={"Range": "bytes=1-4"}).status_code == 404
    task = {"client_task_key": "1", "strategy_version_id": version, "inputs": {"text": "风景"}}
    bad = {
        "client_batch_key": "order",
        "tasks": [task, {**task, "client_task_key": "2", "inputs": {"image": asset_id}}],
    }
    invalid = user.post("/api/v1/batches", headers=bob, json=bad)
    assert invalid.status_code == 422, invalid.text
    assert {e["code"] for e in invalid.json()["error"]["details"]} == {
        "REQUIRED",
        "ASSET_UNAVAILABLE",
    }
    assert all(
        e["index"] == 1 and e["client_task_key"] == "2" for e in invalid.json()["error"]["details"]
    )
    with mysql_sessions() as db:
        assert db.scalar(select(func.count()).select_from(Batch)) == 0

    good = {**bad, "tasks": [task, {**task, "client_task_key": "2"}]}

    def send(_index):
        """两个并发请求使用同一用户、同一幂等键与相同请求内容。"""
        return user.post("/api/v1/batches", headers=bob, json=good)

    with ThreadPoolExecutor(2) as pool:
        first, second = pool.map(send, range(2))
    assert first.status_code == 202 and second.status_code == 202, (first.text, second.text)
    assert first.json() == second.json()
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]
    job_id = first.json()["tasks"][0]["job_id"]
    batch_id = first.json()["batch_id"]
    detail = user.get(f"/api/v1/batches/{batch_id}", params={"limit": 1}).json()
    assert detail["counts"] == {"QUEUED": 2} and detail["items"][0]["job_id"] == job_id
    assert user.get("/api/v1/jobs", params={"cursor": detail["next_cursor"]}).status_code == 400

    with mysql_sessions.begin() as db:
        db.get(EngineInstance, "engine").configured = False
    replay = user.post("/api/v1/batches", headers=bob, json=good)
    assert replay.status_code == 202 and replay.json() == first.json()
    conflict = user.post("/api/v1/batches", headers=bob, json={**good, "tasks": [task]})
    assert conflict.status_code == 409
    reject = user.post("/api/v1/batches", headers=bob, json={**good, "client_batch_key": "other"})
    assert reject.status_code == 422
    cancelled = user.post(f"/api/v1/jobs/{job_id}/cancel", headers=bob)
    assert cancelled.status_code == 200
    assert user.post(f"/api/v1/jobs/{job_id}/cancel", headers=bob).json() == cancelled.json()
    with mysql_sessions() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 2
        events = db.scalars(select(SourceEvent).where(SourceEvent.type == "job.cancelled")).all()
        assert len(events) == 1
    login(user, "alice", register=False)
    assert user.get(f"/api/v1/jobs/{job_id}").status_code == 404


def test_public_local_boundary_and_version_concurrency(clients):
    """验证回环 Host、Origin、维护凭据、公开隔离及并发版本分配。"""
    user, local = clients
    url = "/local/v1/strategy-versions"
    assert user.post(url, headers=MAINTENANCE, json=strategy()).status_code == 404
    assert local.post(url, json=strategy()).status_code == 403
    for headers in [{"Origin": PUBLIC}, {"Host": "elsewhere.example"}]:
        assert (
            local.post(url, headers={**MAINTENANCE, **headers}, json=strategy()).status_code == 403
        )
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(publish, [local, local]))
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    assert {r.json()["version"] for r in responses} == {1, 2}
    login(user, "reader")
    latest = user.get("/api/v1/strategies").json()["items"][0]
    assert latest["version"] == 2 and latest["asset_fields"] == ["image"]
    history = user.get("/api/v1/strategies/clip/versions?limit=1").json()
    tail = user.get(
        "/api/v1/strategies/clip/versions", params={"cursor": history["next_cursor"]}
    ).json()
    assert [v["version"] for v in tail["items"]] == [1]


@pytest.fixture
def original_job(clients):
    """用实际上传与批次接口记录含多行提示词、素材列表和默认参数的任务。"""
    user, local = clients
    headers = login(user, "original-owner")
    upload = user.post("/api/v1/assets", headers=headers, files={"file": ("图.png", png_sample())})
    asset = upload.raise_for_status().json()
    config = strategy()
    properties = config["input_schema"]["properties"]
    properties["text"]["title"] = "初版提示词"
    properties["image"] = {"type": "array", "items": {"type": "string"}}
    properties["seed"] = {"type": "integer", "default": 0}
    config["workflow"]["1"]["inputs"]["seed"] = 0
    config["bindings"].append(
        {"input": "seed", "node_id": "1", "node_input": "seed", "kind": "value"}
    )
    version = (
        local.post("/local/v1/strategy-versions", json=config, headers=MAINTENANCE)
        .raise_for_status()
        .json()
    )
    asset_id = asset["asset_id"]
    inputs = {"text": "  林间清晨\n逆光下的落叶  ", "image": [asset_id.upper(), asset_id]}
    first = {"client_task_key": "分镜一", "strategy_version_id": version["strategy_version_id"]}
    tasks = [
        {**first, "inputs": inputs},
        {**first, "client_task_key": "分镜二", "inputs": {"text": "纯文字"}},
    ]
    batch = (
        user.post(
            "/api/v1/batches", headers=headers, json={"client_batch_key": "镜头", "tasks": tasks}
        )
        .raise_for_status()
        .json()
    )
    return batch, version, {**inputs, "seed": 0}, asset


def test_job_original_preserves_admitted_inputs_and_strategy(clients, original_job):
    """发布新版后仍按任务原版显示标题、输入和素材，普通任务接口不附带原始输入。"""
    user, local = clients
    batch, version, inputs, asset = original_job
    latest = local.post(
        "/local/v1/strategy-versions", json={**strategy(), "name": "新版本"}, headers=MAINTENANCE
    )
    assert latest.raise_for_status().json()["version"] == 2
    job_id = batch["tasks"][0]["job_id"]
    url = f"/api/v1/jobs/{job_id}"
    detail = user.get(url).raise_for_status().json()
    original = user.get(f"{url}/original").raise_for_status().json()
    assert original == {
        "job_id": job_id,
        "batch_id": batch["batch_id"],
        "client_task_key": "分镜一",
        "created_at": detail["created_at"],
        "strategy_version_id": version["strategy_version_id"],
        "strategy_key": "clip",
        "strategy_name": "演示",
        "strategy_version": 1,
        "inputs": inputs,
        "field_labels": {"text": "初版提示词", "image": "image", "seed": "seed"},
        "asset_fields": ["image"],
        "input_assets": {requested_id: asset for requested_id in inputs["image"]},
    }
    assert user.get(asset["content_url"]).content == png_sample()
    second_id = batch["tasks"][1]["job_id"]
    second = user.get(f"/api/v1/jobs/{second_id}/original").raise_for_status().json()
    assert second["inputs"] == {"text": "纯文字", "seed": 0}
    assert second["asset_fields"] == [] and second["input_assets"] == {}
    private_fields = {"inputs", "input_assets", "asset_fields", "field_labels"}
    assert private_fields.isdisjoint(detail)
    for listing in ("/api/v1/jobs", f"/api/v1/batches/{batch['batch_id']}"):
        rows = user.get(listing).raise_for_status().json()["items"]
        assert {row["job_id"] for row in rows} == {job_id, second_id}
        assert all(private_fields.isdisjoint(row) for row in rows)


def test_job_original_enforces_job_and_file_permissions(clients, original_job):
    """本人可按现有分组回看输入，文件元数据和内容另行鉴权，其他账户无法读取。"""
    user, local = clients
    batch, _, inputs, asset = original_job
    url = f"/api/v1/jobs/{batch['tasks'][0]['job_id']}/original"
    owner = user.get("/api/v1/me").raise_for_status().json()["user_id"]
    group = {"key": "jobs-only", "name": "任务查阅", "permissions": ["jobs.view"]}
    local.post("/local/v1/groups", json=group, headers=MAINTENANCE).raise_for_status()
    membership = f"/local/v1/users/{owner}/groups"
    local.put(
        membership, json={"group_keys": [group["key"]]}, headers=MAINTENANCE
    ).raise_for_status()
    limited = user.get(url).raise_for_status().json()
    assert limited["inputs"] == inputs and limited["asset_fields"] == ["image"]
    assert limited["input_assets"] == {}
    assert user.get(asset["content_url"]).status_code == 403

    local.put(
        f"/local/v1/groups/{group['key']}",
        json={"name": "文件下载", "permissions": ["assets.download"]},
        headers=MAINTENANCE,
    ).raise_for_status()
    denied = user.get(url)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "PERMISSION_DENIED"
    login(user, "other-viewer")
    assert user.get(url).status_code == 404


def test_template_page_authorization_is_scoped_to_local_publication(
    clients, mysql_sessions, tmp_path
):
    """同源模板页可在没有凭据文件的环境中发布，其他维护写入仍需独立授权。"""
    public, local = clients
    url = "/local/v1/strategy-versions"
    # 缺少任一浏览器条件或显式提供无效凭据，都不能获得模板写权限。
    for headers in (
        {"Origin": LOCAL},
        {"X-MyComfy-Admin": "templates"},
        {**TEMPLATE_PAGE, "Origin": PUBLIC},
        {**TEMPLATE_PAGE, "X-MyComfy-Admin": "groups"},
        {**TEMPLATE_PAGE, "Sec-Fetch-Site": "same-site"},
        {**TEMPLATE_PAGE, "Sec-Fetch-Site": "cross-site"},
        {**TEMPLATE_PAGE, "Authorization": "Bearer invalid"},
    ):
        assert local.post(url, headers=headers, json=strategy()).status_code == 403
    for content_type in ("text/plain", "application/x-www-form-urlencoded"):
        headers = {**TEMPLATE_PAGE, "Content-Type": content_type}
        assert local.post(url, headers=headers, json=strategy()).status_code in (403, 415)
    assert local.post(url, headers=TEMPLATE_PAGE, content=b"{}").status_code in (403, 415)
    assert local.post("/local/v1/groups", headers=TEMPLATE_PAGE, json={}).status_code == 403
    assert public.post(url, headers=TEMPLATE_PAGE, json=strategy()).status_code == 404
    assert public.get("/local/v1/strategies").status_code == 404

    # 删除夹具的临时令牌，验证合法同源浏览器请求独立于文件的存在。
    (tmp_path / "maintenance-token").unlink()
    result = local.post(
        url, headers={**TEMPLATE_PAGE, "Sec-Fetch-Site": "same-origin"}, json=strategy()
    )
    assert result.status_code == 201, result.text
    with mysql_sessions() as db:
        audit = db.get(Audit, result.headers["X-Request-ID"])
        assert audit.actor == "local-template-admin"
        assert audit.target == result.json()["strategy_version_id"]


def test_create_only_template_concurrency_and_local_catalog(clients):
    """并发新建同一标识仅产生 v1，后续版本能在本机列表分页中正确呈现。"""
    _, local = clients
    versions = "/local/v1/strategy-versions"
    catalog = "/local/v1/strategies"
    start = Barrier(2)

    def create(_):
        """同时发送两份相同的模板创建请求，以观察唯一标识冲突。"""
        start.wait(timeout=10)
        return local.post(
            versions, params={"create_only": True}, headers=TEMPLATE_PAGE, json=strategy()
        )

    with ThreadPoolExecutor(2) as pool:
        responses = sorted(pool.map(create, range(2)), key=lambda r: r.status_code)
    created, conflict = responses
    assert created.status_code == 201 and created.json()["version"] == 1
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "STRATEGY_ALREADY_EXISTS"

    # 先建立另一系列，再发布同一系列的 v2，验证最新模板排在首页。
    second = local.post(
        versions, json={**strategy(), "strategy_key": "clip-b"}, headers=MAINTENANCE
    )
    second.raise_for_status()
    latest = local.post(versions, json={**strategy(), "name": "更新版"}, headers=TEMPLATE_PAGE)
    latest.raise_for_status()
    assert latest.json()["version"] == 2
    first_page = local.get(catalog, params={"limit": 1}).raise_for_status().json()
    assert first_page["items"] == [
        {
            "strategy_key": "clip",
            "strategy_version_id": latest.json()["strategy_version_id"],
            "name": "更新版",
            "version": 2,
        }
    ]
    next_page = (
        local.get(catalog, params={"cursor": first_page["next_cursor"]}).raise_for_status().json()
    )
    assert [v["strategy_key"] for v in next_page["items"]] == ["clip-b"]
    assert next_page["next_cursor"] is None
    assert local.get(catalog, params={"cursor": "invalid"}).status_code == 400


def test_content_detects_damage_records_alert_and_recovers_same_asset(
    clients, mysql_sessions, tmp_path
):
    """让正式文件发生同长度损坏，在用户读取时核对可用性，并验证备份恢复后的 Range。"""
    from mycomfy.artifacts.storage import LocalStore
    from mycomfy.database.models import Asset

    user, local = clients
    headers = login(user, "owner")
    data = png_sample()
    uploaded = user.post("/api/v1/assets", files={"file": ("photo", data)}, headers=headers)
    assert uploaded.status_code == 201, uploaded.text
    asset_id = uploaded.json()["asset_id"]
    with mysql_sessions() as db:
        target = LocalStore(tmp_path / "data").path(db.get(Asset, asset_id).object_key)
    # 文件大小未变化，必须用摘要识别损坏。
    target.write_bytes(b"x" * len(data))
    endpoint = f"/api/v1/assets/{asset_id}/content"
    failed = user.get(endpoint, headers={"Range": "bytes=0-1"})
    assert failed.status_code == 503 and failed.json()["error"]["code"] == "OBJECT_UNAVAILABLE"
    assert not user.get(f"/api/v1/assets/{asset_id}").json()["available"]
    monitor = local.get(f"/local/v1/monitor/files/{asset_id}")
    assert monitor.status_code == 200 and not monitor.json()["available"]
    overview = local.get("/local/v1/monitor/overview").json()
    assert overview["alerts"][f"asset/{asset_id}"] == "OBJECT_UNAVAILABLE"
    target.write_bytes(data)
    restored = user.get(endpoint, headers={"Range": "bytes=3-6"})
    assert restored.status_code == 206 and restored.content == data[3:7]
    assert user.get(f"/api/v1/assets/{asset_id}").json()["available"]
    assert f"asset/{asset_id}" not in local.get("/local/v1/monitor/overview").json()["alerts"]


@pytest.mark.parametrize("disconnect", [True, False], ids=["client-disconnect", "spool-io-error"])
def test_interrupted_multipart_releases_spool_and_capacity(
    clients, mysql_sessions, tmp_path, monkeypatch, disconnect
):
    """在已创建解包文件后中断输入，核对文件句柄、上传租约和登记记录。"""
    import anyio
    from starlette import formparsers
    from starlette.requests import Request

    from mycomfy.api.app import _upload
    from mycomfy.artifacts.storage import LocalStore
    from mycomfy.database.capacity_models import DiskReservation
    from mycomfy.database.models import Asset
    from mycomfy.errors import ApiError
    from mycomfy.ops.capacity import Capacity

    user, _ = clients
    auth_headers = login(user, "upload")
    boundary = "test-boundary"
    # 先送出完整文件头和部分字节以确保 parser 已持有临时文件。
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="sample"\r\n'
        "Content-Type: image/png\r\n\r\npartial"
    ).encode()
    chunks = iter([{"type": "http.request", "body": prefix, "more_body": True}])

    async def receive():
        """在一个合法文件片段后模拟客户端断开，或模拟底层输入卷 I/O 错误。"""
        message = next(chunks, None)
        if message is not None:
            return message
        if disconnect:
            return {"type": "http.disconnect"}
        raise OSError(28, "test volume I/O error")

    opened = []
    original = formparsers.SpooledTemporaryFile

    def record_spool(*args, **kwargs):
        """记录真实暂存句柄，以便断言异常路径确实关闭了它。"""
        spool = original(*args, **kwargs)
        opened.append(spool)
        return spool

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", record_spool)
    monkeypatch.setattr(formparsers.MultiPartParser, "spool_max_size", 1)
    headers = {
        **auth_headers,
        "Cookie": f"{COOKIE}={user.cookies[COOKIE]}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/assets",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        },
        receive,
    )
    settings = Settings(database_url="unused", data_root=tmp_path / "data")
    with pytest.raises(ApiError) as failure:
        anyio.run(
            _upload,
            request,
            mysql_sessions,
            LocalStore(settings.data_root),
            Capacity(settings),
            settings,
        )
    assert failure.value.status == (400 if disconnect else 503)
    assert opened and all(spool.closed for spool in opened)
    assert not list((settings.data_root / ".upload-leases").iterdir())
    with mysql_sessions() as db:
        assert db.scalar(select(func.count()).select_from(DiskReservation)) == 0
        assert db.scalar(select(func.count()).select_from(Asset)) == 0
