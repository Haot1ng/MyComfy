"""公开 API 与通过本机监听器提供的管理应用。"""

import hmac
import ipaddress
import logging
import os
import re
import stat
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated
from urllib.parse import urlsplit

from anyio import CancelScope
from fastapi import APIRouter, FastAPI, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import defer, load_only
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from mycomfy.artifacts.publication import reconcile_asset
from mycomfy.artifacts.storage import LocalStore, content_response
from mycomfy.database.models import (
    Asset,
    Audit,
    Batch,
    Job,
    StrategySeries,
    StrategyVersion,
    User,
    UserSession,
)
from mycomfy.database.session import db_now, make_sessions, new_id
from mycomfy.errors import ApiError, InvalidSession, not_found
from mycomfy.generation import jobs
from mycomfy.generation.strategy import json_pointer, sql_publish, strategy_payload
from mycomfy.ops.capacity import Capacity

from . import auth
from .events import build_event_router, event_dispatcher
from .group_admin import build_group_admin_router
from .pagination import invalid_cursor, page, read_position
from .permissions import Permission, access_profile, has_permission, require_permission
from .session_cache import SessionPrecheckCache
from .web import install_web_routes

logger = logging.getLogger(__name__)
Limit = Annotated[int, Query(ge=1, le=100)]
Password = Annotated[str, Field(min_length=1, max_length=1024)]


class Input(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Credentials(Input):
    username: str = Field(min_length=1, max_length=100)
    password: Password


class PasswordChange(Input):
    current_password: Password
    new_password: Password


class PasswordReset(Input):
    new_password: Password


class Cancel(Input):
    reason: str | None = Field(None, max_length=255)


class RetryJob(Input):
    expected_job_version: int = Field(gt=0, le=2**63 - 1)


def _error(request, exc):
    """将业务错误、字段明细和本次请求标识编码为统一响应。"""
    return JSONResponse(
        {
            "error": {"code": exc.code, "message": str(exc), "details": exc.details},
            "request_id": request.state.request_id,
        },
        status_code=exc.status,
        headers=exc.headers,
    )


def _user_admin_operation(request):
    """识别用户列表、分组目录和单个用户的分组读写，不匹配其他维护路由。"""
    path = request.scope["path"]
    if path in {"/local/v1/users", "/local/v1/groups"}:
        return request.method == "GET"
    return request.method in {"GET", "PUT"} and bool(
        re.fullmatch(r"/local/v1/users/[^/]+/groups", path)
    )


def _local_request(request, test_origin):
    """校验管理监听器的直连对端和目标 Host，识别配置的 HTTPS 浏览器来源。"""

    def loopback(host):
        """识别回环 IP 或本机主机名。"""
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return host == "localhost"

    hosts = request.headers.getlist("host")
    if len(hosts) != 1 or not request.client or not loopback(request.client.host):
        raise ApiError(403, "LOCAL_ONLY", "Loopback access required")

    # 隧道在本机终止 TLS 并保留原始 Host；转发头不能用来选择信任的访问来源。
    raw_host = hosts[0]
    if test_origin:
        configured = urlsplit(test_origin)
        test_host = raw_host.lower()
        if test_host == configured.netloc or (
            configured.port is None and test_host == configured.netloc + ":443"
        ):
            origins = request.headers.getlist("origin")
            if origins and origins != [test_origin]:
                raise ApiError(403, "ORIGIN_REJECTED", "Request origin is not allowed")
            return test_origin

    # Funnel 标记只用于拒绝降级：即使隧道请求携带 localhost Host，也不能走本机维护路径。
    if "tailscale-funnel-request" in request.headers:
        raise ApiError(403, "LOCAL_ONLY", "Loopback access required")
    try:
        host = urlsplit("//" + raw_host)
        _ = host.port
        if host.username or host.password or host.path or host.query or host.fragment:
            raise ValueError
        if not loopback(host.hostname):
            raise ValueError
    except (TypeError, ValueError):
        raise ApiError(403, "LOCAL_ONLY", "Loopback access required") from None
    origins = request.headers.getlist("origin")
    if origins and origins != [f"{request.url.scheme}://{raw_host}"]:
        raise ApiError(403, "ORIGIN_REJECTED", "Request origin is not allowed")
    return None


def _maintenance(request, settings, media_type, test_origin):
    """按页面和操作核验浏览器同源交互，或用本机维护凭据授权程序调用。"""
    page = request.headers.getlist("x-mycomfy-admin")
    templates = (
        page == ["templates"]
        and request.method == "POST"
        and request.scope["path"] == "/local/v1/strategy-versions"
    )
    users = page == ["users"] and _user_admin_operation(request)
    if (templates or users) and "authorization" not in request.headers:
        origin = test_origin or f"{request.url.scheme}://{request.headers['host']}"
        origins = request.headers.getlist("origin")
        sites = request.headers.getlist("sec-fetch-site")
        # 浏览器同源 GET 常省略 Origin；仅在浏览器明确标识同源读取时接受该情形。
        same_origin = origins == [origin] or (
            request.method == "GET" and not origins and sites == ["same-origin"]
        )
        if not same_origin or sites not in ([], ["same-origin"]):
            raise ApiError(403, "ORIGIN_REJECTED", "Same-origin admin page required")
        if request.method != "GET" and (
            media_type != "application/json" or len(request.headers.getlist("content-type")) != 1
        ):
            raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Expected application/json")
        location = "test" if test_origin else "local"
        area = "template" if templates else "user"
        request.state.actor = f"{location}-{area}-admin"
        return

    # 两种管理入口都使用同一个维护凭据，带有显式凭据的请求不能回退到页面授权。
    path = settings.maintenance_token_file
    if path is None:
        raise ApiError(503, "MAINTENANCE_DISABLED", "Maintenance credentials are unavailable")
    try:
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise OSError
            token = stream.read(4097).strip()
    except OSError:
        raise ApiError(
            503, "MAINTENANCE_DISABLED", "Maintenance credentials are unavailable"
        ) from None
    if not token or len(token) > 4096:
        raise ApiError(503, "MAINTENANCE_DISABLED", "Maintenance credentials are unavailable")
    supplied = request.headers.get("authorization", "").encode()
    if not hmac.compare_digest(b"Bearer " + token, supplied):
        raise ApiError(403, "MAINTENANCE_FORBIDDEN", "Maintenance credentials required")
    request.state.actor = "test-maintenance" if test_origin else "local-maintenance"


def _audit(db, request, status):
    """在当前事务中记录资源、调用者、状态和耗时，不记录请求体或令牌。"""
    route = request.scope.get("route")
    action = getattr(route, "path", "<unmatched>")
    target = getattr(request.state, "target", None)
    if target is None:
        # 使用已匹配的资源路径参数建立查询链，忽略查询串与原始路径中的敏感内容。
        for name in ("job_id", "batch_id", "asset_id", "version_id", "notification_id", "user_id"):
            if name in request.path_params:
                target = str(request.path_params[name])[:48]
                break
    db.add(
        Audit(
            id=request.state.request_id,
            actor=getattr(request.state, "actor", None),
            action=f"{request.method} {action}"[:160],
            target=target,
            result=status,
            duration_ms=int((time.monotonic() - request.state.started) * 1000),
            created_at=db_now(db),
        )
    )


def _session(db, request, *, permission=None, **options):
    """验证会话、关联审计主体，并按路由要求核验组授权。"""
    user, session = auth.authenticate(db, request, **options)
    request.state.actor = user.id
    if permission is not None:
        require_permission(db, user.id, permission, write=options.get("write", False))
    return user, session


def _cookie(response, settings, token):
    """写入带有效期和浏览器保护属性的主机专属会话 Cookie。"""
    response.set_cookie(
        auth.COOKIE,
        token,
        max_age=settings.session_seconds,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
        path="/",
    )


def create_app(settings, *, local=False, sessions=None, stop_streams=None):
    """按监听用途装配公开或本机应用，并注入会话工厂和容量策略。"""
    sessions = sessions if sessions is not None else make_sessions(settings.database_url)
    capacity = Capacity.from_settings(settings)
    cache = (
        SessionPrecheckCache(settings.redis_url, settings.session_cache_ttl_seconds)
        if settings.redis_url
        else None
    )

    @asynccontextmanager
    async def lifespan(app):
        """启动时恢复容量预留，并在公开 API 生命周期内运行持久事件分发器。"""
        try:
            await run_in_threadpool(capacity.initialize, sessions)
            if local:
                yield
            else:
                async with event_dispatcher(settings, sessions):
                    yield
        finally:
            if cache is not None:
                await run_in_threadpool(cache.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessions = sessions

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        """转换业务异常，在数据库会话退出后修复已确认失效的缓存。"""
        if (
            cache is not None
            and isinstance(exc, InvalidSession)
            and exc.inactive_token_digest is not None
            and exc.inactive_token_digest
            == getattr(request.state, "prechecked_session_digest", None)
        ):
            await run_in_threadpool(cache.invalidate, exc.inactive_token_digest)
        return _error(request, exc)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        """将结构校验错误转换为字段路径，并保留批次中的任务位置。"""
        details = []
        bad_json = False
        for issue in exc.errors():
            loc = list(issue["loc"])
            if loc and loc[0] == "body":
                loc.pop(0)
            detail = {"path": json_pointer(loc), "code": issue["type"]}
            if len(loc) > 1 and loc[0] == "tasks" and isinstance(loc[1], int):
                detail["index"] = loc[1]
                if isinstance(exc.body, dict):
                    tasks = exc.body.get("tasks")
                    if (
                        isinstance(tasks, list)
                        and loc[1] < len(tasks)
                        and isinstance(tasks[loc[1]], dict)
                    ):
                        key = tasks[loc[1]].get("client_task_key")
                        if isinstance(key, str):
                            detail["client_task_key"] = key
            details.append(detail)
            bad_json |= issue["type"] == "json_invalid"
        error = (
            ApiError(400, "INVALID_JSON", "Malformed JSON")
            if bad_json
            else ApiError(422, "VALIDATION_FAILED", "Check request fields", details)
        )
        return _error(request, error)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        """为路由和协议层错误附加统一错误码及请求标识。"""
        code = {
            400: "BAD_REQUEST",
            404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
            415: "UNSUPPORTED_MEDIA_TYPE",
        }.get(exc.status_code, "HTTP_ERROR")
        return _error(
            request, ApiError(exc.status_code, code, str(exc.detail), headers=exc.headers)
        )

    def finish_audit(request, status):
        """为尚未随业务事务记账的请求补写审计。"""
        with sessions.begin() as db:
            # 业务事务已写审计时复用同一记录，避免重复登记。
            if db.get(Audit, request.state.request_id) is None:
                _audit(db, request, status)

    def check_page_write(request):
        """优先从 Redis 预检写请求，未命中时读取 MySQL 并回填。"""
        if cache is not None:
            token_digest = auth.cookie_digest(request)
            cached = cache.get(token_digest)
            if cached is not None:
                auth.check_page_binding(request, cached.user_id, cached.generation)
                auth.check_write_headers(request, cached.generation, cached.csrf_digest)
                request.state.actor = cached.user_id
                request.state.prechecked_session_digest = token_digest
                return
        with sessions() as db:
            _, session = _session(db, request, write=True)
        # 数据库预检会锁定用户行，结束事务后再尝试写缓存。
        if cache is not None:
            cache.remember(session)
            request.state.prechecked_session_digest = session.token_digest

    @app.middleware("http")
    async def request_scope(request: Request, call_next):
        """分配请求标识，在路由前核验访问边界并在返回前登记审计。"""
        request.state.request_id = new_id("r")
        request.state.started = time.monotonic()
        path = request.scope["path"]
        media_type = request.headers.get("content-type", "").split(";", 1)[0].lower().strip()
        try:
            if local:
                test_origin = _local_request(request, settings.admin_test_origin)
                if path.startswith("/local/v1/") and (
                    request.method not in {"GET", "HEAD", "OPTIONS"}
                    or path.rstrip("/") in {"/local/v1/groups", "/local/v1/users"}
                    or path.startswith(("/local/v1/groups/", "/local/v1/users/"))
                ):
                    await run_in_threadpool(
                        _maintenance, request, settings, media_type, test_origin
                    )
            elif path.startswith("/api/v1/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
                auth.check_origin(request, settings.public_origin)
                # 避免失效标签页或无凭据调用在参数校验前占用解析资源。
                if request.method == "POST" and (
                    path in {"/api/v1/me/password", "/api/v1/batches"}
                    or (path.startswith("/api/v1/jobs/") and path.endswith(("/cancel", "/retry")))
                ):
                    await run_in_threadpool(check_page_write, request)
            if request.method in {"POST", "PUT"} and path.startswith(("/api/v1/", "/local/v1/")):
                if path == "/api/v1/assets":
                    if media_type != "multipart/form-data":
                        raise ApiError(
                            415, "UNSUPPORTED_MEDIA_TYPE", "Expected multipart/form-data"
                        )
                elif media_type and media_type != "application/json":
                    raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Expected application/json")
            response = await call_next(request)
        except ApiError as exc:
            response = await api_error(request, exc)
        except SQLAlchemyError:
            logger.error("Database operation failed for %s", request.state.request_id)
            response = _error(
                request, ApiError(503, "DATABASE_UNAVAILABLE", "Database unavailable")
            )
        except Exception as exc:  # noqa: BLE001 — HTTP 边界统一登记未处理异常
            # 原始异常可能含 SQL 参数或认证字段，仅记录异常类别和追踪号。
            logger.error(
                "HTTP handler %s failed for %s", type(exc).__name__, request.state.request_id
            )
            response = _error(request, ApiError(500, "INTERNAL_ERROR", "Unexpected server error"))
        if getattr(request.state, "web_asset", False):
            response.headers["Cache-Control"] = "no-cache"
            if local and (path == "/" or path.endswith(".html")):
                # 管理页面统一禁止被其他页面嵌入，防止诱导点击权限操作。
                response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        else:
            try:
                await run_in_threadpool(finish_audit, request, response.status_code)
            except SQLAlchemyError:
                logger.error("Audit unavailable for %s", request.state.request_id)
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    install_web_routes(app, local=local)
    if local:
        from mycomfy.ops.monitor import build_monitor_router

        app.include_router(_local_routes(settings, sessions, cache))
        app.include_router(build_group_admin_router(sessions, _audit))
        app.include_router(build_monitor_router(settings, sessions))
    else:
        store = LocalStore(settings.data_root)
        app.include_router(_public_routes(settings, sessions, store, capacity, cache))
        app.include_router(build_event_router(settings, sessions, stop_streams=stop_streams))
    return app


def _latest_strategies():
    """只选择每个策略系列的当前版本，供工作台与本机模板列表使用。"""
    return (
        select(StrategyVersion)
        .join(StrategySeries)
        .where(StrategyVersion.version == StrategySeries.latest_version)
    )


def _local_routes(settings, sessions, cache):
    """构造本机策略发布与维护重置密码路由。"""
    router = APIRouter(prefix="/local/v1")

    @router.post("/strategy-versions", status_code=201)
    def publish(request: Request, body: dict, create_only: bool = False):
        """在策略版本事务中写入内容摘要、审计和版本号。"""
        with sessions.begin() as db:
            version = sql_publish(db, body, request.state.request_id, create_only=create_only)
            request.state.target = version.id
            _audit(db, request, 201)
            return {
                "strategy_version_id": version.id,
                "strategy_key": version.strategy_key,
                "version": version.version,
                "content_digest": version.content_digest,
            }

    @router.get("/strategies")
    def published_templates(limit: Limit = 50, cursor: str | None = None):
        """按发布时间倒序分页读取各模板最新版本的名称、标识和版本号。"""
        scope = ["local-strategies", "published_at.desc", "id.desc"]
        position = read_position(cursor, scope, str, str)
        # 列表只需元数据，避免从数据库加载可能很大的工作流与模型清单。
        query = _latest_strategies().options(
            load_only(
                StrategyVersion.id,
                StrategyVersion.strategy_key,
                StrategyVersion.name,
                StrategyVersion.version,
                StrategyVersion.published_at,
            )
        )
        if position:
            try:
                at = datetime.fromisoformat(position[0])
            except ValueError:
                raise invalid_cursor() from None
            query = query.where(
                or_(
                    StrategyVersion.published_at < at,
                    and_(StrategyVersion.published_at == at, StrategyVersion.id < position[1]),
                )
            )
        with sessions() as db:
            return page(
                db,
                query.order_by(StrategyVersion.published_at.desc(), StrategyVersion.id.desc()),
                limit,
                scope,
                lambda row: [row.published_at.isoformat(), row.id],
                lambda row: {
                    "strategy_key": row.strategy_key,
                    "strategy_version_id": row.id,
                    "name": row.name,
                    "version": row.version,
                },
            )

    @router.get("/strategy-versions/{version_id}")
    def published_template(version_id: str):
        """按不可变版本 ID 读取完整策略，供管理页预览节点或再次发布。"""
        with sessions() as db:
            # 列表只取摘要，选定模板后才读取该版本的工作流、绑定和输出契约。
            content = db.scalar(
                select(StrategyVersion.content).where(StrategyVersion.id == version_id)
            )
            if content is None:
                raise not_found()
            return content

    @router.post("/users/{user_id}/password-reset", status_code=204)
    def reset(user_id: str, request: Request, body: PasswordReset):
        """由本机维护者重置密码并在同一事务撤销旧会话。"""
        request.state.target = user_id[:48]
        new_hash = auth.password_hash(body.new_password)
        retired_digest = None
        with sessions.begin() as db:
            user = db.get(User, user_id, with_for_update=True)
            if user is None:
                raise not_found()
            user.password_hash = new_hash
            user.credential_version += 1
            if user.current_session_id:
                retired_digest = auth.revoke_session(
                    db, user, db.get(UserSession, user.current_session_id)
                )
            _audit(db, request, 204)
        if cache is not None:
            cache.invalidate(retired_digest)
        return Response(status_code=204)

    return router


def _public_routes(settings, sessions, store, capacity, cache):
    """构造账号、策略、素材、任务及批次的用户 HTTP 路由。"""
    router = APIRouter(prefix="/api/v1")

    @router.post("/accounts", status_code=201)
    def register(request: Request, body: Credentials):
        """注册规范化登录名并返回稳定的用户 ID。"""
        with sessions() as db:
            user_id = auth.register(db, body.username, body.password)
        request.state.actor = user_id
        return {"user_id": user_id}

    @router.post("/sessions")
    def login(request: Request, body: Credentials, response: Response):
        """验证登录凭据，轮换当前会话并返回页面代次和 CSRF 令牌。"""
        source = request.client.host if request.client else "unknown"
        with sessions() as db:
            payload, token, retired_digest = auth.login(
                db, body.username, body.password, source, settings.session_seconds
            )
        if cache is not None:
            cache.invalidate(retired_digest)
        request.state.actor = payload["user_id"]
        _cookie(response, settings, token)
        return payload

    @router.get("/me")
    def me(request: Request):
        """返回当前会话，以及前端判断页面和按钮所需的分组与权限。"""
        with sessions() as db:
            user, session = _session(db, request)
            return {**auth.session_payload(user, session), **access_profile(db, user.id)}

    @router.delete("/sessions/current", status_code=204)
    def logout(request: Request):
        """只撤销已匹配的会话，允许旧退出请求幂等重放。"""
        with sessions.begin() as db:
            user, session = _session(db, request, write=True, allow_revoked=True)
            retired_digest = auth.revoke_session(db, user, session)
            _audit(db, request, 204)
        if cache is not None:
            cache.invalidate(retired_digest)
        return Response(status_code=204)

    @router.post("/me/password")
    def change_password(request: Request, body: PasswordChange, response: Response):
        """校验旧密码，更新凭据版本并为当前浏览器轮换会话。"""
        with sessions() as db:
            user, _ = _session(db, request, write=True)
            current = user.password_hash
        if not auth.verify_password(current, body.current_password):
            raise ApiError(401, "AUTH_FAILED", "Invalid current password")
        new_hash = auth.password_hash(body.new_password)
        with sessions.begin() as db:
            # 慢哈希比较在事务外完成，提交前复核会话和原密码摘要。
            user, _ = _session(db, request, write=True)
            if current != user.password_hash:
                raise ApiError(401, "SESSION_INVALID", "Credentials have changed")
            user.password_hash = new_hash
            user.credential_version += 1
            payload, token, retired_digest = auth.rotate_session(db, user, settings.session_seconds)
            _audit(db, request, 200)
        if cache is not None:
            cache.invalidate(retired_digest)
        _cookie(response, settings, token)
        return payload

    @router.get("/strategies")
    def strategies(request: Request, limit: Limit = 50, cursor: str | None = None):
        """按系列键分页查询每个系列当前发布的最新版本。"""
        with sessions() as db:
            user, _ = _session(db, request, permission=Permission.VIEW_STRATEGIES)
            scope = [user.id, "strategies", "strategy_key.asc"]
            pos = read_position(cursor, scope, str)
            query = _latest_strategies()
            if pos:
                query = query.where(StrategyVersion.strategy_key > pos[0])
            return page(
                db,
                query.order_by(StrategyVersion.strategy_key),
                limit,
                scope,
                lambda v: [v.strategy_key],
                lambda v: strategy_payload(db, v),
            )

    @router.get("/strategies/{key}/versions")
    def versions(key: str, request: Request, limit: Limit = 50, cursor: str | None = None):
        """按版本号倒序分页返回指定系列的历史版本。"""
        with sessions() as db:
            user, _ = _session(db, request, permission=Permission.VIEW_STRATEGIES)
            scope = [user.id, "versions", key, "version.desc"]
            pos = read_position(cursor, scope, int)
            query = select(StrategyVersion).where(StrategyVersion.strategy_key == key)
            if pos:
                query = query.where(StrategyVersion.version < pos[0])
            return page(
                db,
                query.order_by(StrategyVersion.version.desc()),
                limit,
                scope,
                lambda v: [v.version],
                lambda v: strategy_payload(db, v),
            )

    @router.get("/strategy-versions/{version_id}")
    def version(version_id: str, request: Request):
        """查询单个策略版本的 Schema、摘要及当前可受理性。"""
        with sessions() as db:
            _session(db, request, permission=Permission.VIEW_STRATEGIES)
            row = db.get(StrategyVersion, version_id)
            if row is None:
                raise not_found()
            return strategy_payload(db, row)

    @router.post("/batches", status_code=202)
    def submit(request: Request, body: jobs.BatchRequest):
        """原子受理整批任务并保存请求与批次之间的审计关联。"""
        with sessions.begin() as db:
            user, _ = _session(db, request, write=True, permission=Permission.SUBMIT_JOBS)
            payload = jobs.admit_batch(db, user, body, settings.max_batch_tasks, store, capacity)
            request.state.target = payload["batch_id"]
            _audit(db, request, 202)
            return payload

    @router.get("/batches/{batch_id}")
    def batch(batch_id: str, request: Request, limit: Limit = 50, cursor: str | None = None):
        """返回当前用户指定批次的状态汇总及按提交顺序排列的一页任务。"""
        with sessions() as db:
            user, _ = _session(db, request, permission=Permission.VIEW_JOBS)
            jobs.resource(db, Batch, batch_id, user.id)
            scope = [user.id, "batch", batch_id, "position.asc"]
            pos = read_position(cursor, scope, int)
            query = select(Job).options(defer(Job.inputs)).where(Job.batch_id == batch_id)
            if pos:
                query = query.where(Job.position > pos[0])
            counts = dict(
                db.execute(
                    select(Job.state, func.count())
                    .where(Job.batch_id == batch_id)
                    .group_by(Job.state)
                ).all()
            )
            return {
                "batch_id": batch_id,
                "counts": counts,
                **page(
                    db,
                    query.order_by(Job.position),
                    limit,
                    scope,
                    lambda j: [j.position],
                    lambda j: jobs.job_payload(db, j, details=False),
                ),
            }

    @router.get("/jobs")
    def list_jobs(
        request: Request,
        limit: Limit = 50,
        cursor: str | None = None,
        state: str | None = None,
        batch_id: str | None = None,
    ):
        """按状态和批次筛选用户任务，并用时间及唯一 ID 续读。"""
        with sessions() as db:
            user, _ = _session(db, request, permission=Permission.VIEW_JOBS)
            scope = [user.id, "jobs", state, batch_id, "created_at.desc,id.desc"]
            pos = read_position(cursor, scope, str, str)
            query = select(Job).options(defer(Job.inputs)).where(Job.user_id == user.id)
            if pos:
                try:
                    at = datetime.fromisoformat(pos[0])
                    if at.tzinfo is not None:
                        raise ValueError
                except ValueError:
                    raise invalid_cursor() from None
                query = query.where(
                    or_(Job.created_at < at, and_(Job.created_at == at, Job.id < pos[1]))
                )
            if state is not None:
                query = query.where(Job.state == state)
            if batch_id is not None:
                query = query.where(Job.batch_id == batch_id)
            return page(
                db,
                query.order_by(Job.created_at.desc(), Job.id.desc()),
                limit,
                scope,
                lambda j: [j.created_at.isoformat(), j.id],
                lambda j: jobs.job_payload(db, j, details=False),
            )

    @router.get("/jobs/{job_id}")
    def job_detail(job_id: str, request: Request):
        """返回用户可访问的任务状态、关联执行和可见文件。"""
        with sessions() as db:
            user, _ = _session(db, request, permission=Permission.VIEW_JOBS)
            job = jobs.resource(db, Job, job_id, user.id, options=(defer(Job.inputs),))
            return {
                **jobs.job_payload(db, job),
                "manual_retry_count": job.manual_retry_count,
                "manual_retry_limit": settings.manual_retry_limit,
            }

    @router.get("/jobs/{job_id}/original")
    def job_original(job_id: str, request: Request):
        """按需读取本人的任务受理参数，并按文件读取权限附带输入素材元数据。"""
        with sessions() as db:
            user, _ = _session(db, request, permission=Permission.VIEW_JOBS)
            job = jobs.resource(db, Job, job_id, user.id)
            can_download = has_permission(db, user.id, Permission.DOWNLOAD_ASSETS)
            return jobs.original_payload(db, job, can_download=can_download)

    @router.post("/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str, request: Request, body: RetryJob):
        """以当前用户的提交权限和所见版本重新受理本人已失败的任务。"""
        with sessions.begin() as db:
            user, _ = _session(db, request, write=True, permission=Permission.SUBMIT_JOBS)
            request.state.target = job_id
            result = jobs.retry_job(
                db,
                job_id,
                user.id,
                body.expected_job_version,
                settings.manual_retry_limit,
                store,
                capacity,
            )
            _audit(db, request, 202)
            return result

    @router.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str, request: Request, body: Cancel | None = None):
        """为非终态任务原子登记取消，终态请求沿用已有结果。"""
        with sessions.begin() as db:
            user, _ = _session(db, request, write=True, permission=Permission.CANCEL_JOBS)
            result = jobs.cancel_job(db, job_id, user.id, body.reason if body else None)
            request.state.target = job_id
            _audit(db, request, 200)
            return result

    @router.get("/jobs/{job_id}/assets")
    def assets(job_id: str, request: Request, limit: Limit = 50, cursor: str | None = None):
        """按固定策略的取消后开放规则分页返回任务产物。"""
        with sessions() as db:
            user, _ = _session(db, request, permission=Permission.VIEW_JOBS)
            job = jobs.resource(db, Job, job_id, user.id, options=(defer(Job.inputs),))
            scope = [user.id, "assets", job_id, "id.asc"]
            pos = read_position(cursor, scope, str)
            if not jobs.assets_visible(db, job):
                return {"items": [], "next_cursor": None}
            query = select(Asset).where(Asset.job_id == job.id)
            if pos:
                query = query.where(Asset.id > pos[0])
            return page(
                db, query.order_by(Asset.id), limit, scope, lambda a: [a.id], jobs.asset_payload
            )

    @router.post("/assets", status_code=201)
    async def upload(request: Request):
        """接收一个 multipart 素材并返回已登记的文件信息。"""
        return await _upload(request, sessions, store, capacity, settings)

    @router.get("/assets/{asset_id}")
    def asset_detail(asset_id: str, request: Request):
        """校验对象归属和任务开放规则后读取文件元数据。"""
        with sessions() as db:
            user, _ = _session(db, request, permission=Permission.DOWNLOAD_ASSETS)
            return jobs.asset_payload(jobs.get_asset(db, asset_id, user.id))

    @router.get("/assets/{asset_id}/content")
    def content(
        asset_id: str,
        request: Request,
        download: Annotated[int, Query(ge=0, le=1)] = 0,
        page_user: str | None = None,
        session_generation: str | None = None,
    ):
        """对本次预览、下载或范围读取重新验证文件访问权。"""

        def authorized_asset():
            """在短事务中检查当前会话、资源归属和最新的取消后可见规则。"""
            with sessions() as db:
                user, _ = _session(
                    db,
                    request,
                    permission=Permission.DOWNLOAD_ASSETS,
                    page_user=page_user,
                    generation=session_generation,
                )
                asset = jobs.get_asset(db, asset_id, user.id)
                request.state.target = asset.id
                return asset

        asset = authorized_asset()
        args = {"range_header": request.headers.get("range"), "download": bool(download)}
        # 关闭鉴权事务后再读取和核验视频，避免大文件长时间占用数据库连接。
        if asset.available:
            try:
                return content_response(store, asset, **args)
            except ApiError as exc:
                if exc.code != "OBJECT_UNAVAILABLE":
                    raise
        # 先登记缺失或损坏，再根据受保护源尝试原位恢复；两次状态变化都可追溯。
        try:
            available = reconcile_asset(sessions, store, asset.id, repair=False)
            if not available and asset.execution_id:
                available = reconcile_asset(sessions, store, asset.id, repair=True)
        except OSError:
            available = False
        if not available:
            raise ApiError(503, "OBJECT_UNAVAILABLE", "Stored object is unavailable")
        # 恢复可能跨越会话轮换或任务取消，应重新鉴权后才交付字节。
        return content_response(store, authorized_asset(), **args)

    return router


async def _upload(request, sessions, store, capacity, settings):
    """在有效会话和卷预留的保护下解析上传、校验内容并登记素材。"""

    def authenticate_upload():
        """读取上传者身份并在接收文件前检查写权限。"""
        with sessions() as db:
            user, _ = _session(db, request, write=True, permission=Permission.UPLOAD_ASSETS)
            return user.id

    user_id = await run_in_threadpool(authenticate_upload)
    max_bytes = settings.max_upload_bytes
    envelope = 65536
    try:
        length = int(request.headers.get("content-length", str(max_bytes + envelope)))
        if length < 0:
            raise ValueError
    except ValueError:
        raise ApiError(400, "INVALID_LENGTH", "Invalid Content-Length") from None
    if length > max_bytes + envelope:
        raise ApiError(413, "UPLOAD_TOO_LARGE", "Upload exceeds size limit")

    class Oversize(MultiPartException):
        pass

    async def bounded_body():
        """按实际收到的字节累计校验请求体上限。"""
        count = 0
        async for chunk in request.stream():
            count += len(chunk)
            if count > max_bytes + envelope or count > length:
                raise Oversize("Upload exceeds size limit")
            yield chunk

    def register_file(file, upper_bound, upload_owner):
        """准备正式文件后再次验证页面代次，随后提交归属和审计。"""
        # 文件复制、媒体探测都在业务事务外执行。
        capacity.finish_spool(sessions, upload_owner)
        with store.prepare_upload(file.file, upper_bound, owner=upload_owner) as prepared:
            with sessions.begin() as db:
                user, _ = _session(db, request, write=True, permission=Permission.UPLOAD_ASSETS)
                asset = prepared.register(db, user.id)
                capacity.credit_upload(db, upload_owner, asset.size)
                request.state.target = asset.id
                _audit(db, request, 201)
            prepared.committed()
            return jobs.asset_payload(asset)

    # 同时为 multipart 解包临时文件和正式归档预留空间。
    upper_bound = min(length, max_bytes)
    reservation = capacity.upload(sessions, user_id, upper_bound)
    owner = await run_in_threadpool(reservation.__enter__)
    try:
        parser = MultiPartParser(request.headers, bounded_body(), max_files=1, max_fields=0)
        try:
            form = await parser.parse()
        except ClientDisconnect:
            raise ApiError(400, "UPLOAD_INTERRUPTED", "Upload was interrupted") from None
        except Oversize:
            raise ApiError(413, "UPLOAD_TOO_LARGE", "Upload exceeds size limit") from None
        except OSError:
            raise ApiError(503, "STORAGE_UNAVAILABLE", "Upload storage is unavailable") from None
        except (MultiPartException, ValueError):
            raise ApiError(400, "INVALID_MULTIPART", "Malformed multipart upload") from None
        try:
            file = form.get("file")
            if not isinstance(file, UploadFile):
                raise ApiError(422, "VALIDATION_FAILED", "Expected multipart file field")
            return await run_in_threadpool(register_file, file, upper_bound, owner)
        finally:
            # 请求协程若被取消，仍须关闭已解析的暂存文件。
            with CancelScope(shield=True):
                await form.close()
    finally:
        with CancelScope(shield=True):
            await run_in_threadpool(reservation.__exit__, None, None, None)
