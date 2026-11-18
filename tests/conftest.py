"""为需要事务、锁和迁移验证的测试建立完全独立的 MySQL 数据库。"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from mycomfy.database.schema import migrate
from mycomfy.database.session import make_sessions


@pytest.fixture
def mysql_sessions():
    """创建随机命名的测试库，执行真实迁移，并在测试结束时回收该库。"""
    raw = os.environ.get("MYCOMFY_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("MySQL 集成测试请用 uv run python scripts/test_mysql.py")
    url = make_url(raw)
    name = "mycomfy_test_" + uuid4().hex
    admin = create_engine(url)
    try:
        with admin.begin() as conn:
            conn.execute(text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4"))
        # 隔离到单条用例，允许不同测试并行操纵队列或恢复器。
        test_url = url.set(database=name).render_as_string(hide_password=False)
        sessions = make_sessions(test_url)
        try:
            migrate(test_url)
            yield sessions
        finally:
            sessions.kw["bind"].dispose()
            with admin.begin() as conn:
                conn.execute(text(f"DROP DATABASE `{name}`"))
    finally:
        admin.dispose()
