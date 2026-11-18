"""按实际文件系统合并水位与预留，使用 MySQL 锁串行化并发准入。"""

import fcntl
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from .db import db_now, new_id
from .errors import ApiError
from .models import Asset, Job, JobInput, StrategyVersion
from .ops_models import CapacityGate, DiskReservation
from .state import TERMINAL

MIB = 1024**2


@dataclass(frozen=True)
class Volume:
    path: Path
    stop: int
    resume: int
    keep_free: int


def lock_gate(db, key):
    """创建并锁定共享准入行；首次并发创建时采用保存点恢复。"""
    row = db.get(CapacityGate, key, with_for_update=True, populate_existing=True)
    if row is None:
        try:
            with db.begin_nested():
                db.add(CapacityGate(id=key, paused=False))
                db.flush()
        except IntegrityError:
            pass
        row = db.get(CapacityGate, key, with_for_update=True, populate_existing=True)
    return row


def release_reservation(db, owner):
    """在调用方事务中幂等释放该上传或执行持有的全部卷预留。"""
    db.execute(delete(DiskReservation).where(DiskReservation.owner == owner))


def reserve_publication(db, owner, outputs):
    """输出写入结束后按实际源大小重估待发布副本的剩余用量。"""
    lock_gate(db, "admission")
    rows = db.scalars(
        select(DiskReservation)
        .where(DiskReservation.owner == owner, DiskReservation.kind == "execution")
        .with_for_update()
    ).all()
    if not any(row.target_bytes > 0 for row in rows):
        # 旧迁移版本的预留缺少目标卷标识，保留到整组归档完成。
        return
    try:
        amount = sum(
            Path(source).stat().st_size for sources in outputs.values() for source in sources
        )
    except OSError:
        amount = None
    for row in rows:
        if row.target_bytes:
            row.kind = "publication"
            row.size = amount if amount is not None else row.target_bytes
            row.target_bytes = row.size
        else:
            db.delete(row)
    db.flush()


def credit_published(db, owner, size):
    """在首次登记 Asset 的事务中核销该件已落盘的正式文件。"""
    for row in db.scalars(
        select(DiskReservation)
        .where(DiskReservation.owner == owner, DiskReservation.kind == "publication")
        .with_for_update()
    ):
        row.size = max(0, row.size - size)
        row.target_bytes = row.size


def measured_volume(path):
    """定位目录的设备编号并读取其可用空间。"""
    try:
        return str(path.stat().st_dev), shutil.disk_usage(path).free
    except OSError as exc:
        raise ApiError(503, "STORAGE_UNAVAILABLE", "Storage volume is unavailable") from exc


class Capacity:
    def __init__(self, settings, policy=None):
        """解析部署配额和分卷水位；同一设备上的目录随后统一核算。"""
        policy = policy or {}
        self.root = settings.data_root
        self.watermark_engine = None
        self.locks = self.root / ".upload-leases"
        self.max_upload = settings.max_upload_bytes
        self.user_bytes = int(policy.get("user_upload_bytes", 10 * 1024 * MIB))
        self.user_jobs = int(policy.get("user_jobs", 200))
        self.global_jobs = int(policy.get("global_jobs", 2000))
        self.execution_bytes = int(policy.get("execution_bytes", 128 * MIB))
        self.publish_bytes = int(policy.get("publish_bytes", 64 * MIB))
        self.estimates = policy.get("strategies", {})
        if (
            min(
                self.user_bytes,
                self.user_jobs,
                self.global_jobs,
                self.execution_bytes,
                self.publish_bytes,
            )
            < 1
        ):
            raise ValueError("Capacity limits must be positive")
        self.volumes = {}
        defaults = {
            "storage": self.root,
            "database": self.root,
            "spool": Path(tempfile.gettempdir()),
        }
        groups = policy.get("volumes", {})
        for role in defaults.keys() | groups.keys():
            cfg = groups.get(role, {})
            path = Path(cfg.get("path", defaults.get(role, self.root)))
            stop = int(cfg.get("stop_bytes", 64 * MIB))
            resume = int(cfg.get("resume_bytes", 96 * MIB))
            reserve = int(cfg.get("reserve_bytes", 16 * MIB))
            if stop < 0 or resume <= stop or reserve < 0 or not path.is_absolute():
                raise ValueError(f"Invalid capacity volume {role}")
            self.volumes[role] = Volume(path, stop, resume, reserve)

    @classmethod
    def from_settings(cls, settings):
        """加载环境变量指定的 JSON 容量策略文件。"""
        path = os.environ.get("MYCOMFY_CAPACITY_CONFIG")
        return cls(settings, json.loads(Path(path).read_text()) if path else {})

    def initialize(self, sessions):
        """启动时核对已失去上传进程的预留，其他执行和发布引用保留。"""
        from .storage import LocalStore

        store = LocalStore(self.root)
        store.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.locks.mkdir(mode=0o700, parents=True, exist_ok=True)
        with sessions.begin() as db:
            lock_gate(db, "admission")
            owners = db.scalars(
                select(DiskReservation.owner).where(DiskReservation.kind == "upload").distinct()
            ).all()
            for owner in owners:
                lease = self.locks / owner
                with lease.open("a+b") as file:
                    try:
                        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    store.cleanup_upload(db, owner)
                    release_reservation(db, owner)
                    lease.unlink(missing_ok=True)

    def _group(self, needs):
        """按设备合并预计字节并采用各角色中最严格的阈值。"""
        groups = {}
        for volume, amount in needs:
            device, free = measured_volume(volume.path)
            group = groups.setdefault(
                device, {"free": free, "requested": 0, "stop": 0, "resume": 0}
            )
            group["free"] = min(group["free"], free)
            group["requested"] += amount
            group["stop"] = max(group["stop"], volume.stop + volume.keep_free)
            group["resume"] = max(group["resume"], volume.resume + volume.keep_free)
        return groups

    def _check(self, db, needs):
        """锁定准入行，检查扣除历史预留后的卷余量并持久化水位状态。"""
        lock_gate(db, "admission")
        groups = self._group(needs)
        for device, group in sorted(groups.items()):
            reserved = db.scalar(
                select(func.coalesce(func.sum(DiskReservation.size), 0)).where(
                    DiskReservation.device == device
                )
            )
            available = group["free"] - int(reserved)
            # 水位状态独立于业务提交；拒绝新工作后的事务回滚也须保持暂停状态。
            if db.bind.dialect.name == "mysql":
                # 独立状态连接不会与已占用的业务连接池互相等待。
                if self.watermark_engine is None:
                    self.watermark_engine = create_engine(
                        db.bind.url, poolclass=NullPool, isolation_level="READ COMMITTED"
                    )
                with Session(self.watermark_engine) as state_db, state_db.begin():
                    paused = self._latch(state_db, device, available, group)
            else:
                paused = self._latch(db, device, available, group)
            if paused or available - group["requested"] < group["stop"]:
                raise ApiError(503, "VOLUME_LOW", "Insufficient unreserved disk space")
        return groups

    @staticmethod
    def _latch(db, device, available, group):
        """按停止和恢复双阈值更新当前卷状态。"""
        state = lock_gate(db, "volume-" + device)
        if available < group["stop"]:
            state.paused = True
        elif available >= group["resume"]:
            state.paused = False
        return state.paused

    def check_batch(self, db, user_id, count):
        """在受理事务内复核水位、用户和全局队列名额。"""
        self._check(db, [(vol, 0) for role, vol in self.volumes.items() if role != "spool"])
        jobs = select(func.count()).select_from(Job).where(Job.state.not_in(TERMINAL))
        if db.scalar(jobs.where(Job.user_id == user_id)) + count > self.user_jobs:
            raise ApiError(429, "USER_JOB_QUOTA", "User queue limit reached")
        if db.scalar(jobs) + count > self.global_jobs:
            raise ApiError(503, "GLOBAL_QUEUE_FULL", "Global queue limit reached")

    def _reserve(self, db, owner, user_id, kind, groups, target_bytes, logical=0):
        """登记每个卷的预留，跨卷复制只记一次用户逻辑用量。"""
        target_device, _ = measured_volume(self.volumes["storage"].path)
        if target_device not in groups:
            raise ApiError(503, "VOLUME_CHANGED", "Storage mount changed during reservation")
        for device, group in groups.items():
            if group["requested"]:
                db.add(
                    DiskReservation(
                        owner=owner,
                        device=device,
                        size=group["requested"],
                        user_id=user_id,
                        kind=kind,
                        upload_bytes=logical,
                        target_bytes=target_bytes if device == target_device else 0,
                        created_at=db_now(db),
                    )
                )
                logical = 0
        db.flush()

    @contextmanager
    def upload(self, sessions, user_id, upper_bound):
        """解析并写入上传体前预留空间；异常退出时利用进程锁恢复。"""
        if not 0 <= upper_bound <= self.max_upload:
            raise ApiError(413, "UPLOAD_TOO_LARGE", "Upload exceeds configured size")
        owner = new_id("up")
        lease = self.locks / owner
        with lease.open("xb") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            reserved = False
            try:
                with sessions.begin() as db:
                    groups = self._check(
                        db,
                        [
                            (self.volumes["storage"], upper_bound),
                            (self.volumes["spool"], upper_bound),
                            (self.volumes["database"], 0),
                        ],
                    )
                    used = db.scalar(
                        select(func.coalesce(func.sum(Asset.size), 0)).where(
                            Asset.user_id == user_id, Asset.job_id.is_(None)
                        )
                    )
                    pending = db.scalar(
                        select(func.coalesce(func.sum(DiskReservation.upload_bytes), 0)).where(
                            DiskReservation.user_id == user_id
                        )
                    )
                    if used + pending + upper_bound > self.user_bytes:
                        raise ApiError(429, "USER_STORAGE_QUOTA", "User upload limit reached")
                    self._reserve(
                        db, owner, user_id, "upload", groups, upper_bound, logical=upper_bound
                    )
                reserved = True
                yield owner
            finally:
                if reserved:
                    with sessions.begin() as db:
                        release_reservation(db, owner)
                lease.unlink(missing_ok=True)

    @staticmethod
    def finish_spool(sessions, owner):
        """表单完成解包后仅保留正式对象目标卷上尚待写入的份额。"""
        with sessions.begin() as db:
            rows = db.scalars(
                select(DiskReservation)
                .where(DiskReservation.owner == owner, DiskReservation.kind == "upload")
                .with_for_update()
            ).all()
            if any(row.target_bytes > 0 for row in rows):
                for row in rows:
                    row.size = row.target_bytes

    @staticmethod
    def credit_upload(db, owner, size):
        """在素材登记事务中把逻辑配额从预留转入实际用量，结清正式文件用量。"""
        for row in db.scalars(
            select(DiskReservation)
            .where(DiskReservation.owner == owner, DiskReservation.kind == "upload")
            .with_for_update()
        ):
            if row.target_bytes and size > row.target_bytes:
                raise ApiError(413, "UPLOAD_TOO_LARGE", "Upload exceeded its reserved limit")
            row.size = max(0, row.size - row.target_bytes)
            row.target_bytes = 0
            row.upload_bytes = 0

    def reserve_execution(self, db, execution_id, job, work_path):
        """领取事务按输入大小和策略峰值预留引擎工作区及正式归档空间。"""
        workspace = self.volumes.get("engine", self.volumes["storage"])
        working = Volume(Path(work_path), workspace.stop, workspace.resume, workspace.keep_free)
        version = db.get(StrategyVersion, job.strategy_version_id)
        override = self.estimates.get(version.strategy_key, {})
        source = int(
            db.scalar(
                select(func.coalesce(func.sum(Asset.size), 0))
                .join(JobInput, JobInput.asset_id == Asset.id)
                .where(JobInput.job_id == job.id)
            )
        )
        working_bytes = int(override.get("execution_bytes", self.execution_bytes))
        publish_bytes = int(override.get("publish_bytes", self.publish_bytes))
        if min(working_bytes, publish_bytes) <= 0:
            raise ValueError("Execution and publication estimates must be positive")
        groups = self._check(
            db,
            [
                (working, source + working_bytes),
                (self.volumes["storage"], publish_bytes),
                (self.volumes["database"], 0),
            ],
        )
        self._reserve(db, execution_id, job.user_id, "execution", groups, publish_bytes)

    release = staticmethod(release_reservation)
