"""用真实工作台验证提示词辅助的显式请求、预览和原文保护。"""

from pathlib import Path
from shutil import which
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

playwright = pytest.importorskip("playwright.sync_api")

ORIGIN = "http://mycomfy.test"
ASSIST_PATH = "/api/v1/strategy-versions/v2/prompt-assist"


@pytest.fixture(scope="module")
def browser():
    """为本模块启动 Chromium，每个用例另外创建隔离的页签环境。"""
    with playwright.sync_playwright() as engine:
        executable = which("chromium") or which("google-chrome") or engine.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("未安装 Chromium")
        with engine.chromium.launch(executable_path=executable) as opened:
            yield opened


@pytest.fixture
def studio(browser, request):
    """提供带两个策略版本的工作台，保留扩写请求以便模拟迟到的回答。"""
    assets = Path(__file__).resolve().parents[2] / "web" / "client"
    version = {
        "strategy_key": "shots",
        "strategy_version_id": "v2",
        "version": 2,
        "name": "分镜",
        "accepting": True,
        "asset_fields": ["image"],
        "prompt_assist": {"fields": ["prompt", "motion"]},
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "title": "主体"},
                "motion": {"type": "string", "title": "运镜"},
                "note": {"type": "string", "title": "备注"},
                "image": {"type": "string", "title": "参考图"},
                "seed": {"type": "integer", "title": "种子"},
            },
        },
    }
    legacy = {**version, "strategy_version_id": "v1", "version": 1, "prompt_assist": None}
    permissions = ["strategies.view"]
    if getattr(request, "param", None) != "read-only":
        permissions.append("jobs.submit")
    page = browser.new_page()
    asks, batches, version_reads = [], [], []

    def serve(route):
        """用受控响应连接会话、策略目录及提交接口，记录扩写和提交的真实请求。"""
        path = urlsplit(route.request.url).path
        endpoint = path.removeprefix("/api/v1/")
        if path == ASSIST_PATH:
            asks.append(route)
            return
        if endpoint == "batches":
            batches.append(route.request.post_data_json)
            route.fulfill(json={"batch_id": "batch1"})
        elif endpoint == "me":
            route.fulfill(
                json={
                    "user_id": "alice",
                    "username": "Alice",
                    "session_generation": "42",
                    "csrf_token": "test-csrf",
                    "permissions": permissions,
                    "groups": [],
                }
            )
        elif endpoint == "events/cursor":
            route.fulfill(json={"user_seq": "0"})
        elif endpoint == "events":
            route.fulfill(content_type="text/event-stream", body=": connected\n\n")
        elif endpoint == "strategy-versions/v2":
            version_reads.append(route.request.method)
            route.fulfill(json=version)
        elif endpoint in {"strategies", "strategies/shots/versions"}:
            items = [version] if endpoint == "strategies" else [version, legacy]
            route.fulfill(json={"items": items, "next_cursor": None})
        else:
            file = assets / (path.lstrip("/") or "index.html")
            if file.parent == assets and file.is_file():
                route.fulfill(path=file)
            else:
                route.fulfill(status=404)

    page.route(ORIGIN + "/**", serve)
    page.goto(ORIGIN + "/#/create")
    playwright.expect(page.get_by_label("主体", exact=True)).to_be_visible()
    # 等独立的历史版本请求结束，避免初始化重绘与用户输入抢焦点。
    playwright.expect(page.get_by_label("策略版本").locator("option")).to_have_count(2)
    try:
        yield SimpleNamespace(
            page=page,
            asks=asks,
            batches=batches,
            version=version,
            version_reads=version_reads,
            can_submit="jobs.submit" in permissions,
        )
    finally:
        page.context.close()


def ask_for_motion(studio, text):
    """显式点击运镜字段的扩写按钮，并检查请求仅携带已选择字段的原文。"""
    page = studio.page
    motion = page.get_by_label("运镜", exact=True)
    motion.fill(text)
    panel = page.locator(".cmp-field").filter(has=motion)
    with page.expect_request(ORIGIN + ASSIST_PATH) as sent:
        panel.get_by_role("button", name="扩写提示词").click()
    assert sent.value.method == "POST"
    assert sent.value.post_data_json == {"field": "motion", "text": text}
    headers = sent.value.all_headers()
    assert headers["x-page-user"] == "alice"
    assert headers["x-session-generation"] == "42"
    assert headers["x-csrf-token"] == "test-csrf"
    return studio.asks[-1]


@pytest.mark.parametrize("choice", ["keep", "edit", "restore"])
def test_expansion_requires_explicit_selection_and_submit(studio, choice):
    """预览和修改建议均不改动原文，只有采用或恢复后才改变手动提交的文本。"""
    page = studio.page
    source = "镜头缓缓移向窗外"
    page.get_by_label("主体", exact=True).fill("狐狸")
    page.get_by_label("备注", exact=True).fill("给剪辑的备注")
    playwright.expect(page.get_by_role("button", name="扩写提示词")).to_have_count(2)
    assert not studio.asks and not studio.batches
    ask_for_motion(studio, source).fulfill(json={"text": "穿过窗框，雨中的树渐渐清晰"})
    preview = page.get_by_label("扩写建议", exact=True)
    playwright.expect(preview).to_have_value("穿过窗框，雨中的树渐渐清晰")
    preview.fill("穿过窗框，在雨中停住")
    motion = page.get_by_label("运镜", exact=True)
    playwright.expect(motion).to_have_value(source)
    if choice == "keep":
        page.get_by_role("button", name="保留原文").click()
    else:
        page.get_by_role("button", name="采用建议").click()
        playwright.expect(motion).to_have_value("穿过窗框，在雨中停住")
        if choice == "restore":
            page.get_by_role("button", name="恢复原文").click()
    expected = "穿过窗框，在雨中停住" if choice == "edit" else source
    playwright.expect(motion).to_have_value(expected)
    assert len(studio.asks) == 1 and not studio.batches
    with page.expect_response(ORIGIN + "/api/v1/batches"):
        page.get_by_role("button", name="提交批次").click()
    assert studio.batches[0]["tasks"][0]["inputs"] == {
        "prompt": "狐狸",
        "motion": expected,
        "note": "给剪辑的备注",
    }


@pytest.mark.parametrize("outcome", ["timeout", "late-success"])
def test_inflight_expansion_retains_current_edit_across_navigation(studio, outcome):
    """等待回答时仍能编辑，迟到的建议或超时均不会覆盖新文本，切回页面还能提交。"""
    page = studio.page
    motion = page.get_by_label("运镜", exact=True)
    waiting = ask_for_motion(studio, "向前推")
    motion.fill("改为慢慢后撤")
    with page.expect_response(ORIGIN + ASSIST_PATH):
        if outcome == "timeout":
            waiting.fulfill(status=504, json={"error": {"code": "PROMPT_PROVIDER_TIMEOUT"}})
        else:
            waiting.fulfill(json={"text": "这是针对旧原文的建议"})
    if outcome == "timeout":
        playwright.expect(page.get_by_role("alert")).to_contain_text("扩写超时")
    else:
        playwright.expect(page.get_by_label("扩写建议")).to_have_value("这是针对旧原文的建议")
    playwright.expect(motion).to_have_value("改为慢慢后撤")
    nav = page.get_by_role("navigation", name="工作台导航")
    nav.get_by_role("link", name="账户设置").click()
    playwright.expect(page).to_have_url(ORIGIN + "/#/account")
    nav.get_by_role("link", name="创建批次").click()
    playwright.expect(motion).to_have_value("改为慢慢后撤")
    assert not studio.batches and len(studio.asks) == 1
    with page.expect_response(ORIGIN + "/api/v1/batches"):
        page.get_by_role("button", name="提交批次").click()
    assert studio.batches[0]["tasks"][0]["inputs"] == {"motion": "改为慢慢后撤"}


def test_leaving_pending_assist_restores_draft_without_replaying(studio):
    """等待中离开页面可中止扩写，返回后保留草稿并仅在再次点击时发送新请求。"""
    page = studio.page
    saved = "环绕水边的亭子"
    interrupted = ask_for_motion(studio, saved)
    playwright.expect(page.get_by_role("button", name="正在扩写…")).to_be_disabled()
    nav = page.get_by_role("navigation", name="工作台导航")
    nav.get_by_role("link", name="账户设置").click()
    playwright.expect(page).to_have_url(ORIGIN + "/#/account")
    nav.get_by_role("link", name="创建批次").click()
    motion = page.get_by_label("运镜", exact=True)
    playwright.expect(motion).to_have_value(saved)
    panel = page.locator(".cmp-field").filter(has=motion)
    playwright.expect(panel.get_by_role("button", name="扩写提示词")).to_be_enabled()
    assert len(studio.asks) == 1 and not studio.batches
    # 释放旧请求的拦截，新的页签视图只能展示自己随后显式请求的建议。
    interrupted.fulfill(json={"text": "上一页的回答"})
    ask_for_motion(studio, "改为俯拍").fulfill(json={"text": "从高空缓缓俯拍水面"})
    playwright.expect(page.get_by_label("扩写建议")).to_have_value("从高空缓缓俯拍水面")
    playwright.expect(motion).to_have_value("改为俯拍")
    assert len(studio.asks) == 2 and not studio.batches


@pytest.mark.parametrize("studio", ["read-only", "writer"], indirect=True)
def test_assistance_visibility_requires_version_and_permission(studio):
    """只读用户和未配置辅助的旧版本继续显示原有可编辑表单。"""
    page = studio.page
    buttons = page.get_by_role("button", name="扩写提示词")
    playwright.expect(buttons).to_have_count(2 if studio.can_submit else 0)
    page.get_by_label("策略版本").select_option("v1")
    playwright.expect(buttons).to_have_count(0)
    page.get_by_label("运镜", exact=True).fill("自己填写的镜头")
    assert not studio.asks and not studio.batches


def test_stale_assist_refresh_loads_current_fields_without_resubmitting(studio):
    """遇到刚调整过的字段设置时只重读当前版本，保留已填参数并允许提交。"""
    page = studio.page
    motions = page.get_by_label("运镜", exact=True)
    notes = page.get_by_label("备注", exact=True)
    notes.fill("现场音乐")
    studio.version["prompt_assist"] = {"fields": ["note"]}
    ask_for_motion(studio, "低机位跟拍").fulfill(
        status=422, json={"error": {"code": "PROMPT_ASSIST_FIELD_UNAVAILABLE"}}
    )
    refresh = page.get_by_role("button", name="刷新扩写设置")
    playwright.expect(refresh).to_be_visible()
    page.get_by_role("button", name="添加任务").click()
    notes.nth(1).fill("保留第二条任务")
    assert not studio.version_reads
    with page.expect_response(ORIGIN + "/api/v1/strategy-versions/v2"):
        refresh.click()
    buttons = page.get_by_role("button", name="扩写提示词")
    playwright.expect(buttons).to_have_count(2)
    note_fields = page.locator(".cmp-field").filter(has=notes)
    playwright.expect(note_fields.get_by_role("button")).to_have_text(["扩写提示词", "扩写提示词"])
    playwright.expect(motions.first).to_have_value("低机位跟拍")
    playwright.expect(notes.first).to_have_value("现场音乐")
    playwright.expect(notes.nth(1)).to_have_value("保留第二条任务")
    assert studio.version_reads == ["GET"] and len(studio.asks) == 1 and not studio.batches
    with page.expect_response(ORIGIN + "/api/v1/batches"):
        page.get_by_role("button", name="提交批次").click()
    assert [task["inputs"] for task in studio.batches[0]["tasks"]] == [
        {"motion": "低机位跟拍", "note": "现场音乐"},
        {"note": "保留第二条任务"},
    ]


def test_can_restore_original_when_assistance_is_disabled_during_navigation(studio):
    """采用扩写后管理员关闭辅助，切回创建页仍能恢复原文并手动提交。"""
    page = studio.page
    motion = page.get_by_label("运镜", exact=True)
    ask_for_motion(studio, "俯拍").fulfill(json={"text": "由湖面上空缓慢俯拍"})
    page.get_by_role("button", name="采用建议").click()
    playwright.expect(motion).to_have_value("由湖面上空缓慢俯拍")
    nav = page.get_by_role("navigation", name="工作台导航")
    nav.get_by_role("link", name="账户设置").click()
    playwright.expect(page).to_have_url(ORIGIN + "/#/account")
    studio.version["prompt_assist"] = None
    nav.get_by_role("link", name="创建批次").click()
    playwright.expect(motion).to_have_value("由湖面上空缓慢俯拍")
    playwright.expect(page.get_by_role("button", name="扩写提示词")).to_have_count(0)
    page.get_by_role("button", name="恢复原文").click()
    playwright.expect(motion).to_have_value("俯拍")
    assert len(studio.asks) == 1 and not studio.batches
    with page.expect_response(ORIGIN + "/api/v1/batches"):
        page.get_by_role("button", name="提交批次").click()
    assert studio.batches[0]["tasks"][0]["inputs"] == {"motion": "俯拍"}
