"""通过本机 HTTP 入口验证提示词服务商、凭据和并发切换。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mycomfy.api.app import create_app
from mycomfy.api.prompt_providers import active_prompt_provider
from mycomfy.config import Settings
from mycomfy.database.models import Audit
from mycomfy.database.prompt_models import PromptProvider
from mycomfy.database.schema import migrate
from mycomfy.database.session import make_sessions
from mycomfy.errors import ApiError
from mycomfy.ops.backup import create_backup, restore_backup

LOCAL = "http://127.0.0.1:8001"
ROUTE = "/local/v1/prompt-providers"
PAGE = {"X-MyComfy-Admin": "prompt-providers", "Origin": LOCAL}
SECRET = "provider-test-fixture-credential"
CONFIG = {"name": "自建模型", "base_url": "http://127.0.0.1:9002/v1/", "model": "test-model"}


@pytest.fixture
def provider_app(tmp_path):
    """用真实迁移创建一次 SQLite 服务，并允许无额外凭据的本机管理页面访问。"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'db.sqlite'}", data_root=tmp_path / "data"
    )
    migrate(settings.database_url)
    factory = make_sessions(settings.database_url)
    try:
        with TestClient(
            create_app(settings, sessions=factory, local=True),
            base_url=LOCAL,
            client=("127.0.0.1", 12345),
        ) as client:
            yield client, factory, settings
    finally:
        factory.kw["bind"].dispose()


def test_prompt_provider_edit_and_secrets(provider_app, caplog):
    """凭据仅服务端可解密；管理员可保留或明确替换、清除，换地址不会误发旧密钥。"""
    client, factory, settings = provider_app
    result = client.post(ROUTE, headers=PAGE, json={**CONFIG, "api_key": SECRET})
    assert result.status_code == 201, result.text
    record = result.json()
    detail = f"{ROUTE}/{record['id']}"
    assert record == {
        **CONFIG,
        "base_url": CONFIG["base_url"].rstrip("/"),
        "id": record["id"],
        "has_api_key": True,
        "active": False,
    }
    assert client.get(
        ROUTE, headers={"X-MyComfy-Admin": "prompt-providers", "Sec-Fetch-Site": "same-origin"}
    ).json() == {"items": [record]}
    with factory() as db:
        assert active_prompt_provider(db, settings) is None
        ciphertext = db.get(PromptProvider, record["id"]).encrypted_api_key
        assert SECRET not in ciphertext
    assert (settings.data_root / "prompt-providers.key").stat().st_mode & 0o777 == 0o600
    assert client.post(detail + "/activate", headers=PAGE, json={}).json()["active"]
    # 通常编辑和显式提交空密码都保留密钥，变更地址必须重新确认凭据。
    for patch in ({}, {"api_key": ""}):
        assert (
            client.put(detail, headers=PAGE, json={**CONFIG, "name": "已修改", **patch}).status_code
            == 200
        )
    changed = {**CONFIG, "base_url": "https://new.example/api"}
    refused = client.put(detail, headers=PAGE, json=changed)
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "PROVIDER_KEY_REQUIRED"
    with factory() as db:
        provider = active_prompt_provider(db, settings)
        assert provider.name == "已修改" and provider.api_key == SECRET
        assert provider.base_url == CONFIG["base_url"].rstrip("/") and SECRET not in repr(provider)
        assert db.get(PromptProvider, record["id"]).encrypted_api_key == ciphertext
    replacement = client.put(detail, headers=PAGE, json={**changed, "api_key": "rotated"})
    assert replacement.status_code == 200 and "rotated" not in replacement.text
    with factory() as db:
        assert active_prompt_provider(db, settings).api_key == "rotated"
    assert not client.put(detail, headers=PAGE, json={**CONFIG, "clear_api_key": True}).json()[
        "has_api_key"
    ]
    with factory() as db:
        assert active_prompt_provider(db, settings).api_key is None
        audits = db.scalars(select(Audit)).all()
        assert any(
            a.target == record["id"] and a.actor == "local-prompt-provider-admin" for a in audits
        )
        assert all(SECRET not in str(vars(a)) and "rotated" not in str(vars(a)) for a in audits)
    assert SECRET not in caplog.text and "rotated" not in caplog.text


def test_prompt_provider_origin_validation_and_key_storage_failure(provider_app):
    """列表和变更都受同源授权，错误响应不回显密钥；主密钥丢失时不会覆盖旧密文。"""
    client, factory, settings = provider_app
    assert client.get(ROUTE).status_code in {403, 503}
    assert (
        client.post(
            ROUTE, headers={**PAGE, "Origin": "http://untrusted.example"}, json=CONFIG
        ).status_code
        == 403
    )
    assert (
        client.post(
            ROUTE, headers={**PAGE, "Sec-Fetch-Site": "cross-site"}, json=CONFIG
        ).status_code
        == 403
    )
    for bad in (
        {"base_url": f"https://{SECRET}@host/v1"},
        {"base_url": f"https://host/v1?key={SECRET}"},
        {"api_key": SECRET + "\n"},
    ):
        response = client.post(ROUTE, headers=PAGE, json={**CONFIG, **bad})
        assert response.status_code == 422 and SECRET not in response.text
    record = client.post(ROUTE, headers=PAGE, json={**CONFIG, "api_key": SECRET}).json()
    assert client.post(f"{ROUTE}/{record['id']}/activate", headers=PAGE, json={}).status_code == 200
    keyfile = settings.data_root / "prompt-providers.key"
    original = keyfile.read_bytes()
    keyfile.unlink()
    with factory() as db, pytest.raises(ApiError) as missing:
        active_prompt_provider(db, settings)
    assert missing.value.code == "PROMPT_PROVIDER_SECRET_UNAVAILABLE"
    response = client.post(ROUTE, headers=PAGE, json={**CONFIG, "api_key": "new-secret"})
    assert response.status_code == 503 and not keyfile.exists()
    keyfile.write_bytes(original)
    keyfile.chmod(0o640)
    with factory() as db, pytest.raises(ApiError) as exposed:
        active_prompt_provider(db, settings)
    assert exposed.value.code == "PROMPT_PROVIDER_SECRET_UNAVAILABLE"
    keyfile.chmod(0o600)
    with factory() as db:
        assert active_prompt_provider(db, settings).api_key == SECRET


def test_backup_restores_active_provider_and_protected_key(provider_app, tmp_path):
    """用成对的数据库和数据目录恢复当前服务商，保留密钥权限并验证可以实际解密。"""
    client, factory, settings = provider_app
    provider = client.post(ROUTE, headers=PAGE, json={**CONFIG, "api_key": SECRET}).json()
    assert (
        client.post(f"{ROUTE}/{provider['id']}/activate", headers=PAGE, json={}).status_code == 200
    )
    backup = tmp_path / "backup"
    create_backup(factory.kw["bind"], backup, {"data": settings.data_root})
    assert SECRET not in (backup / "database" / "prompt_providers.jsonl").read_text()

    recovered = replace(
        settings,
        database_url=f"sqlite:///{tmp_path / 'target.sqlite'}",
        data_root=tmp_path / "restored" / "data",
    )
    migrate(recovered.database_url)
    target = make_sessions(recovered.database_url)
    try:
        restore_backup(target.kw["bind"], backup, recovered.data_root.parent)
        assert (recovered.data_root / "prompt-providers.key").stat().st_mode & 0o777 == 0o600
        with target() as db:
            restored = active_prompt_provider(db, recovered)
            assert restored.id == provider["id"] and restored.api_key == SECRET
    finally:
        target.kw["bind"].dispose()


@pytest.mark.mysql
def test_concurrent_prompt_provider_switches(mysql_sessions, tmp_path):
    """在 MySQL 行锁下并发激活不同服务商，始终只有一个当前选择，旧页面停用不影响它。"""
    settings = Settings(database_url="unused", data_root=tmp_path)
    app = create_app(settings, sessions=mysql_sessions, local=True)
    with TestClient(app, base_url=LOCAL, client=("127.0.0.1", 12345)) as client:
        records = [
            client.post(ROUTE, headers=PAGE, json={**CONFIG, "name": n}).json() for n in ("a", "b")
        ]
        barrier = Barrier(2)

        def activate(record):
            """同时进入切换接口，验证首次激活也通过同一行锁串行提交。"""
            barrier.wait(timeout=5)
            return client.post(
                f"{ROUTE}/{record['id']}/activate", headers=PAGE, json={}
            ).status_code

        with ThreadPoolExecutor(2) as pool:
            assert list(pool.map(activate, records)) == [200, 200]
        items = client.get(ROUTE, headers=PAGE).json()["items"]
        (selected,) = [item for item in items if item["active"]]
        (previous,) = [item for item in items if not item["active"]]
        assert (
            client.post(f"{ROUTE}/{previous['id']}/disable", headers=PAGE, json={}).status_code
            == 200
        )
        with mysql_sessions() as db:
            assert active_prompt_provider(db, settings).id == selected["id"]
        assert not client.post(f"{ROUTE}/{selected['id']}/disable", headers=PAGE, json={}).json()[
            "active"
        ]
        with mysql_sessions() as db:
            assert active_prompt_provider(db, settings) is None
