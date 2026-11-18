"""用真实 HTTP 入口和独立 MySQL 检验用户、维护与批次契约。"""

import struct
import zlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from mycomfy.api import create_app
from mycomfy.auth import COOKIE
from mycomfy.config import Settings
from mycomfy.models import Audit, Batch, EngineInstance, Job, SourceEvent

pytestmark = pytest.mark.mysql
PUBLIC = "https://video.example"
LOCAL = "http://127.0.0.1:8001"
ORIGIN = {"Origin": PUBLIC}
MAINTENANCE = {"Authorization": "Bearer test-maintenance"}


@pytest.fixture
def clients(mysql_sessions, tmp_path):
    """以独立监听地址启动用户和本机应用，并注册离线兼容的引擎配置。"""
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

    bob = login(user, "bob")
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
    assert user.get("/api/v1/strategies").json()["items"][0]["version"] == 2
    history = user.get("/api/v1/strategies/clip/versions?limit=1").json()
    tail = user.get(
        "/api/v1/strategies/clip/versions", params={"cursor": history["next_cursor"]}
    ).json()
    assert [v["version"] for v in tail["items"]] == [1]


def test_content_detects_damage_records_alert_and_recovers_same_asset(
    clients, mysql_sessions, tmp_path
):
    """让正式文件发生同长度损坏，在用户读取时核对可用性，并验证备份恢复后的 Range。"""
    from mycomfy.models import Asset
    from mycomfy.storage import LocalStore

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

    from mycomfy.api import _upload
    from mycomfy.capacity import Capacity
    from mycomfy.errors import ApiError
    from mycomfy.models import Asset
    from mycomfy.ops_models import DiskReservation
    from mycomfy.storage import LocalStore

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
