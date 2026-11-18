import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_root: Path
    public_origin: str = "https://localhost"
    cookie_secure: bool = True
    session_seconds: int = 86_400
    max_upload_bytes: int = 100 * 1024 * 1024
    max_batch_tasks: int = 20
    aging_seconds: int = 60
    maintenance_token_file: Path | None = None
    engine_config: Path | None = None
    poll_seconds: float = 1.0
    sse_heartbeat_seconds: float = 15.0
    sse_send_timeout_seconds: float = 10.0

    def __post_init__(self):
        """检查容量、调度周期和会话时长等必需为正数的配置。"""
        if (
            min(
                self.session_seconds,
                self.max_upload_bytes,
                self.max_batch_tasks,
                self.aging_seconds,
                self.poll_seconds,
                self.sse_heartbeat_seconds,
                self.sse_send_timeout_seconds,
            )
            <= 0
        ):
            raise ValueError("Session, upload, batch, aging and polling settings must be positive")

    @classmethod
    def from_env(cls):
        """从部署环境变量构造一份进程设置，并解析本机文件路径。"""

        def optional_path(name: str) -> Path | None:
            """读取可选路径配置并将其解析为绝对路径。"""
            value = os.environ.get(name)
            return Path(value).resolve() if value else None

        return cls(
            database_url=os.environ["MYCOMFY_DATABASE_URL"],
            data_root=Path(os.environ.get("MYCOMFY_DATA_ROOT", "var")).resolve(),
            public_origin=os.environ.get("MYCOMFY_PUBLIC_ORIGIN", "https://localhost").rstrip("/"),
            cookie_secure=os.environ.get("MYCOMFY_COOKIE_SECURE", "true").lower() != "false",
            session_seconds=int(os.environ.get("MYCOMFY_SESSION_SECONDS", "86400")),
            max_upload_bytes=int(
                os.environ.get("MYCOMFY_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024))
            ),
            max_batch_tasks=int(os.environ.get("MYCOMFY_MAX_BATCH_TASKS", "20")),
            aging_seconds=int(os.environ.get("MYCOMFY_AGING_SECONDS", "60")),
            maintenance_token_file=optional_path("MYCOMFY_MAINTENANCE_TOKEN_FILE"),
            engine_config=optional_path("MYCOMFY_ENGINE_CONFIG"),
            poll_seconds=float(os.environ.get("MYCOMFY_POLL_SECONDS", "1")),
            sse_heartbeat_seconds=float(os.environ.get("MYCOMFY_SSE_HEARTBEAT_SECONDS", "15")),
            sse_send_timeout_seconds=float(
                os.environ.get("MYCOMFY_SSE_SEND_TIMEOUT_SECONDS", "10")
            ),
        )
