"""增加提示词模型服务商、当前服务选择和模板级扩写设置。"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    """创建服务商与模板配置，并用固定 ID 的空选择记录串行化服务商切换。"""
    op.create_table(
        "prompt_providers",
        sa.Column("id", sa.String(48), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=True),
    )
    selection = op.create_table(
        "prompt_provider_selection",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("provider_id", sa.String(48), sa.ForeignKey("prompt_providers.id")),
        sa.CheckConstraint("id = 1", name="ck_prompt_provider_singleton"),
    )
    op.bulk_insert(selection, [{"id": 1, "provider_id": None}])
    # 新表保持为空，让已有模板继续使用各自版本发布时保存的设置。
    op.create_table(
        "template_prompt_settings",
        sa.Column(
            "strategy_key", sa.String(100), sa.ForeignKey("strategy_series.key"), primary_key=True
        ),
        sa.Column("config", sa.JSON(none_as_null=True), nullable=True),
    )


def downgrade():
    """先移除模板覆盖与选择记录，再移除服务商配置。"""
    op.drop_table("template_prompt_settings")
    op.drop_table("prompt_provider_selection")
    op.drop_table("prompt_providers")
