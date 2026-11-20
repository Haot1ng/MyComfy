"""在本机临时仓库及临时虚拟环境中验证可重复部署。"""

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from mycomfy.engine import deploy
from mycomfy.engine.runtime import load_config


def create_repository(path, files):
    """创建有固定提交的本机 Git 仓库作为部署源。"""
    subprocess.run(["git", "init", "-q", path], check=True)
    for name, data in files.items():
        (path / name).write_text(data)
    subprocess.run(["git", "-C", path, "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            path,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test",
            "commit",
            "-qm",
            "test fixture",
        ],
        check=True,
    )
    commit = subprocess.check_output(["git", "-C", path, "rev-parse", "HEAD"], text=True).strip()
    return {"url": str(path), "commit": commit}


def make_local_wheel(path):
    """制作声明 CUDA 可用的测试轮子，供无 GPU 的安装流程验证使用。"""
    wheel = path / "test_engine_probe-1.0-py3-none-any.whl"
    metadata = "test_engine_probe-1.0.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            metadata + "/METADATA", "Metadata-Version: 2.1\nName: test-engine-probe\nVersion: 1.0\n"
        )
        archive.writestr(
            metadata + "/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        )
        archive.writestr(metadata + "/RECORD", "")
        archive.writestr(
            "torch.py",
            '''
from types import SimpleNamespace
__version__ = "test"
version = SimpleNamespace(cuda="test")
def available():
    """返回测试 GPU 的模拟可用状态。"""
    return True
cuda = SimpleNamespace(is_available=available)
''',
        )
    return wheel


def test_verified_deploy_twice_offline(tmp_path, monkeypatch):
    """验证锁定源码和节点的部署、重复复用、GPU 配置及模型损坏检测。"""
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    monkeypatch.setattr(deploy, "probe_gpu", lambda _gpu: {"uuid": "test-gpu"})
    engine = create_repository(
        tmp_path / "engine", {"main.py": "", "requirements.txt": "test-engine-probe==1.0\n"}
    )
    node = create_repository(tmp_path / "node", {"__init__.py": ""})
    wheel = make_local_wheel(tmp_path)
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        f"test-engine-probe @ {wheel.as_uri()} --hash=sha256:{deploy.file_digest(wheel)}\n"
    )
    model = tmp_path / "input.bin"
    model.write_bytes(b"sample-model")
    model_hash = deploy.file_digest(model)
    spec = {
        "comfyui": engine,
        "nodes": {"Example": node},
        "models": {
            "Example": {
                "path": "checkpoints/sample.bin",
                "source": model.name,
                "sha256": model_hash,
            }
        },
        "python": sys._base_executable,
        "requirements_lock": lock.name,
        "requirements_sha256": deploy.file_digest(lock),
        "instances": [
            {
                "id": "sample",
                "gpu": "0",
                "port": 8188,
                "observation_seconds": 60,
                "timeout_seconds": 360,
                "reconcile_seconds": 15,
            }
        ],
    }
    manifest = tmp_path / "deploy.json"
    manifest.write_text(json.dumps(spec))
    root = tmp_path / "install"
    cfg_path = deploy.provision(manifest, root)
    cfg = load_config(cfg_path)["sample"]
    assert cfg.manifest["comfyui"] == engine["commit"]
    assert cfg.manifest["nodes"]["Example"] == node["commit"]
    assert cfg.manifest["hardware"]["torch"] == "test"
    expected = cfg_path.read_bytes()
    model.unlink()
    deploy.provision(manifest, root)
    assert cfg_path.read_bytes() == expected
    (root / "model-objects" / model_hash).write_bytes(b"damaged")
    with pytest.raises(ValueError, match="摘要不符"):
        deploy.provision(manifest, root)
    assert cfg_path.read_bytes() == expected


def test_partial_model_never_becomes_visible(tmp_path):
    """验证失败的模型传输未发布目标文件，修复源文件后可重试。"""
    source = tmp_path / "model"
    target = tmp_path / "cache" / "target"
    source.write_bytes(b"partial")
    model = deploy.ModelFile(
        path="vae/test.bin", source=source.name, sha256=hashlib.sha256(b"complete").hexdigest()
    )
    with pytest.raises(ValueError, match="摘要不符"):
        deploy.copy_model(model, target, tmp_path)
    assert not target.exists()
    source.write_bytes(b"complete")
    deploy.copy_model(model, target, tmp_path)
    assert target.read_bytes() == b"complete"


def test_recipe_keeps_tag_and_lock_but_detects_changed_local_model(tmp_path, monkeypatch):
    """验证标签漂移、模型更新和缓存损坏时分别使用正确的锁与清单。"""
    from mycomfy.engine import recipe

    source = tmp_path / "engine"
    initial = create_repository(source, {"main.py": "", "requirements.txt": "torch\n"})
    subprocess.run(
        [
            "git",
            "-C",
            source,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "tag",
            "-am",
            "first",
            "v1",
        ],
        check=True,
    )
    model = tmp_path / "video.bin"
    model.write_bytes(b"original")
    settings = recipe.Recipe.model_validate(
        {
            "comfyui": {"url": "engine", "version": "v1"},
            "models": {"video": {"source": "video.bin", "path": "checkpoints/video.bin"}},
            "pytorch": {
                "index_url": "https://download.pytorch.org/whl/cu126",
                "torch": "2.7.1+cu126",
            },
        }
    )
    root = tmp_path / "data"
    calls = []

    def write_test_lock(pytorch, requirements, target):
        """记录参与解析的固定源码依赖，并写出供快照检测的测试锁。"""
        calls.append((pytorch["torch"], [path.read_text() for path in requirements]))
        target.write_text("# test lock\n")

    monkeypatch.setattr(recipe, "generate_lock", write_test_lock)
    first = recipe.materialize(settings, root, tmp_path)
    original = deploy.Deployment.model_validate_json(first.read_bytes())
    assert original.comfyui.commit == initial["commit"]
    assert original.models["video"].sha256 == deploy.file_digest(model)
    assert original.instances[0].id == "gpu0"

    # 输入标签由发布者移动。已归档的解析仍使用第一次实际部署的提交。
    subprocess.run(
        [
            "git",
            "-C",
            source,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "second",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", source, "tag", "-f", "v1"], check=True)
    assert recipe.materialize(settings, root, tmp_path) == first
    model.write_bytes(b"updated")
    second = recipe.materialize(settings, root, tmp_path)
    updated = deploy.Deployment.model_validate_json(second.read_bytes())
    assert second != first
    assert updated.models["video"].sha256 == deploy.file_digest(model)
    assert updated.comfyui == original.comfyui
    assert updated.requirements_lock == original.requirements_lock
    assert calls == [("2.7.1+cu126", ["torch\n"])]
    Path(original.requirements_lock).write_text("tampered")
    with pytest.raises(ValueError, match="依赖锁摘要不符"):
        recipe.materialize(settings, root, tmp_path)


def test_recipe_requires_remote_model_digest_before_resolving(tmp_path):
    """HTTPS 模型缺少摘要时先报告配置错误，避免开始锁定仓库或发布配置。"""
    from mycomfy.engine import recipe

    settings = recipe.Recipe.model_validate(
        {
            "comfyui": {"url": "unavailable", "version": "v1"},
            "pytorch": {
                "index_url": "https://download.pytorch.org/whl/cu126",
                "torch": "2.7.1+cu126",
            },
            "models": {"v": {"source": "https://example.invalid/v.bin", "path": "vae/v.bin"}},
        }
    )
    with pytest.raises(ValueError, match="SHA-256"):
        recipe.materialize(settings, tmp_path / "engines", tmp_path)
    assert not (tmp_path / "engines").exists()
