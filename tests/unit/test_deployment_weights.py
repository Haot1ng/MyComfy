"""验证部署器在宿主机准备 Ref2VA 构建上下文时的缓存、下载和故障处理。"""

import hashlib
import io
import os
import runpy
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
prepare = runpy.run_path(str(REPO / "scripts/deployment_weights.py"))["prepare_ref2va_context"]
CONTENT = b"tiny simulated weight"
DIGEST = hashlib.sha256(CONTENT).hexdigest()
MODEL = {"source": "https://weights.example/ref", "path": "ref.safetensors", "sha256": DIGEST}


def local_docker(tmp_path, monkeypatch, *, old_image=False, cat_code=0):
    """提供仅支持本机镜像查询和只读提取的 Docker，以便断言不会拉取镜像。"""
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if sys.argv[1:4] == ['image', 'ls', '--quiet']:\n"
        f"    if {old_image}: print('old-image-id')\n"
        "else:\n"
        "    assert sys.argv[1:] == ['run', '--rm', '--pull', 'never', '--network', 'none', "
        f"'--entrypoint', 'cat', 'old-image-id', '/opt/mycomfy/model-objects/{DIGEST}']\n"
        f"    sys.stdout.buffer.write({CONTENT!r})\n"
        f"    sys.exit({cat_code})\n"
    )
    docker.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])


def test_first_download_and_explicit_refresh(tmp_path, monkeypatch):
    """首次获取只下载一次，重复构建和缺少缓存路径可复用本机文件，刷新保留健康 inode。"""
    local_docker(tmp_path, monkeypatch)
    downloads = []

    def fetch(url, timeout):
        """记录实际网络读取次数，并返回一份可校验的测试内容。"""
        downloads.append(url)
        return io.BytesIO(CONTENT)

    monkeypatch.setattr(urllib.request, "urlopen", fetch)
    data = tmp_path / "data"
    args = (MODEL, data, tmp_path, "mycomfy-engine-ref2va:local")
    context = prepare(*args)
    obj = data / "engines" / "model-objects" / DIGEST
    assert list(context.iterdir()) == [context / DIGEST]
    assert obj.samefile(context / DIGEST) and obj.read_bytes() == CONTENT
    original = obj.stat().st_ino
    prepare(*args)
    obj.unlink()
    prepare(*args)
    assert downloads == [MODEL["source"]] and obj.stat().st_ino == original
    prepare(*args, refresh=True)
    assert downloads == [MODEL["source"]] * 2 and obj.stat().st_ino == original

    # 刷新获取坏内容时仍能从原缓存启动，并保留所有硬链接。
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(b"corrupt"))
    with pytest.raises(ValueError, match="SHA-256"):
        prepare(*args, refresh=True)
    assert obj.read_bytes() == CONTENT and obj.stat().st_ino == original


@pytest.mark.parametrize("cat_code", [0, 23])
def test_recovers_from_previous_baked_image(tmp_path, monkeypatch, cat_code):
    """旧镜像的校验内容可以离线复用，提取异常时不下载也不发布半成品。"""
    local_docker(tmp_path, monkeypatch, old_image=True, cat_code=cat_code)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("不能下载"))
    args = (MODEL, tmp_path / "data", tmp_path, "mycomfy-engine-ref2va:local")
    if cat_code:
        with pytest.raises(subprocess.CalledProcessError):
            prepare(*args)
        assert not (tmp_path / "data" / "engines" / "model-objects" / DIGEST).exists()
    else:
        assert (prepare(*args) / DIGEST).read_bytes() == CONTENT


def test_local_source_and_corrupt_cache(tmp_path, monkeypatch):
    """本地来源可离线补齐，损坏缓存须显式刷新，恢复后上下文仍引用正确文件。"""
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("不能下载"))
    (tmp_path / "ref").write_bytes(CONTENT)
    model = {**MODEL, "source": "/deployment/ref"}
    args = (model, tmp_path / "data", tmp_path, "existing-image")
    file = prepare(*args) / DIGEST
    file.write_bytes(b"damaged")
    with pytest.raises(ValueError, match="--refresh-weights"):
        prepare(*args)
    prepare(*args, refresh=True)
    assert file.read_bytes() == CONTENT


def test_recovers_from_an_installed_release(tmp_path, monkeypatch):
    """对象缓存和上下文均缺失但已有引擎仍保存模型时，恢复硬链接即可重新构建镜像。"""
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("不能下载"))
    data = tmp_path / "data"
    previous = data / "engines" / "releases" / "old" / "models" / MODEL["path"]
    previous.parent.mkdir(parents=True)
    previous.write_bytes(CONTENT)
    context = prepare(MODEL, data, tmp_path, "old-image")
    assert previous.samefile(context / DIGEST)
