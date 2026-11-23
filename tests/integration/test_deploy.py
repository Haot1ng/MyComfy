"""在本机临时仓库及临时虚拟环境中验证可重复部署。"""

import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from mycomfy.engine import bundled, deploy
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

    # 将同一检出模拟为受支持的 H3 版本。手写清单升级后，旧进程仍能使用原路径与环境。
    monkeypatch.setattr(bundled, "H3_COMFYUI", engine["commit"])
    monkeypatch.setattr(bundled, "media_ready", lambda: True)
    original_paths = Path(cfg.extra_model_paths).read_bytes()
    assert bundled.H3_NODE not in cfg.manifest["nodes"]
    deploy.provision(manifest, root)
    upgraded = load_config(cfg_path)["sample"]
    version = bundled.node_digest(bundled.SOURCE)
    assert upgraded.python == cfg.python
    assert upgraded.entrypoint == cfg.entrypoint
    assert upgraded.manifest["nodes"] == {"Example": node["commit"], bundled.H3_NODE: version}
    paths = json.loads(Path(upgraded.extra_model_paths).read_bytes())
    assert json.loads(original_paths)["mycomfy"] == paths["mycomfy"]
    assert Path(cfg.extra_model_paths).read_bytes() == original_paths
    installed = Path(paths["mycomfy_bundled"]["custom_nodes"]) / bundled.H3_NODE
    assert bundled.node_digest(installed) == version
    assert sorted(p.name for p in installed.iterdir()) == list(bundled.H3_FILES)
    expected = cfg_path.read_bytes()
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
    monkeypatch.setattr(bundled, "H3_COMFYUI", initial["commit"])
    monkeypatch.setattr(bundled, "media_ready", lambda: True)
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
    assert original.bundled_nodes == {bundled.H3_NODE: bundled.node_digest(bundled.SOURCE)}
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


def test_bundled_node_preserves_old_versions_and_detects_tampering(tmp_path, monkeypatch):
    """升级只新增一份小节点包，旧版固定清单可重放，损坏的历史版本不能继续登记。"""
    source = tmp_path / "source"
    shutil.copytree(bundled.SOURCE, source, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(bundled, "SOURCE", source)
    root = tmp_path / "engines"
    first = bundled.node_digest(source)
    previous = bundled.install_node(root, first)
    # 模拟下一版应用仅改变分段实现，移除旧源码后仍能复用已安装的正确版本。
    with (source / "segment.py").open("a") as stream:
        stream.write("\n# next revision\n")
    second = bundled.node_digest(source)
    current = bundled.install_node(root, second)
    assert current != previous
    assert bundled.install_node(root, first) == previous
    assert bundled.node_digest(current / bundled.H3_NODE) == second
    with pytest.raises(ValueError, match="未提供"):
        bundled.install_node(root, "0" * 64)
    assert not (root / "bundled-nodes" / ("0" * 64)).exists()
    (previous / bundled.H3_NODE / "segment.py").write_text("damaged")
    with pytest.raises(ValueError, match="摘要不符"):
        bundled.install_node(root, first)
    assert bundled.install_node(root, second) == current


def test_bundled_node_requires_compatible_engine_and_media(monkeypatch):
    """手写清单与生成清单都须满足提交和编码工具要求，其他工作流仍可继续部署。"""
    monkeypatch.setattr(bundled, "media_ready", lambda: False)
    pinned = {bundled.H3_NODE: bundled.node_digest(bundled.SOURCE)}
    assert bundled.select_nodes("a" * 40, {}) == {}
    assert bundled.select_nodes(bundled.H3_COMFYUI, {}) == {}
    with pytest.raises(ValueError, match="libx264"):
        bundled.select_nodes(bundled.H3_COMFYUI, {}, pinned)
    with pytest.raises(ValueError, match="不兼容"):
        bundled.select_nodes("a" * 40, {}, pinned)
    with pytest.raises(ValueError, match="不兼容"):
        bundled.select_nodes(bundled.H3_COMFYUI, {bundled.H3_NODE: "external"}, pinned)
