"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade():
    """执行本版本包含的数据库结构变更。"""
    ${upgrades if upgrades else "pass"}


def downgrade():
    """以相反顺序撤销本版本包含的结构变更。"""
    ${downgrades if downgrades else "pass"}
