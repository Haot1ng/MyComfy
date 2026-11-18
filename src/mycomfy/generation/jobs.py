from collections import Counter
from typing import Annotated, Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from mycomfy.database.models import (
    Asset,
    Batch,
    EngineInstance,
    Execution,
    Job,
    JobInput,
    StrategyVersion,
)
from mycomfy.database.session import db_now, new_id, timestamp
from mycomfy.errors import ApiError, not_found

from .state import TERMINAL, advance, canonical_digest, emit
from .strategy import configured_for, json_pointer

ClientKey = Annotated[str, Field(min_length=1, max_length=200)]


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    client_task_key: ClientKey
    strategy_version_id: Annotated[str, Field(min_length=1, max_length=48)]
    inputs: dict[str, Any]


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    client_batch_key: ClientKey
    tasks: list[TaskRequest]


def asset_ids(value):
    """将标量和列表素材参数统一为可逐项校验的 ID 序列。"""
    return value if isinstance(value, list) else [value]


def admit_batch(db, user, request, max_tasks, store, capacity=None):
    """按用户幂等键受理整批任务，返回与原始任务顺序对应的 ID。"""
    try:
        digest = canonical_digest(request.model_dump(mode="json"))
    except ValueError as exc:
        raise ApiError(400, "INVALID_JSON", "JSON numbers must be finite") from exc
    batch = db.scalar(
        select(Batch).where(Batch.user_id == user.id, Batch.client_key == request.client_batch_key)
    )
    # 已受理请求先返回原映射，后来的配额或部署变化只影响新批次。
    if batch:
        if batch.request_digest != digest:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "Batch key has already been used")
        return batch_payload(db, batch.id)

    if not 1 <= len(request.tasks) <= max_tasks:
        raise ApiError(422, "VALIDATION_FAILED", f"Expected between 1 and {max_tasks} tasks")
    offsets = Counter(task.client_task_key for task in request.tasks)
    instances = db.scalars(
        select(EngineInstance).where(EngineInstance.configured).with_for_update(read=True)
    ).all()
    errors = []
    validated = []
    for index, task in enumerate(request.tasks):

        def issue(path, code, *, index=index, task=task):
            """记录当前任务的字段错误，同时保留批次索引和客户端任务键。"""
            errors.append(
                {
                    "index": index,
                    "client_task_key": task.client_task_key,
                    "path": json_pointer(("tasks", index, *path)),
                    "code": code,
                }
            )

        if offsets[task.client_task_key] > 1:
            issue(("client_task_key",), "DUPLICATE")
        strategy = db.get(StrategyVersion, task.strategy_version_id)
        if strategy is None:
            issue(("strategy_version_id",), "UNKNOWN_VERSION")
            continue
        if not configured_for(db, strategy, instances):
            issue(("strategy_version_id",), "NO_COMPATIBLE_DEPLOYMENT")

        schema = strategy.content["input_schema"]
        properties = schema.get("properties", {})
        inputs = {
            name: rule["default"]
            for name, rule in properties.items()
            if isinstance(rule, dict) and "default" in rule
        }
        inputs.update(task.inputs)
        for name in task.inputs.keys() - properties.keys():
            issue(("inputs", name), "UNKNOWN_INPUT")
        for error in Draft202012Validator(schema).iter_errors(inputs):
            path = ("inputs", *error.absolute_path)
            if error.validator == "required" and isinstance(error.instance, dict):
                for missing in error.validator_value:
                    if missing not in error.instance:
                        error_path = json_pointer(("tasks", index, *path, missing))
                        if not any(
                            e["path"] == error_path and e["code"] == "REQUIRED" for e in errors
                        ):
                            issue((*path, missing), "REQUIRED")
            elif error.validator != "additionalProperties" or error.absolute_path:
                issue(path, "SCHEMA_INVALID")
        references = set()
        asset_fields = {b["input"] for b in strategy.content["bindings"] if b["kind"] == "asset"}
        for name in asset_fields & inputs.keys():
            ids = inputs[name]
            for offset, asset_id in enumerate(asset_ids(ids)):
                asset = db.get(Asset, asset_id) if isinstance(asset_id, str) else None
                if (
                    asset is None
                    or asset.user_id != user.id
                    or asset.job_id is not None
                    or not asset.available
                    or not store.readable(asset.object_key, asset.size)
                ):
                    path = ("inputs", name, offset) if isinstance(ids, list) else ("inputs", name)
                    issue(path, "ASSET_UNAVAILABLE")
                else:
                    references.add(asset.id)
        validated.append((index, task, strategy, inputs, references))
    # 所有任务先完成校验，任何一项失败都不会留下批次或任务。
    if errors:
        raise ApiError(422, "VALIDATION_FAILED", "Check the task parameters", errors)

    if capacity is not None:
        capacity.check_batch(db, user.id, len(request.tasks))
    now = db_now(db)
    batch = Batch(
        id=new_id("b"),
        user_id=user.id,
        client_key=request.client_batch_key,
        request_digest=digest,
        created_at=now,
    )
    db.add(batch)
    db.flush()
    jobs = []
    for index, task, strategy, inputs, references in validated:
        job = Job(
            id=new_id("j"),
            user_id=user.id,
            batch_id=batch.id,
            position=index,
            client_key=task.client_task_key,
            strategy_version_id=strategy.id,
            inputs=inputs,
            state="QUEUED",
            version=1,
            base_priority=strategy.content["scheduling"]["base_priority"],
            created_at=now,
            enqueued_at=now,
        )
        db.add(job)
        jobs.append((job, references))
    db.flush()
    # 输入引用与首条事件和任务一起提交，使 Worker 总能读到完整受理结果。
    for job, references in jobs:
        db.add_all(JobInput(job_id=job.id, asset_id=asset_id) for asset_id in references)
        emit(db, job, now, "job.queued")
    return batch_payload(db, batch.id)


def batch_payload(db, batch_id):
    """按提交顺序重建批次受理响应，供首次提交和重放共同使用。"""
    return {
        "batch_id": batch_id,
        "tasks": [
            {"client_task_key": job.client_key, "job_id": job.id}
            for job in db.scalars(
                select(Job).where(Job.batch_id == batch_id).order_by(Job.position)
            )
        ],
    }


def resource(db, model, id_value, user_id, *, lock=False):
    """按资源 ID 和用户归属加载记录，写入方可请求行锁。"""
    row = db.get(model, id_value, with_for_update=lock)
    if row is None or row.user_id != user_id:
        raise not_found()
    return row


def cancel_job(db, job_id, user_id, reason):
    """持锁取消非终态任务，重复取消返回已经提交的终态与版本。"""
    job = resource(db, Job, job_id, user_id, lock=True)
    if job.state not in TERMINAL:
        advance(db, job, "CANCELLED", db_now(db))
        if reason:
            job.failure = reason
    return {"job_id": job.id, "state": job.state, "job_version": job.version}


def asset_payload(asset):
    """生成面向用户的文件元数据及授权读取地址。"""
    return {
        "asset_id": asset.id,
        "role": asset.role,
        "media_type": asset.media_type,
        "size": asset.size,
        "digest": asset.digest,
        "media": asset.media,
        "complete": asset.complete,
        "available": asset.available,
        "content_url": f"/api/v1/assets/{asset.id}/content" if asset.available else None,
    }


def assets_visible(db, job):
    """依据任务绑定的策略版本判断取消后是否向用户开放产物。"""
    return (
        job.state != "CANCELLED"
        or db.get(StrategyVersion, job.strategy_version_id).content["artifacts"][
            "expose_after_cancel"
        ]
    )


def job_assets(db, job):
    """列出当前用户可见的任务产物，按角色和序号稳定排序。"""
    if not assets_visible(db, job):
        return []
    return [
        asset_payload(asset)
        for asset in db.scalars(
            select(Asset).where(Asset.job_id == job.id).order_by(Asset.role, Asset.output_index)
        )
    ]


def get_asset(db, asset_id, user_id):
    """验证文件归属以及取消后的开放规则，返回获准访问的文件记录。"""
    asset = resource(db, Asset, asset_id, user_id)
    if asset.job_id and not assets_visible(db, db.get(Job, asset.job_id)):
        raise not_found()
    return asset


def job_payload(db, job, *, details=True):
    """生成任务快照，详情额外关联当前执行、失败原因及可见产物。"""
    execution = db.get(Execution, job.current_execution_id) if job.current_execution_id else None
    payload = {
        "job_id": job.id,
        "batch_id": job.batch_id,
        "strategy_version_id": job.strategy_version_id,
        "client_task_key": job.client_key,
        "state": job.state,
        "job_version": job.version,
        "created_at": timestamp(job.created_at),
        "blocked_reason": job.blocked_reason,
        "retry_at": timestamp(job.retry_at),
    }
    if details:
        payload.update(
            {
                "current_execution_id": job.current_execution_id,
                "stage": execution.stage if execution else None,
                "execution_phase": execution.phase if execution else None,
                "progress": None,
                "attempt_no": execution.attempt_no if execution else None,
                "failure": job.failure,
                "assets": job_assets(db, job),
            }
        )
    return payload
