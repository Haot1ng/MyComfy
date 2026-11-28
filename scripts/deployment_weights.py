"""为 Ref2VA 镜像准备本机 BuildKit 上下文，复用引擎的持久权重缓存。"""

import fcntl
import hashlib
import os
import re
import runpy
import subprocess
import tempfile
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

# 宿主与引擎使用同一发布逻辑，修复损坏对象时同步保留已安装版本的硬链接。
model_objects = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "src/mycomfy/engine/model_objects.py")
)
publish_verified_object = model_objects["publish_verified_object"]
reuse_model_object = model_objects["reuse_model_object"]


@contextmanager
def _source_stream(model, deployment, image, refresh):
    """打开配置的本地文件或 HTTPS 源，首次补齐缓存时优先从本机已有镜像提取。"""
    source = model["source"]
    remote = urlsplit(source)
    if not remote.scheme:
        # 将容器中的绝对 /deployment 路径转换回相同的宿主挂载，限制解析后的位置。
        path = Path(source)
        if path.is_relative_to("/deployment"):
            path = path.relative_to("/deployment")
        base = Path(deployment).resolve()
        path = (base / path).resolve()
        if not path.is_relative_to(base):
            raise ValueError(f"本地模型来源超出部署目录 {source}")
        with path.open("rb") as stream:
            yield stream
        return
    if remote.scheme != "https" or not remote.hostname:
        raise ValueError("模型来源须为 HTTPS 或部署目录内的本地路径")

    if not refresh:
        # 本地镜像可能是升级前唯一的权重副本。Docker 查询失败时停止，避免误触发下载。
        images = subprocess.check_output(
            ["docker", "image", "ls", "--quiet", "--no-trunc", image], text=True
        ).splitlines()
        if images:
            print("从本机已有引擎镜像恢复 Ref2VA 权重。", flush=True)
            command = [
                "docker",
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--entrypoint",
                "cat",
                images[0],
                "/opt/mycomfy/model-objects/" + model["sha256"],
            ]
            with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
                with process.stdout as stream:
                    yield stream
                if process.wait():
                    raise subprocess.CalledProcessError(process.returncode, command)
            return

    print("正在从配置的 HTTPS 来源读取 Ref2VA 权重。", flush=True)
    with urllib.request.urlopen(source, timeout=60) as stream:
        yield stream


def prepare_ref2va_context(model, data, deployment, image, *, refresh=False):
    """校验或补齐一个持久权重对象，返回只含该权重的本地 Docker 构建目录。"""
    digest = model["sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Ref2VA 需要 64 位小写 SHA-256")
    engines = Path(data) / "engines"
    obj = engines / "model-objects" / digest
    context = engines / "build-contexts" / "ref2va"
    obj.parent.mkdir(parents=True, exist_ok=True)
    context.mkdir(parents=True, exist_ok=True)
    target = context / digest
    with (engines / "deploy.lock").open("a") as guard:
        # 与安装器共用锁。并发的部署进程只需补齐一次同摘要的对象。
        fcntl.flock(guard, fcntl.LOCK_EX)
        valid = reuse_model_object(obj, digest, model["path"], context_file=target)
        if (obj.exists() or obj.is_symlink()) and not valid and not refresh:
            raise ValueError(f"Ref2VA 缓存摘要不符。可用 --refresh-weights 修复 {obj}")

        if refresh or not valid:
            with tempfile.TemporaryDirectory(dir=obj.parent) as temp:
                staged = Path(temp) / "weight"
                hasher = hashlib.sha256()
                with (
                    _source_stream(model, deployment, image, refresh) as src,
                    staged.open("xb") as out,
                ):
                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                        hasher.update(chunk)
                        # 刷新已验证的对象只比对新内容，不占用第二份权重的磁盘空间。
                        if not valid:
                            out.write(chunk)
                    if not valid:
                        out.flush()
                        os.fsync(out.fileno())
                if hasher.hexdigest() != digest:
                    raise ValueError("Ref2VA 来源文件 SHA-256 不符，已保留原缓存")
                if not valid:
                    publish_verified_object(staged, obj)

        # 只向 BuildKit 提供一个文件。硬链接不额外占用约 21 GB 的宿主磁盘。
        if target.is_symlink() or not target.is_file() or not obj.samefile(target):
            with tempfile.TemporaryDirectory(dir=context.parent) as temp:
                incoming = Path(temp) / "weight"
                os.link(obj, incoming)
                incoming.replace(target)
    return context
