"""用 Chromium 和真实回环管理 API 验证服务商切换与写入式密钥编辑。"""

from pathlib import Path
from shutil import which
from threading import Thread
from time import monotonic, sleep

import pytest

from mycomfy.api.server import api_server
from mycomfy.config import Settings
from mycomfy.database.models import Base
from mycomfy.database.prompt_models import PromptProviderSelection
from mycomfy.database.session import make_sessions

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def provider_page(tmp_path):
    """提供临时数据库和独立后台端口，浏览器通过页面自动产生真实同源请求头。"""
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}", data_root=tmp_path)
    sessions = make_sessions(settings.database_url)
    db_engine = sessions.kw["bind"]
    Base.metadata.create_all(db_engine)
    with sessions.begin() as db:
        db.add(PromptProviderSelection(id=1))
    try:
        with playwright.sync_playwright() as driver:
            exe = which("chromium") or which("google-chrome") or driver.chromium.executable_path
            if not Path(exe).is_file():
                pytest.skip("未安装 Chromium")
            with (
                driver.chromium.launch(executable_path=exe) as browser,
                api_server(settings, "127.0.0.1", 0, 0, sessions=sessions) as (server, sockets),
            ):
                worker = Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
                worker.start()
                try:
                    deadline = monotonic() + 10
                    while not server.started:
                        assert worker.is_alive() and monotonic() < deadline
                        sleep(0.01)
                    port = sockets[1].getsockname()[1]
                    page = browser.new_page(viewport={"width": 420, "height": 850})
                    page.goto(f"http://127.0.0.1:{port}/")
                    page.get_by_role("link", name="提示词服务商").click()
                    playwright.expect(page.get_by_text("尚未添加服务商。")).to_be_visible()
                    yield page
                finally:
                    server.should_exit = True
                    worker.join(5)
    finally:
        db_engine.dispose()


def add_provider(page, name, key=""):
    """从新增窗口保存配置，等到列表出现对应行再继续下一次操作。"""
    page.get_by_role("button", name="添加服务商").click()
    page.get_by_label("服务商名称").fill(name)
    page.get_by_label("API 基址", exact=True).fill("https://provider.example/v1")
    page.get_by_label("模型标识").fill("model-one")
    page.get_by_label("API 密钥", exact=True).fill(key)
    page.get_by_role("button", name="保存", exact=True).click()
    row = page.get_by_role("row").filter(has=page.get_by_text(name, exact=True))
    playwright.expect(row).to_be_visible()
    return row


def test_admin_configure_switch_and_clear_provider_key(provider_page):
    """验证切换互斥、原密钥省略、跨基址限制、清除及停用，刷新后仍显示真实状态。"""
    page = provider_page
    secret = "test-only-provider-key"
    first = add_provider(page, "工作室 A", secret)
    second = add_provider(page, "工作室 <B>")
    playwright.expect(first.get_by_text("已配置", exact=True)).to_be_visible()
    playwright.expect(second.get_by_text("未配置", exact=True)).to_be_visible()
    first.get_by_role("button", name="启用 工作室 A").click()
    playwright.expect(first.get_by_text("已启用")).to_be_visible()
    second.get_by_role("button", name="启用 工作室 <B>").click()
    playwright.expect(first.get_by_text("未启用")).to_be_visible()
    playwright.expect(second.get_by_text("已启用")).to_be_visible()

    first.get_by_role("button", name="编辑 工作室 A").click()
    field = page.get_by_label("API 密钥", exact=True)
    playwright.expect(field).to_have_value("")
    assert field.get_attribute("type") == "password"
    page.get_by_label("模型标识").fill("model-two")
    with page.expect_request(lambda request: request.method == "PUT") as sent:
        page.get_by_role("button", name="保存", exact=True).click()
    assert "api_key" not in sent.value.post_data_json
    playwright.expect(first.get_by_text("model-two")).to_be_visible()
    playwright.expect(first.get_by_text("已配置", exact=True)).to_be_visible()

    # 更换地址绝不能把旧密钥送往新站点。管理员勾选清除后才可保存。
    first.get_by_role("button", name="编辑 工作室 A").click()
    page.get_by_label("API 基址", exact=True).fill("https://new-provider.example/v1")
    page.get_by_role("button", name="保存", exact=True).click()
    playwright.expect(
        page.get_by_text("更换 API 基址时，请填写新密钥或选择清除已保存的密钥。")
    ).to_be_visible()
    page.get_by_label("清除已保存的密钥").check()
    with page.expect_request(lambda request: request.method == "PUT") as sent:
        page.get_by_role("button", name="保存", exact=True).click()
    assert sent.value.post_data_json["clear_api_key"] is True
    assert "api_key" not in sent.value.post_data_json
    playwright.expect(first.get_by_text("未配置", exact=True)).to_be_visible()
    second.get_by_role("button", name="停用 工作室 <B>").click()
    playwright.expect(second.get_by_text("未启用")).to_be_visible()
    page.reload()
    playwright.expect(first.get_by_text("https://new-provider.example/v1")).to_be_visible()
    playwright.expect(second.get_by_text("未启用")).to_be_visible()
    assert secret not in page.content()
    assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
    assert page.evaluate("document.documentElement.scrollWidth === window.innerWidth")


def test_provider_rotation_retry_hides_error_body(provider_page):
    """替换密钥时只显示固定错误说明，重试成功后清空输入框并更新地址。"""
    page = provider_page
    row = add_provider(page, "备用模型", "test-old-key")
    row.get_by_role("button", name="编辑 备用模型").click()
    page.get_by_label("API 基址", exact=True).fill("https://new.example/v1")
    field = page.get_by_label("API 密钥", exact=True)
    secret = "test-replacement-key"
    field.fill(secret)

    # 模拟包含敏感文本的非预期错误响应，保持表单可重试，禁止回显返回体。
    page.route(
        "**/local/v1/prompt-providers/*",
        lambda route: route.fulfill(status=502, json={"error": {"message": secret}}),
        times=1,
    )
    save = page.get_by_role("button", name="保存", exact=True)
    save.click()
    playwright.expect(page.get_by_text("管理接口响应异常（HTTP 502）。")).to_be_visible()
    assert secret not in page.locator("body").inner_text()
    with page.expect_response(lambda response: response.request.method == "PUT") as saved:
        save.click()
    assert saved.value.status == 200 and secret not in saved.value.text()
    playwright.expect(row.get_by_text("https://new.example/v1")).to_be_visible()
    playwright.expect(row.get_by_text("已配置", exact=True)).to_be_visible()
    assert field.input_value() == ""
