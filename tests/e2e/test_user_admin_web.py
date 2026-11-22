"""在真实管理页面中翻页、编辑分组，并从普通用户的现有会话核对授权结果。"""

from pathlib import Path
from shutil import which
from threading import Thread
from time import monotonic, sleep

import httpx
import pytest

from mycomfy.api.server import api_server
from mycomfy.config import Settings
from mycomfy.database.models import User, UserGroup, UserGroupPermission
from mycomfy.database.schema import migrate
from mycomfy.database.session import make_sessions


@pytest.fixture
def api_urls(tmp_path):
    """启动双监听 API，预置一个可选组和刚好填满第一页的普通账户。"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'users.sqlite'}",
        data_root=tmp_path / "files",
        maintenance_token_file=None,
        public_origin="http://127.0.0.1",
        cookie_secure=False,
    )
    migrate(settings.database_url)
    sessions = make_sessions(settings.database_url)
    try:
        with sessions.begin() as db:
            # 填充账户的 ID 排在通过注册接口创建的用户之前，使第二页稳定出现。
            db.add_all(
                User(id=f"a_{i:03}", username=f"sample-{i:03}", password_hash="unused")
                for i in range(50)
            )
            db.add(UserGroup(key="reviewer", name="审核组"))
            db.flush()
            db.add(UserGroupPermission(group_key="reviewer", permission="jobs.view"))
        with api_server(settings, "127.0.0.1", 0, 0, sessions=sessions) as (server, sockets):
            thread = Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
            thread.start()
            try:
                deadline = monotonic() + 10
                while not server.started:
                    assert thread.is_alive() and monotonic() < deadline
                    sleep(0.01)
                yield [f"http://127.0.0.1:{sock.getsockname()[1]}" for sock in sockets]
            finally:
                server.should_exit = True
                thread.join(timeout=10)
    finally:
        sessions.kw["bind"].dispose()


def save_user_groups(page, username, groups, expect):
    """用页面控件替换某个用户的分组，并等待服务端确认和列表恢复。"""
    edit = page.get_by_role("button", name=f"编辑 {username} 的分组", exact=True)
    edit.click()
    dialog = page.get_by_role("dialog", name="编辑权限组")
    # 编辑器打开后异步读取组目录和用户现有分组，加载成功才允许操作。
    expect(dialog.locator('input[value="standard"]')).to_be_enabled()
    for checkbox in dialog.get_by_role("checkbox").all():
        checkbox.set_checked(checkbox.input_value() in groups)
    with page.expect_response(
        lambda r: r.request.method == "PUT" and r.url.endswith("/groups")
    ) as put:
        dialog.get_by_role("button", name="保存分组").click()
    assert put.value.status == 200, put.value.text()
    expect(dialog).not_to_be_visible()
    return put.value


def test_user_management_changes_live_permissions(api_urls):
    """管理页跨页选人，对两个账户增减分组，原有 C 端会话随即使用更新后的权限。"""
    playwright = pytest.importorskip("playwright.sync_api")
    public, admin = api_urls
    origin = {"Origin": "http://127.0.0.1"}
    with (
        httpx.Client(base_url=public, headers=origin, trust_env=False) as writer,
        httpx.Client(base_url=public, headers=origin, trust_env=False) as reader,
        playwright.sync_playwright() as engine,
    ):
        executable = which("chromium") or which("google-chrome") or engine.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("未安装 Chromium")
        for name, client in (("writer", writer), ("reader", reader)):
            credential = {"username": name, "password": "only-for-this-test"}
            client.post("/api/v1/accounts", json=credential).raise_for_status()
            client.post("/api/v1/sessions", json=credential).raise_for_status()

        with engine.chromium.launch(executable_path=executable) as browser:
            page = browser.new_page()
            page.goto(admin)
            page.get_by_role("link", name="用户管理").click()
            table = page.get_by_role("table", name="已注册用户")
            playwright.expect(table.get_by_text("sample-000", exact=True)).to_be_visible()
            playwright.expect(table.get_by_text("writer", exact=True)).to_have_count(0)
            page.get_by_role("button", name="下一页").click()
            first = save_user_groups(page, "writer", {"reviewer"}, playwright.expect)
            assert "authorization" not in first.request.all_headers()
            assert "cookie" not in first.request.all_headers()
            playwright.expect(table.get_by_role("row").filter(has_text="writer")).to_contain_text(
                "审核组"
            )
            assert writer.get("/api/v1/me").raise_for_status().json()["permissions"] == [
                "jobs.view"
            ]

            # 分别清空、再多选第二个账户，确保编辑器不会继承上一行的选中状态。
            save_user_groups(page, "reader", set(), playwright.expect)
            empty = reader.get("/api/v1/me").raise_for_status().json()
            assert empty["groups"] == empty["permissions"] == []
            assert writer.get("/api/v1/me").raise_for_status().json()["permissions"] == [
                "jobs.view"
            ]
            save_user_groups(page, "reader", {"standard", "reviewer"}, playwright.expect)
            updated = reader.get("/api/v1/me").raise_for_status().json()
            assert {group["key"] for group in updated["groups"]} == {"standard", "reviewer"}
            assert {"jobs.view", "jobs.submit"} <= set(updated["permissions"])
            page.get_by_role("button", name="上一页").click()
            playwright.expect(table.get_by_text("sample-000", exact=True)).to_be_visible()
