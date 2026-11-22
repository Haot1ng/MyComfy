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
    UserGroupPermission,
    UserSequence,
    UserSession,
)
from mycomfy.database.session import db_now, new_id, timestamp
from mycomfy.errors import ApiError, invalid_session, not_found

from .auth import authenticate, check_origin
from .pagination import decode_cursor, encode_cursor
from .permissions import Permission, permission_query, require_permission

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


def stream_block_reason(sessions, user_id, session_id):
    """检查流绑定的会话和组权限，区分重新登录与刷新权限的原因。"""
    allowed = (
        permission_query(user_id)
        .where(UserGroupPermission.permission == Permission.VIEW_JOBS)
        .exists()
    )
    with sessions() as db:
        result = db.scalar(
            select(allowed)
            .select_from(UserSession)
            .join(User, User.id == UserSession.user_id)
            .where(
                UserSession.id == session_id,
                UserSession.user_id == user_id,
                User.current_session_id == session_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > db_now(db),
            )
        )
    if result is None:
        return "session.revoked"
    if not result:
        return "permissions.changed"
    return None


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


async def user_stream(sessions, settings, user_id, session_id, cursor, *, stop_streams=None):
    """持续补发任务事件；在会话或授权失效、服务停机时结束订阅。"""
    if stop_streams is None:
        stop_streams = asyncio.Event()
    heartbeat_at = monotonic()
    while not stop_streams.is_set():
        page = await asyncio.to_thread(event_page, sessions, user_id, cursor)
        if page:
            for seq, kind, data in page:
                if stop_streams.is_set():
                    return
                reason = await asyncio.to_thread(stream_block_reason, sessions, user_id, session_id)
                if reason:
                    yield event_frame(reason, {})
                    return
                yield event_frame(kind, data, seq)
                cursor = seq
            heartbeat_at = monotonic() + settings.sse_heartbeat_seconds
            continue

        reason = await asyncio.to_thread(stream_block_reason, sessions, user_id, session_id)
        if reason:
            yield event_frame(reason, {})
            return
        if monotonic() >= heartbeat_at:
            yield ": heartbeat\n\n"
            heartbeat_at = monotonic() + settings.sse_heartbeat_seconds
        # 即使部署采用长轮询间隔，停机也应及时发送 EOF；客户端按原游标重连。
        delay = min(settings.poll_seconds, max(0, heartbeat_at - monotonic()))
        try:
            await asyncio.wait_for(stop_streams.wait(), timeout=delay)
        except TimeoutError:
            pass


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


def build_event_router(settings, sessions, *, stop_streams=None):
    """创建当前用户的事件水位、订阅、通知列表及已读接口。"""
    router = APIRouter(prefix="/api/v1")

    @router.get("/events/cursor")
    def get_event_cursor(request: Request):
        """返回当前用户已提交的最新事件序号。"""
        with sessions() as db:
            user, _ = authenticate(db, request)
            request.state.actor = user.id
            require_permission(db, user.id, Permission.VIEW_JOBS)
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
            require_permission(db, user.id, Permission.VIEW_JOBS)
            cursor = stream_cursor(
                request.headers.get("last-event-id"),
                after,
                db.get(UserSequence, user.id).latest,
            )
            stream = user_stream(
                sessions, settings, user.id, session.id, cursor, stop_streams=stop_streams
            )
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
            require_permission(db, user.id, Permission.VIEW_NOTIFICATIONS)
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
            require_permission(db, user.id, Permission.MARK_NOTIFICATIONS_READ, write=True)
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
