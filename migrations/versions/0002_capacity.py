"""增加卷水位和并发磁盘预留。

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    """创建容量准入互斥行、卷水位记录和可追溯预留。"""
    gate = op.create_table(
        "capacity_gates",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("paused", sa.Boolean(), nullable=False),
    )
    op.bulk_insert(gate, [{"id": "admission", "paused": False}])
    op.create_table(
        "disk_reservations",
        sa.Column("owner", sa.String(48), primary_key=True),
        sa.Column("device", sa.String(40), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("user_id", sa.String(48), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("upload_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False
        ),
    )
    op.create_index("ix_disk_reservations_user_id", "disk_reservations", ["user_id"])


def downgrade():
    """移除磁盘预留及容量准入表。"""
    op.drop_table("disk_reservations")
    op.drop_table("capacity_gates")
