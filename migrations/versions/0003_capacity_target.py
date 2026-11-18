"""记录预留中属于正式文件目标卷的份额。"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    """为旧预留保留保守估计，新预留可随暂存和归档进度核销。"""
    op.add_column(
        "disk_reservations",
        sa.Column("target_bytes", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade():
    """回到只记录整次上传和执行总预留的结构。"""
    op.drop_column("disk_reservations", "target_bytes")
