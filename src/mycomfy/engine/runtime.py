"""管理本机 ComfyUI 实例和每次启动的持久身份。"""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from mycomfy.config import Settings
from mycomfy.database.models import Audit, EngineInstance
from mycomfy.database.schema import require_current_schema
from mycomfy.database.session import db_now, make_sessions, new_id

from .deploy import InstanceSpec, Record, write_json
from .supervisor import descendants, process_identity


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


def ended(runtime, generation):
    """检查退出凭据是否属于当前已持久化的实例代次。"""
    if not runtime.get("run_dir"):
        return False
    evidence = read_json(Path(runtime["run_dir"]) / "ended.json")
    return bool(evidence and evidence["generation"] == generation)


def stop_generation(sessions, instance_id, generation, timeout=12):
    """关闭指定代次的接单，等待整棵进程树退出，保留原有槽位供 Worker 核对。"""
    with sessions.begin() as db:
        row = db.get(EngineInstance, instance_id, with_for_update=True)
        if not row or row.generation != generation or not row.runtime.get("run_dir"):
            return False
        row.accepting = False
        row.runtime = {**row.runtime, "stop_requested": True}
        runtime = dict(row.runtime)
    # 文件只对该代次的监护程序生效；不会因操作系统重新分配 PID 而停止其他进程。
    directory = Path(runtime["run_dir"])
    if directory.exists():
        (directory / "stop").touch(exist_ok=True)
    deadline = time.monotonic() + timeout
    while not ended(runtime, generation) and time.monotonic() < deadline:
        time.sleep(0.1)
    if ended(runtime, generation):
        return True
    with sessions.begin() as db:
        row = db.get(EngineInstance, instance_id, with_for_update=True)
        if row and row.generation == generation:
            row.quarantine_reason = "PROCESS_TREE_EXIT_UNCONFIRMED"
            row.accepting = False
    return False


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
        return {
            "sampled_at": datetime.now(UTC).isoformat(),
            "gpu_index": index,
            "gpu_name": name,
            "gpu_memory_used_mib": int(used),
            "gpu_memory_total_mib": int(total),
            "gpu_utilization_percent": int(load),
            "gpu_memory_peak_mib": max(int(used), previous.get("gpu_memory_peak_mib", 0)),
        }
    except (OSError, subprocess.SubprocessError, ValueError):
        return previous


class Manager:
    def __init__(self, sessions):
        """保存数据库会话工厂及本进程发起的子进程回收句柄。"""
        self.sessions = sessions
        self.children = {}

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

    def inspect(self, snapshot, config):
        """核对一个代次的进程身份、端口和 HTTP 接单健康状况。"""
        state = snapshot.runtime
        generation = snapshot.generation
        directory = Path(state["run_dir"]) if state.get("run_dir") else None
        finished = ended(state, generation)
        paused = state.get("paused", False)
        if not directory or finished:
            if (
                config
                and not paused
                and not snapshot.quarantine_reason
                and not snapshot.active_execution_id
            ):
                self.launch(config, generation)
                return
            alive = reachable = False
            identity = None
        else:
            if (
                not config or paused or config_hash(config) != state["config_hash"]
            ) and not snapshot.active_execution_id:
                stop_generation(self.sessions, snapshot.id, generation)
            identity = read_json(directory / "identity.json")
            alive = bool(identity and process_identity(identity["pid"]) == identity)
            reachable = False
            port = int(state["base_url"].rsplit(":", 1)[-1])
            if alive and owns_port(identity["pid"], port):
                try:
                    with httpx.Client(trust_env=False, timeout=1) as client:
                        response = client.get(state["base_url"] + "/system_stats")
                        reachable = response.is_success and isinstance(response.json(), dict)
                except (httpx.HTTPError, ValueError):
                    pass
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
    parser.add_argument("action", choices=["serve", "apply", "drain", "resume", "resolve"])
    parser.add_argument("--instance")
    parser.add_argument("--generation", type=int)
    parser.add_argument("--reason")
    args = parser.parse_args(argv)
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
    settings.data_root.mkdir(parents=True, exist_ok=True)
    with (settings.data_root / "runtime.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manager = Manager(sessions)
        if args.action == "apply":
            manager.configure(load_config(settings.engine_config))
            return
        while True:
            try:
                manager.tick(load_config(settings.engine_config))
            except (SQLAlchemyError, OSError, ValueError) as exc:
                # 数据库或配置临时不可用时保留全部身份记录。下一轮从持久事实接管。
                logging.getLogger(__name__).error("运行管理等待重试 %s", type(exc).__name__)
            time.sleep(2)


if __name__ == "__main__":
    main()
