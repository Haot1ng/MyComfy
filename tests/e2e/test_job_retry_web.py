"""通过真实任务详情页验证手动重试所用的版本、额度和提交权限。"""

from pathlib import Path
from shutil import which
from urllib.parse import urlsplit

import pytest


@pytest.mark.parametrize("case", ["accepted", "conflict", "lost", "read-only", "exhausted", "auto"])
def test_failed_job_manual_retry(case):
    """验证双击只提交一次，响应丢失或版本冲突先同步详情，额度和权限由服务端决定。"""
    playwright = pytest.importorskip("playwright.sync_api")
    assets = Path(__file__).resolve().parents[2] / "web" / "client"
    origin = "http://mycomfy.test"
    job = {
        "job_id": "job-1",
        "client_task_key": "镜头一",
        "job_version": 8,
        "state": "RETRY_WAIT" if case == "auto" else "FAILED",
        "batch_id": "batch-1",
        "strategy_version_id": "sv-3",
        "manual_retry_count": 2 if case == "exhausted" else 0,
        "manual_retry_limit": 2,
        "assets": [],
    }
    attempts = []
    details = []

    def serve(route):
        """模拟业务返回最新状态，记录真实浏览器发出的 POST 及其会话标识。"""
        request = route.request
        path = urlsplit(request.url).path
        if not path.startswith("/api/v1/"):
            file = assets / (path.lstrip("/") or "index.html")
            if file.parent == assets and file.is_file():
                route.fulfill(path=file)
            else:
                route.fulfill(status=404)
            return
        if path.endswith("/jobs/job-1/retry"):
            attempts.append((request.method, request.post_data_json, request.all_headers()))
            if case == "conflict":
                job.update(job_version=11, manual_retry_count=2)
                route.fulfill(status=409, json={"error": {"code": "JOB_VERSION_CONFLICT"}})
            else:
                job.update(job_version=9, manual_retry_count=1, state="QUEUED")
                if case == "lost":
                    route.abort()
                else:
                    route.fulfill(
                        status=202,
                        json={
                            k: job[k]
                            for k in (
                                "job_id",
                                "job_version",
                                "state",
                                "manual_retry_count",
                                "manual_retry_limit",
                            )
                        },
                    )
        elif path.endswith("/jobs/job-1"):
            details.append(job["job_version"])
            route.fulfill(json=job)
        elif path.endswith("/me"):
            route.fulfill(
                json={
                    "username": "owner",
                    "user_id": "u_1",
                    "session_generation": "3",
                    "groups": [],
                    "csrf_token": "test-token",
                    "permissions": ["jobs.view"]
                    if case == "read-only"
                    else ["jobs.view", "jobs.submit"],
                }
            )
        elif path.endswith("/events/cursor"):
            route.fulfill(json={"user_seq": "0"})
        elif path.endswith("/events"):
            route.fulfill(content_type="text/event-stream", body=": connected\n\n")
        else:
            route.fulfill(status=404)

    with playwright.sync_playwright() as engine:
        executable = which("chromium") or which("google-chrome") or engine.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("未安装 Chromium")
        with engine.chromium.launch(executable_path=executable) as browser:
            page = browser.new_page()
            page.route(origin + "/**", serve)
            page.goto(origin + "/#/job/job-1")
            main = page.locator("main")
            playwright.expect(main.get_by_role("heading", name="镜头一")).to_be_visible()
            button = main.get_by_role("button", name="手动重试", exact=True)
            if case in {"read-only", "exhausted", "auto"}:
                playwright.expect(button).to_have_count(0)
                assert not attempts
                return
            playwright.expect(main).to_contain_text("已用 0 / 2 次")
            expected = 11 if case == "conflict" else 9
            # 等待点击后的详情重读，即使写请求因网络错误没有收到响应。
            with page.expect_response(
                lambda response: (
                    bool(attempts) and urlsplit(response.url).path == "/api/v1/jobs/job-1"
                )
            ) as refreshed:
                button.evaluate("button => { button.click(); button.click(); }")
            assert refreshed.value.json()["job_version"] == expected
            used = 2 if case == "conflict" else 1
            playwright.expect(main).to_contain_text(f"已用 {used} / 2 次")
            playwright.expect(button).to_have_count(0)
            if case == "conflict":
                playwright.expect(main).to_contain_text("任务已更新")
            else:
                playwright.expect(main.locator(".hist-state")).to_have_text("排队中")
            assert len(attempts) == 1
            method, payload, headers = attempts[0]
            assert method == "POST" and payload == {"expected_job_version": 8}
            assert (
                headers["x-session-generation"] == "3" and headers["x-csrf-token"] == "test-token"
            )
            assert details[0] == 8 and expected in details
