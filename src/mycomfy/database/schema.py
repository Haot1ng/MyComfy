"""部署时执行数据库迁移，服务启动时检查数据库版本。"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from .session import make_sessions


def migration_config(database_url: str) -> Config:
    """定位源码或已安装包中的迁移，并传入当前数据库连接地址。"""
    package_root = Path(__file__).resolve().parents[1]
    # wheel 将迁移放在包内；源码检出保留仓库顶层的 migrations/。
    location = package_root / "migrations"
    if not location.is_dir():
        location = package_root.parents[1] / "migrations"
    config = Config()
    config.set_main_option("script_location", str(location))
    # 连接串可包含百分号编码的密码。通过 attributes 绕开 INI 字符串插值。
    config.attributes["database_url"] = database_url
    return config


def migrate(database_url: str) -> None:
    """按版本顺序升级到本发布包的最新数据库结构。"""
    command.upgrade(migration_config(database_url), "head")


def require_current_schema(database_url: str) -> None:
    """对比数据库与程序所需迁移版本，避免旧库被新进程写入。"""
    expected = set(ScriptDirectory.from_config(migration_config(database_url)).get_heads())
    engine = make_sessions(database_url).kw["bind"]
    try:
        with engine.connect() as connection:
            actual = set(MigrationContext.configure(connection).get_current_heads())
    finally:
        engine.dispose()
    if actual != expected:
        raise RuntimeError("数据库结构与程序版本不同，请先执行 mycomfy migrate")
