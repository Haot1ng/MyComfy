"""提供用户分组、组权限和默认成员关系。"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    """创建分组表，并给现有账户分配初始权限。"""
    groups = op.create_table(
        "user_groups",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
    )
    members = op.create_table(
        "user_group_members",
        sa.Column("user_id", sa.String(48), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("group_key", sa.String(64), sa.ForeignKey("user_groups.key"), primary_key=True),
    )
    permissions = op.create_table(
        "user_group_permissions",
        sa.Column("group_key", sa.String(64), sa.ForeignKey("user_groups.key"), primary_key=True),
        sa.Column("permission", sa.String(64), primary_key=True),
    )
    # 固定初始权限清单，后续版本增删权限不影响本次迁移。
    op.bulk_insert(groups, [{"key": "standard", "name": "标准用户"}])
    op.bulk_insert(
        permissions,
        [
            {"group_key": "standard", "permission": item}
            for item in (
                "strategies.view",
                "jobs.submit",
                "jobs.view",
                "jobs.cancel",
                "assets.upload",
                "assets.download",
                "notifications.view",
                "notifications.mark_read",
            )
        ],
    )
    op.execute(
        members.insert().from_select(
            ["user_id", "group_key"],
            sa.select(sa.column("id"), sa.literal("standard")).select_from(sa.table("users")),
        )
    )


def downgrade():
    """移除成员和权限关系，再删除分组表。"""
    op.drop_table("user_group_permissions")
    op.drop_table("user_group_members")
    op.drop_table("user_groups")
