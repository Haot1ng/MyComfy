from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker


def make_sessions(url: str) -> sessionmaker[Session]:
    """建立会话工厂，为 MySQL 设置 UTC、UTF-8 及读已提交隔离级别。"""
    options = {"pool_pre_ping": True}
    if url.startswith("mysql"):
        options["isolation_level"] = "READ COMMITTED"
        options["connect_args"] = {
            "init_command": "SET time_zone = '+00:00'",
            "charset": "utf8mb4",
        }
    return sessionmaker(create_engine(url, **options), expire_on_commit=False)


def db_now(db: Session) -> datetime:
    """读取数据库的当前 UTC 时间，作为调度和事务结算的统一时钟。"""
    expression = func.utc_timestamp(6) if db.bind.dialect.name == "mysql" else func.now()
    value = db.scalar(select(expression))
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def new_id(prefix: str) -> str:
    """生成带资源类型前缀的随机字符串 ID。"""
    return f"{prefix}_{uuid4().hex}"


def timestamp(value: datetime | None) -> str | None:
    """将数据库存储的 UTC 时间转为带时区的 API 时间字符串。"""
    return value.replace(tzinfo=UTC).isoformat() if value else None
