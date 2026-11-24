"""单协调器调度、单次提交授权和按实例代次恢复。"""

from __future__ import annotations

import fcntl
import logging
import os
import signal
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from mycomfy.database.models import (
    Asset,
    EngineInstance,
    Execution,
    Incompatibility,
    Job,
    JobInput,
    StrategyVersion,
)
from mycomfy.database.session import db_now, make_sessions, new_id, timestamp
from mycomfy.engine.adapter import (
    ComfyAdapter,
    NotSent,
    Observation,
    Rejected,
    Unconfirmed,
    completed_outputs,
    output_paths,
    prepare_workflow,
    stopped_outputs,
)
from mycomfy.engine.progress import ProgressChannel
from mycomfy.errors import ApiError

from .state import (
    TERMINAL,
    advance,
    canonical_digest,
    charge,
    effective_priority,
    refund,
    set_blocked,
    settle_failure,
)
from .strategy import compatible, configured_for
from .worker_health import FILE_GRACE, POLL_GRACE, ignore_progress, report_health

log = logging.getLogger(__name__)


@contextmanager
def coordinator_lock(data_root):
    """独占协调器锁直至退出，让进程崩溃后由内核释放锁。"""
    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "worker.lock").open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("A worker coordinator is already running") from exc
        yield


def record_phase(execution, phase, now, evidence):
    """将执行阶段、发生时间和判定依据一并记录在该次执行上。"""
    if execution.phase == phase:
        return
    execution.phase = phase
    execution.updated_at = now
    result = dict(execution.result or {})
    result["transitions"] = [
        *result.get("transitions", []),
        {
            "phase": phase,
            "at": timestamp(now),
            "evidence": evidence,
            "generation": execution.generation,
        },
    ]
    execution.result = result


def record_lookup(execution, lookup, now, *, force=False):
    """保存一次历史和队列探测摘要，不改变状态的起始时间或延长核对期限。"""
    if not lookup:
        return
    result = execution.result or {}
    previous = result.get("last_lookup", {})
    # 状态码、命中或异常变化立即记录；稳定轮询只按观察间隔刷新耗时，避免逐秒改写 MySQL。
    if previous and not force:
        interval = execution.workspace.get(
            "observation_seconds", execution.workspace.get("timeout_seconds", 120)
        )
        boundary = timestamp(now - timedelta(seconds=interval))
        if previous["at"] > boundary and all(
            previous.get(endpoint, {}).get(key) == sample.get(key)
            for endpoint, sample in lookup.items()
            for key in ("status", "http_status", "error")
        ):
            return
    execution.result = {**result, "last_lookup": {"at": timestamp(now), **lookup}}


def take_owned(db, instance_id, execution_id):
    """依次锁定实例、任务及执行并复核所有权，过时代次无法释放新槽位。"""
    instance = db.get(EngineInstance, instance_id, with_for_update=True)
    if instance is None or instance.active_execution_id != execution_id:
        return None
    execution = db.get(Execution, execution_id, with_for_update=True)
    if not execution or execution.generation != instance.generation:
        return None
    job = db.get(Job, execution.job_id, with_for_update=True)
    if job.current_execution_id != execution_id:
        return None
    return instance, job, execution


class Worker:
    def __init__(
        self,
        settings,
        sessions,
        store,
        adapter_factory=ComfyAdapter,
        *,
        capacity=None,
        stop_generation=None,
        progress_factory=ProgressChannel,
    ):
        """为协调器配置会话工厂、存储、引擎适配器以及按代次停止实例的入口。"""
        from mycomfy.ops.capacity import Capacity

        self.settings = settings
        self.sessions = sessions
        self.store = store
        self.adapter_factory = adapter_factory
        self.capacity = capacity if capacity is not None else Capacity.from_settings(settings)
        if stop_generation is None:
            from mycomfy.engine.runtime import stop_generation as stop_runtime

            stop_generation = stop_runtime
        self.stop_generation = stop_generation
        self.progress_factory = progress_factory
        self.channels = {}
        self.draining = Event()

    def refresh_queue(self):
        """在数据库时间到期后唤醒重试任务，并刷新当前配置对排队任务的阻塞原因。"""
        with self.sessions.begin() as db:
            now = db_now(db)
            due = db.scalars(
                select(Job).where(Job.state == "RETRY_WAIT", Job.retry_at <= now).with_for_update()
            ).all()
            for job in due:
                # 重试只从本次可运行时间累计等待分。
                job.enqueued_at = job.retry_at
                advance(db, job, "QUEUED", now)
            instances = db.scalars(select(EngineInstance).where(EngineInstance.configured)).all()
            versions = db.scalars(
                select(StrategyVersion).join(Job).where(Job.state == "QUEUED").distinct()
            ).all()
            for version in versions:
                reason = (
                    None if configured_for(db, version, instances) else "NO_COMPATIBLE_DEPLOYMENT"
                )
                query = select(Job).where(
                    Job.state == "QUEUED", Job.strategy_version_id == version.id
                )
                query = query.where(Job.blocked_reason.is_distinct_from(reason)).with_for_update()
                for job in db.scalars(query):
                    set_blocked(db, job, now, reason)

    def claim(self, instance_id):
        """按等待加分和稳定排序领取一个任务，同时创建执行并登记独占槽位。"""
        with self.sessions.begin() as db:
            now = db_now(db)
            instance = db.get(EngineInstance, instance_id, with_for_update=True)
            if (
                not instance
                or not instance.configured
                or not instance.accepting
                or instance.quarantine_reason
                or instance.active_execution_id
                or not instance.heartbeat_at
                or instance.heartbeat_at
                < now - timedelta(seconds=instance.runtime.get("stale_seconds", 20))
            ):
                return None
            rows = db.execute(
                select(Job.id, Job.base_priority, Job.enqueued_at, Job.strategy_version_id).where(
                    Job.state == "QUEUED"
                )
            ).all()
            rankings = sorted(
                rows,
                key=lambda r: (
                    -effective_priority(
                        r.base_priority, r.enqueued_at, now, self.settings.aging_seconds
                    ),
                    r.enqueued_at,
                    r.id,
                ),
            )
            for row in rankings:
                version = db.get(StrategyVersion, row.strategy_version_id)
                if not compatible(db, version, instance):
                    continue
                job = db.get(Job, row.id, with_for_update=True, populate_existing=True)
                if job.state != "QUEUED":
                    continue
                execution_id = new_id("ex")
                if self.draining.is_set():
                    return None
                runtime = dict(instance.runtime)
                overrides = runtime.get("strategy_timeouts", {})
                runtime.update(overrides.get(version.strategy_key, {}))
                runtime.update(overrides.get(version.id, {}))
                # 预留和槽位占用共用事务，容量不足不会留下孤立执行。
                self.capacity.reserve_execution(db, execution_id, job, Path(runtime["output_dir"]))
                job.attempt_count += 1
                execution = Execution(
                    id=execution_id,
                    job_id=job.id,
                    instance_id=instance.id,
                    generation=instance.generation,
                    attempt_no=job.attempt_count,
                    phase="PREPARING",
                    stage="preparing",
                    environment=instance.manifest,
                    workspace=dict(runtime),
                    created_at=now,
                    updated_at=now,
                )
                db.add(execution)
                job.current_execution_id = execution.id
                instance.active_execution_id = execution.id
                job.failure = None
                set_blocked(db, job, now, None)
                advance(db, job, "EXECUTING", now)
                return execution.id
        return None

    def prepare(self, instance_id, execution_id):
        """从受理时的输入与固定版本准备工作区，再复核取消状态及槽位归属。"""
        with self.sessions.begin() as db:
            owned = take_owned(db, instance_id, execution_id)
            if not owned:
                return
            instance, job, execution = owned
            if execution.phase != "PREPARING":
                return
            if job.state in TERMINAL:
                self.finish_unsent(db, instance, job, execution, "CANCELLED_BEFORE_AUTHORIZATION")
                return
            content = db.get(StrategyVersion, job.strategy_version_id).content
            assets = {
                asset.id: asset
                for asset in db.scalars(
                    select(Asset).join(JobInput).where(JobInput.job_id == job.id)
                )
            }
        # 大文件复制在事务外进行；期间到达的取消由下一次锁定读取裁决。
        try:
            workflow = prepare_workflow(execution, content, job.inputs, assets, self.store)
        except OSError:
            log.warning("Preparation waiting for storage, execution=%s", execution_id)
            return
        except (ValueError, KeyError):
            self.quarantine(instance_id, execution_id, "PREPARATION_CONTRACT_MISMATCH")
            return
        with self.sessions.begin() as db:
            owned = take_owned(db, instance_id, execution_id)
            if not owned:
                return
            instance, job, execution = owned
            if execution.phase != "PREPARING":
                return
            if job.state in TERMINAL:
                self.finish_unsent(db, instance, job, execution, "CANCELLED_BEFORE_AUTHORIZATION")
                return
            execution.prepared_workflow = workflow
            execution.workflow_digest = canonical_digest(workflow)
            record_phase(execution, "PREPARED", db_now(db), "INPUTS_READY")

    def finish_unsent(self, db, instance, job, execution, reason, *, incompatible=False):
        """以明确未送达证据结清执行，按执行记账且保留取消终态。"""
        now = db_now(db)
        refund(job, execution)
        execution.budget_settled = True
        record_phase(execution, "NOT_SUBMITTED", now, reason)
        instance.active_execution_id = None
        self.capacity.release(db, execution.id)
        if incompatible:
            key = (job.strategy_version_id, canonical_digest(execution.environment))
            if db.get(Incompatibility, key) is None:
                db.add(
                    Incompatibility(
                        strategy_version_id=key[0], environment_digest=key[1], reason=reason
                    )
                )
        if job.state not in TERMINAL:
            advance(db, job, "QUEUED", now)

    def submit_prepared(self, instance_id, execution_id, adapter):
        """在事务中唯一授权，再由获得授权的调用者单次提交。"""
        with self.sessions.begin() as db:
            owned = take_owned(db, instance_id, execution_id)
            if not owned:
                return
            instance, job, execution = owned
            if execution.phase != "PREPARED":
                return
            if job.state in TERMINAL:
                self.finish_unsent(db, instance, job, execution, "CANCELLED_BEFORE_AUTHORIZATION")
                return
            if instance.quarantine_reason:
                return
            now = db_now(db)
            prompt_id = str(uuid4())
            execution.prompt_id = prompt_id
            execution.authorized_at = now
            charge(job, execution)
            record_phase(execution, "SUBMITTING", now, "SUBMIT_AUTHORIZED")
            workflow = execution.prepared_workflow
        # 进程若在提交前后退出，恢复者看到 SUBMITTING 必须核对，不得再次发 POST。
        try:
            adapter.submit(prompt_id, workflow, execution_id)
        except (Rejected, NotSent) as exc:
            with self.sessions.begin() as db:
                owned = take_owned(db, instance_id, execution_id)
                if owned:
                    self.finish_unsent(db, *owned, str(exc), incompatible=isinstance(exc, Rejected))
            return
        except Unconfirmed as exc:
            self.recovering(instance_id, execution_id, str(exc))
            return
        with self.sessions.begin() as db:
            owned = take_owned(db, instance_id, execution_id)
            if owned:
                execution = owned[2]
                record_phase(execution, "ACCEPTED", db_now(db), "PROMPT_RECEIPT_MATCHED")
                execution.stage = "engine_queue"

    def recovering(self, instance_id, execution_id, reason):
        """保留未知执行占用并发布可见的核对状态。"""
        with self.sessions.begin() as db:
            owned = take_owned(db, instance_id, execution_id)
            if not owned:
                return
            _, job, execution = owned
            now = db_now(db)
            record_phase(execution, "UNCONFIRMED", now, reason)
            if job.state == "EXECUTING":
                advance(db, job, "RECOVERING", now)

    def quarantine(self, instance_id, execution_id, reason):
        """隔离指定代次，保留执行、占用与诊断依据供运维核实。"""
        with self.sessions.begin() as db:
            owned = take_owned(db, instance_id, execution_id)
            if owned:
                instance, job, execution = owned
                now = db_now(db)
                instance.accepting = False
                instance.quarantine_reason = reason
                record_phase(execution, "QUARANTINED", now, reason)
                if job.state == "EXECUTING":
                    advance(db, job, "RECOVERING", now)

    def finish(self, instance_id, execution_id, outcome, outputs, reason):
        """按代次原子保存引擎事实、归档意图并释放槽位，业务终态只按当前执行裁决。"""
        from mycomfy.artifacts.publication import register_outputs

        with self.sessions.begin() as db:
            owned = take_owned(db, instance_id, execution_id)
            if not owned:
                return
            instance, job, execution = owned
            now = db_now(db)
            version = db.get(StrategyVersion, job.strategy_version_id)
            record_phase(execution, outcome, now, reason)
            execution.stage = outcome.lower()
            if job.state == "CANCELLED":
                execution.budget_settled = True
            if outcome == "SUCCEEDED" and job.state not in TERMINAL:
                advance(db, job, "PUBLISHING", now)
            elif outcome != "SUCCEEDED":
                settle_failure(db, job, execution, version.content, now, reason)
            # 发布记录先与执行结论一起提交，源文件占用由发布者在归档后释放。
            register_outputs(db, execution, job, version, outputs, now)
            instance.active_execution_id = None

    def stop_and_reconcile(self, instance_id, execution, contracts, trigger):
        """保存触发核对的依据，确认本代次退出后核对其独占输出目录。"""
        with self.sessions.begin() as db:
            owned = take_owned(db, instance_id, execution.id)
            if not owned:
                return
            current = owned[2]
            result = dict(current.result or {})
            # 先提交触发原因；若停机期间 Worker 重启，后续看到退出凭据仍保留第一次判断。
            if "reconciliation_trigger" not in result:
                result["reconciliation_trigger"] = {"reason": trigger, "at": timestamp(db_now(db))}
                current.result = result
        try:
            confirmed = self.stop_generation(self.sessions, instance_id, execution.generation)
        except (OSError, ValueError, KeyError):
            confirmed = False
        if not confirmed:
            self.quarantine(instance_id, execution.id, "PROCESS_EXIT_UNCONFIRMED")
            return
        try:
            outputs = stopped_outputs(execution, contracts)
        except OSError:
            self.quarantine(instance_id, execution.id, "STOPPED_SOURCE_UNAVAILABLE")
            return
        complete_set = all(
            c["min_count"] <= len(outputs.get(c["role"], [])) <= c["max_count"] for c in contracts
        )
        # 已确认全部写入者退出，只有完整角色集合才进入媒体校验与发布。
        outcome = "SUCCEEDED" if complete_set else "LOST"
        self.finish(instance_id, execution.id, outcome, outputs, "STOPPED_GENERATION_RECONCILED")

    def observe(self, instance_id, execution, adapter, sample=None):
        """按原代次及 prompt_id 对照队列或历史，并在期限到达时受控停止。"""
        execution_id = execution.id
        unknown_reason = "QUEUE_HISTORY_INCONCLUSIVE"
        # HTTP 可能超时，查询期间不占用 MySQL 行锁。回写前重新确认槽位仍归本次执行。
        try:
            result = adapter.observe(execution.prompt_id)
        except Unconfirmed as exc:
            unknown_reason = "ENGINE_LOOKUP_UNAVAILABLE"
            result = Observation("UNKNOWN", lookup=exc.lookup)
        with self.sessions.begin() as db:
            owned = take_owned(db, instance_id, execution_id)
            if not owned:
                return
            instance, job, execution = owned
            if execution.phase == "QUARANTINED" or instance.quarantine_reason:
                return
            now = db_now(db)
            runtime = execution.workspace
            reconcile_due = (
                result.state == "UNKNOWN"
                and execution.phase == "UNCONFIRMED"
                and (now - execution.updated_at).total_seconds() >= runtime["reconcile_seconds"]
            )
            total_due = result.state == "RUNNING" and now >= execution.authorized_at + timedelta(
                seconds=runtime["timeout_seconds"]
            )
            record_lookup(
                execution,
                result.lookup,
                now,
                force=reconcile_due or total_due or result.state not in {"UNKNOWN", "RUNNING"},
            )
            contracts = db.get(StrategyVersion, job.strategy_version_id).content["output_contract"]
        if result.state in {"SUCCEEDED", "FAILED", "INTERRUPTED"}:
            try:
                outputs = completed_outputs(execution, contracts, result.outputs)
            except OSError:
                # 已有完整历史定位时允许释放引擎槽，卷恢复后由持久发布意图继续复制。
                outputs = output_paths(execution, contracts, result.outputs)
                if any(len(outputs.get(c["role"], [])) < c["min_count"] for c in contracts):
                    self.recovering(instance_id, execution_id, "SOURCE_VOLUME_UNAVAILABLE")
                    return
            except (ValueError, TypeError, AttributeError):
                self.finish(instance_id, execution_id, "FAILED", {}, "INVALID_OUTPUT_LOCATION")
                return
            self.finish(instance_id, execution_id, result.state, outputs, "TERMINAL_HISTORY")
            return
        if result.state == "UNKNOWN":
            if reconcile_due:
                self.stop_and_reconcile(instance_id, execution, contracts, "RECONCILE_TIME_LIMIT")
            else:
                self.recovering(instance_id, execution_id, unknown_reason)
            return
        if total_due:
            self.recovering(instance_id, execution_id, "TOTAL_TIME_LIMIT")
            self.stop_and_reconcile(instance_id, execution, contracts, "TOTAL_TIME_LIMIT")
            return
        interrupt = False
        with self.sessions.begin() as db:
            owned = take_owned(db, instance_id, execution_id)
            if not owned:
                return
            instance, job, execution = owned
            now = db_now(db)
            record_phase(execution, "RUNNING", now, "MATCHED_QUEUE_ENTRY")
            evidence = dict(execution.result or {})
            if sample is not None:
                evidence["last_node_sample"] = {**sample, "at": timestamp(now)}
                execution.stage = sample["stage"]
            elif "last_node_sample" not in evidence:
                execution.stage = result.stage or "running"
            # 达到策略观察时限时，保存主动查询的队列状态供排查模型加载等长阶段。
            boundary = now - timedelta(
                seconds=runtime.get("observation_seconds", runtime["timeout_seconds"])
            )
            previous = evidence.get("last_observation_at") or evidence.get(
                "last_node_sample", {}
            ).get("at")
            if not previous:
                previous = timestamp(execution.authorized_at)
            if previous <= timestamp(boundary):
                evidence["last_observation_at"] = timestamp(now)
                evidence["last_observed_stage"] = execution.stage
            execution.result = evidence
            if job.state == "RECOVERING":
                advance(db, job, "EXECUTING", now)
            if (
                job.state == "CANCELLED"
                and runtime.get("directed_interrupt")
                and not execution.interrupt_requested
            ):
                execution.interrupt_requested = True
                interrupt = True
        if interrupt:
            try:
                adapter.interrupt(execution.prompt_id)
            except httpx.HTTPError:
                # 中断回执丢失也必须继续占槽，直到实际执行结束。
                log.warning("Interrupt unconfirmed, execution=%s", execution_id)

    def process(self, instance_id, execution_id, *, progress=ignore_progress):
        """处理准备、提交或恢复的一步，恢复入口只允许 PREPARED 获取发送权。"""
        with self.sessions.begin() as db:
            owned = take_owned(db, instance_id, execution_id)
            if not owned:
                return
            instance, job, execution = owned
            if instance.quarantine_reason:
                return
            phase = execution.phase
            runtime = execution.workspace
            control = dict(instance.runtime)
            strategy = db.get(StrategyVersion, job.strategy_version_id).content
        from mycomfy.engine.runtime import ended

        # 只信任当前运行管理器持有的代次凭据。隔离库恢复后控制目录可能已改变。
        try:
            generation_ended = ended(control, execution.generation)
        except (OSError, ValueError, KeyError):
            self.quarantine(instance_id, execution_id, "GENERATION_EVIDENCE_UNREADABLE")
            return
        if generation_ended:
            if not execution.authorized_at:
                with self.sessions.begin() as db:
                    owned = take_owned(db, instance_id, execution_id)
                    if owned:
                        self.finish_unsent(db, *owned, "STOPPED_BEFORE_AUTHORIZATION")
            else:
                self.stop_and_reconcile(
                    instance_id, execution, strategy["output_contract"], "GENERATION_ALREADY_ENDED"
                )
            return
        if phase == "QUARANTINED":
            return
        if phase == "PREPARING":
            # 新任务和崩溃后恢复的任务都可能需要复制大输入文件。
            progress(FILE_GRACE)
            self.prepare(instance_id, execution_id)
            progress(POLL_GRACE)
            phase = "PREPARED"
        mapping = {
            node: step["stage"]
            for step in strategy["progress_mapping"]
            for node in step["node_ids"]
        }
        sample = None
        if self.progress_factory and mapping:
            channel = self.channels.get(execution_id)
            if channel is None:
                channel = self.progress_factory(runtime, execution_id)
                self.channels[execution_id] = channel
            sample = channel.sample(execution.prompt_id, mapping)
        adapter = self.adapter_factory(runtime)
        try:
            if phase == "PREPARED":
                self.submit_prepared(instance_id, execution_id, adapter)
            else:
                self.observe(instance_id, execution, adapter, sample)
        finally:
            adapter.close()

    def tick(self, *, publish=True, claim=True, progress=ignore_progress):
        """先恢复已有占用，再为空闲实例领取，最后推动已持久化的发布记录。"""
        from mycomfy.artifacts.publication import cleanup_inputs, publish_pending

        with self.sessions.begin() as db:
            rows = db.execute(
                select(EngineInstance.id, EngineInstance.active_execution_id).order_by(
                    EngineInstance.id
                )
            ).all()
        processed = set()
        for instance_id, active in rows:
            if active:
                # GPU 推理在引擎进程中执行，每次轮询只核对一次进展。
                progress(POLL_GRACE)
                self.process(instance_id, active, progress=progress)
                processed.add(active)
        if claim and not self.draining.is_set():
            self.refresh_queue()
            for instance_id, _ in rows:
                progress(POLL_GRACE)
                try:
                    active = self.claim(instance_id)
                except ApiError as exc:
                    if exc.status != 503:
                        raise
                    continue
                if active:
                    self.process(instance_id, active, progress=progress)
                    processed.add(active)
        if publish:
            publish_pending(self.sessions, self.store, before_file=lambda: progress(FILE_GRACE))
        with self.sessions.begin() as db:
            active = set(
                db.scalars(
                    select(EngineInstance.active_execution_id).where(
                        EngineInstance.active_execution_id.is_not(None)
                    )
                )
            )
        for finished in self.channels.keys() - active:
            self.channels.pop(finished).close()
        for finished in processed - active:
            progress(FILE_GRACE)
            cleanup_inputs(self.sessions, self.store, finished)

    def drained(self):
        """停机时等待已授权执行结束或隔离，暂时观测不到引擎时继续核对。"""
        with self.sessions.begin() as db:
            query = (
                select(EngineInstance.id)
                .join(Execution, Execution.id == EngineInstance.active_execution_id)
                .where(
                    EngineInstance.quarantine_reason.is_(None),
                    # 已授权的 UNCONFIRMED 执行仍可能在生成，应继续使用原核对期限。
                    Execution.phase.not_in(("QUARANTINED", "PREPARING", "PREPARED")),
                )
            )
            return db.scalar(query.limit(1)) is None

    def close(self):
        """关闭各执行的进度连接，释放协调器资源。"""
        for channel in self.channels.values():
            channel.close()
        self.channels.clear()


def run_worker(settings, *, once=False):
    """持有单协调器进程锁循环恢复与领取，数据库故障时等待下一次核对。"""
    from mycomfy.artifacts.storage import LocalStore

    sessions = make_sessions(settings.database_url)
    with (
        coordinator_lock(settings.data_root),
        report_health(os.environ.get("MYCOMFY_WORKER_HEALTH_FILE")) as heartbeat,
    ):
        worker = Worker(settings, sessions, LocalStore(settings.data_root))
        worker.capacity.initialize(sessions)

        def request_drain(signum, frame):
            """停机信号先关闭领取，循环继续核对当前在途执行。"""
            worker.draining.set()

        previous = {}
        if not once:
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, request_drain)
        try:
            progress = heartbeat
            while True:
                try:
                    worker.tick(progress=progress)
                    # 即使队列始终为空，成功完成轮询也证明协调器和数据库可用。
                    heartbeat(settings.poll_seconds + POLL_GRACE)
                    progress = heartbeat
                    if once or (worker.draining.is_set() and worker.drained()):
                        return
                except SQLAlchemyError:
                    # 持续失败的循环不能通过短暂成功的子查询延长健康期限。
                    heartbeat(0)
                    progress = ignore_progress
                    if once:
                        raise
                    # 未确认的提交仍在数据库中占槽，重新连接后按原执行恢复。
                    log.error("Database unavailable during worker tick; retrying")
                time.sleep(settings.poll_seconds)
        finally:
            worker.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            sessions.kw["bind"].dispose()
