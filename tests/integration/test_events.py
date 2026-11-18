"""验证持久事件分发、用户游标、会话撤销以及通知已读。"""

import asyncio
import socket
from datetime import timedelta
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from mycomfy.api.auth import COOKIE, digest
from mycomfy.api.events import (
    EventStreamResponse,
    build_event_router,
    dispatch_one,
    event_dispatcher,
    stream_cursor,
    user_stream,
)
from mycomfy.database.models import (
    Base,
    Batch,
    Job,
    Notification,
    SourceEvent,
    StrategySeries,
    StrategyVersion,
    User,
    UserEvent,
    UserSequence,
    UserSession,
)
from mycomfy.database.session import db_now, make_sessions, new_id
from mycomfy.errors import ApiError
from mycomfy.generation.state import emit


@pytest.fixture
def sessions(tmp_path):
    """为每例创建可跨线程访问的临时数据库。"""
    factory = make_sessions(f"sqlite:///{tmp_path / 'events.sqlite'}")
    engine = factory.kw["bind"]
    Base.metadata.create_all(engine)
    yield factory
    engine.dispose()


@pytest.fixture
def settings():
    """提供适合在测试中观察空闲轮询和心跳的短间隔。"""
    return SimpleNamespace(
        poll_seconds=0.01,
        sse_heartbeat_seconds=0.15,
        sse_send_timeout_seconds=1.0,
        public_origin="https://testserver",
    )


def seed(sessions):
    """创建完整的归属链以及一条已提交但尚未分发的任务来源事件。"""
    with sessions.begin() as db:
        now = db_now(db)
        user_id, session_id, token = new_id("u"), new_id("s"), new_id("token")
        series_key, version_id, batch_id, job_id = (
            new_id("series"),
            new_id("sv"),
            new_id("b"),
            new_id("j"),
        )
        db.add(
            User(
                id=user_id,
                username=user_id,
                password_hash="unused",
                session_generation=1,
                current_session_id=session_id,
            )
        )
        db.add(StrategySeries(key=series_key, latest_version=1))
        db.flush()
        db.add_all(
            [
                UserSequence(user_id=user_id, latest=0),
                UserSession(
                    id=session_id,
                    user_id=user_id,
                    token_digest=digest(token),
                    csrf_token="test-csrf",
                    generation=1,
                    expires_at=now + timedelta(hours=1),
                ),
                StrategyVersion(
                    id=version_id,
                    strategy_key=series_key,
                    version=1,
                    name="test",
                    content={},
                    content_digest="a" * 64,
                    published_at=now,
                    request_id=new_id("r"),
                ),
                Batch(
                    id=batch_id,
                    user_id=user_id,
                    client_key=batch_id,
                    request_digest="b" * 64,
                    created_at=now,
                ),
            ]
        )
        db.flush()
        job = Job(
            id=job_id,
            batch_id=batch_id,
            user_id=user_id,
            strategy_version_id=version_id,
            client_key=job_id,
            position=0,
            inputs={},
            state="QUEUED",
            version=1,
            base_priority=0,
            created_at=now,
            enqueued_at=now,
        )
        db.add(job)
        db.flush()
        emit(db, job, now, "job.queued")
    return SimpleNamespace(user_id=user_id, session_id=session_id, token=token, job_id=job_id)


def terminal(sessions, job_id, state="SUCCEEDED"):
    """提交产生站内通知的终态来源，用于分发与补发验证。"""
    with sessions.begin() as db:
        job = db.get(Job, job_id)
        job.state = state
        job.version += 1
        emit(db, job, db_now(db), f"job.{state.lower()}")


def assert_dispatch_recovery(sessions):
    """在写入已刷新而事务还未提交时故障，检查重启后的唯一投递。"""
    ids = seed(sessions)
    assert dispatch_one(sessions)
    terminal(sessions, ids.job_id)

    def interrupt(*args):
        """在发件箱事务提交之前模拟进程终止。"""
        raise RuntimeError("interrupted")

    event.listen(sessions, "after_flush_postexec", interrupt, once=True)
    with pytest.raises(RuntimeError, match="interrupted"):
        dispatch_one(sessions)
    with sessions() as db:
        assert db.get(UserSequence, ids.user_id).latest == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(Notification)
                .where(Notification.user_id == ids.user_id)
            )
            == 0
        )
    assert dispatch_one(sessions)
    assert not dispatch_one(sessions)
    with sessions() as db:
        assert db.get(UserSequence, ids.user_id).latest == 2
        assert db.scalars(
            select(UserEvent.seq).where(UserEvent.user_id == ids.user_id).order_by(UserEvent.seq)
        ).all() == [1, 2]
        notification = db.scalar(select(Notification).where(Notification.user_id == ids.user_id))
        assert notification.seq == 2 and notification.read_at is None
        assert db.get(SourceEvent, notification.source_event_id).dispatched_at is not None


def test_dispatch_rollback_and_restart(sessions):
    """验证事务中断后从来源继续，重复扫描保留原有用户序号。"""
    assert_dispatch_recovery(sessions)


@pytest.mark.mysql
def test_mysql_dispatch_rollback_and_restart(mysql_sessions):
    """在真实 InnoDB 事务上复核事件、通知和序号的一致性。"""
    assert_dispatch_recovery(mysql_sessions)


def test_catchup_then_live_then_revocation(sessions, settings):
    """验证历史到实时交界的新事件会补发，过期连接只接收无游标的撤销帧。"""
    ids = seed(sessions)

    async def observe():
        """以先读快照后跟进事件的顺序模拟一次浏览器订阅。"""
        async with event_dispatcher(settings, sessions):
            stream = user_stream(sessions, settings, ids.user_id, ids.session_id, 0)
            async with asyncio.timeout(3):
                while "id: 1\n" not in await anext(stream):
                    pass
                terminal(sessions, ids.job_id)
                while "id: 2\nevent: job.succeeded" not in await anext(stream):
                    pass
                with sessions.begin() as db:
                    db.get(UserSession, ids.session_id).revoked_at = db_now(db)
                    db.get(User, ids.user_id).current_session_id = None
                assert await anext(stream) == "event: session.revoked\ndata: {}\n\n"
                with pytest.raises(StopAsyncIteration):
                    await anext(stream)

    asyncio.run(observe())


@pytest.mark.parametrize("value", ["-1", "", "1.0", "NaN", "3", "9" * 64, None])
def test_resync_for_invalid_cursor(value):
    """越界或无法解析的游标会明确要求重新取快照。"""
    with pytest.raises(ApiError) as exc:
        stream_cursor(None, value, 2)
    assert exc.value.status == 400 and exc.value.code == "RESYNC_REQUIRED"
    assert stream_cursor("1", value, 2) == 1


def test_notification_api_and_session_generation(sessions, settings):
    """验证通知归属、已读幂等及浏览器持有新 Cookie 的旧页面拒绝重连。"""
    owner, other = seed(sessions), seed(sessions)
    terminal(sessions, owner.job_id)
    while dispatch_one(sessions):
        pass
    app = FastAPI()
    app.include_router(build_event_router(settings, sessions))

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        """将领域异常转换为测试需要断言的 HTTP 状态。"""
        return JSONResponse({"code": exc.code}, status_code=exc.status)

    headers = {
        "origin": settings.public_origin,
        "x-session-generation": "1",
        "x-csrf-token": "test-csrf",
    }
    with TestClient(app, base_url=settings.public_origin) as client:
        client.cookies.set(COOKIE, owner.token)
        assert client.get("/api/v1/events/cursor").json() == {"user_seq": "2"}
        response = client.get("/api/v1/events?session_generation=0&after=0")
        assert response.status_code == 401
        assert client.get("/api/v1/events?session_generation=1&after=3").json() == {
            "code": "RESYNC_REQUIRED"
        }
        result = client.get("/api/v1/notifications?unread=true").json()
        (item,) = result["items"]
        url = f"/api/v1/notifications/{item['notification_id']}/read"
        client.cookies.set(COOKIE, other.token)
        assert client.get("/api/v1/notifications").json()["items"] == []
        assert client.put(url, headers=headers).status_code == 404
        client.cookies.set(COOKIE, owner.token)
        assert client.put(url).status_code == 403
        assert client.put(url, headers=headers).status_code == 204
        first = client.get("/api/v1/notifications?unread=false").json()["items"][0]["read_at"]
        assert first
        assert client.put(url, headers=headers).status_code == 204
        assert (
            client.get("/api/v1/notifications?unread=false").json()["items"][0]["read_at"] == first
        )
        assert client.get("/api/v1/notifications?unread=true").json()["items"] == []


def test_slow_consumer_is_disconnected():
    """网络长期阻塞时关闭连接，生成器持有的资源随即释放。"""
    closed = []

    async def frames():
        """记录生成器是否在发送超时后完成清理。"""
        try:
            yield "event: job.succeeded\n\n"
        finally:
            closed.append(True)

    async def send(message):
        """让响应头正常送出，随后模拟永远停滞的客户端。"""
        if message["type"] == "http.response.body":
            await asyncio.Event().wait()

    async def consume():
        """执行受超时控制的 ASGI 流。"""
        response = EventStreamResponse(frames(), 0.01)
        with pytest.raises(TimeoutError):
            await response.stream_response(send)

    asyncio.run(consume())
    assert closed == [True]


def test_http_stream_replays_and_closes_revoked_connection(sessions, settings):
    """在真实回环 HTTP 连接上验证重连请求头优先、实时分发以及撤销后结束响应。"""
    ids = seed(sessions)
    assert dispatch_one(sessions)

    def lifespan(app):
        """让这个测试服务器使用与公开 API 相同的后台分发器。"""
        return event_dispatcher(settings, sessions)

    app = FastAPI(lifespan=lifespan)
    app.include_router(build_event_router(settings, sessions))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", ws="none"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 5
            while not server.started:
                assert thread.is_alive() and monotonic() < deadline
                sleep(0.01)
            url = f"http://127.0.0.1:{port}/api/v1/events"
            with (
                httpx.Client(timeout=2, trust_env=False, cookies={COOKIE: ids.token}) as client,
                client.stream(
                    "GET",
                    url,
                    params={"after": "0", "session_generation": "1"},
                    headers={"Last-Event-ID": "1"},
                ) as response,
            ):
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                assert response.headers["x-accel-buffering"] == "no"
                lines = iter(response.iter_lines())
                # 初始心跳表明等待的是序号 1 之后的事件，历史事件不会抢先重放。
                assert next(lines) == ": heartbeat"
                terminal(sessions, ids.job_id)
                next_id = next(line for line in lines if line.startswith("id: "))
                assert next_id == "id: 2"
                assert next(lines) == "event: job.succeeded"
                with sessions.begin() as db:
                    db.get(UserSession, ids.session_id).revoked_at = db_now(db)
                remaining = list(lines)
                assert "event: session.revoked" in remaining
                assert not any(line.startswith("id: ") for line in remaining)
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()
