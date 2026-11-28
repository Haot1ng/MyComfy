"""按目标机清单部署 ComfyUI、节点、Python 环境和模型。"""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import bundled
from .model_objects import publish_verified_object, relink_model, reuse_model_object

Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
IMAGE_MODEL_OBJECTS = Path("/opt/mycomfy/model-objects")


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Repo(Record):
    url: str = Field(min_length=1)
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")


class ModelFile(Record):
    path: str
    sha256: Sha256
    source: str

    @model_validator(mode="after")
    def validate_path(self):
        """确保模型放进 ComfyUI 约定的分类目录。"""
        path = Path(self.path)
        if path.is_absolute() or len(path.parts) < 2 or ".." in path.parts:
            raise ValueError("模型路径须形如 checkpoints/模型名")
        if not re.fullmatch(r"[a-zA-Z0-9_]+", path.parts[0]):
            raise ValueError("模型目录名无效")
        return self


class TimeLimits(Record):
    observation_seconds: int = Field(gt=0)
    timeout_seconds: int = Field(gt=0)
    reconcile_seconds: int = Field(gt=0)


class InstanceSpec(TimeLimits):
    id: Name
    gpu: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    directed_interrupt: bool = False
    strategy_timeouts: dict[str, TimeLimits] = Field(default_factory=dict)


class EngineLayout(Record):
    """引擎输入和已锁定清单共用的模型目录与实例布局。"""

    models: dict[Name, ModelFile]
    instances: list[InstanceSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_locations(self):
        """拒绝端口、实例标识和模型目标路径冲突。"""
        for values in (
            [i.id for i in self.instances],
            [i.port for i in self.instances],
            [model.path for model in self.models.values()],
        ):
            if len(values) != len(set(values)):
                raise ValueError("实例标识、端口和模型路径必须各自唯一")
        return self


class Deployment(EngineLayout):
    comfyui: Repo
    nodes: dict[Name, Repo] = Field(default_factory=dict)
    bundled_nodes: dict[Name, Sha256] | None = None
    python: str = sys.executable
    requirements_lock: str
    requirements_sha256: Sha256


def file_digest(path):
    """计算文件的 SHA-256 摘要。"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    """原子写入 UTF-8 JSON，并同步文件和父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        name = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(name, path)
            fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            name.unlink(missing_ok=True)


def copy_model(model, target, base, *, refresh=False):
    """优先复用已校验的缓存和镜像，显式刷新时从配置的来源重新传输并校验。"""
    if target.exists():
        print("校验持久权重缓存", model.path, flush=True)
    cached = reuse_model_object(target, model.sha256, model.path)
    if not refresh:
        if cached:
            return
        if target.exists() or target.is_symlink():
            raise ValueError(f"已存在模型摘要不符，保留原文件 {model.path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    source = urlsplit(model.source)
    if source.scheme and source.scheme != "https":
        raise ValueError("模型来源须为 HTTPS 或本机文件")
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            # 首次安装可离线使用镜像内置副本；显式刷新远端权重必须实际访问原地址。
            packaged = IMAGE_MODEL_OBJECTS / model.sha256
            if not source.scheme:
                print("读取本地权重", model.path, flush=True)
                stream = (base / model.source).open("rb")
            elif not refresh and packaged.is_file():
                print("读取镜像内置权重", model.path, flush=True)
                stream = packaged.open("rb")
            else:
                print("显式重新下载权重" if refresh else "下载缺失权重", model.path, flush=True)
                stream = urllib.request.urlopen(model.source, timeout=60)
            digest = hashlib.sha256()
            with stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    if not cached:
                        output.write(chunk)
            if digest.hexdigest() != model.sha256:
                raise ValueError(f"模型内容摘要不符 {model.path}")
            # 同一摘要的健康缓存继续使用原 inode，运行中的各版本仍共享同一份权重。
            if cached:
                return
            output.flush()
            os.fsync(output.fileno())
            publish_verified_object(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def install_models(models, root, release, base, *, refresh_weights=False, prepared_weight=None):
    """按摘要准备一次模型数据，再将同一对象链接到各自的版本路径。"""
    verified = set()
    for model in models.values():
        cached = root / "model-objects" / model.sha256
        location = release / "models" / model.path
        if location.is_symlink() or (location.exists() and not location.is_file()):
            raise ValueError(f"模型路径存在冲突 {model.path}")
        if model.sha256 not in verified:
            # 宿主已刷新的镜像权重只能从持久对象交接；若对象丢失则提示重试。
            prepared = model.sha256 == prepared_weight
            if prepared and not cached.is_file():
                raise ValueError(f"宿主已刷新的权重缺少持久缓存 {model.path}")
            copy_model(model, cached, base, refresh=refresh_weights and not prepared)
            verified.add(model.sha256)
        location.parent.mkdir(parents=True, exist_ok=True)
        if not location.exists():
            os.link(cached, location)
        elif not location.samefile(cached):
            # 损坏缓存修复中断时，先前已修复的别名可能指向另一份同摘要文件。
            if not refresh_weights and file_digest(location) != model.sha256:
                raise ValueError(f"模型路径存在冲突 {model.path}")
            relink_model(cached, location)


def checkout(repo, target, base):
    """检出清单中的完整提交，重复运行时验证已有检出目录。"""
    if target.exists():
        head = subprocess.check_output(["git", "-C", target, "rev-parse", "HEAD"], text=True)
        changed = subprocess.run(
            ["git", "-C", target, "diff", "--quiet", "HEAD", "--"], check=False
        ).returncode
        if head.strip() != repo.commit or changed:
            raise ValueError(f"已有检出目录发生变化 {target}")
        return
    url = repo.url if urlsplit(repo.url).scheme else str((base / repo.url).resolve())
    if urlsplit(url).scheme not in ("https", ""):
        raise ValueError("Git 来源须为 HTTPS 或本机目录")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as incoming:
        repo_dir = Path(incoming) / "repo"
        subprocess.run(["git", "init", "-q", repo_dir], check=True)
        subprocess.run(
            ["git", "-C", repo_dir, "fetch", "--quiet", "--depth=1", url, repo.commit], check=True
        )
        subprocess.run(
            ["git", "-C", repo_dir, "checkout", "--quiet", "--detach", repo.commit], check=True
        )
        repo_dir.rename(target)


def probe_gpu(gpu):
    """从 NVIDIA 驱动读取目标 GPU 身份、驱动版本和显存。"""
    output = subprocess.check_output(
        [
            "nvidia-smi",
            "-i",
            gpu,
            "--query-gpu=uuid,name,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ],
        text=True,
        timeout=15,
    )
    gpu_id, name, driver, memory = [part.strip() for part in output.strip().split(",")]
    return {"uuid": gpu_id, "name": name, "driver": driver, "memory_mib": int(memory)}


def provision(manifest_path, root, *, refresh_weights=False, prepared_weight=None):
    """安装不可变版本，生成经过 GPU 探测的实例配置。"""
    manifest_path, root = Path(manifest_path).resolve(), Path(root).resolve()
    spec = Deployment.model_validate_json(manifest_path.read_bytes())
    if prepared_weight is not None and (
        not refresh_weights
        or prepared_weight not in {model.sha256 for model in spec.models.values()}
    ):
        raise ValueError("宿主已刷新的权重摘要必须是本次刷新清单中的模型")
    base = manifest_path.parent
    lock = (base / spec.requirements_lock).resolve()
    if file_digest(lock) != spec.requirements_sha256:
        raise ValueError("Python 依赖锁文件摘要不符")
    bundled_nodes = bundled.select_nodes(spec.comfyui.commit, spec.nodes, spec.bundled_nodes)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "deploy.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        hardware = {gpu: probe_gpu(gpu) for gpu in {i.gpu for i in spec.instances}}
        # 安装环境路径固定，避免移动虚拟环境后破坏其可执行文件内的绝对路径。
        # 纯 Python 内置节点按摘要独立安装，保持原有模型、ComfyUI 和 GPU 虚拟环境可复用。
        description = spec.model_dump(exclude={"instances", "bundled_nodes"})
        release_id = hashlib.sha256(json.dumps(description, sort_keys=True).encode()).hexdigest()
        release = root / "releases" / release_id
        release.mkdir(parents=True, exist_ok=True)
        source = release / "ComfyUI"
        checkout(spec.comfyui, source, base)
        if not (source / "main.py").is_file():
            raise ValueError("ComfyUI 检出目录缺少 main.py")
        for name, node in spec.nodes.items():
            checkout(node, source / "custom_nodes" / name, base)
        install_models(
            spec.models,
            root,
            release,
            base,
            refresh_weights=refresh_weights,
            prepared_weight=prepared_weight,
        )
        python = release / "venv" / "bin" / "python"
        if (
            not python.exists()
            or subprocess.run(
                [python, "-m", "pip", "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode
        ):
            subprocess.run([spec.python, "-m", "venv", release / "venv"], check=True)
        # 锁文件同时包含引擎、CUDA 轮子、自定义节点及其全部传递依赖。
        subprocess.run([python, "-m", "pip", "install", "--require-hashes", "-r", lock], check=True)
        for path in [source, *(source / "custom_nodes" / name for name in spec.nodes)]:
            reqs = path / "requirements.txt"
            if reqs.exists():
                report = release / "requirements-check.json"
                subprocess.run(
                    [
                        python,
                        "-m",
                        "pip",
                        "install",
                        "--dry-run",
                        "--report",
                        str(report),
                        "--no-index",
                        "--no-deps",
                        "-r",
                        reqs,
                    ],
                    check=True,
                )
                if json.loads(report.read_bytes())["install"]:
                    raise ValueError(f"锁定环境缺少组件依赖 {reqs}")
        subprocess.run([python, "-m", "pip", "check"], check=True)
        for gpu, details in hardware.items():
            env = dict(os.environ, CUDA_VISIBLE_DEVICES=gpu)
            probe = (
                "import json,torch; assert torch.cuda.is_available(), 'CUDA unavailable'; "
                "print(json.dumps({'torch':torch.__version__, 'cuda':torch.version.cuda}))"
            )
            details.update(
                json.loads(subprocess.check_output([python, "-c", probe], env=env, text=True))
            )
        model_paths = {Path(model.path).parts[0] for model in spec.models.values()}
        extra = release / "model-paths.yaml"
        paths = {
            "mycomfy": {
                "base_path": str(release / "models"),
                **{key: key for key in sorted(model_paths)},
            }
        }
        if bundled_nodes:
            version = bundled_nodes[bundled.H3_NODE]
            directory = bundled.install_node(root, version)
            paths["mycomfy_bundled"] = {"custom_nodes": str(directory)}
            # 每代运行时持有自己的搜索路径文件；切换节点版本不改写旧代次读到的路径。
            extra = root / "generated" / "paths" / release_id / f"{version}.yaml"
        if bundled_nodes and extra.exists():
            if json.loads(extra.read_bytes()) != paths:
                raise ValueError(f"已有模型或节点路径配置发生变化 {extra}")
        else:
            write_json(extra, paths)
        verified = {
            "comfyui": spec.comfyui.commit,
            "nodes": {**{key: node.commit for key, node in spec.nodes.items()}, **bundled_nodes},
            "models": {key: model.sha256 for key, model in spec.models.items()},
            "requirements_sha256": spec.requirements_sha256,
        }
        instances = []
        for instance in spec.instances:
            instances.append(
                {
                    **instance.model_dump(),
                    "root": str(root / "instances" / instance.id),
                    "python": str(python),
                    "entrypoint": str(source / "main.py"),
                    "extra_model_paths": str(extra),
                    "manifest": {**verified, "hardware": hardware[instance.gpu]},
                }
            )
        write_json(root / "engines.json", {"instances": instances})
    return root / "engines.json"
