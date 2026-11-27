"""记录 ComfyUI 每个实例最近一次成功读取的节点清单。"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    """增加独立的清单表，升级前的实例在引擎报告后才获得清单。"""
    op.create_table(
        "engine_node_catalogs",
        sa.Column(
            "instance_id",
            sa.String(48),
            sa.ForeignKey("engine_instances.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("nodes", sa.JSON(), nullable=False),
    )


def downgrade():
    """移除可从运行引擎重新采集的节点清单。"""
    op.drop_table("engine_node_catalogs")
