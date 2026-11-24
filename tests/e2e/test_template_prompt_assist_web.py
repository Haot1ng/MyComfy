"""在独立 SQLite 数据库和 Chromium 中核对模板辅助配置的创建和版本发布。"""

import json
from pathlib import Path
from shutil import which
from threading import Thread
from time import monotonic, sleep

import pytest

from mycomfy.api.server import api_server
from mycomfy.config import Settings
from mycomfy.database.schema import migrate


@pytest.fixture
def admin_origin(tmp_path):
    """提供隔离的管理端页面，所有浏览器请求经过正常的本机发布路由。"""
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'templates.db'}", data_root=tmp_path)
    migrate(settings.database_url)
    with api_server(settings, "127.0.0.1", 0, 0) as (server, sockets):
        thread = Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 10
            while not server.started:
                assert thread.is_alive() and monotonic() < deadline
                sleep(0.01)
            yield f"http://127.0.0.1:{sockets[1].getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)


def publish_template(page, origin, *, create=False):
    """点击正常发布控件，验证操作类型，并从不可变版本地址读取实际保存的正文。"""
    endpoint = origin + "/local/v1/strategy-versions"
    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.startswith(endpoint)
    ) as write:
        page.locator("#publish-template").click()
    response = write.value
    assert response.url == endpoint + ("?create_only=true" if create else "")
    assert response.status == 201, response.text()
    receipt = response.json()
    version = page.request.get(endpoint + "/" + receipt["strategy_version_id"])
    assert version.ok
    return receipt["version"], version.json()


def save_assist(page):
    """从独立保存按钮发起 PUT，同时核对页面维护标识及写后读取结果。"""
    with page.expect_response(
        lambda response: (
            response.request.method == "PUT" and response.url.endswith("/prompt-assist-settings")
        )
    ) as write:
        page.get_by_role("button", name="保存扩写设置").click()
    response = write.value
    assert response.status == 200, response.text()
    assert response.request.all_headers()["x-mycomfy-admin"] == "templates"
    assert response.request.all_headers()["if-match"].startswith('"')
    assert response.json() == response.request.post_data_json
    return response.url


def test_template_assist_import_live_save_and_create(admin_origin):
    """验证导入和新建的默认规则、已发布模板热更新、撤销修改及不可变版本的正文。"""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as engine:
        executable = which("chromium") or which("google-chrome") or engine.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("未安装 Chromium")
        with engine.chromium.launch(executable_path=executable) as browser:
            page = browser.new_page()
            page.goto(admin_origin + "/templates.html")
            expect = playwright.expect
            # 未配置的历史版本也必须能从界面启用，导入种子超出 JS 安全整数范围。
            source = page.request.get(admin_origin + "/presets/h3-i2v.strategy.json").json()
            source.pop("prompt_assist", None)
            source.update(strategy_key="imported-scenes", name="导入镜头")
            seed = 18446744073709551613
            sample = next(n for n in source["workflow"].values() if "noise_seed" in n["inputs"])
            sample["inputs"]["noise_seed"] = seed
            page.locator("#template-file").set_input_files(
                {
                    "name": "old.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(source).encode(),
                }
            )
            description = page.locator("#publish-description")
            expect(description).to_contain_text("imported-scenes")
            enabled = page.locator("#assist-enabled")
            instructions = page.get_by_label("给 AI 的编写要求")
            prompt = page.locator('#assist-fields input[value="prompt"]')
            publish = page.locator("#publish-template")
            expect(enabled).not_to_be_checked()
            enabled.check()
            instructions.fill("先描述主体，再描述镜头运动。")
            expect(publish).to_be_disabled()
            expect(page.locator("#assist-fields input")).to_have_count(1)
            prompt.check()
            version, saved = publish_template(page, admin_origin)
            assert version == 1 and saved["prompt_assist"] == {
                "instructions": "先描述主体，再描述镜头运动。",
                "fields": ["prompt"],
            }
            assert saved["workflow"] == source["workflow"]

            # 草稿在切换模板时保留。独立保存立即更新设置，不会改变已发布的图。
            published = page.get_by_role("button", name="导入镜头 imported-scenes · v1")
            builtin = page.get_by_role("button", name="MiniMax H3 图生视频 minimax-h3-i2v · 内置")
            save = page.get_by_role("button", name="保存扩写设置")
            published.click()
            expect(description).to_contain_text("imported-scenes")
            expect(prompt).to_be_checked()
            expect(instructions).to_have_value(saved["prompt_assist"]["instructions"])
            instructions.fill("人物动作优先。")
            expect(publish).to_be_disabled()
            builtin.click()
            expect(description).to_contain_text("minimax-h3-i2v")
            expect(save).not_to_be_visible()
            published.click()
            expect(page.locator("#assist-save-status")).to_contain_text("已恢复")
            expect(instructions).to_have_value("人物动作优先。")

            # 另一管理页面先保存；过期的写入必须保留两份文本，由用户核对之后再写。
            settings_path = published.get_attribute("data-url") + "/prompt-assist-settings"
            headers = {"Origin": admin_origin, "X-MyComfy-Admin": "templates"}
            latest = page.request.get(admin_origin + settings_path, headers=headers)
            assert latest.ok
            competing = page.request.put(
                admin_origin + settings_path,
                headers={**headers, "If-Match": latest.headers["etag"]},
                data={"prompt_assist": {"instructions": "另一管理员的修改", "fields": ["prompt"]}},
            )
            assert competing.ok
            with page.expect_response(
                lambda r: r.request.method == "PUT" and r.url.endswith(settings_path)
            ) as stale:
                save.click()
            assert stale.value.status == 412
            expect(page.locator("#assist-current")).to_contain_text("另一管理员的修改")
            expect(instructions).to_have_value("人物动作优先。")
            expect(save).to_be_disabled()
            page.get_by_role("button", name="已核对，保留我的修改").click()
            settings_url = save_assist(page)
            expect(page.locator("#assist-save-status")).to_contain_text("立即生效")
            assert (
                page.request.get(settings_url.removesuffix("/prompt-assist-settings")).json()
                == saved
            )
            assert (
                page.request.get(admin_origin + "/local/v1/strategies").json()["items"][0][
                    "version"
                ]
                == 1
            )
            published.click()
            expect(instructions).to_be_visible()
            expect(instructions).to_have_value("人物动作优先。")
            instructions.fill("  ")
            expect(save).to_be_disabled()
            page.get_by_role("button", name="撤销修改").click()
            expect(instructions).to_have_value("人物动作优先。")
            enabled.uncheck()
            assert save_assist(page) == settings_url
            expect(page.locator("#assist-save-status")).to_contain_text("立即生效")

            # 显式发布的是不可变原文，新版本仍服从该系列刚保存的关闭设置。
            version, republished = publish_template(page, admin_origin)
            assert version == 2 and republished == saved
            page.get_by_role("button", name="导入镜头 imported-scenes · v2").click()
            expect(page.locator("#assist-enabled")).to_be_visible()
            expect(enabled).not_to_be_checked()
            expect(instructions).not_to_be_visible()

            # 让上一个已发布版本的 GET 迟到，当前内置模板的输入仍须保留。
            delayed = []
            page.route("**/prompt-assist-settings", lambda route: delayed.append(route), times=1)
            with page.expect_request(lambda r: r.url.endswith("/prompt-assist-settings")):
                page.get_by_role("button", name="导入镜头 imported-scenes · v2").click()
            builtin.click()
            expect(description).to_contain_text("minimax-h3-i2v")
            enabled.check()
            expect(prompt).to_be_visible()
            instructions.fill("只在内置模板预览中修改。")
            with page.expect_response(lambda r: r.url.endswith("/prompt-assist-settings")) as old:
                delayed.pop().fulfill(json={"prompt_assist": None}, headers={"ETag": '"old"'})
            old.value.finished()
            expect(instructions).to_have_value("只在内置模板预览中修改。")
            expect(save).not_to_be_visible()

            # 从内置模板新建时，仍可先编辑版本默认值再创建并发布。
            page.get_by_role("button", name="新增模板").click()
            page.locator("#new-template-name").fill("新系列")
            page.locator("#new-template-key").fill("new-scenes")
            page.get_by_role("button", name="预览新模板").click()
            expect(description).to_contain_text("new-scenes")
            enabled.check()
            instructions.fill("描述两段连续运动。")
            prompt.check()
            version, saved = publish_template(page, admin_origin, create=True)
            assert version == 1 and saved["strategy_key"] == "new-scenes"
            assert saved["prompt_assist"] == {
                "instructions": "描述两段连续运动。",
                "fields": ["prompt"],
            }


def test_download_example_matches_display_and_imports(admin_origin, tmp_path):
    """通过打开 JSON 旁的示例复制和下载同一原文，再用下载的文件完成首次发布。"""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as engine:
        executable = which("chromium") or which("google-chrome") or engine.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("未安装 Chromium")
        with engine.chromium.launch(executable_path=executable) as browser:
            context = browser.new_context(viewport={"width": 420, "height": 800})
            context.grant_permissions(["clipboard-read", "clipboard-write"], origin=admin_origin)
            page = context.new_page()
            page.goto(admin_origin + "/templates.html")
            page.get_by_role("button", name="查看 JSON 示例").click()
            dialog = page.get_by_role("dialog", name="模板 JSON 示例")
            download = dialog.get_by_role("link", name="下载 JSON")
            playwright.expect(download).to_be_visible()
            displayed = dialog.get_by_role("textbox", name="示例 JSON 原文").input_value()
            dialog.get_by_role("button", name="复制 JSON").click()
            playwright.expect(dialog.get_by_role("status")).to_have_text("示例 JSON 已复制。")
            assert page.evaluate("navigator.clipboard.readText()") == displayed
            with page.expect_download() as delivery:
                download.click()
            filename = delivery.value.suggested_filename
            assert filename == "example-h3-text-to-video.strategy.json"
            file = tmp_path / filename
            delivery.value.save_as(file)
            assert file.read_bytes() == displayed.encode("utf-8")
            body = json.loads(displayed)
            assert body["strategy_key"] == "example-h3-text-to-video"
            assert "prompt_assist" not in body
            assert dialog.evaluate("el => el.getBoundingClientRect().width <= window.innerWidth")
            dialog.get_by_role("button", name="关闭").click()

            # 实际导入下载文件，通过服务端完整策略校验，不依赖选配的 AI 规则。
            page.get_by_label("打开 JSON 文件").set_input_files(file)
            playwright.expect(page.locator("#preview-source")).to_have_text(filename)
            playwright.expect(page.locator("#publish-template")).to_be_enabled()
            version, stored = publish_template(page, admin_origin)
            assert version == 1 and stored["strategy_key"] == body["strategy_key"]
            assert stored["workflow"] == body["workflow"]
            assert "prompt_assist" not in stored
