"""通过管理 API 验证独立于发布版本的模板扩写设置。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from mycomfy.api.app import create_app
from mycomfy.config import Settings
from mycomfy.database.models import StrategySeries, StrategyVersion
from mycomfy.database.prompt_models import TemplatePromptSettings
from mycomfy.database.schema import migrate
from mycomfy.database.session import make_sessions
from mycomfy.ops.backup import create_backup, restore_backup

LOCAL = "http://127.0.0.1:8001"
PAGE = {"X-MyComfy-Admin": "templates", "Origin": LOCAL}
VERSIONS = "/local/v1/strategy-versions"
DEFAULT = {"instructions": "已发布版本内的说明", "fields": ["prompt"]}


def publish(client, series_key, fields, *, inline=DEFAULT):
    """发布一个绑定若干可编辑字段的独立工作流，返回不可变版本信息和设置路径。"""
    raw = {
        "strategy_key": series_key,
        "name": "模板",
        "workflow": {
            "1": {"class_type": "Encoder", "inputs": dict.fromkeys(fields, "")},
            "2": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "video"}},
        },
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {field: {"type": "string"} for field in fields},
        },
        "bindings": [
            {"kind": "value", "input": field, "node_id": "1", "node_input": field}
            for field in fields
        ],
        "prompt_assist": inline,
        "requirements": {},
        "progress_mapping": [],
        "scheduling": {},
        "retry": {},
        "artifacts": {},
        "output_contract": [{"role": "video", "node_id": "2", "media_types": ["video/mp4"]}],
    }
    published = client.post(VERSIONS, json=raw, headers=PAGE)
    assert published.status_code == 201, published.text
    record = published.json()
    return record, f"{VERSIONS}/{record['strategy_version_id']}/prompt-assist-settings"


def test_changes_existing_series_without_publishing_and_restores_it(tmp_path):
    """直接改指令、筛选老版本字段、关闭并恢复备份，仍保留版本原始内容。"""
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    settings = Settings(database_url=url, data_root=tmp_path / "data")
    migrate(url)
    factory = make_sessions(url)
    try:
        with TestClient(
            create_app(settings, sessions=factory, local=True),
            base_url=LOCAL,
            client=("127.0.0.1", 12345),
        ) as client:
            first, old = publish(client, "camera", ["prompt"])
            first_etag = client.get(old, headers=PAGE).headers["etag"]
            newer_default = {"instructions": "新版本原有说明", "fields": ["prompt", "shot"]}
            second, latest = publish(client, "camera", ["prompt", "shot"], inline=newer_default)
            _, other = publish(client, "costume", ["prompt"])
            assert client.get(old, headers=PAGE).json() == {"prompt_assist": DEFAULT}
            assert client.get(old).status_code in {403, 503}
            config = {"instructions": "立即生效的新说明", "fields": ["prompt", "shot"]}
            body = {"prompt_assist": config}
            assert (
                client.put(
                    old, headers={**PAGE, "If-Match": first_etag}, json={"prompt_assist": DEFAULT}
                ).status_code
                == 412
            )
            # 尚无覆盖时也要保护新版内置的 shot 字段，不能通过旧页将它隐式删除。
            assert client.put(old, headers=PAGE, json={"prompt_assist": DEFAULT}).status_code == 409
            assert (
                client.put(
                    latest, headers={**PAGE, "Origin": "http://foreign.example"}, json=body
                ).status_code
                == 403
            )
            initial = client.get(latest, headers=PAGE).headers["etag"]
            updated = client.put(latest, headers={**PAGE, "If-Match": initial}, json=body)
            assert updated.status_code == 200 and updated.json() == body
            current = client.get(latest, headers=PAGE)
            assert current.json() == body
            assert current.headers["etag"] == updated.headers["etag"] != initial
            stale = client.put(
                latest, headers={**PAGE, "If-Match": initial}, json={"prompt_assist": None}
            )
            assert (
                stale.status_code == 412
                and stale.json()["error"]["code"] == "PROMPT_SETTINGS_CHANGED"
            )
            hidden = client.put(old, headers=PAGE, json={"prompt_assist": DEFAULT})
            assert (
                hidden.status_code == 409
                and hidden.json()["error"]["code"] == "PROMPT_SETTINGS_VERSION_CONFLICT"
            )
            assert client.get(old, headers=PAGE).json() == {
                "prompt_assist": {**config, "fields": ["prompt"]}
            }
            assert client.get(other, headers=PAGE).json() == {"prompt_assist": DEFAULT}
            # 当前版本没有 shot 时不能用它保存；结构错误也不能误关现有设置。
            for invalid in (body, {}, {"prompt_assist": None, "extra": True}):
                assert client.put(old, headers=PAGE, json=invalid).status_code == 422
            assert client.get(latest, headers=PAGE).json() == body
            for record, inline in ((first, DEFAULT), (second, newer_default)):
                with factory() as db:
                    row = db.get(StrategyVersion, record["strategy_version_id"])
                    assert row.content["prompt_assist"] == inline
                    assert row.content_digest == record["content_digest"]
                    assert db.get(StrategySeries, "camera").latest_version == 2
            disabled = {"prompt_assist": None}
            assert client.put(old, headers=PAGE, json=disabled).json() == disabled
            assert client.get(latest, headers=PAGE).json() == disabled
            _, future = publish(client, "camera", ["prompt", "shot"])
            assert client.get(future, headers=PAGE).json() == disabled
            assert client.get(other, headers=PAGE).json() == {"prompt_assist": DEFAULT}
            assert (
                client.get(f"{VERSIONS}/unknown/prompt-assist-settings", headers=PAGE).status_code
                == 404
            )
        # 备份会保留“明确关闭”的覆盖行和所属系列，即使指令值为 SQL NULL。
        target_url = f"sqlite:///{tmp_path / 'restored.sqlite'}"
        migrate(target_url)
        target = make_sessions(target_url)
        try:
            settings.data_root.mkdir(exist_ok=True)
            create_backup(factory.kw["bind"], tmp_path / "backup", {"data": settings.data_root})
            restore_backup(target.kw["bind"], tmp_path / "backup", tmp_path / "restored")
            with target() as db:
                assert db.get(TemplatePromptSettings, "camera").config is None
                assert db.get(TemplatePromptSettings, "costume") is None
        finally:
            target.kw["bind"].dispose()
    finally:
        factory.kw["bind"].dispose()


@pytest.mark.mysql
def test_concurrent_first_template_setting_upserts(mysql_sessions, tmp_path):
    """两个管理员在首次保存时共同锁住系列，只保留一行完整设置。"""
    settings = Settings(database_url="unused", data_root=tmp_path)
    app = create_app(settings, sessions=mysql_sessions, local=True)
    with TestClient(app, base_url=LOCAL, client=("127.0.0.1", 12345)) as client:
        _, route = publish(client, "camera", ["prompt"])
        barrier = Barrier(2)
        headers = PAGE

        def save(instructions):
            """在两个线程就绪后同时发出系列的首次设置更新。"""
            barrier.wait(timeout=5)
            return client.put(
                route,
                headers=headers,
                json={"prompt_assist": {"instructions": instructions, "fields": ["prompt"]}},
            ).status_code

        with ThreadPoolExecutor(2) as pool:
            assert list(pool.map(save, ["a", "b"])) == [200, 200]
            # 对同一标签发起两个条件写，只能有一个成功提交。
            headers = {**PAGE, "If-Match": client.get(route, headers=PAGE).headers["etag"]}
            assert sorted(pool.map(save, ["c", "d"])) == [200, 412]
        assert client.get(route, headers=PAGE).json()["prompt_assist"]["instructions"] in {"c", "d"}
        with mysql_sessions() as db:
            assert db.scalar(select(func.count()).select_from(TemplatePromptSettings)) == 1
            assert db.get(StrategySeries, "camera").latest_version == 1
