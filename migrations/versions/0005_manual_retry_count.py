"""记录每个任务已受理的手动重试次数。"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    """已有任务从零次手动重试开始，历史自动重试不占用该额度。"""
    op.add_column(
        "jobs", sa.Column("manual_retry_count", sa.Integer(), server_default="0", nullable=False)
    )


def downgrade():
    """移除手动重试计数，保留已有任务、执行与事件。"""
    op.drop_column("jobs", "manual_retry_count")
