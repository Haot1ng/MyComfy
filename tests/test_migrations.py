"""以空 MySQL 业务库验证完整迁移及服务启动版本检查。"""

import pytest
from alembic import command
from sqlalchemy import inspect

from mycomfy.schema import migrate, migration_config, require_current_schema


@pytest.mark.mysql
def test_schema_upgrade_downgrade_and_version_gate(mysql_sessions):
    """验证已安装迁移能回到空库再升级，旧库会被启动检查识别。"""
    engine = mysql_sessions.kw["bind"]
    url = engine.url.render_as_string(hide_password=False)
    config = migration_config(url)
    require_current_schema(url)
    command.downgrade(config, "base")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    with pytest.raises(RuntimeError, match="mycomfy migrate"):
        require_current_schema(url)
    migrate(url)
    require_current_schema(url)
    # 自动生成器也要识别全部业务表，防止迁移文件与 ORM 模型逐渐分离。
    command.check(config)
