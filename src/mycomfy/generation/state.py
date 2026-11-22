import hashlib
import json
from datetime import timedelta

from mycomfy.database.models import SourceEvent
from mycomfy.database.session import new_id, timestamp

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
TRANSITIONS = {
    "QUEUED": {"EXECUTING", "CANCELLED"},
    "EXECUTING": {"QUEUED", "RECOVERING", "PUBLISHING", "RETRY_WAIT", "FAILED", "CANCELLED"},
    "RECOVERING": {"QUEUED", "EXECUTING", "PUBLISHING", "RETRY_WAIT", "FAILED", "CANCELLED"},
    "RETRY_WAIT": {"QUEUED", "CANCELLED"},
    "PUBLISHING": {"SUCCEEDED", "RETRY_WAIT", "FAILED", "CANCELLED"},
    "FAILED": {"QUEUED"},  # 仅用户获准手动重试时进入新一轮生成。
}


def canonical_digest(value):
    """保留数组顺序并规范化对象键，计算策略或请求的稳定摘要。"""
    data = json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(data.encode()).hexdigest()


def emit(db, job, now, event_type, **payload):
    """在业务事务内持久化携带任务版本的来源事件，供后续分发。"""
    event_id = new_id("ev")
    db.add(
        SourceEvent(
            id=event_id,
            user_id=job.user_id,
            job_id=job.id,
            job_version=job.version,
            type=event_type,
            data={
                "event_id": event_id,
                "job_id": job.id,
                "job_version": job.version,
                "state": job.state,
                "occurred_at": timestamp(now),
                **payload,
            },
            created_at=now,
        )
    )


def advance(db, job, new_state, now, **event_data):
    """校验状态迁移，递增公开版本并记录同一事务内的变更事件。"""
    if new_state not in TRANSITIONS.get(job.state, set()):
        raise ValueError(f"Invalid job transition {job.state} → {new_state}")
    job.state = new_state
    job.version += 1
    # 清理上一轮的唤醒时间，防止终态或运行中的任务被再次唤醒。
    if new_state != "RETRY_WAIT":
        job.retry_at = None
    emit(db, job, now, f"job.{new_state.lower()}", **event_data)


def set_blocked(db, job, now, reason):
    """仅在阻塞原因有变化时更新任务版本并发出事件。"""
    if job.blocked_reason != reason:
        job.blocked_reason = reason
        job.version += 1
        emit(db, job, now, "job.blocked_changed", blocked_reason=reason)


def charge(job, execution):
    """按执行记录一次生成预算，允许重复处理同一授权事实。"""
    if not execution.budget_charged:
        job.budget_used += 1
        execution.budget_charged = True


def refund(job, execution):
    """在确证未受理时返还一次已扣预算，保留核对所需的执行标志。"""
    if execution.budget_charged and not execution.budget_refunded:
        job.budget_used -= 1
        execution.budget_refunded = True


def settle_failure(db, job, execution, strategy, now, reason):
    """结算一次执行，按绑定版本的重试额度决定等待或最终失败。"""
    if execution.budget_settled:
        return
    # 迟到结果仍可完成执行记账，只有当前执行可以推进非终态任务。
    execution.budget_settled = True
    if job.state in TERMINAL or job.current_execution_id != execution.id:
        return
    job.failure = reason
    retry = strategy["retry"]
    if job.budget_used < 1 + retry["max_retries"]:
        job.retry_at = now + timedelta(seconds=retry["interval_seconds"])
        advance(db, job, "RETRY_WAIT", now)
    else:
        advance(db, job, "FAILED", now)


def effective_priority(priority, enqueued_at, now, aging_seconds):
    """以完整等待周期计算优先级加分，未来入队时间按零等待处理。"""
    return priority + int(max(0, (now - enqueued_at).total_seconds()) // aging_seconds)
