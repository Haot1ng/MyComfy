import os
from dataclasses import dataclass, field
from pathlib import Path

from mycomfy.origins import normalize_test_origins


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
    redis_url: str | None = field(default=None, repr=False)
    session_cache_ttl_seconds: int = 60
    admin_test_origin: str | None = None
    manual_retry_limit: int = 3

    def __post_init__(self):
        """校验容量和时间配置，并保存浏览器用于同源比较的入口地址。"""
        if (
            min(
                self.session_seconds,
                self.max_upload_bytes,
                self.max_batch_tasks,
                self.aging_seconds,
                self.poll_seconds,
                self.sse_heartbeat_seconds,
                self.sse_send_timeout_seconds,
                self.session_cache_ttl_seconds,
            )
            <= 0
        ):
            raise ValueError("Session, upload, batch, aging and polling settings must be positive")
        if type(self.manual_retry_limit) is not int or self.manual_retry_limit < 0:
            raise ValueError("manual_retry_limit 需要填写非负整数")
        public, admin = normalize_test_origins(self.public_origin, self.admin_test_origin)
        object.__setattr__(self, "public_origin", public.rstrip("/"))
        object.__setattr__(self, "admin_test_origin", admin)

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
            public_origin=os.environ.get("MYCOMFY_PUBLIC_ORIGIN", "https://localhost"),
            admin_test_origin=os.environ.get("MYCOMFY_ADMIN_TEST_ORIGIN"),
            cookie_secure=os.environ.get("MYCOMFY_COOKIE_SECURE", "true").lower() != "false",
            session_seconds=int(os.environ.get("MYCOMFY_SESSION_SECONDS", "86400")),
            max_upload_bytes=int(
                os.environ.get("MYCOMFY_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024))
            ),
            max_batch_tasks=int(os.environ.get("MYCOMFY_MAX_BATCH_TASKS", "20")),
            manual_retry_limit=int(os.environ.get("MYCOMFY_MANUAL_RETRY_LIMIT", "3")),
            aging_seconds=int(os.environ.get("MYCOMFY_AGING_SECONDS", "60")),
            maintenance_token_file=optional_path("MYCOMFY_MAINTENANCE_TOKEN_FILE"),
            engine_config=optional_path("MYCOMFY_ENGINE_CONFIG"),
            poll_seconds=float(os.environ.get("MYCOMFY_POLL_SECONDS", "1")),
            sse_heartbeat_seconds=float(os.environ.get("MYCOMFY_SSE_HEARTBEAT_SECONDS", "15")),
            sse_send_timeout_seconds=float(
                os.environ.get("MYCOMFY_SSE_SEND_TIMEOUT_SECONDS", "10")
            ),
            redis_url=os.environ.get("MYCOMFY_REDIS_URL") or None,
            session_cache_ttl_seconds=int(
                os.environ.get("MYCOMFY_SESSION_CACHE_TTL_SECONDS", "60")
            ),
        )
