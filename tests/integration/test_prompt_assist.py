"""验证用户显式扩写单个字段的权限、数据边界及离线的服务商交互。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from mycomfy.api import prompt_assist
from mycomfy.api.app import create_app
from mycomfy.config import Settings
from mycomfy.database.models import Job, StrategySeries, StrategyVersion, UserGroupPermission
from mycomfy.database.schema import migrate
from mycomfy.database.session import db_now, make_sessions
from mycomfy.errors import ApiError

PROVIDER = SimpleNamespace(
    base_url="https://provider.invalid/v1", model="editor", api_key="test-secret"
)
ORIGIN = "https://workspace.example"
PATH = "/api/v1/strategy-versions/v1/prompt-assist"


def test_provider_receives_only_selected_draft():
    """捕获实际发出的兼容 API 请求，验证认证、端点、消息内容和候选文本。"""

    def reply(request):
        """核对服务端唯一一次请求中的凭据及字段。"""
        assert request.url == "https://provider.invalid/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert "Cookie" not in request.headers and "Origin" not in request.headers
        payload = json.loads(request.content)
        system, user = payload["messages"]
        assert payload["model"] == "editor" and not payload["stream"]
        assert system == {"role": "system", "content": "请保留镜头方向"}
        assert user["role"] == "user" and json.loads(user["content"]) == {
            "field": "shot",
            "text": "轻微向左",
        }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "  镜头缓缓向左。\n"}}]}
        )

    result = asyncio.run(
        prompt_assist.generate_text(
            PROVIDER, "请保留镜头方向", "shot", "轻微向左", transport=httpx.MockTransport(reply)
        )
    )
    assert result == "镜头缓缓向左。"


@pytest.mark.parametrize(
    "case", ["redirect", "failure", "oversized", "malformed", "missing", "truncated", "timeout"]
)
def test_provider_failure_is_bounded_and_redacted(case, caplog):
    """各种拒绝和损坏的响应均只返回固定错误，不跟随跳转或输出输入和密钥。"""
    calls = []

    def reply(request):
        """构造上游故障并记录请求次数，测试全过程不联网。"""
        calls.append(request.url)
        if case == "redirect":
            return httpx.Response(307, headers={"Location": "http://elsewhere.invalid/"})
        if case == "failure":
            return httpx.Response(401, text="test-secret PRIVATE-DRAFT")
        if case == "oversized":
            return httpx.Response(200, content=b" " * (prompt_assist.MAX_BODY_BYTES + 1))
        if case == "malformed":
            return httpx.Response(200, content=b"PRIVATE-DRAFT")
        if case == "missing":
            return httpx.Response(200, json={"choices": []})
        if case == "truncated":
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]},
            )
        raise httpx.ReadTimeout("test-secret PRIVATE-DRAFT", request=request)

    with pytest.raises(ApiError) as error:
        asyncio.run(
            prompt_assist.generate_text(
                PROVIDER, "RULE", "shot", "PRIVATE-DRAFT", transport=httpx.MockTransport(reply)
            )
        )
    assert error.value.status == (504 if case == "timeout" else 502)
    assert len(calls) == 1
    for private in ("PRIVATE-DRAFT", "test-secret"):
        assert private not in str(error.value) and private not in caplog.text


@pytest.fixture
def assist_backend(tmp_path):
    """创建两个已发布版本，第二版本多出一项可扩写的文本输入。"""
    url = f"sqlite:///{tmp_path}/app.sqlite"
    migrate(url)
    sessions = make_sessions(url)
    settings = Settings(database_url=url, data_root=tmp_path / "data", public_origin=ORIGIN)
    with sessions.begin() as db:
        db.add(StrategySeries(key="clip", latest_version=2))
        db.flush()
        for n, assist in [(1, {"fields": ["shot"], "instructions": "VERSION ONE"}), (2, None)]:
            properties = {"shot": {"type": "string"}}
            if n == 2:
                properties["new_shot"] = {"type": "string"}
            content = {
                "input_schema": {"type": "object", "properties": properties},
                "bindings": [],
            }
            if assist:
                content["prompt_assist"] = assist
            db.add(
                StrategyVersion(
                    id=f"v{n}",
                    strategy_key="clip",
                    version=n,
                    name="模板",
                    content=content,
                    content_digest=str(n) * 64,
                    published_at=db_now(db),
                    request_id=f"request{n}",
                )
            )
    try:
        yield settings, sessions
    finally:
        sessions.kw["bind"].dispose()


def login_headers(client):
    """注册测试账户并取得同源写请求所需的当前页凭据。"""
    origin = {"Origin": ORIGIN}
    credentials = {"username": "writer", "password": "fixture-password"}
    client.post("/api/v1/accounts", json=credentials, headers=origin).raise_for_status()
    session = (
        client.post("/api/v1/sessions", json=credentials, headers=origin).raise_for_status().json()
    )
    return {
        **origin,
        "X-CSRF-Token": session["csrf_token"],
        "X-Page-User": session["user_id"],
        "X-Session-Generation": str(session["session_generation"]),
    }


def test_public_assist_authorizes_version_and_does_not_submit(assist_backend, monkeypatch):
    """经实际会话预检和数据表验证字段选择、权限撤销、请求边界与纯建议语义。"""
    settings, sessions = assist_backend
    generate = AsyncMock(return_value="候选内容")
    monkeypatch.setattr(prompt_assist, "generate_text", generate)
    with TestClient(create_app(settings, sessions=sessions), base_url=ORIGIN) as client:
        draft = {"field": "shot", "text": "我的构想"}
        assert client.post(PATH, json=draft, headers={"Origin": ORIGIN}).status_code == 401
        headers = login_headers(client)
        for changed, status in [
            ({"Origin": "https://other.invalid"}, 403),
            ({"X-CSRF-Token": "wrong"}, 403),
            ({"X-Session-Generation": "0"}, 401),
        ]:
            assert (
                client.post(PATH, json=draft, headers={**headers, **changed}).status_code == status
            )
        assert (
            client.post(PATH, json={**draft, "field": "image"}, headers=headers).status_code == 422
        )
        assert (
            client.post(
                PATH.replace("/v1/prompt-assist", "/v2/prompt-assist"),
                json=draft,
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                PATH.replace("/v1/prompt-assist", "/absent/prompt-assist"),
                json=draft,
                headers=headers,
            ).status_code
            == 404
        )
        assert client.post(PATH, json={**draft, "text": "  "}, headers=headers).status_code == 422
        assert (
            client.post(PATH, json={**draft, "other": "unexpected"}, headers=headers).status_code
            == 422
        )
        assert (
            client.post(
                PATH,
                json={**draft, "text": "a" * (prompt_assist.MAX_BODY_BYTES + 1)},
                headers=headers,
            ).status_code
            == 413
        )
        assert client.post(PATH, json=draft, headers=headers).status_code == 503
        generate.assert_not_called()
        monkeypatch.setattr(prompt_assist, "active_prompt_provider", lambda db, settings: PROVIDER)
        result = client.post(PATH, json=draft, headers=headers)
        assert result.status_code == 200 and result.json() == {"text": "候选内容"}
        generate.assert_awaited_once_with(PROVIDER, "VERSION ONE", "shot", "我的构想")
        with sessions.begin() as db:
            assert db.scalar(select(func.count()).select_from(Job)) == 0
            db.execute(
                delete(UserGroupPermission).where(UserGroupPermission.permission == "jobs.submit")
            )
        assert client.post(PATH, json=draft, headers=headers).status_code == 403
        assert generate.await_count == 1


def test_historical_page_applies_current_series_settings(assist_backend, monkeypatch):
    """在同一个已打开的旧版页面上，立即采用后台新指令、排除新版专属字段，并响应停用。"""
    settings, sessions = assist_backend
    generate = AsyncMock(return_value="更新后的候选")
    monkeypatch.setattr(prompt_assist, "generate_text", generate)
    monkeypatch.setattr(prompt_assist, "active_prompt_provider", lambda db, settings: PROVIDER)
    admin_origin = "http://127.0.0.1:8001"
    settings_url = "/local/v1/strategy-versions/v2/prompt-assist-settings"
    admin_headers = {"Origin": admin_origin, "X-MyComfy-Admin": "templates"}
    with (
        TestClient(create_app(settings, sessions=sessions), base_url=ORIGIN) as public,
        TestClient(
            create_app(settings, sessions=sessions, local=True),
            base_url=admin_origin,
            client=("127.0.0.1", 45678),
        ) as admin,
    ):
        headers = login_headers(public)
        draft = {"field": "shot", "text": "缓缓转身"}
        # 从最新版本更新共享指令；此前载入 v1 的页面继续使用它自己的 URL。
        updated = {"instructions": "新的系列指令", "fields": ["new_shot", "shot"]}
        admin.put(
            settings_url, json={"prompt_assist": updated}, headers=admin_headers
        ).raise_for_status()
        public.post(PATH, json=draft, headers=headers).raise_for_status()
        generate.assert_awaited_once_with(PROVIDER, "新的系列指令", "shot", "缓缓转身")
        for field in ["new_shot", "absent"]:
            reject = public.post(PATH, json={**draft, "field": field}, headers=headers)
            assert (
                reject.status_code == 422
                and reject.json()["error"]["code"] == "PROMPT_ASSIST_FIELD_UNAVAILABLE"
            )

        # 显式关闭后，旧版本自带的默认配置也不能继续触发服务商调用。
        admin.put(
            settings_url, json={"prompt_assist": None}, headers=admin_headers
        ).raise_for_status()
        reject = public.post(PATH, json=draft, headers=headers)
        assert (
            reject.status_code == 422
            and reject.json()["error"]["code"] == "PROMPT_ASSIST_FIELD_UNAVAILABLE"
        )
        assert generate.await_count == 1
