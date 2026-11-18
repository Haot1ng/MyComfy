"""按目标机清单部署 ComfyUI、节点、Python 环境和模型。"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


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


class Deployment(Record):
    comfyui: Repo
    nodes: dict[Name, Repo] = Field(default_factory=dict)
    models: dict[Name, ModelFile]
    python: str = sys.executable
    requirements_lock: str
    requirements_sha256: Sha256
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


def copy_model(model, target, base):
    """复用通过摘要校验的模型，或流式下载到目标盘上再原子发布。"""
    if target.exists():
        if file_digest(target) != model.sha256:
            raise ValueError(f"已存在模型摘要不符，保留原文件 {model.path}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    source = urlsplit(model.source)
    if source.scheme and source.scheme != "https":
        raise ValueError("模型来源须为 HTTPS 或本机文件")
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            stream = (
                urllib.request.urlopen(model.source, timeout=60)
                if source.scheme
                else (base / model.source).open("rb")
            )
            with stream:
                shutil.copyfileobj(stream, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
            if file_digest(temporary) != model.sha256:
                raise ValueError(f"模型下载摘要不符 {model.path}")
            os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


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


def provision(manifest_path, root):
    """安装不可变版本，生成经过 GPU 探测的实例配置。"""
    manifest_path, root = Path(manifest_path).resolve(), Path(root).resolve()
    spec = Deployment.model_validate_json(manifest_path.read_bytes())
    base = manifest_path.parent
    lock = (base / spec.requirements_lock).resolve()
    if file_digest(lock) != spec.requirements_sha256:
        raise ValueError("Python 依赖锁文件摘要不符")
    root.mkdir(parents=True, exist_ok=True)
    with (root / "deploy.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        hardware = {gpu: probe_gpu(gpu) for gpu in {i.gpu for i in spec.instances}}
        # 安装环境路径固定，避免移动虚拟环境后破坏其可执行文件内的绝对路径。
        description = spec.model_dump(exclude={"instances"})
        release_id = hashlib.sha256(json.dumps(description, sort_keys=True).encode()).hexdigest()
        release = root / "releases" / release_id
        release.mkdir(parents=True, exist_ok=True)
        source = release / "ComfyUI"
        checkout(spec.comfyui, source, base)
        if not (source / "main.py").is_file():
            raise ValueError("ComfyUI 检出目录缺少 main.py")
        for name, node in spec.nodes.items():
            checkout(node, source / "custom_nodes" / name, base)
        for model in spec.models.values():
            cached = root / "model-objects" / model.sha256
            copy_model(model, cached, base)
            location = release / "models" / model.path
            location.parent.mkdir(parents=True, exist_ok=True)
            if not location.exists():
                os.link(cached, location)
            elif not location.samefile(cached):
                raise ValueError(f"模型路径存在冲突 {model.path}")
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
        write_json(
            extra,
            {
                "mycomfy": {
                    "base_path": str(release / "models"),
                    **{key: key for key in sorted(model_paths)},
                }
            },
        )
        verified = {
            "comfyui": spec.comfyui.commit,
            "nodes": {key: node.commit for key, node in spec.nodes.items()},
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


def main(argv=None):
    """从命令行校验部署清单或安装到指定根目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--root", type=Path, default=Path("var/comfy"))
    parser.add_argument("--check", action="store_true", help="校验清单及依赖锁摘要")
    args = parser.parse_args(argv)
    if args.check:
        spec = Deployment.model_validate_json(args.manifest.read_bytes())
        lock = args.manifest.parent / spec.requirements_lock
        if file_digest(lock) != spec.requirements_sha256:
            raise ValueError("依赖锁文件摘要不符")
        print(f"已校验 {len(spec.instances)} 个实例配置")
    else:
        print(provision(args.manifest, args.root))


if __name__ == "__main__":
    main()
