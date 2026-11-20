import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from .auth import digest

log = logging.getLogger(__name__)
KEY_PREFIX = "mycomfy:session-precheck:v1:"


@dataclass(frozen=True)
class CachedSession:
    user_id: str
    generation: str
    csrf_digest: str


class SessionPrecheckCache:
    def __init__(self, url: str, ttl_seconds: int):
        """为可选的预检缓存建立 Redis 客户端，限制故障时的等待时间。"""
        self.client = Redis.from_url(
            url, socket_connect_timeout=0.25, socket_timeout=0.25, retry=Retry(NoBackoff(), 0)
        )
        self.ttl_seconds = ttl_seconds

    def get(self, token_digest: str) -> CachedSession | None:
        """读取暂存的会话头部信息，缺失、损坏或 Redis 失败时交给数据库。"""
        try:
            raw = self.client.get(KEY_PREFIX + token_digest)
        except RedisError:
            log.debug("Redis session precheck unavailable")
            return None
        if raw is None:
            return None
        try:
            values = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(values, list) or len(values) != 3:
            return None
        if not all(isinstance(value, str) and value.isascii() for value in values):
            return None
        return CachedSession(*values)

    def remember(self, session):
        """暂存已通过 MySQL 校验的会话信息，缓存有效期最多到会话到期。"""
        remaining = (session.expires_at.replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds()
        ttl = min(self.ttl_seconds, int(remaining))
        if ttl <= 0:
            return
        # Redis 只保存令牌摘要及预检字段。数据库写事务仍重新核验会话和用户。
        value = json.dumps([session.user_id, str(session.generation), digest(session.csrf_token)])
        try:
            self.client.set(KEY_PREFIX + session.token_digest, value, ex=ttl)
        except RedisError:
            log.debug("Redis session precheck cache could not be populated")

    def invalidate(self, token_digest: str | None):
        """尽力删除已失效的会话缓存，删除失败时由后续校验和 TTL 收敛。"""
        if token_digest is None:
            return
        try:
            self.client.delete(KEY_PREFIX + token_digest)
        except RedisError:
            log.warning("Redis session cache invalidation failed")

    def close(self):
        """在服务关闭时回收 Redis 客户端连接。"""
        self.client.close()
