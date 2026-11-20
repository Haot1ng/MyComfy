"""用两个本机索引验证 GPU 锁选择的包来源及 pip 哈希安装。"""

import hashlib
import subprocess
import sys
from zipfile import ZipFile

import pytest

from mycomfy.engine.lockfile import generate_lock


def publish_wheel(index, name, source, dependency=None):
    """在 PEP 503 测试索引发布可识别来源的纯 Python 轮子和文件摘要。"""
    directory = index / name
    directory.mkdir(parents=True)
    wheel = directory / f"{name}-1.0-py3-none-any.whl"
    info = f"{name}-1.0.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
    if dependency:
        metadata += f"Requires-Dist: {dependency}\n"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(f"{name}.py", f"source = {source!r}\n")
        archive.writestr(f"{info}/METADATA", metadata)
        archive.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
        archive.writestr(f"{info}/RECORD", "")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    (directory / "index.html").write_text(
        f'<a href="{wheel.name}#sha256={digest}">{wheel.name}</a>\n'
    )
    return wheel


def test_resolve_and_install_pinned_sources(tmp_path, monkeypatch):
    """同名包同时存在时只从 CUDA 索引选 PyTorch，普通传递依赖使用常规索引。"""
    gpu = tmp_path / "gpu"
    base = tmp_path / "base"
    torch = publish_wheel(gpu, "torch", "cuda", "shared>=1")
    publish_wheel(gpu, "torchvision", "cuda", "torch>=1")
    publish_wheel(base, "torch", "cpu")
    shared = publish_wheel(base, "shared", "ordinary")
    publish_wheel(gpu, "shared", "shadowed")
    publish_wheel(base, "nodehelper", "custom-node")
    # 同时解析仓库与节点的 requirements.txt，整个测试无公网依赖。
    monkeypatch.setenv("UV_DEFAULT_INDEX", base.as_uri())
    monkeypatch.setenv("UV_OFFLINE", "1")
    req = tmp_path / "requirements.txt"
    req.write_text("nodehelper>=1\n")
    config = {"index_url": gpu.as_uri(), "torch": "1.0", "torchvision": "1.0"}
    lock = tmp_path / "requirements-gpu.lock"
    generate_lock(config, [req], lock)
    published = lock.read_text()
    assert torch.as_uri() in published and shared.as_uri() in published
    assert published.count("--hash=sha256:") == 4
    dest = tmp_path / "installed"
    subprocess.run(
        [
            sys._base_executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--require-hashes",
            "--target",
            str(dest),
            "-r",
            str(lock),
        ],
        check=True,
    )
    assert (dest / "torch.py").read_text() == "source = 'cuda'\n"
    assert (dest / "shared.py").read_text() == "source = 'ordinary'\n"

    # 若所选 GPU 索引缺少版本，不能从常规索引补包，也不能覆盖已发布的锁。
    with pytest.raises(subprocess.CalledProcessError):
        generate_lock({**config, "index_url": (tmp_path / "empty").as_uri()}, [req], lock)
    assert lock.read_text() == published
