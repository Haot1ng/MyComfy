"""业务端的按用户频率限制和按服务商计数的生成并发租约。"""

import asyncio
import hashlib
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager

from anyio import CancelScope
from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from mycomfy.errors import ApiError

WINDOW_MS = 60_000
PREFIX = "mycomfy:traffic:v1:"
log = logging.getLogger(__name__)

# 窗口从该用户在该路由的首次放行开始。拒绝不会自增，也不会续期。
_CHECK_WINDOW = """
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
if count >= tonumber(ARGV[1]) then
    return math.max(1, redis.call('PTTL', KEYS[1]))
end
if redis.call('INCR', KEYS[1]) == 1 then redis.call('PEXPIRE', KEYS[1], ARGV[2]) end
return 0
"""

# 使用同一 Redis 时钟给所有进程排序；失联进程的令牌到期后自动回收。
_TAKE_SLOT = """
local now = redis.call('TIME')
local millis = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', millis)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then
    local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    return math.max(1, tonumber(oldest[2]) - millis)
end
redis.call('ZADD', KEYS[1], millis + tonumber(ARGV[2]), ARGV[3])
local newest = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')
redis.call('PEXPIRE', KEYS[1], tonumber(newest[2]) - millis)
return 0
"""


def _key(kind, *parts):
    """用命名空间与固定长度摘要定位计数器，路径参数、提示词和令牌不进入键名。"""
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()
    return PREFIX + kind + ":" + digest


def _unavailable():
    """在已配置的共享限流后端失联时返回可重试的服务错误。"""
    return ApiError(
        503, "RATE_LIMIT_UNAVAILABLE", "Traffic limiter unavailable", headers={"Retry-After": "1"}
    )


def _reject(request, code, retry_ms):
    """将流控 429 与任务配额区分，并将毫秒等待时间向上取整为 HTTP 秒数。"""
    request.state.rate_limit_layer = "core"
    seconds = max(1, (retry_ms + 999) // 1000)
    raise ApiError(429, code, "Traffic limit exceeded", headers={"Retry-After": str(seconds)})


class TrafficLimiter:
    def __init__(self, redis_url):
        """有 Redis 时跨 API 进程共享计数；单进程开发环境使用加锁的等价计数器。"""
        self.client = (
            Redis.from_url(
                redis_url,
                socket_connect_timeout=0.25,
                socket_timeout=0.25,
                retry=Retry(NoBackoff(), 0),
            )
            if redis_url
            else None
        )
        self.lock = threading.Lock()
        self.windows = {}
        self.next_sweep = 0
        self.leases = {}

    def close(self):
        """在监听器关闭时回收本进程使用的 Redis 连接。"""
        if self.client is not None:
            self.client.close()

    def user_wait(self, user_id, route, rpm):
        """原子消费当前用户在已匹配路由上的一次额度，返回毫秒等待时间，零表示放行。"""
        if not rpm:
            return 0
        key = _key("rpm", route["listener"], route["method"], route["path"], user_id)
        if self.client is not None:
            try:
                return self.client.eval(_CHECK_WINDOW, 1, key, rpm, WINDOW_MS)
            except RedisError:
                raise _unavailable() from None
        with self.lock:
            now = time.monotonic_ns() // 1_000_000
            if now >= self.next_sweep:
                self.windows = {k: v for k, v in self.windows.items() if v[1] > now}
                self.next_sweep = now + WINDOW_MS
            count, end = self.windows.get(key, (0, now + WINDOW_MS))
            if now >= end:
                count, end = 0, now + WINDOW_MS
            if count >= rpm:
                return end - now
            self.windows[key] = (count + 1, end)
            return 0

    def take_slot(self, provider_id, token, cap, ttl_ms):
        """在目标服务商的共享池中预留唯一令牌，返回可重试时间或零。"""
        key = _key("provider", provider_id)
        if self.client is not None:
            try:
                return self.client.eval(_TAKE_SLOT, 1, key, cap, ttl_ms, token)
            except RedisError:
                raise _unavailable() from None
        with self.lock:
            now = time.monotonic_ns() // 1_000_000
            leases = {t: end for t, end in self.leases.get(key, {}).items() if end > now}
            self.leases[key] = leases
            if len(leases) >= cap:
                return min(leases.values()) - now
            leases[token] = now + ttl_ms
            return 0

    def release_slot(self, provider_id, token):
        """仅移除本次令牌，Redis 失联时依靠租期回收，保留原请求的执行结果。"""
        key = _key("provider", provider_id)
        if self.client is not None:
            try:
                self.client.zrem(key, token)
            except RedisError:
                log.warning("Could not release provider traffic lease; expiry will reclaim it")
            return
        with self.lock:
            leases = self.leases.get(key)
            if leases is not None:
                leases.pop(token, None)
                if not leases:
                    del self.leases[key]

    @asynccontextmanager
    async def provider_slot(self, request, provider_id, deadline):
        """在实际调用期间持有服务商租约，确保取消、异常和迟到的获取结果均可回收。"""
        cap = request.state.traffic_limits["provider_concurrency"]
        if not cap:
            yield
            return
        token = secrets.token_hex(16)
        ttl_ms = (deadline + 15) * 1000
        acquire = asyncio.create_task(
            asyncio.to_thread(self.take_slot, provider_id, token, cap, ttl_ms)
        )

        async def release_after_acquire():
            """等待在途操作结束再归还令牌，避免取消时先释放后获取而泄漏名额。"""
            try:
                if await acquire:
                    return
            except ApiError:
                # 网络超时不能证明 Redis 没有执行获取，仍按唯一令牌尝试释放。
                pass
            await asyncio.to_thread(self.release_slot, provider_id, token)

        try:
            retry = await asyncio.shield(acquire)
            if retry:
                _reject(request, "PROVIDER_CONCURRENCY_LIMIT", retry)
            yield
        finally:
            # 即使 ASGI 取消了请求，也让后台获取与清理按顺序走完。
            with CancelScope(shield=True):
                await asyncio.shield(release_after_acquire())


def check_user_traffic(db, request, user_id):
    """最终鉴权成功后按同一请求只读一次生效规则，重复复核不再重复计数。"""
    from mycomfy.ops.traffic_store import effective_rule

    route = getattr(request.state, "traffic_route", None)
    if not route or not route["user_scoped"] or not request.scope.get("route"):
        return
    if hasattr(request.state, "traffic_limits"):
        return
    limits = effective_rule(db, route)
    request.state.traffic_limits = limits
    request.state.actor = user_id
    retry = request.app.state.traffic_limiter.user_wait(user_id, route, limits["user_rpm"])
    if retry:
        _reject(request, "USER_RATE_LIMIT", retry)
