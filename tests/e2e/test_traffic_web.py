"""用 Chromium 和独立管理端验证接口流量、两层限流规则和网关同步反馈。"""

from copy import deepcopy
from pathlib import Path
from shutil import which
from threading import Thread
from time import monotonic, sleep
from urllib.parse import parse_qs, urlsplit

import pytest

from mycomfy.api.server import api_server
from mycomfy.config import Settings
from mycomfy.database.schema import migrate

playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect
TRAFFIC = "/local/v1/traffic"
ASSIST = "/api/v1/strategy-versions/{version_id}/prompt-assist"


@pytest.fixture(scope="module")
def browser():
    """启动真实 Chromium，各测试独立创建页面和浏览器上下文。"""
    with playwright.sync_playwright() as driver:
        executable = which("chromium") or which("google-chrome") or driver.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("未安装 Chromium")
        with driver.chromium.launch(executable_path=executable) as opened:
            yield opened


@pytest.fixture
def site(browser, tmp_path):
    """启动临时数据库和本机双监听器，页面由正式的后台静态资源路由提供。"""
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'traffic.db'}", data_root=tmp_path)
    migrate(settings.database_url)
    with api_server(settings, "127.0.0.1", 0, 0) as (server, sockets):
        thread = Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 10
            while not server.started:
                assert thread.is_alive() and monotonic() < deadline
                sleep(0.01)
            origin = f"http://127.0.0.1:{sockets[1].getsockname()[1]}"
            with browser.new_context(viewport={"width": 420, "height": 850}) as context:
                page = context.new_page()
                page.goto(origin)
                yield page, f"http://127.0.0.1:{sockets[0].getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)


def open_traffic(page):
    """从侧栏打开流量页面，核对同源请求头并取得服务端目录快照。"""
    with page.expect_response(lambda r: urlsplit(r.url).path == TRAFFIC) as result:
        page.get_by_role("link", name="API 流量").click()
    assert result.value.status == 200, result.value.text()
    assert result.value.request.all_headers()["x-mycomfy-admin"] == "traffic"
    return result.value.json()


def submit_rule(page, button):
    """提交当前弹窗，验证正式维护接口的响应、写入方式和操作来源。"""
    with page.expect_response(lambda r: urlsplit(r.url).path == TRAFFIC + "/rules") as result:
        button.click()
    response = result.value
    assert response.status == 200, response.text()
    assert response.request.method == "PUT"
    assert response.request.all_headers()["x-mycomfy-admin"] == "traffic"
    return response.request.post_data_json


def test_edit_and_reset_traffic_rules(site):
    """从真实管理目录编辑两层规则，写入失败时保留输入，刷新后仍可恢复原默认值。"""
    page, public = site
    # 真实业务口产生一次 401，后台立即展示其请求量和 4xx 分类。
    assert page.request.get(public + "/api/v1/me").status == 401
    data = open_traffic(page)
    rows = page.locator("#traffic-rows tr")
    expect(rows).to_have_count(len(data["items"]))
    expect(page.locator("#edge-sync")).to_have_attribute("data-state", data["gateway"]["state"])
    assert {"public", "local"} == {item["listener"] for item in data["items"]}
    expect(page.get_by_role("link", name="API 流量")).to_have_attribute("aria-current", "page")
    page.locator("#listener").select_option("local")
    expect(rows).to_have_count(sum(item["listener"] == "local" for item in data["items"]))
    page.locator("#listener").select_option("public")
    me = next(
        row for row in data["items"] if row["path"] == "/api/v1/me" and row["method"] == "GET"
    )
    assert me["metrics"]["requests"] == me["metrics"]["errors_4xx"] == 1
    me_row = rows.filter(has=page.get_by_role("button", name="编辑规则 GET /api/v1/me", exact=True))
    expect(me_row.get_by_text("1 / 0", exact=True)).to_be_visible()
    item = next(item for item in data["items"] if item["path"] == ASSIST)
    assert item["user_scoped"] and item["provider_scoped"]
    page.locator("#route-search").fill(ASSIST)
    expect(rows).to_have_count(1)
    edit = rows.get_by_role("button", name="编辑规则 POST " + ASSIST)
    edit.click()
    dialog = page.locator("#rule-editor")
    modified = {
        "edge_rpm": 247,
        "edge_burst": 7,
        "user_rpm": 81,
        "provider_concurrency": 3,
    }
    for key, value in modified.items():
        field = dialog.locator("#" + key.replace("_", "-"))
        expect(field).to_have_value(str(item["defaults"][key]))
        field.fill(str(value))
    save = dialog.get_by_role("button", name="保存规则")
    # 模拟一次写入未获确认。原有设置和输入草稿都应保留，管理员可明确重试。
    page.route("**" + TRAFFIC + "/rules", lambda route: route.fulfill(status=503), times=1)
    with page.expect_response(lambda r: r.status == 503) as failed:
        save.click()
    assert failed.value.request.method == "PUT"
    expect(dialog).to_be_visible()
    expect(dialog.locator("#edge-rpm")).to_have_value("247")
    expect(dialog.get_by_role("status")).to_contain_text("503")
    sent = submit_rule(page, save)
    assert sent == {"listener": "public", "method": "POST", "path": ASSIST, "rule": modified}
    expect(dialog).not_to_be_visible()
    # 待写后自动刷新完成，再发起新的页面导航，以免拿到导航时被取消的上一笔 GET。
    expect(page.locator("#list-status")).to_contain_text("已保存规则")
    with page.expect_response(lambda r: urlsplit(r.url).path == TRAFFIC) as fresh:
        page.reload()
    updated = next(item for item in fresh.value.json()["items"] if item["path"] == ASSIST)
    assert updated["rule"] == modified and updated["overridden"]
    assert updated["defaults"] == item["defaults"]
    page.locator("#route-search").fill(ASSIST)
    edit.click()
    expect(dialog.locator("#edge-burst")).to_have_value("7")
    expect(dialog.locator("#edge-rpm-help")).to_contain_text(str(item["defaults"]["edge_rpm"]))
    reset = submit_rule(page, dialog.get_by_role("button", name="恢复默认"))
    assert reset == {**sent, "rule": None}
    expect(dialog).not_to_be_visible()
    expect(page.locator("#list-status")).to_contain_text("已恢复默认规则")
    edit.click()
    expect(dialog.locator("#edge-rpm")).to_have_value(str(item["defaults"]["edge_rpm"]))
    dialog.get_by_role("button", name="取消").click()

    # 匿名接口不能按用户或服务商限流，界面仅提供可执行的网关规则。
    page.locator("#route-search").fill("/api/v1/sessions")
    rows.get_by_role("button", name="编辑规则 POST /api/v1/sessions", exact=True).click()
    expect(dialog.locator("#edge-rpm")).to_be_visible()
    expect(dialog.locator("#user-rpm")).not_to_be_visible()
    expect(dialog.locator("#provider-concurrency")).not_to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth === window.innerWidth")


def test_window_metrics_and_edge_propagation(site):
    """变更统计窗口时重读数据，分别显示两层拒绝及已应用、待同步和不可用状态。"""
    page, _ = site
    windows = []
    baseline = open_traffic(page)

    def supply_metrics(route):
        """只替换环境相关的统计和网关状态，保留真实 API 目录与策略。"""
        body = deepcopy(baseline)
        window = int(parse_qs(urlsplit(route.request.url).query)["window_minutes"][0])
        body["window_minutes"] = window
        windows.append(window)
        gateway = {15: "applied", 60: "pending", 5: "unavailable"}[window]
        body["gateway"] = {
            "state": gateway,
            "desired_revision": "new-revision",
            "applied_revision": "new-revision" if gateway == "applied" else "older-revision",
            "updated_at": body["as_of"],
            "metrics_available": gateway != "unavailable",
        }
        body["metrics_source"] = "core" if gateway == "unavailable" else "gateway"
        row = next(item for item in body["items"] if item["path"] == ASSIST)
        row["metrics"].update(
            requests=window * 7,
            errors_4xx=9,
            errors_5xx=5,
            requests_per_minute=7,
            edge_rejections=11 if gateway != "unavailable" else None,
            core_rejections=3,
        )
        route.fulfill(json=body)

    page.route("**" + TRAFFIC + "?*", supply_metrics)
    page.get_by_role("button", name="刷新", exact=True).click()
    page.locator("#route-search").fill(ASSIST)
    row = page.locator("#traffic-rows tr")
    sync = page.locator("#edge-sync")
    expect(sync).to_have_attribute("data-state", "applied")
    expect(row.locator('[data-metric="requests"]')).to_contain_text("105")
    expect(row.locator('[data-metric="requests"] small')).to_have_text("7 次/分")
    expect(row.locator('[data-metric="edge_rejections"]')).to_have_text("11")
    expect(row.locator('[data-metric="core_rejections"]')).to_have_text("3")
    page.locator("#window-minutes").select_option("60")
    expect(sync).to_have_attribute("data-state", "pending")
    expect(row.locator('[data-metric="requests"]')).to_contain_text("420")
    page.locator("#window-minutes").select_option("5")
    expect(sync).to_have_attribute("data-state", "unavailable")
    expect(row.locator('[data-metric="core_rejections"]')).to_have_text("3")
    expect(row.locator('[data-metric="edge_rejections"]')).to_have_text("—")
    expect(page.locator("#metrics-note")).to_be_visible()
    assert windows == [15, 60, 5]


def test_auto_refresh_preserves_editing_and_follows_gateway(site):
    """用浏览器时钟核对 15 秒轮询，编辑、保存、后台页签与手动暂停时都应停读。"""
    page, _ = site
    page.clock.install(time="2026-01-02T00:00:00Z")
    page.clock.pause_at("2026-01-02T00:00:05Z")
    # 在浏览器调用 fetch 时同步计数，避免未发起请求的断言受到网络事件到达时序的影响。
    page.add_init_script("""
        window.trafficReads = 0;
        const fetch = window.fetch.bind(window);
        window.fetch = (url, options) => {
          if (new URL(url, location.href).pathname === '/local/v1/traffic') window.trafficReads++;
          return fetch(url, options);
        };
    """)
    body = open_traffic(page)
    row = next(item for item in body["items"] if item["path"] == ASSIST)
    edit = page.get_by_role("button", name="编辑规则 POST " + ASSIST)
    sync = page.locator("#edge-sync")
    auto = page.locator("#auto-refresh")
    expect(auto).to_be_checked()
    page.locator("#route-search").fill(ASSIST)
    edit.focus()

    def report_gateway(route):
        """按测试设置返回相同目录及新的网关回执，让浏览器自己决定何时发起读取。"""
        route.fulfill(json=body)

    page.route("**" + TRAFFIC + "?*", report_gateway)
    body["gateway"].update(state="pending", desired_revision="v2", applied_revision="v1")
    # 故意保持第一次读取未返回，核对 15 秒到期前不请求，到期后不锁表格、不播报加载中。
    first_read = []
    page.route("**" + TRAFFIC + "?*", lambda route: first_read.append(route), times=1)
    page.clock.fast_forward(14_000)
    assert page.evaluate("window.trafficReads") == 1
    with page.expect_request(lambda r: urlsplit(r.url).path == TRAFFIC):
        page.clock.fast_forward(1_000)
    expect(page.locator("#list-status")).to_have_text("")
    expect(page.get_by_role("button", name="刷新", exact=True)).to_be_enabled()
    expect(edit).to_be_focused()
    first_read.pop().fulfill(json=body)
    expect(sync).to_have_attribute("data-state", "pending")
    expect(edit).to_be_focused()
    assert page.evaluate("window.trafficReads") == 2

    # 时间推进多个刷新周期，仍应在同一弹窗里且保持未经提交的限额。
    edit.click()
    dialog = page.locator("#rule-editor")
    field = dialog.locator("#edge-rpm")
    field.fill("171")
    page.clock.fast_forward(45_000)
    expect(field).to_have_value("171")
    assert page.evaluate("window.trafficReads") == 2

    # 控制写入耗时：PUT 在途时不轮询，真实保存后先看到待应用，下一次自动读取取得回执。
    held = []
    page.route("**" + TRAFFIC + "/rules", lambda route: held.append(route), times=1)
    with page.expect_request(lambda r: urlsplit(r.url).path == TRAFFIC + "/rules"):
        dialog.get_by_role("button", name="保存规则").click()
    expect(dialog.get_by_role("button", name="保存规则")).to_be_disabled()
    page.clock.fast_forward(30_000)
    assert page.evaluate("window.trafficReads") == 2
    row["rule"] = {**row["rule"], "edge_rpm": 171}
    row["overridden"] = True
    with page.expect_response(lambda r: urlsplit(r.url).path == TRAFFIC + "/rules") as saved:
        held.pop().continue_()
    assert saved.value.ok
    expect(page.locator("#list-status")).to_contain_text("已保存规则")
    expect(sync).to_have_attribute("data-state", "pending")
    body["gateway"].update(state="applied", applied_revision="v2")
    page.clock.fast_forward(15_000)
    expect(sync).to_have_attribute("data-state", "applied")
    expect(page.locator("#list-status")).to_contain_text("已保存规则")
    assert page.evaluate("window.trafficReads") == 4

    # 暂停后和隐藏页签中都不应发起新的 GET；切回可见后应重新取得当前状态。
    auto.uncheck()
    page.clock.fast_forward(45_000)
    assert page.evaluate("window.trafficReads") == 4
    page.evaluate("""() => {
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
        Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    auto.check()
    page.clock.fast_forward(30_000)
    assert page.evaluate("window.trafficReads") == 4
    body["gateway"]["state"] = "error"
    page.evaluate("""() => {
        delete document.hidden;
        delete document.visibilityState;
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.clock.fast_forward(15_000)
    expect(sync).to_have_attribute("data-state", "error")
