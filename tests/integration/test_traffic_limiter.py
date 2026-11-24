"""验证真实 Redis 的跨进程限流语义，以及无 Redis 单进程开发模式的相同行为。"""

import asyncio
import os
import secrets
import socket
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from threading import Event
from time import sleep
from types import SimpleNamespace

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from mycomfy.api import traffic_limiter
from mycomfy.api.traffic_limiter import TrafficLimiter
from mycomfy.errors import ApiError

ROUTE = {"listener": "public", "method": "GET", "path": "/api/v1/jobs/{job_id}"}


@pytest.fixture(params=["local", "redis"])
def gates(request):
    """本地用一个共享实例；真实 Redis 用两个独立连接池模拟不同 API 进程。"""
    url = None
    if request.param == "redis":
        url = os.environ.get("MYCOMFY_TEST_REDIS_URL")
        if not url:
            pytest.skip("未配置专用 MYCOMFY_TEST_REDIS_URL")
    with ExitStack() as stack:
        gate = stack.enter_context(closing(TrafficLimiter(url)))
        second = stack.enter_context(closing(TrafficLimiter(url))) if url else gate
        yield gate, second


def test_user_window_is_atomic_and_isolated(gates):
    """两个调用方并发竞争同一窗口，只放行配额内的次数；其他用户、模板和方法独立。"""
    owner = secrets.token_hex(16)

    def attempt(index):
        """从不同连接执行同一用户名及固定模板的原子计数。"""
        return gates[index % 2].user_wait(owner, ROUTE, 5)

    with ThreadPoolExecutor(max_workers=12) as executor:
        waits = list(executor.map(attempt, range(40)))
    assert waits.count(0) == 5
    assert all(0 < wait <= 60000 for wait in waits if wait)
    gate = gates[0]
    assert gate.user_wait(owner + "b", ROUTE, 1) == 0
    for dimension, changed in (("listener", "admin"), ("method", "HEAD"), ("path", "/api/v1/me")):
        assert gate.user_wait(owner, {**ROUTE, dimension: changed}, 1) == 0
    # 拒绝不增加已用次数；把上限从 5 调到 6 恰能再放行一次。
    assert gate.user_wait(owner, ROUTE, 6) == 0
    assert gate.user_wait(owner, ROUTE, 6) > 0
    assert gate.user_wait(owner, ROUTE, 0) == 0


def test_global_provider_slots_are_token_scoped(gates):
    """服务商各有共享池；重复释放及已过期持有者的迟到释放均不会归还他人的名额。"""
    provider = secrets.token_hex(16)
    tokens = [secrets.token_hex(16) for _ in range(20)]

    def attempt(index):
        """每个请求用自己的令牌竞争同一服务商的三个名额。"""
        return gates[index % 2].take_slot(provider, tokens[index], 3, 2000)

    with ThreadPoolExecutor(max_workers=12) as executor:
        waits = list(executor.map(attempt, range(len(tokens))))
    held = [token for token, wait in zip(tokens, waits, strict=True) if not wait]
    assert len(held) == 3
    gate, second = gates
    assert second.take_slot(provider + "b", "other-provider", 1, 1000) == 0
    for token in held:
        gate.release_slot(provider, token)
    assert gate.take_slot(provider, "old", 1, 40) == 0
    sleep(0.08)
    assert second.take_slot(provider, "new", 1, 1000) == 0
    gate.release_slot(provider, "old")
    gate.release_slot(provider, "old")
    assert second.take_slot(provider, "another", 1, 1000) > 0
    second.release_slot(provider, "new")


def test_user_window_expires_without_renewal(gates, monkeypatch):
    """缩短窗口以验证真实到期和再次放行，持续被拒的请求不延长窗口。"""
    owner = secrets.token_hex(16)
    monkeypatch.setattr(traffic_limiter, "WINDOW_MS", 500)
    gate, second = gates
    assert gate.user_wait(owner, ROUTE, 1) == 0
    assert 0 < second.user_wait(owner, ROUTE, 1) <= 500
    sleep(0.55)
    assert second.user_wait(owner, ROUTE, 1) == 0


def test_provider_slot_released_on_exception_or_cancellation(gates):
    """正在调用和正在获取名额时取消都不泄漏，并发拒绝携带业务限流标记及重试时间。"""
    gate, other = gates
    provider = secrets.token_hex(16)
    request = SimpleNamespace(state=SimpleNamespace(traffic_limits={"provider_concurrency": 1}))

    async def exercise():
        """验证原始异常及取消传到调用者，后面的请求立即能再次获取唯一名额。"""
        started = asyncio.Event()

        async def in_flight():
            """模拟尚未返回的外部模型请求。"""
            async with gate.provider_slot(request, provider, 45):
                started.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(in_flight())
        await started.wait()
        with pytest.raises(ApiError) as denied:
            async with other.provider_slot(request, provider, 45):
                pytest.fail("并发已满")
        assert denied.value.status == 429
        assert 1 <= int(denied.value.headers["Retry-After"]) <= 60
        assert request.state.rate_limit_layer == "core"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ValueError, match="model failed"):
            async with other.provider_slot(request, provider, 45):
                raise ValueError("model failed")
        async with gate.provider_slot(request, provider, 45):
            pass

    asyncio.run(exercise())


def test_cancel_during_acquire_waits_for_safe_release(monkeypatch, gates):
    """取消在途的获取时先等操作落定，再用原令牌释放，不提前释放或泄漏名额。"""
    gate, other = gates
    provider = secrets.token_hex(16)
    request = SimpleNamespace(state=SimpleNamespace(traffic_limits={"provider_concurrency": 1}))
    entered, proceed = Event(), Event()
    take = gate.take_slot

    def delayed(*args):
        """把获取阻塞在工作线程内，允许事件循环在 Redis 请求仍在途时取消。"""
        entered.set()
        assert proceed.wait(3)
        return take(*args)

    async def holder():
        """取消应直接退出，不进入本应持有名额的调用体。"""
        async with gate.provider_slot(request, provider, 45):
            pytest.fail("已取消的模型调用不应执行")

    async def exercise():
        """控制取消顺序，并等待异步清理实际完成。"""
        task = asyncio.create_task(holder())
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr(gate, "take_slot", delayed)
    asyncio.run(exercise())
    assert other.take_slot(provider, "next", 1, 200) == 0


def test_redis_outage_fails_closed_and_preserves_upstream_failure(monkeypatch):
    """共享后端故障时拒绝受限请求；未启用的规则仍放行，清理错误不替换模型异常。"""
    with closing(TrafficLimiter("redis://localhost:1/0")) as gate:

        def outage(*args, **kwargs):
            """模拟 Redis 不可达，避免向宿主固定端口发送真实请求。"""
            raise RedisConnectionError("unavailable")

        monkeypatch.setattr(gate.client, "eval", outage)
        monkeypatch.setattr(gate.client, "zrem", outage)
        assert gate.user_wait("u", ROUTE, 0) == 0
        with pytest.raises(ApiError) as failed:
            gate.user_wait("u", ROUTE, 1)
        assert failed.value.status == 503 and failed.value.code == "RATE_LIMIT_UNAVAILABLE"
        request = SimpleNamespace(state=SimpleNamespace(traffic_limits={"provider_concurrency": 1}))

        async def exercise():
            """获取失败不调用模型；耗尽租期前清理失败保留已产生的错误。"""
            with pytest.raises(ApiError) as unavailable:
                async with gate.provider_slot(request, "p", 45):
                    pytest.fail("Redis 失联")
            assert unavailable.value.status == 503
            monkeypatch.setattr(gate.client, "eval", lambda *args: 0)
            with pytest.raises(ValueError, match="upstream"):
                async with gate.provider_slot(request, "p", 45):
                    raise ValueError("upstream")

        asyncio.run(exercise())


def test_unreachable_shared_redis_never_falls_back_to_local_quota():
    """共享后端端口不可连接时，真实 Redis 客户端返回 503，避免多副本误用独立额度。"""
    # 占住临时端口但不 listen，确保此测试既无外网依赖也不会接入另一服务。
    with socket.socket() as endpoint:
        endpoint.bind(("127.0.0.1", 0))
        port = endpoint.getsockname()[1]
        with closing(TrafficLimiter(f"redis://127.0.0.1:{port}/0")) as gate:
            with pytest.raises(ApiError) as failed:
                gate.user_wait("owner", ROUTE, 1)
            assert failed.value.status == 503 and failed.value.headers["Retry-After"] == "1"
            assert gate.user_wait("owner", ROUTE, 0) == 0
