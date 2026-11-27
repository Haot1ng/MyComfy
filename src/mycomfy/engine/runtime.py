"""管理本机 ComfyUI 实例和每次启动的持久身份。"""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import pwd
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from mycomfy.config import Settings
from mycomfy.database.models import Audit, EngineInstance, Execution
from mycomfy.database.schema import require_current_schema
from mycomfy.database.session import db_now, make_sessions, new_id

from .deploy import InstanceSpec, Record, write_json
from .node_catalog import NodeCatalogCollector
from .supervisor import descendants, process_identity

# 只有由进程树是否退出引起的隔离，才能用内核重启凭据解除。
EXIT_QUARANTINES = {
    "PROCESS_IDENTITY_UNCONFIRMED",
    "PROCESS_TREE_EXIT_UNCONFIRMED",
    "PROCESS_EXIT_UNCONFIRMED",
}


class EngineConfig(InstanceSpec):
    root: str
    python: str
    entrypoint: str
    extra_model_paths: str | None = None
    manifest: dict[str, Any]


class ConfigFile(Record):
    instances: list[EngineConfig]


def load_config(path):
    """读取已部署的多个实例并校验所有端口和工作目录彼此独立。"""
    configs = ConfigFile.model_validate_json(Path(path).read_bytes()).instances
    for values in (
        [i.id for i in configs],
        [i.port for i in configs],
        [str(Path(i.root).resolve()) for i in configs],
    ):
        if len(values) != len(set(values)):
            raise ValueError("实例 ID、监听端口或工作目录冲突")
    for cfg in configs:
        if not all(Path(v).is_absolute() for v in (cfg.root, cfg.python, cfg.entrypoint)):
            raise ValueError("实例根目录、解释器和入口应为绝对路径")
    return {i.id: i for i in configs}


def config_hash(config):
    """计算部署配置摘要以识别需要排空的更新。"""
    return hashlib.sha256(config.model_dump_json().encode()).hexdigest()


def read_json(path):
    """读取已原子发布的运行记录，文件尚未发布时返回空值。"""
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return None


def read_identity(path):
    """读取本机生成的进程身份，仅接受具有有效 PID、启动时刻和内核 UUID 的记录。"""
    try:
        if path.is_symlink():
            return None
        identity = read_json(path)
        if not isinstance(identity, dict):
            return None
        pid, ticks = identity.get("pid"), identity.get("ticks")
        if type(pid) is not int or pid <= 0 or type(ticks) is not int or ticks < 0:
            return None
        boot = identity.get("boot")
        parsed = uuid.UUID(boot) if isinstance(boot, str) else None
        if parsed and parsed.int and str(parsed) == boot:
            return identity
    except (OSError, ValueError):
        pass
    return None


def ended(runtime, generation):
    """检查退出凭据是否属于当前已持久化的实例代次。"""
    if not runtime.get("run_dir"):
        return False
    marker = Path(runtime["run_dir"]) / "ended.json"
    if marker.is_symlink():
        raise ValueError("退出凭据必须是本代次目录内的文件")
    evidence = read_json(marker)
    if evidence is None:
        return False
    if not isinstance(evidence, dict) or type(evidence.get("generation")) is not int:
        raise ValueError("退出凭据缺少有效的代次")
    return evidence["generation"] == generation


def stop_snapshot(sessions, instances):
    """Worker 排空后列出本机需要核验退出凭据的代次，阻止中断仍待核实的已提交执行。"""
    targets = []
    with sessions() as db:
        for row in db.scalars(select(EngineInstance).where(EngineInstance.id.in_(instances))):
            if not row.runtime.get("run_dir"):
                continue
            # 代次已有退出凭据时可继续停机，否则须确认活动执行尚未获准提交。
            if row.active_execution_id and not ended(row.runtime, row.generation):
                execution = db.get(Execution, row.active_execution_id)
                if execution is None or execution.authorized_at is not None:
                    raise SystemExit(
                        f"实例 {row.id} 仍有未核实的执行 {row.active_execution_id}，"
                        "请启动 Worker 并等待对账后重试停机。"
                    )
            targets.append({"generation": row.generation, "run_dir": row.runtime["run_dir"]})
    return targets


def stop_generation(sessions, instance_id, generation, timeout=12):
    """关闭指定代次的接单，等待整棵进程树退出，保留原有槽位供 Worker 核对。"""
    targets = select(EngineInstance).where(
        EngineInstance.id == instance_id, EngineInstance.generation == generation
    )
    return _stop_generations(sessions, targets, timeout)


def _stop_generations(sessions, targets, timeout=12, *, allow_empty=False):
    """通知选定代次的监护程序停止，在同一个等待窗口内核验各自的退出凭据。"""
    pending = {}
    with sessions.begin() as db:
        for row in db.scalars(targets.with_for_update()):
            if row.runtime.get("run_dir"):
                row.accepting = False
                row.runtime = {**row.runtime, "stop_requested": True}
                pending[(row.id, row.generation)] = row.runtime
    if not pending:
        # 服务可能在首次启动引擎前收到停机信号；定向停止仍要求找到实际代次。
        return allow_empty
    # 各代次先收到自己的停止文件，避免多个 GPU 的退出时间串行累加。
    for runtime in pending.values():
        directory = Path(runtime["run_dir"])
        if directory.exists():
            (directory / "stop").touch(exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        pending = {key: state for key, state in pending.items() if not ended(state, key[1])}
        if not pending:
            return True
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    # 保留尚未证实已退出的执行和槽位，交给后续恢复与人工核对。
    all_confirmed = True
    with sessions.begin() as db:
        for (instance_id, generation), state in pending.items():
            row = db.get(EngineInstance, instance_id, with_for_update=True)
            # 等待行锁时管理器可能刚写入掉电凭据，再查一次以免重新隔离已恢复的实例。
            if ended(state, generation):
                continue
            all_confirmed = False
            if row and row.generation == generation and not row.quarantine_reason:
                row.quarantine_reason = "PROCESS_TREE_EXIT_UNCONFIRMED"
                row.accepting = False
    return all_confirmed


def owns_port(pid, port):
    """核验监听回环端口的套接字确实由本代次引擎进程树持有。"""
    listeners = set()
    for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
        fields = line.split()
        if fields[1] == f"0100007F:{port:04X}" and fields[3] == "0A":
            listeners.add("socket:[" + fields[9] + "]")
    if not listeners:
        return False
    for member in descendants(pid):
        for descriptor in Path(f"/proc/{member}/fd").glob("*"):
            try:
                if os.readlink(descriptor) in listeners:
                    return True
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
    return False


def probe_generation(runtime):
    """核对监护进程、进程树的端口与 ComfyUI 队列摘要，供运行管理和就绪探测共用。"""
    identity = read_identity(Path(runtime["run_dir"]) / "identity.json")
    alive = bool(identity and process_identity(identity["pid"]) == identity)
    reachable = False
    port = int(runtime["base_url"].rsplit(":", 1)[-1])
    if alive and owns_port(identity["pid"], port):
        # 只访问通过进程树所有权核对的端口，避免把占用旧端口的其他服务当作新代次。
        try:
            with httpx.Client(trust_env=False, timeout=1) as client:
                # GET /prompt 仅读取队列计数，避免频繁探活触发引擎 HTTP 线程同步查询显存。
                response = client.get(runtime["base_url"] + "/prompt")
                response.raise_for_status()
                remaining = response.json()["exec_info"]["queue_remaining"]
                reachable = type(remaining) is int and remaining >= 0
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
    return identity, alive, reachable


def runtime_ready(sessions, configs):
    """至少一个当前配置的代次有新鲜心跳且实际可访问时就绪，包括仍在生成的代次。"""
    with sessions() as db:
        now = db_now(db)
        rows = db.scalars(
            select(EngineInstance).where(
                EngineInstance.configured,
                EngineInstance.accepting,
                EngineInstance.quarantine_reason.is_(None),
            )
        ).all()
    for row in rows:
        config = configs.get(row.id)
        state = row.runtime
        if (
            config
            and config_hash(config) == state.get("config_hash")
            and row.heartbeat_at
            and row.heartbeat_at >= now - timedelta(seconds=state.get("stale_seconds", 20))
            and not ended(state, row.generation)
            and probe_generation(state)[2]
        ):
            return True
    return False


def sample_gpu(gpu, previous):
    """采样 GPU 显存及使用率，保留本代次峰值和采样时间。"""
    try:
        query = subprocess.check_output(
            [
                "nvidia-smi",
                "-i",
                gpu,
                "--query-gpu=index,name,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        )
        index, name, used, total, load = [p.strip() for p in query.strip().split(",")]
        sample = {
            "sampled_at": datetime.now(UTC).isoformat(),
            "gpu_index": index,
            "gpu_name": name,
        }
        # WSL 等驱动可能只为部分指标返回数字，其余列显示 [N/A]。
        for key, value in (
            ("gpu_memory_used_mib", used),
            ("gpu_memory_total_mib", total),
            ("gpu_utilization_percent", load),
        ):
            if value.isdecimal():
                sample[key] = int(value)
        if used.isdecimal() or "gpu_memory_peak_mib" in previous:
            sample["gpu_memory_peak_mib"] = max(
                sample.get("gpu_memory_used_mib", 0), previous.get("gpu_memory_peak_mib", 0)
            )
        return sample
    except (OSError, subprocess.SubprocessError, ValueError):
        return previous


class Manager:
    def __init__(self, sessions):
        """保存数据库会话工厂及本进程发起的子进程回收句柄。"""
        self.sessions = sessions
        self.children = {}
        self.node_catalog = NodeCatalogCollector(sessions)

    def configure(self, configs):
        """登记部署能力，并立即停止向已移除或需升级的实例派单。"""
        with self.sessions.begin() as db:
            rows = {row.id: row for row in db.scalars(select(EngineInstance).with_for_update())}
            for key, config in configs.items():
                row = rows.pop(key, None)
                if row is None:
                    db.add(
                        EngineInstance(
                            id=key,
                            generation=0,
                            manifest=config.manifest,
                            runtime={"configured_manifest": config.manifest},
                        )
                    )
                else:
                    row.configured = True
                    if row.runtime.get("configured_manifest") != config.manifest:
                        row.runtime = {**row.runtime, "configured_manifest": config.manifest}
                    if not row.runtime.get("run_dir"):
                        row.manifest = config.manifest
                    if row.runtime.get("config_hash") != config_hash(config):
                        row.accepting = False
            for row in rows.values():
                row.configured = False
                row.accepting = False

    def launch(self, config, previous):
        """先持久化代次和启动意图，再创建唯一的一棵引擎进程树。"""
        generation = previous + 1
        directory = Path(config.root) / f"g{generation}-{uuid.uuid4().hex}"
        runtime = {
            "run_dir": str(directory),
            "base_url": f"http://127.0.0.1:{config.port}",
            "log_path": str(directory / "engine.log"),
            "config_hash": config_hash(config),
            "configured_manifest": config.manifest,
            "strategy_timeouts": {k: v.model_dump() for k, v in config.strategy_timeouts.items()},
            "launched_at": time.time(),
            "stale_seconds": 20,
            "directed_interrupt": config.directed_interrupt,
            "observation_seconds": config.observation_seconds,
            "timeout_seconds": config.timeout_seconds,
            "reconcile_seconds": config.reconcile_seconds,
            "gpu": config.gpu,
        }
        for name in ("input", "output", "temp", "state"):
            path = directory / name
            path.mkdir(parents=True, exist_ok=True)
            runtime[name + "_dir"] = str(path)
        command = [
            config.python,
            config.entrypoint,
            "--listen",
            "127.0.0.1",
            "--port",
            str(config.port),
            "--input-directory",
            runtime["input_dir"],
            "--output-directory",
            runtime["output_dir"],
            "--temp-directory",
            runtime["temp_dir"],
            "--user-directory",
            runtime["state_dir"],
        ]
        if config.extra_model_paths:
            command.extend(["--extra-model-paths-config", config.extra_model_paths])
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            in {
                "PATH",
                "HOME",
                "LANG",
                "LC_ALL",
                "CUDA_HOME",
                "CUDA_PATH",
                "LD_LIBRARY_PATH",
                "CUDA_DEVICE_ORDER",
                "SSL_CERT_FILE",
            }
        }
        # Compose 可用宿主 UID 运行。即使镜像没有对应账号，也让 PyTorch 能解析缓存用户名。
        uid = os.getuid()
        try:
            env["USER"] = pwd.getpwuid(uid).pw_name
        except KeyError:
            env["USER"] = str(uid)
        env.update(
            {
                "CUDA_VISIBLE_DEVICES": config.gpu,
                "PYTHONUNBUFFERED": "1",
                "TMPDIR": runtime["temp_dir"],
            }
        )
        write_json(
            directory / "launch.json",
            {
                "generation": generation,
                "cwd": str(Path(config.entrypoint).parent),
                "command": command,
                "environment": env,
            },
        )
        with self.sessions.begin() as db:
            row = db.get(EngineInstance, config.id, with_for_update=True)
            if (
                not row
                or not row.configured
                or row.generation != previous
                or row.active_execution_id
                or row.quarantine_reason
                or row.runtime.get("paused")
            ):
                return
            row.runtime = runtime
            row.manifest = config.manifest
            row.generation = generation
            row.accepting = False
            row.heartbeat_at = db_now(db)
        try:
            self.children[config.id] = subprocess.Popen(
                [sys.executable, "-m", "mycomfy.engine.supervisor", str(directory)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            write_json(
                directory / "ended.json",
                {"generation": generation, "reason": "wrapper launch failed"},
            )
            raise

    def tick(self, configs):
        """根据持久凭据周期性接管、采样、排空与重启各实例。"""
        self.configure(configs)
        with self.sessions.begin() as db:
            instances = db.scalars(select(EngineInstance)).all()
        self.children = {k: p for k, p in self.children.items() if p.poll() is None}
        for row in instances:
            self.inspect(row, configs.get(row.id))

    def recover_after_reboot(self, snapshot, config, directory):
        """用原 GPU 机的本地代次记录核实旧内核已退出，保留槽位给 Worker 核对。"""
        generation = snapshot.generation
        if (
            directory.parent.resolve() != Path(config.root).resolve()
            or directory.is_symlink()
            or not re.fullmatch(rf"g{generation}-[0-9a-f]{{32}}", directory.name)
        ):
            return False
        identity = read_identity(directory / "identity.json")
        if identity is None:
            return False
        previous = identity["boot"]
        try:
            current_boot = uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip())
            if str(current_boot) == previous or not current_boot.int:
                return False
            # 核对该目录的启动代次。若另有子进程身份，两份身份须来自同一内核。
            launch_path = directory / "launch.json"
            if launch_path.is_symlink():
                return False
            launch = read_json(launch_path)
            if (
                not isinstance(launch, dict)
                or type(launch.get("generation")) is not int
                or launch.get("generation") != generation
            ):
                return False
            child = directory / "child.json"
            if child.is_symlink() or (
                child.exists() and (read_identity(child) or {}).get("boot") != previous
            ):
                return False
        except (OSError, ValueError):
            return False

        with self.sessions.begin() as db:
            row = db.get(EngineInstance, snapshot.id, with_for_update=True)
            if (
                not row
                or row.generation != generation
                or row.runtime.get("run_dir") != str(directory)
                or (row.quarantine_reason and row.quarantine_reason not in EXIT_QUARANTINES)
            ):
                return False
            marker = directory / "ended.json"
            if marker.is_symlink():
                return False
            if marker.exists():
                # 文件先于数据库提交。若进程在两者之间中断，重用本代原凭据，保留首次观察时间。
                receipt = read_json(marker)
                if not (
                    isinstance(receipt, dict)
                    and receipt.get("generation") == generation
                    and receipt.get("reason") == "HOST_REBOOT_CONFIRMED"
                    and receipt.get("previous_boot_id") == previous
                ):
                    return False
            else:
                write_json(
                    marker,
                    {
                        "generation": generation,
                        "reason": "HOST_REBOOT_CONFIRMED",
                        "previous_boot_id": previous,
                        "current_boot_id": str(current_boot),
                        "observed_at": datetime.now(UTC).isoformat(),
                    },
                )
            row.quarantine_reason = None
            row.accepting = False
            row.heartbeat_at = db_now(db)
            row.runtime = {
                **row.runtime,
                "pid": None,
                "process_alive": False,
                "api_reachable": False,
            }
        return True

    def inspect(self, snapshot, config):
        """核对一个代次的进程身份、端口和 HTTP 接单健康状况。"""
        state = snapshot.runtime
        generation = snapshot.generation
        directory = Path(state["run_dir"]) if state.get("run_dir") else None
        finished = ended(state, generation)
        paused = state.get("paused", False)
        identity, alive, reachable = None, False, False
        if directory and not finished:
            identity, alive, reachable = probe_generation(state)
        # 既要处理首次发现的掉电，也要修复先前版本留下的退出类隔离。
        needs_exit_proof = snapshot.quarantine_reason in EXIT_QUARANTINES or (
            not finished and snapshot.quarantine_reason is None
        )
        if (
            directory
            and config
            and not alive
            and needs_exit_proof
            and self.recover_after_reboot(snapshot, config, directory)
        ):
            return
        if not directory or finished:
            if (
                config
                and not paused
                and not snapshot.quarantine_reason
                and not snapshot.active_execution_id
            ):
                self.launch(config, generation)
                return
        elif (
            not config or paused or config_hash(config) != state["config_hash"]
        ) and not snapshot.active_execution_id:
            stop_generation(self.sessions, snapshot.id, generation)
            identity, alive, reachable = probe_generation(state)
        metrics = state.get("metrics", {})
        if config and time.time() - state.get("metrics_checked", 0) >= 15:
            metrics = sample_gpu(config.gpu, metrics)
            checked = time.time()
        else:
            checked = state.get("metrics_checked", 0)
        with self.sessions.begin() as db:
            row = db.get(EngineInstance, snapshot.id, with_for_update=True)
            if row.generation != generation:
                return
            row.runtime = {
                **row.runtime,
                "process_alive": alive,
                "api_reachable": reachable,
                "pid": identity["pid"] if identity else None,
                "metrics": metrics,
                "metrics_checked": checked,
            }
            if (
                directory
                and not finished
                and not alive
                and not row.quarantine_reason
                and time.time() - state["launched_at"] > 15
                and not ended(state, generation)
            ):
                row.quarantine_reason = "PROCESS_IDENTITY_UNCONFIRMED"
            row.heartbeat_at = db_now(db)
            row.accepting = bool(
                reachable
                and config
                and row.configured
                and config_hash(config) == state["config_hash"]
                and not row.quarantine_reason
                and not row.runtime.get("paused")
                and not row.runtime.get("stop_requested")
            )
        if reachable and config and config_hash(config) == state["config_hash"]:
            # 只有本代进程树和监听端口通过探测后，才读取这个引擎的节点清单。
            self.node_catalog.observe(snapshot.id, generation, state["base_url"])


def administrative_action(sessions, name, instance_id, generation, reason):
    """对指定代次执行暂停、恢复或人工核实后的解除隔离操作并审计。"""
    with sessions.begin() as db:
        row = db.get(EngineInstance, instance_id, with_for_update=True)
        if not row or row.generation != generation:
            raise ValueError("实例代次已改变")
        data = dict(row.runtime)
        if name == "resolve":
            if not row.quarantine_reason or not reason:
                raise ValueError("解除隔离需要现场核实依据")
            directory = Path(data["run_dir"])
            # 挂起的旧监护程序即使稍后恢复，也必须先看到该代次的停止请求。
            (directory / "stop").touch(exist_ok=True)
            identity = read_json(directory / "identity.json")
            if identity and process_identity(identity["pid"]) == identity:
                raise ValueError("监护进程仍存活，请先核实旧代次的进程树")
            write_json(
                directory / "ended.json",
                {"generation": generation, "reason": reason, "verified_by_uid": os.getuid()},
            )
            row.quarantine_reason = None
        else:
            data["paused"] = name == "drain"
            row.runtime = data
        row.accepting = False
        db.add(
            Audit(
                id=new_id("r"),
                actor=f"uid_{os.getuid()}",
                action=f"runtime.{name}.g{generation}",
                result=200,
                duration_ms=0,
                target=instance_id,
                created_at=db_now(db),
            )
        )


def main(argv=None):
    """从命令行运行监护循环，或对实例执行具备代次校验的管理操作。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["serve", "ready", "apply", "drain", "resume", "resolve", "stop-snapshot"]
    )
    parser.add_argument("--instance")
    parser.add_argument("--generation", type=int)
    parser.add_argument("--reason")
    parser.add_argument(
        "--stop-engines-on-exit", action="store_true", help="容器停机时等待受管引擎进程树退出"
    )
    args = parser.parse_args(argv)
    if args.stop_engines_on_exit and args.action != "serve":
        parser.error("--stop-engines-on-exit 仅用于 serve")
    settings = Settings.from_env()
    require_current_schema(settings.database_url)
    sessions = make_sessions(settings.database_url)
    if args.action in {"drain", "resume", "resolve"}:
        if args.instance is None or args.generation is None:
            parser.error("管理操作需要 --instance 和 --generation")
        administrative_action(sessions, args.action, args.instance, args.generation, args.reason)
        return
    if settings.engine_config is None:
        parser.error("需要 MYCOMFY_ENGINE_CONFIG")
    if args.action == "stop-snapshot":
        # 停机脚本读取 JSON 后再停止服务。读取现场时运行管理仍持有 runtime.lock。
        print(json.dumps(stop_snapshot(sessions, load_config(settings.engine_config))))
        return
    if args.action == "ready":
        # 探测单独运行，不会和正在管理引擎的进程竞争 runtime.lock。
        if not runtime_ready(sessions, load_config(settings.engine_config)):
            raise SystemExit(1)
        return
    settings.data_root.mkdir(parents=True, exist_ok=True)
    with (settings.data_root / "runtime.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manager = Manager(sessions)
        if args.action == "apply":
            manager.configure(load_config(settings.engine_config))
            return
        stopping = Event()

        def request_stop(signum, frame):
            """通知管理循环结束本轮检查并关闭受管代次。"""
            stopping.set()

        handlers = {}
        if args.stop_engines_on_exit:
            for sig in (signal.SIGTERM, signal.SIGINT):
                handlers[sig] = signal.signal(sig, request_stop)
        try:
            while not stopping.is_set():
                try:
                    manager.tick(load_config(settings.engine_config))
                except (SQLAlchemyError, OSError, ValueError) as exc:
                    # 数据库或配置临时不可用时保留全部身份记录。下一轮从持久事实接管。
                    logging.getLogger(__name__).error("运行管理等待重试 %s", type(exc).__name__)
                stopping.wait(2)
        finally:
            try:
                # 多卡部署可能还有排队采集，不让这些请求延长容器停机。
                manager.node_catalog.close()
                # 容器主进程退出将终止 PID 命名空间，先等所有受管代次落盘退出凭据。
                if args.stop_engines_on_exit and not _stop_generations(
                    sessions, select(EngineInstance), allow_empty=True
                ):
                    raise SystemExit("引擎代次退出凭据不完整，请检查隔离状态")
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
