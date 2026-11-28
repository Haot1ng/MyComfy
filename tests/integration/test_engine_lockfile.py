"""用两个本机索引验证 GPU 锁选择的包来源及 pip 哈希安装。"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
from pydantic import ValidationError

from mycomfy.engine import recipe
from mycomfy.engine.deploy import Repo
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


def test_sageattention_recipe_locks_index_or_verified_local_wheel(tmp_path, monkeypatch):
    """真实离线解析两种 SageAttention 来源，检查 Torch 固定版本、哈希安装和历史缓存复用。"""
    source = tmp_path / "comfy"
    source.mkdir()
    repo = Repo(url=str(source), commit="a" * 40)
    monkeypatch.setattr(recipe, "pin_repo", lambda *_args: repo)
    gpu = tmp_path / "gpu"
    base = tmp_path / "ordinary"
    torch = publish_wheel(gpu, "torch", "gpu")
    package = publish_wheel(base, "sageattention", "index", dependency="torch==1.0")
    local = publish_wheel(tmp_path / "wheel dir", "sageattention", "local", dependency="torch==1.0")
    digest = hashlib.sha256(local.read_bytes()).hexdigest()
    indexed = {"version": "1.0"}
    pinned = {"wheel": str(local.relative_to(tmp_path)), "sha256": digest}
    monkeypatch.setenv("UV_DEFAULT_INDEX", base.as_uri())
    monkeypatch.setenv("UV_OFFLINE", "1")
    config = {
        "comfyui": {"url": "comfy", "version": repo.commit},
        "pytorch": {"index_url": gpu.as_uri(), "torch": "1.0"},
    }

    def build_manifest(options):
        """从同一引擎配方生成锁快照，只替换可选的 SageAttention 设置。"""
        settings = recipe.Recipe.model_validate({**config, **options})
        path = recipe.materialize(settings, tmp_path / "engine", tmp_path)
        return json.loads(path.read_bytes())

    default = build_manifest({})
    assert build_manifest({"sageattention": None}) == default
    assert "verify_sageattention" not in default
    assert [
        line.split(" @ ")[0] for line in Path(default["requirements_lock"]).read_text().splitlines()
    ] == ["torch"]
    by_version = build_manifest({"sageattention": indexed})
    by_file = build_manifest({"sageattention": pinned})
    assert by_version["verify_sageattention"] and by_file["verify_sageattention"]
    assert package.as_uri() in Path(by_version["requirements_lock"]).read_text()
    lock = Path(by_file["requirements_lock"])
    assert f"sageattention @ {local.as_uri()} --hash=sha256:{digest}" in lock.read_text()
    assert torch.as_uri() in lock.read_text()
    assert len({m["requirements_lock"] for m in (default, by_version, by_file)}) == 3
    assert build_manifest({"sageattention": pinned}) == by_file
    target = tmp_path / "installed"
    subprocess.run(
        [
            sys._base_executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--require-hashes",
            "--target",
            str(target),
            "-r",
            str(lock),
        ],
        check=True,
    )
    assert (target / "sageattention.py").read_text() == "source = 'local'\n"

    # 原始 wheel 被替换时，已有锁不能使其绕过用户固定的哈希。
    local.write_bytes(b"modified")
    with pytest.raises(ValueError, match="SHA-256"):
        build_manifest({"sageattention": pinned})


@pytest.mark.parametrize(
    "invalid",
    [
        {"version": ">=1.0"},
        {"version": "1.0\n--extra-index-url x"},
        {"version": "1.0", "wheel": "other.whl"},
        {"version": "1.0", "sha256": "b" * 64},
        {"wheel": "sageattention-1.0-py3-none-any.whl"},
        {"wheel": "x", "sha256": "bad"},
        {},
        "1.0",
        True,
    ],
)
def test_sageattention_rejects_ambiguous_settings(invalid):
    """拒绝非精确版本、混合来源及未固定摘要的本地 wheel。"""
    with pytest.raises(ValidationError):
        recipe.Recipe.model_validate(
            {"sageattention": invalid, "pytorch": {}, "comfyui": {"url": "comfy", "version": "v1"}}
        )


@pytest.mark.parametrize(
    "name",
    [
        "../outside.whl",
        "/outside.whl",
        "https://example.org/x.whl",
        "another-1.0-py3-none-any.whl",
        "sageattention-1.0.tar.gz",
    ],
)
def test_sageattention_wheel_requires_local_package(tmp_path, name):
    """拒绝部署目录以外的路径、远程 URL、异名包和源码压缩文件。"""
    spec = recipe.SageAttentionWheel(wheel=name, sha256="c" * 64)
    with pytest.raises(ValueError, match="sageattention.wheel"):
        recipe.sageattention_requirement(spec, tmp_path)
