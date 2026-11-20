"""分发事务内的来源事件，并提供绑定登录会话的用户事件流。"""

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from time import monotonic

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import StreamingResponse

from mycomfy.database.models import (
    Notification,
    SourceEvent,
    User,
    UserEvent,
    UserSequence,
    UserSession,
)
from mycomfy.database.session import db_now, new_id, timestamp
from mycomfy.errors import ApiError, invalid_session, not_found

from .auth import authenticate, check_origin
from .pagination import decode_cursor, encode_cursor

log = logging.getLogger(__name__)
TERMINAL_EVENTS = {"job.succeeded", "job.failed", "job.cancelled"}
PAGE_SIZE = 32
MAX_SEQ = (1 << 63) - 1


def dispatch_one(sessions):
    """锁定最早的待分发事件，在同一事务中提交用户序号、通知和处理标记。"""
    with sessions.begin() as db:
        source = db.scalar(
            select(SourceEvent)
            .where(SourceEvent.dispatched_at.is_(None))
            .order_by(
                SourceEvent.created_at, SourceEvent.job_id, SourceEvent.job_version, SourceEvent.id
            )
            .limit(1)
            .with_for_update()
        )
        if source is None:
            return False
        counter = db.get(UserSequence, source.user_id, with_for_update=True)
        counter.latest += 1
        db.add(
            UserEvent(
                user_id=source.user_id,
                seq=counter.latest,
                source_event_id=source.id,
                type=source.type,
                data=source.data,
            )
        )
        if source.type in TERMINAL_EVENTS:
            db.add(
                Notification(
                    id=new_id("n"),
                    user_id=source.user_id,
                    seq=counter.latest,
                    source_event_id=source.id,
                    type=source.type,
                    data=source.data,
                    created_at=source.created_at,
                )
            )
        source.dispatched_at = db_now(db)
    return True


@asynccontextmanager
async def event_dispatcher(settings, sessions):
    """随公开 API 启停后台分发任务，关闭时等待在途事务结束。"""
    stop = asyncio.Event()

    async def dispatch():
        """扫描待分发来源，数据库暂不可用时按部署间隔重试。"""
        while not stop.is_set():
            try:
                if await asyncio.to_thread(dispatch_one, sessions):
                    continue
            except SQLAlchemyError as exc:
                log.warning("User event delivery will retry (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.poll_seconds)
            except TimeoutError:
                pass

    task = asyncio.create_task(dispatch())
    try:
        yield
    finally:
        stop.set()
        await task


def stream_cursor(last_event_id, after, watermark):
    """按协议优先读取重连请求头，并验证游标位于当前用户水位内。"""
    value = last_event_id if last_event_id is not None else after
    if value is not None and re.fullmatch(r"[0-9]{1,19}", value):
        cursor = int(value)
        if cursor <= watermark:
            return cursor
    raise ApiError(
        400,
        "RESYNC_REQUIRED",
        "Reload the event cursor and snapshots before opening a new stream",
    )


def active_session(sessions, user_id, session_id):
    """用已绑定的服务端会话 ID 检查当前指针、撤销状态和到期时间。"""
    with sessions() as db:
        return (
            db.scalar(
                select(UserSession.id)
                .join(User, User.id == UserSession.user_id)
                .where(
                    UserSession.id == session_id,
                    UserSession.user_id == user_id,
                    User.current_session_id == session_id,
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > db_now(db),
                )
            )
            is not None
        )


def event_page(sessions, user_id, cursor):
    """用短查询按用户序号读取一页已提交事件。"""
    with sessions() as db:
        return db.execute(
            select(UserEvent.seq, UserEvent.type, UserEvent.data)
            .where(UserEvent.user_id == user_id, UserEvent.seq > cursor)
            .order_by(UserEvent.seq)
            .limit(PAGE_SIZE)
        ).all()


def event_frame(kind, data, seq=None):
    """编码 SSE 帧，只有持久事件才携带用于重连的序号。"""
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    prefix = "" if seq is None else f"id: {seq}\n"
    return f"{prefix}event: {kind}\ndata: {body}\n\n"


async def user_stream(sessions, settings, user_id, session_id, cursor):
    """从同一持久游标补发并跟进新事件，撤销会话后只发控制帧。"""
    heartbeat_at = monotonic()
    while True:
        page = await asyncio.to_thread(event_page, sessions, user_id, cursor)
        if page:
            for seq, kind, data in page:
                if not await asyncio.to_thread(active_session, sessions, user_id, session_id):
                    yield event_frame("session.revoked", {})
                    return
                yield event_frame(kind, data, seq)
                cursor = seq
            heartbeat_at = monotonic() + settings.sse_heartbeat_seconds
            continue

        if not await asyncio.to_thread(active_session, sessions, user_id, session_id):
            yield event_frame("session.revoked", {})
            return
        if monotonic() >= heartbeat_at:
            yield ": heartbeat\n\n"
            heartbeat_at = monotonic() + settings.sse_heartbeat_seconds
        await asyncio.sleep(min(settings.poll_seconds, max(0, heartbeat_at - monotonic())))


class EventStreamResponse(StreamingResponse):
    def __init__(self, stream, timeout):
        """设置事件流响应头及每次网络写入允许的最长耗时。"""
        super().__init__(
            stream,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )
        self.timeout = timeout

    async def stream_response(self, send):
        """在每帧发送设置超时，并在断开时释放尚未消费的记录。"""

        async def send_with_deadline(message):
            """在配置的期限内向 ASGI 服务器交付一条消息。"""
            async with asyncio.timeout(self.timeout):
                await send(message)

        # 逐帧等待网络写入，生成器只保留一页记录，超时后释放生成器。
        try:
            await super().stream_response(send_with_deadline)
        finally:
            await self.body_iterator.aclose()


def build_event_router(settings, sessions):
    """创建当前用户的事件水位、订阅、通知列表及已读接口。"""
    router = APIRouter(prefix="/api/v1")

    @router.get("/events/cursor")
    def get_event_cursor(request: Request):
        """返回当前用户已提交的最新事件序号。"""
        with sessions() as db:
            user, _ = authenticate(db, request)
            request.state.actor = user.id
            return {"user_seq": str(db.get(UserSequence, user.id).latest)}

    @router.get("/events")
    def open_stream(
        request: Request,
        session_generation: str | None = None,
        after: str | None = None,
        page_user: str | None = None,
    ):
        """校验页面代次和恢复游标后创建绑定会话的 SSE 响应。"""
        if session_generation is None:
            raise invalid_session()
        with sessions() as db:
            user, session = authenticate(
                db, request, generation=session_generation, page_user=page_user
            )
            request.state.actor = user.id
            cursor = stream_cursor(
                request.headers.get("last-event-id"),
                after,
                db.get(UserSequence, user.id).latest,
            )
            stream = user_stream(sessions, settings, user.id, session.id, cursor)
        return EventStreamResponse(stream, settings.sse_send_timeout_seconds)

    @router.get("/notifications")
    def list_notifications(
        request: Request,
        cursor: str | None = None,
        unread: bool | None = None,
        limit: int = Query(50, ge=1, le=100),
    ):
        """按当前用户和已读筛选倒序分页读取通知。"""
        with sessions() as db:
            user, _ = authenticate(db, request)
            request.state.actor = user.id
            scope = ["notifications", user.id, unread, "seq_desc"]
            position = decode_cursor(cursor, scope)
            statement = select(Notification).where(Notification.user_id == user.id)
            if unread is not None:
                criterion = (
                    Notification.read_at.is_(None) if unread else Notification.read_at.is_not(None)
                )
                statement = statement.where(criterion)
            if position is not None:
                if (
                    len(position) != 1
                    or type(position[0]) is not int
                    or not 1 <= position[0] <= MAX_SEQ
                ):
                    raise ApiError(400, "INVALID_CURSOR", "Invalid notification cursor")
                statement = statement.where(Notification.seq < position[0])
            rows = db.scalars(statement.order_by(Notification.seq.desc()).limit(limit + 1)).all()
            items = [
                {
                    "notification_id": row.id,
                    "user_seq": str(row.seq),
                    "type": row.type,
                    "data": row.data,
                    "created_at": timestamp(row.created_at),
                    "read_at": timestamp(row.read_at),
                }
                for row in rows[:limit]
            ]
            next_cursor = encode_cursor(scope, [rows[limit - 1].seq]) if len(rows) > limit else None
            return {"items": items, "next_cursor": next_cursor}

    @router.put("/notifications/{notification_id}/read", status_code=204)
    def mark_read(request: Request, notification_id: str):
        """验证会话的写入权限后仅记录首次已读时间。"""
        check_origin(request, settings.public_origin)
        with sessions.begin() as db:
            user, _ = authenticate(db, request, write=True)
            request.state.actor = user.id
            row = db.scalar(
                select(Notification)
                .where(Notification.id == notification_id, Notification.user_id == user.id)
                .with_for_update()
            )
            if row is None:
                raise not_found()
            request.state.target = row.id
            if row.read_at is None:
                row.read_at = db_now(db)
        return Response(status_code=204)

    return router
