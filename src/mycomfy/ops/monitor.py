"""回环监听器上的只读监控视图及独立于浏览器的告警轮询。"""

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from mycomfy.api.pagination import encode_cursor, invalid_cursor, page, read_position
from mycomfy.database.capacity_models import CapacityGate, DiskReservation
from mycomfy.database.models import (
    Asset,
    Audit,
    Batch,
    EngineInstance,
    Execution,
    Job,
    Publication,
    SourceEvent,
    StrategyVersion,
)
from mycomfy.database.session import db_now, timestamp
from mycomfy.errors import ApiError, not_found

from . import capacity as disk_capacity
from .capacity import Capacity

LOG = logging.getLogger("mycomfy.alerts")
MODELS = {
    "requests": Audit,
    "batches": Batch,
    "jobs": Job,
    "tasks": Job,
    "executions": Execution,
    "instances": EngineInstance,
    "metrics": EngineInstance,
    "files": Asset,
    "logs": Execution,
}
FIELDS = {
    Audit: "id actor action result duration_ms target created_at",
    Batch: "id user_id created_at",
    Job: "id batch_id user_id strategy_version_id state version current_execution_id created_at retry_at blocked_reason failure",
    Execution: "id job_id instance_id generation attempt_no phase prompt_id environment workflow_digest stage budget_charged budget_refunded created_at authorized_at updated_at result",
    EngineInstance: "id generation configured accepting manifest heartbeat_at active_execution_id quarantine_reason",
    Asset: "id job_id execution_id user_id role output_index provider size media_type digest media complete available created_at",
}
SENSITIVE_LINE = re.compile(
    r"(?i)\b(?:authorization|cookie|csrf(?:[_-]token)?|password|bearer|[a-z0-9_-]*(?:token|secret|api[_-]key))\b"
)


def view(db, row):
    """从白名单投影业务和诊断字段，关联代次、日志定位和预算。"""
    result = {name: getattr(row, name) for name in FIELDS[type(row)].split()}
    for key, value in result.items():
        if isinstance(value, datetime):
            result[key] = timestamp(value)
    if isinstance(row, Execution):
        job = db.get(Job, row.job_id)
        strategy = db.get(StrategyVersion, job.strategy_version_id)
        result.update(
            batch_id=job.batch_id,
            strategy_version_id=strategy.id,
            remaining_attempts=max(
                0, 1 + strategy.content["retry"]["max_retries"] - job.budget_used
            ),
            nodes={k: v.get("class_type") for k, v in (row.prepared_workflow or {}).items()},
            log_url=f"/local/v1/monitor/logs/{row.id}",
        )
    if isinstance(row, EngineInstance):
        runtime = row.runtime or {}
        stale_at = row.heartbeat_at
        threshold = db_now(db) - timedelta(seconds=runtime.get("stale_seconds", 30))
        metrics = runtime.get("metrics") or {}
        try:
            sampled = filter_time(metrics["sampled_at"])
        except (KeyError, ApiError):
            sampled = None
        result.update(
            process_alive=runtime.get("process_alive"),
            api_reachable=runtime.get("api_reachable"),
            occupied=row.active_execution_id is not None,
            metrics=metrics,
            metrics_stale=not sampled or sampled < threshold,
            stale=not stale_at or stale_at < threshold,
        )
    return result


def overview(db, capacity):
    """计算队列、发布、磁盘和长期未处理事件的实时指标与告警。"""
    now = db_now(db)
    counts = dict(db.execute(select(Job.state, func.count()).group_by(Job.state)).all())
    oldest = db.scalar(select(func.min(Job.enqueued_at)).where(Job.state == "QUEUED"))
    stalled = db.scalar(
        select(func.min(SourceEvent.created_at)).where(SourceEvent.dispatched_at.is_(None))
    )
    pending = db.scalar(
        select(func.count()).select_from(Publication).where(Publication.phase != "DONE")
    )
    issues = {}
    for asset_id in db.scalars(select(Asset.id).where(Asset.available.is_(False))):
        issues[f"asset/{asset_id}"] = "OBJECT_UNAVAILABLE"
    for inst in db.scalars(
        select(EngineInstance).where(EngineInstance.quarantine_reason.is_not(None))
    ):
        issues[f"instance/{inst.id}"] = inst.quarantine_reason
    for execution in db.scalars(
        select(Execution).where(Execution.phase.in_(["UNCONFIRMED", "QUARANTINED"]))
    ):
        issues[f"execution/{execution.id}"] = execution.phase
    for item in db.scalars(
        select(Publication).where(Publication.phase != "DONE", Publication.reason.is_not(None))
    ):
        issues[f"publication/{item.id}"] = item.reason
    if stalled and (now - stalled).total_seconds() >= int(
        os.getenv("MYCOMFY_EVENT_ALERT_SECONDS", "60")
    ):
        issues["events"] = "EVENT_DISPATCH_DELAY"
    groups = {}
    for role, volume in sorted(capacity.volumes.items()):
        try:
            dev, free = disk_capacity.measured_volume(volume.path)
        except ApiError:
            issues[f"volume/{role}"] = "STORAGE_UNAVAILABLE"
            continue
        state = db.get(CapacityGate, "volume-" + dev)
        info = groups.setdefault(
            dev,
            {
                "roles": [],
                "free_bytes": free,
                "reserved_bytes": 0,
                "stop_bytes": 0,
                "resume_bytes": 0,
                "paused": bool(state and state.paused),
            },
        )
        info["roles"].append(role)
        info["stop_bytes"] = max(info["stop_bytes"], volume.stop + volume.keep_free)
        info["resume_bytes"] = max(info["resume_bytes"], volume.resume + volume.keep_free)
    for device, info in groups.items():
        reserved = db.scalar(
            select(func.coalesce(func.sum(DiskReservation.size), 0)).where(
                DiskReservation.device == device
            )
        )
        info["reserved_bytes"] = int(reserved)
        available = info["free_bytes"] - info["reserved_bytes"]
        if available < info["stop_bytes"] or (info["paused"] and available < info["resume_bytes"]):
            issues[f"volume/{device}"] = "VOLUME_LOW"
    return {
        "sampled_at": timestamp(now),
        "states": counts,
        "queue_length": counts.get("QUEUED", 0),
        "oldest_wait_seconds": (now - oldest).total_seconds() if oldest else 0,
        "retries": counts.get("RETRY_WAIT", 0),
        "isolated": sum(k.startswith("instance/") for k in issues),
        "publication_backlog": pending,
        "volumes": groups,
        "alerts": issues,
    }


def poll_alerts(sessions, capacity, previous):
    """采集当前告警并向可配置的 Python 日志通道发送首次出现和恢复事件。"""
    with sessions() as db:
        current = overview(db, capacity)["alerts"]
    for key, reason in current.items():
        if previous.get(key) != reason:
            LOG.warning("active %s %s", key, reason)
    for key in previous.keys() - current.keys():
        LOG.info("resolved %s", key)
    return current


def filter_time(raw):
    """将包含时区的 RFC 3339 过滤值转换为 UTC 数据库时间。"""
    try:
        value = datetime.fromisoformat(raw)
        if value.utcoffset() is None:
            raise ValueError("Missing timezone")
        return value.astimezone(UTC).replace(tzinfo=None)
    except ValueError as exc:
        raise ApiError(400, "INVALID_FILTER", "Use RFC 3339 timestamps with timezone") from exc


def filtered(model, params):
    """把任务、执行、实例和时间筛选应用于对应的只读集合。"""
    query = select(model)
    allowed = {
        "limit",
        "cursor",
        "since",
        "until",
        "job_id",
        "execution_id",
        "instance_id",
        "batch_id",
        "user_id",
        "request_id",
        "level",
    }
    if params.keys() - allowed:
        raise ApiError(400, "INVALID_FILTER", "Unknown monitor filter")
    for field in ("job_id", "execution_id", "instance_id", "batch_id", "request_id", "user_id"):
        value = params.get(field)
        if not value:
            continue
        primary_model = {
            "job_id": Job,
            "execution_id": Execution,
            "instance_id": EngineInstance,
            "batch_id": Batch,
            "request_id": Audit,
        }.get(field)
        if primary_model == model:
            query = query.where(model.id == value)
        elif hasattr(model, field):
            query = query.where(getattr(model, field) == value)
        elif model is Audit:
            query = query.where((Audit.actor if field == "user_id" else Audit.target) == value)
        elif model is Execution and field in {"batch_id", "user_id"}:
            query = query.where(
                Execution.job_id.in_(select(Job.id).where(getattr(Job, field) == value))
            )
        elif model is Asset and field == "instance_id":
            query = query.where(
                Asset.execution_id.in_(select(Execution.id).where(Execution.instance_id == value))
            )
        else:
            raise ApiError(400, "INVALID_FILTER", "Filter does not apply to this collection")
    when = getattr(model, "created_at", None)
    if when is None:
        when = model.heartbeat_at
    for name in ("since", "until"):
        if name not in params:
            continue
        dt = filter_time(params[name])
        query = query.where(when >= dt if name == "since" else when <= dt)
    return query


def log_page(row, request, limit, cursor):
    """按日志文件标识和字节位置跨轮转翻页，每次读取有界、再次脱敏。"""
    run_dir, log_name = row.workspace.get("run_dir"), row.workspace.get("log_path")
    if not run_dir or not log_name:
        return {"items": [], "next_cursor": None}
    run_dir, base = Path(run_dir).resolve(), Path(log_name)
    if base.parent.resolve() != run_dir or base.name != "engine.log":
        raise not_found()
    paths = [base.with_name(f"{base.name}.{n}") for n in (3, 2, 1)] + [base]
    params = request.query_params
    level = params.get("level", "").upper()
    since = filter_time(params["since"]) if params.get("since") else datetime.min
    until = filter_time(params["until"]) if params.get("until") else datetime.max
    # 每行引擎日志带 UTC 毫秒级时间戳。按执行时窗截取同一代次的多项任务。
    if row.created_at:
        lower = row.created_at.replace(microsecond=row.created_at.microsecond // 1000 * 1000)
        since = max(since, lower)
    if (
        row.phase in {"SUCCEEDED", "FAILED", "INTERRUPTED", "LOST", "NOT_SUBMITTED"}
        and row.updated_at
    ):
        until = min(until, row.updated_at)
    scope = ["monitor-log", row.id, level, params.get("since"), params.get("until")]
    position = read_position(cursor, scope, int, int, bool)
    result, examined = [], 0
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            stream = path.open("rb")
        except FileNotFoundError:
            continue
        with stream:
            inode = os.fstat(stream.fileno()).st_ino
            discarding = False
            if position:
                if position[0] != inode:
                    continue
                stream.seek(position[1])
                discarding = position[2]
                position = None
            while len(result) < limit and examined < 262144:
                offset = stream.tell()
                line = stream.readline(8193)
                if not line:
                    break
                examined += len(line)
                truncated = len(line) == 8193 and not line.endswith(b"\n")
                if discarding:
                    discarding = truncated
                    continue
                discarding = truncated
                message = line.decode("utf-8", "replace").rstrip()
                stamp = None
                if since != datetime.min or until != datetime.max:
                    try:
                        stamp = datetime.strptime(message[:23], "%Y-%m-%d %H:%M:%S,%f")
                    except ValueError:
                        continue
                    if not since <= stamp <= until:
                        continue
                if truncated or SENSITIVE_LINE.search(message):
                    message = "[redacted]"
                if not level or level in message.upper():
                    result.append(
                        {
                            "instance_id": row.instance_id,
                            "generation": row.generation,
                            "execution_id": row.id,
                            "offset": offset,
                            "sampled_at": timestamp(stamp),
                            "message": message,
                        }
                    )
            if len(result) >= limit or examined >= 262144:
                return {
                    "items": result,
                    "next_cursor": encode_cursor(scope, [inode, stream.tell(), discarding]),
                }
    if position is not None:
        raise invalid_cursor()
    return {"items": result, "next_cursor": None}


def log_collection(db, request, limit, cursor):
    """按任务、实例、时间或级别读取日志行，并将跨执行和文件分页位置一并保存。"""
    params = dict(request.query_params)
    # 单实例全局日志包含冷启动阶段，可在执行尚未产生时诊断引擎。
    if "instance_id" in params and not (
        params.keys() & {"job_id", "execution_id", "batch_id", "user_id"}
    ):
        instance = db.get(EngineInstance, params["instance_id"])
        if not instance:
            raise not_found()
        source = SimpleNamespace(
            id=f"instance-{instance.id}-{instance.generation}",
            instance_id=instance.id,
            generation=instance.generation,
            workspace=instance.runtime,
            phase="RUNNING",
            created_at=None,
        )
        result = log_page(source, request, limit, cursor)
        for item in result["items"]:
            item["execution_id"] = None
        return result
    # 时间条件由日志行与执行时窗共同判断，以包含早于 since 启动的长任务。
    filters = {k: v for k, v in params.items() if k not in {"since", "until"}}
    query = filtered(Execution, filters).order_by(Execution.id)
    scope = [
        "monitor-log-collection",
        sorted((k, v) for k, v in params.items() if k not in {"limit", "cursor"}),
    ]
    pos = read_position(cursor, scope, str, str)
    line_cursor = None
    if pos:
        execution_id, line_cursor = pos
        query = query.where(
            Execution.id >= execution_id if line_cursor else Execution.id > execution_id
        )
    rows = db.scalars(query.limit(9)).all()
    items = []
    for row in rows[:8]:
        lines = log_page(row, request, limit - len(items), line_cursor or None)
        line_cursor = None
        items.extend(lines["items"])
        if lines["next_cursor"] or len(items) == limit:
            return {
                "items": items,
                "next_cursor": encode_cursor(scope, [row.id, lines["next_cursor"] or ""]),
            }
    return {
        "items": items,
        "next_cursor": encode_cursor(scope, [rows[7].id, ""]) if len(rows) > 8 else None,
    }


def build_monitor_router(settings, sessions):
    """构建本机监控 API，并随本机服务生命周期运行告警采集。"""
    capacity = Capacity.from_settings(settings)

    @asynccontextmanager
    async def lifespan(app):
        """运行独立采样循环，停止监听时一起回收后台任务。"""

        async def report():
            """比较前后采样，只发送新告警和已恢复告警。"""
            previous = {}
            while True:
                try:
                    previous = await asyncio.to_thread(poll_alerts, sessions, capacity, previous)
                except Exception:
                    LOG.exception("Monitor sampling failed")
                await asyncio.sleep(10)

        task = asyncio.create_task(report())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    router = APIRouter(prefix="/local/v1/monitor", lifespan=lifespan)

    @router.get("/{collection}")
    def collection_view(
        collection: str,
        request: Request,
        limit: int = Query(50, ge=1, le=200),
        cursor: str | None = None,
    ):
        """分页读取已注册监控集合或获取实时总览。"""
        with sessions() as db:
            if collection == "overview":
                return overview(db, capacity)
            model = MODELS.get(collection)
            if model is None:
                raise not_found()
            params = dict(request.query_params)
            if collection == "logs":
                return log_collection(db, request, limit, cursor)
            scope = [
                "monitor",
                collection,
                sorted((k, v) for k, v in params.items() if k not in {"limit", "cursor"}),
            ]
            pos = read_position(cursor, scope, str)
            query = filtered(model, params).order_by(model.id)
            if pos:
                query = query.where(model.id > pos[0])
            return page(db, query, limit, scope, lambda row: [row.id], lambda row: view(db, row))

    @router.get("/{collection}/{row_id}")
    def detail(
        collection: str,
        row_id: str,
        request: Request,
        limit: int = Query(50, ge=1, le=200),
        cursor: str | None = None,
    ):
        """按资源标识查看单条诊断事实，日志详情支持分页。"""
        model = MODELS.get(collection)
        if model is None:
            raise not_found()
        with sessions() as db:
            row = db.get(model, row_id)
            if row is None:
                raise not_found()
            if collection == "logs":
                return log_page(row, request, limit, cursor)
            return view(db, row)

    return router
