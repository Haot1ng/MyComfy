"""保存动态接口限流规则，并在请求审计中记录监听器及限流层。"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    """为空规则库启用程序默认值，同时允许旧审计保留未知的监听器与限流层。"""
    op.create_table(
        "traffic_rules",
        sa.Column("listener", sa.String(8), primary_key=True),
        sa.Column("method", sa.String(10), primary_key=True),
        sa.Column("path", sa.String(255), primary_key=True),
        sa.Column("edge_rpm", sa.Integer(), nullable=False),
        sa.Column("edge_burst", sa.Integer(), nullable=False),
        sa.Column("user_rpm", sa.Integer(), nullable=False),
        sa.Column("provider_concurrency", sa.Integer(), nullable=False),
    )
    op.add_column("audits", sa.Column("traffic_listener", sa.String(8)))
    op.add_column("audits", sa.Column("rate_limit_layer", sa.String(12)))
    op.create_index("ix_audits_rate_layer_time", "audits", ["rate_limit_layer", "created_at"])


def downgrade():
    """移除限流覆盖及两项审计标记，保留原始操作和响应状态。"""
    op.drop_index("ix_audits_rate_layer_time", table_name="audits")
    op.drop_column("audits", "rate_limit_layer")
    op.drop_column("audits", "traffic_listener")
    op.drop_table("traffic_rules")
