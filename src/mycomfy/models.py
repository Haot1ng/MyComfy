from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME, VARCHAR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ID = String(48)
TIME = DateTime().with_variant(DATETIME(fsp=6), "mysql")


def exact_key(length):
    """为区分大小写的幂等键选用 MySQL 二进制排序规则。"""
    return String(length).with_variant(VARCHAR(length, collation="utf8mb4_0900_bin"), "mysql")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    username: Mapped[str] = mapped_column(exact_key(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    credential_version: Mapped[int] = mapped_column(default=1)
    session_generation: Mapped[int] = mapped_column(BigInteger, default=0)
    current_session_id: Mapped[str | None] = mapped_column(ID)


class UserSession(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(TIME)
    revoked_at: Mapped[datetime | None] = mapped_column(TIME)


class LoginLimit(Base):
    __tablename__ = "login_limits"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_at: Mapped[datetime] = mapped_column(TIME)
    attempts: Mapped[int]


class StrategySeries(Base):
    __tablename__ = "strategy_series"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    latest_version: Mapped[int] = mapped_column(default=0)


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (UniqueConstraint("strategy_key", "version", name="uq_strategy_version"),)

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    strategy_key: Mapped[str] = mapped_column(ForeignKey("strategy_series.key"))
    version: Mapped[int]
    name: Mapped[str] = mapped_column(String(180))
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_digest: Mapped[str] = mapped_column(String(64))
    published_at: Mapped[datetime] = mapped_column(TIME)
    request_id: Mapped[str] = mapped_column(ID)


class EngineInstance(Base):
    __tablename__ = "engine_instances"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    configured: Mapped[bool] = mapped_column(Boolean, default=True)
    accepting: Mapped[bool] = mapped_column(Boolean, default=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(TIME)
    active_execution_id: Mapped[str | None] = mapped_column(ID, unique=True)
    quarantine_reason: Mapped[str | None] = mapped_column(String(255))
    runtime: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Incompatibility(Base):
    __tablename__ = "incompatibilities"

    strategy_version_id: Mapped[str] = mapped_column(
        ForeignKey("strategy_versions.id"), primary_key=True
    )
    environment_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    reason: Mapped[str] = mapped_column(String(100))


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (UniqueConstraint("user_id", "client_key", name="uq_batch_client_key"),)

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    client_key: Mapped[str] = mapped_column(exact_key(200))
    request_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TIME)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("batch_id", "client_key", name="uq_job_client_key"),
        UniqueConstraint("batch_id", "position", name="uq_job_batch_position"),
        Index("ix_jobs_schedule", "state", "retry_at", "enqueued_at"),
        Index("ix_jobs_user_created", "user_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    strategy_version_id: Mapped[str] = mapped_column(ForeignKey("strategy_versions.id"))
    client_key: Mapped[str] = mapped_column(exact_key(200))
    position: Mapped[int]
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(22))
    version: Mapped[int] = mapped_column(BigInteger, default=1)
    current_execution_id: Mapped[str | None] = mapped_column(ID)
    base_priority: Mapped[int]
    budget_used: Mapped[int] = mapped_column(default=0)
    attempt_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(TIME)
    enqueued_at: Mapped[datetime] = mapped_column(TIME)
    retry_at: Mapped[datetime | None] = mapped_column(TIME)
    blocked_reason: Mapped[str | None] = mapped_column(String(80))
    failure: Mapped[str | None] = mapped_column(String(255))


class Execution(Base):
    __tablename__ = "executions"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_no", name="uq_execution_attempt"),
        UniqueConstraint("prompt_id", name="uq_execution_prompt"),
    )

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    instance_id: Mapped[str] = mapped_column(ForeignKey("engine_instances.id"))
    generation: Mapped[int] = mapped_column(BigInteger)
    attempt_no: Mapped[int]
    phase: Mapped[str] = mapped_column(String(32), index=True)
    prompt_id: Mapped[str | None] = mapped_column(ID)
    prepared_workflow: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    workflow_digest: Mapped[str | None] = mapped_column(String(64))
    environment: Mapped[dict[str, Any]] = mapped_column(JSON)
    workspace: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    stage: Mapped[str | None] = mapped_column(String(80))
    budget_charged: Mapped[bool] = mapped_column(default=False)
    budget_refunded: Mapped[bool] = mapped_column(default=False)
    budget_settled: Mapped[bool] = mapped_column(default=False)
    interrupt_requested: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(TIME)
    authorized_at: Mapped[datetime | None] = mapped_column(TIME)
    created_at: Mapped[datetime] = mapped_column(TIME)


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("execution_id", "role", "output_index", name="uq_asset_output"),
        UniqueConstraint("provider", "object_key", name="uq_asset_object"),
    )

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"))
    execution_id: Mapped[str | None] = mapped_column(ForeignKey("executions.id"))
    role: Mapped[str] = mapped_column(String(64))
    output_index: Mapped[int | None]
    provider: Mapped[str] = mapped_column(String(24), default="local")
    object_key: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(BigInteger)
    digest: Mapped[str] = mapped_column(String(64))
    media: Mapped[dict[str, Any]] = mapped_column(JSON)
    complete: Mapped[bool]
    available: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(TIME)


class JobInput(Base):
    __tablename__ = "job_inputs"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), primary_key=True)


class Publication(Base):
    __tablename__ = "publications"
    __table_args__ = (
        UniqueConstraint("execution_id", "role", "output_index", name="uq_publication_output"),
    )

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id"))
    role: Mapped[str] = mapped_column(String(64))
    output_index: Mapped[int]
    source_path: Mapped[str] = mapped_column(String(1000))
    object_key: Mapped[str] = mapped_column(String(255))
    contract: Mapped[dict[str, Any]] = mapped_column(JSON)
    phase: Mapped[str] = mapped_column(String(20), index=True)
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(String(255))


class SourceEvent(Base):
    __tablename__ = "source_events"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    job_version: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(TIME)
    dispatched_at: Mapped[datetime | None] = mapped_column(TIME, index=True)


class UserSequence(Base):
    __tablename__ = "user_sequences"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    latest: Mapped[int] = mapped_column(BigInteger, default=0)


class UserEvent(Base):
    __tablename__ = "user_events"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_event_id: Mapped[str] = mapped_column(ForeignKey("source_events.id"), unique=True)
    type: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_read", "user_id", "read_at", "seq"),)

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    seq: Mapped[int] = mapped_column(BigInteger)
    source_event_id: Mapped[str] = mapped_column(ForeignKey("source_events.id"), unique=True)
    type: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    read_at: Mapped[datetime | None] = mapped_column(TIME)
    created_at: Mapped[datetime] = mapped_column(TIME)


class Audit(Base):
    __tablename__ = "audits"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    actor: Mapped[str | None] = mapped_column(ID)
    action: Mapped[str] = mapped_column(String(160))
    result: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer)
    target: Mapped[str | None] = mapped_column(ID, index=True)
    created_at: Mapped[datetime] = mapped_column(TIME, index=True)
