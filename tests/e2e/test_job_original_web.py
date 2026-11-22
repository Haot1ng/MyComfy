"""在安装了 Playwright 与 Chromium 的环境中验证任务原始信息页的浏览行为。"""

from base64 import b64decode
from pathlib import Path
from shutil import which
from urllib.parse import parse_qs, urlsplit

import pytest


@pytest.mark.parametrize("downloads", [True, False], ids=["media-access", "jobs-only"])
def test_original_job_navigation_and_media(downloads):
    """通过完整用户网页导航，在受控接口下核对按需取数、素材权限和历史路由。"""
    playwright = pytest.importorskip("playwright.sync_api")
    client = Path(__file__).resolve().parents[2] / "web" / "client"
    origin = "http://mycomfy.test"
    permissions = ["jobs.view", "assets.download"] if downloads else ["jobs.view"]
    profile = {
        "user_id": "u_test",
        "username": "画师",
        "session_generation": "3",
        "csrf_token": "test-token",
        "permissions": permissions,
        "groups": [],
    }
    jobs = {
        key: {
            "job_id": key,
            "batch_id": "batch",
            "client_task_key": name,
            "state": "SUCCEEDED",
            "created_at": "2026-11-22T09:00:00Z",
            "job_version": 2,
            "strategy_version_id": "sv_10",
            "assets": [],
        }
        for key, name in [("j1", "镜头一"), ("j2", "镜头二")]
    }
    ids = ["a_first", "a_second"]
    png = b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg+M/wHwAE"
        "AQH/cetH5QAAAABJRU5ErkJggg=="
    )
    assets = {
        key: {
            "asset_id": key,
            "role": "input",
            "media_type": "image/png",
            "size": len(png),
            "complete": True,
            "available": True,
            "media": {"width": 1, "height": 1},
            "content_url": f"/api/v1/assets/{key}/content",
        }
        for key in ids
    }
    requests = []

    def serve(route):
        """提供真实前台资源与当前测试会话的 API 响应，记录请求以便核对。"""
        url = urlsplit(route.request.url)
        path = url.path
        if not path.startswith("/api/v1/"):
            file = client / (path.lstrip("/") or "index.html")
            if file.parent == client and file.is_file():
                route.fulfill(path=file)
            else:
                route.fulfill(status=404)
            return
        requests.append((path, parse_qs(url.query)))
        endpoint = path.removeprefix("/api/v1/")
        if endpoint == "events":
            route.fulfill(body=": connected\n\n", content_type="text/event-stream")
            return
        if endpoint.startswith("assets/"):
            route.fulfill(body=png, content_type="image/png")
            return
        if endpoint == "me":
            data = profile
        elif endpoint == "jobs":
            data = {"items": list(jobs.values()), "next_cursor": None}
        elif endpoint == "events/cursor":
            data = {"user_seq": "0"}
        elif endpoint.startswith("jobs/") and endpoint.split("/")[1] in jobs:
            job = jobs[endpoint.split("/")[1]]
            if endpoint.endswith("/original"):
                data = {
                    **job,
                    "strategy_name": "分镜",
                    "strategy_key": "storyboard",
                    "strategy_version": 10,
                    "inputs": {
                        "prompt": f"<b>{job['job_id']} 的创作指令</b>",
                        "seed": 18446744073709551615,
                        "refs": ids,
                    },
                    "field_labels": {"prompt": "画面描述", "refs": "参考图"},
                    "asset_fields": ["refs"],
                    "input_assets": assets if downloads else {},
                }
            else:
                data = job
        else:
            route.fulfill(status=404)
            return
        route.fulfill(json=data)

    with playwright.sync_playwright() as engine:
        executable = which("chromium") or which("google-chrome") or engine.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("未安装 Chromium")
        with engine.chromium.launch(executable_path=executable) as browser:
            page = browser.new_page()
            page.route(origin + "/**", serve)
            page.goto(origin + "/#/jobs")
            page.get_by_role("link", name="镜头一", exact=True).click()
            original = page.get_by_role("link", name="原始任务信息", exact=True)
            playwright.expect(original).to_be_visible()
            assert not any(path.endswith("/original") for path, _ in requests)

            original.click()
            playwright.expect(page).to_have_url(origin + "/#/job/j1/original")
            playwright.expect(page.locator("main")).to_contain_text("<b>j1 的创作指令</b>")
            playwright.expect(page.locator("main")).to_contain_text("参考图")
            playwright.expect(page.locator("main")).to_contain_text("18446744073709551615")
            assert sum(path.endswith("/jobs/j1/original") for path, _ in requests) == 1
            assert not any(path.endswith("/content") for path, _ in requests)

            links = page.locator("main a[download]")
            previews = page.get_by_role("button", name="预览", exact=False)
            if downloads:
                playwright.expect(links).to_have_count(2)
                for i, asset_id in enumerate(ids):
                    url = urlsplit(links.nth(i).get_attribute("href"))
                    assert url.path == f"/api/v1/assets/{asset_id}/content"
                    assert parse_qs(url.query) == {
                        "page_user": ["u_test"],
                        "session_generation": ["3"],
                        "download": ["1"],
                    }
                with page.expect_request("**/assets/a_second/content?*") as requested_media:
                    previews.nth(1).click()
                assert parse_qs(urlsplit(requested_media.value.url).query) == {
                    "page_user": ["u_test"],
                    "session_generation": ["3"],
                }
                image = page.get_by_role("img", name="参考图")
                playwright.expect(image).to_have_js_property("naturalWidth", 1)
                page.get_by_role("button", name="收起预览").click()
                playwright.expect(image).to_have_count(0)
            else:
                playwright.expect(links).to_have_count(0)
                playwright.expect(previews).to_have_count(0)
                for asset_id in ids:
                    playwright.expect(page.locator("main")).to_contain_text(asset_id)

            page.get_by_role("link", name="返回任务详情").click()
            playwright.expect(page).to_have_url(origin + "/#/job/j1")
            playwright.expect(original).to_be_visible()
            playwright.expect(page.locator("main")).not_to_contain_text("j1 的创作指令")
            assert sum(path.endswith("/jobs/j1/original") for path, _ in requests) == 1
            page.get_by_role("navigation", name="工作台导航").get_by_role(
                "link", name="任务记录"
            ).click()
            page.get_by_role("link", name="镜头二", exact=True).click()
            original.click()
            playwright.expect(page.locator("main")).to_contain_text("<b>j2 的创作指令</b>")
            playwright.expect(page.locator("main")).not_to_contain_text("j1 的创作指令")
            page.go_back()
            playwright.expect(page).to_have_url(origin + "/#/job/j2")
            playwright.expect(original).to_be_visible()
