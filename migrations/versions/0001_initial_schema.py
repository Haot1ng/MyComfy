"""initial schema

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    """按外键依赖顺序建立业务表、唯一约束及查询索引。"""
    op.create_table(
        "audits",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("actor", sa.String(length=48), nullable=True),
        sa.Column("action", sa.String(length=160), nullable=False),
        sa.Column("result", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("target", sa.String(length=48), nullable=True),
        sa.Column(
            "created_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audits_created_at"), "audits", ["created_at"], unique=False)
    op.create_index(op.f("ix_audits_target"), "audits", ["target"], unique=False)
    op.create_table(
        "engine_instances",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("configured", sa.Boolean(), nullable=False),
        sa.Column("accepting", sa.Boolean(), nullable=False),
        sa.Column(
            "heartbeat_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column("active_execution_id", sa.String(length=48), nullable=True),
        sa.Column("quarantine_reason", sa.String(length=255), nullable=True),
        sa.Column("runtime", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("active_execution_id"),
    )
    op.create_table(
        "login_limits",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "window_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "strategy_series",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column(
            "username",
            sa.String(length=100).with_variant(
                mysql.VARCHAR(collation="utf8mb4_0900_bin", length=100), "mysql"
            ),
            nullable=False,
        ),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("session_generation", sa.BigInteger(), nullable=False),
        sa.Column("current_session_id", sa.String(length=48), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "batches",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("user_id", sa.String(length=48), nullable=False),
        sa.Column(
            "client_key",
            sa.String(length=200).with_variant(
                mysql.VARCHAR(collation="utf8mb4_0900_bin", length=200), "mysql"
            ),
            nullable=False,
        ),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "client_key", name="uq_batch_client_key"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("user_id", sa.String(length=48), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column(
            "expires_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False
        ),
        sa.Column(
            "revoked_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"], unique=False)
    op.create_table(
        "strategy_versions",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("strategy_key", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=180), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "published_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(length=48), nullable=False),
        sa.ForeignKeyConstraint(
            ["strategy_key"],
            ["strategy_series.key"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy_key", "version", name="uq_strategy_version"),
    )
    op.create_table(
        "user_sequences",
        sa.Column("user_id", sa.String(length=48), nullable=False),
        sa.Column("latest", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "incompatibilities",
        sa.Column("strategy_version_id", sa.String(length=48), nullable=False),
        sa.Column("environment_digest", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_versions.id"],
        ),
        sa.PrimaryKeyConstraint("strategy_version_id", "environment_digest"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("batch_id", sa.String(length=48), nullable=False),
        sa.Column("user_id", sa.String(length=48), nullable=False),
        sa.Column("strategy_version_id", sa.String(length=48), nullable=False),
        sa.Column(
            "client_key",
            sa.String(length=200).with_variant(
                mysql.VARCHAR(collation="utf8mb4_0900_bin", length=200), "mysql"
            ),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=22), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("current_execution_id", sa.String(length=48), nullable=True),
        sa.Column("base_priority", sa.Integer(), nullable=False),
        sa.Column("budget_used", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False
        ),
        sa.Column(
            "enqueued_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.Column(
            "retry_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=True
        ),
        sa.Column("blocked_reason", sa.String(length=80), nullable=True),
        sa.Column("failure", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batches.id"],
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "client_key", name="uq_job_client_key"),
        sa.UniqueConstraint("batch_id", "position", name="uq_job_batch_position"),
    )
    op.create_index("ix_jobs_schedule", "jobs", ["state", "retry_at", "enqueued_at"], unique=False)
    op.create_index("ix_jobs_user_created", "jobs", ["user_id", "created_at", "id"], unique=False)
    op.create_table(
        "executions",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("job_id", sa.String(length=48), nullable=False),
        sa.Column("instance_id", sa.String(length=48), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("prompt_id", sa.String(length=48), nullable=True),
        sa.Column("prepared_workflow", sa.JSON(), nullable=True),
        sa.Column("workflow_digest", sa.String(length=64), nullable=True),
        sa.Column("environment", sa.JSON(), nullable=False),
        sa.Column("workspace", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("stage", sa.String(length=80), nullable=True),
        sa.Column("budget_charged", sa.Boolean(), nullable=False),
        sa.Column("budget_refunded", sa.Boolean(), nullable=False),
        sa.Column("budget_settled", sa.Boolean(), nullable=False),
        sa.Column("interrupt_requested", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False
        ),
        sa.Column(
            "authorized_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["engine_instances.id"],
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_no", name="uq_execution_attempt"),
        sa.UniqueConstraint("prompt_id", name="uq_execution_prompt"),
    )
    op.create_index(op.f("ix_executions_phase"), "executions", ["phase"], unique=False)
    op.create_table(
        "source_events",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("user_id", sa.String(length=48), nullable=False),
        sa.Column("job_id", sa.String(length=48), nullable=False),
        sa.Column("job_version", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False
        ),
        sa.Column(
            "dispatched_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_source_events_dispatched_at"), "source_events", ["dispatched_at"], unique=False
    )
    op.create_table(
        "assets",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("user_id", sa.String(length=48), nullable=False),
        sa.Column("job_id", sa.String(length=48), nullable=True),
        sa.Column("execution_id", sa.String(length=48), nullable=True),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("output_index", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("object_key", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=100), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("media", sa.JSON(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_id", "role", "output_index", name="uq_asset_output"),
        sa.UniqueConstraint("provider", "object_key", name="uq_asset_object"),
    )
    op.create_index(op.f("ix_assets_user_id"), "assets", ["user_id"], unique=False)
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("user_id", sa.String(length=48), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("source_event_id", sa.String(length=48), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column(
            "read_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["source_event_id"],
            ["source_events.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_event_id"),
    )
    op.create_index(
        "ix_notifications_user_read", "notifications", ["user_id", "read_at", "seq"], unique=False
    )
    op.create_table(
        "publications",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("execution_id", sa.String(length=48), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("output_index", sa.Integer(), nullable=False),
        sa.Column("source_path", sa.String(length=1000), nullable=False),
        sa.Column("object_key", sa.String(length=255), nullable=False),
        sa.Column("contract", sa.JSON(), nullable=False),
        sa.Column("phase", sa.String(length=20), nullable=False),
        sa.Column("verification", sa.JSON(), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_id", "role", "output_index", name="uq_publication_output"),
    )
    op.create_index(op.f("ix_publications_phase"), "publications", ["phase"], unique=False)
    op.create_table(
        "user_events",
        sa.Column("user_id", sa.String(length=48), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("source_event_id", sa.String(length=48), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_event_id"],
            ["source_events.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("user_id", "seq"),
        sa.UniqueConstraint("source_event_id"),
    )
    op.create_table(
        "job_inputs",
        sa.Column("job_id", sa.String(length=48), nullable=False),
        sa.Column("asset_id", sa.String(length=48), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
        ),
        sa.PrimaryKeyConstraint("job_id", "asset_id"),
    )


def downgrade():
    """反向解除索引和外键依赖，回滚本次建库迁移。"""
    op.drop_table("job_inputs")
    op.drop_table("user_events")
    op.drop_table("publications")
    op.drop_table("notifications")
    op.drop_table("assets")
    op.drop_table("source_events")
    op.drop_table("executions")
    op.drop_table("jobs")
    op.drop_table("incompatibilities")
    op.drop_table("user_sequences")
    op.drop_table("strategy_versions")
    op.drop_table("sessions")
    op.drop_table("batches")
    op.drop_table("users")
    op.drop_table("strategy_series")
    op.drop_table("login_limits")
    op.drop_table("engine_instances")
    op.drop_table("audits")
