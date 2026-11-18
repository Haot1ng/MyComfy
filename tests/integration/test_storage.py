"""验证上传、正式对象冲突、媒体契约和范围读取。"""

import asyncio
import hashlib
import io
import subprocess
from types import SimpleNamespace

import pytest

from mycomfy.artifacts.storage import (
    LocalStore,
    MediaInvalid,
    ObjectConflict,
    content_response,
    inspect_media,
    parse_range,
)
from mycomfy.errors import ApiError


@pytest.fixture
def video(tmp_path):
    """用 CPU 生成可完整解码的测试视频。"""
    path = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=32x24:rate=2",
            "-t",
            "1",
            "-c:v",
            "mpeg4",
            "-y",
            str(path),
        ],
        check=True,
    )
    return path


def test_media_and_upload_rollback(video, tmp_path):
    """验证实际视频规格、拒绝伪造的内容并清理未受理上传。"""
    store = LocalStore(tmp_path)
    with video.open("rb") as f, store.prepare_upload(f, video.stat().st_size) as upload:
        assert upload.verification["media_type"] == "video/mp4"
        assert upload.verification["media"]["width"] == 32
        assert upload.verification["media"]["fps"] == 2
        assert store.path(upload.object_key).read_bytes() == video.read_bytes()
    assert not store.path(upload.object_key).exists()
    with pytest.raises(MediaInvalid, match="WIDTH_MISMATCH"):
        inspect_media(video, {"width": 16})
    with pytest.raises(ApiError) as error, store.prepare_upload(io.BytesIO(b"wrong"), 99):
        pytest.fail("不应受理无效媒体")
    assert error.value.status == 415
    with pytest.raises(ApiError) as error, store.prepare_upload(io.BytesIO(b"1234"), 3):
        pytest.fail("不应写入超限上传")
    assert error.value.status == 413
    assert not list((store.root / "uploads").iterdir())


def test_local_provider_conflicting_key(tmp_path):
    """同键重放复用一致内容，摘要不一致时保留原正式文件。"""
    store = LocalStore(tmp_path)
    key = "execution/clip.mp4"
    stage = store.stage_path(key)
    stage.parent.mkdir(parents=True)
    stage.write_bytes(b"first")
    digest = hashlib.sha256(b"first").hexdigest()
    store.publish(key, 5, digest)
    store.publish(key, 5, digest)
    stage.unlink()
    stage.write_bytes(b"other")
    with pytest.raises(ObjectConflict):
        store.publish(key, 5, hashlib.sha256(b"other").hexdigest())
    assert store.path(key).read_bytes() == b"first"


def test_content_range_and_integrity(tmp_path):
    """验证下载字节数、越界、部分文件预览限制与正式文件完整性。"""
    store = LocalStore(tmp_path)
    asset = SimpleNamespace(
        id="a_1",
        object_key="one",
        size=8,
        digest=hashlib.sha256(b"01234567").hexdigest(),
        complete=True,
        available=True,
        media_type="video/mp4",
        provider="local",
    )
    store.root.mkdir()
    store.path("one").write_bytes(b"01234567")
    response = content_response(store, asset, "bytes=2-4")

    async def read_response():
        """收集流式响应以核对实际传输的字节。"""
        return b"".join([chunk async for chunk in response.body_iterator])

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 2-4/8"
    assert asyncio.run(read_response()) == b"234"
    assert parse_range("bytes=-2", 8) == (6, 8)
    assert parse_range("bytes=3-", 8) == (3, 8)
    for value in ("bytes=12-", "bytes=4-2", "bytes=-0", "bytes=0-1,3-4", "000"):
        with pytest.raises(ApiError) as error:
            content_response(store, asset, value)
        assert error.value.status == 416
    asset.complete = False
    with pytest.raises(ApiError) as error:
        content_response(store, asset)
    assert error.value.status == 404
    store.path("one").write_bytes(b"XXXXXXXX")
    with pytest.raises(ApiError) as error:
        content_response(store, asset, download=True)
    assert error.value.status == 503


@pytest.mark.mysql
def test_recover_known_uploads_only(mysql_sessions, tmp_path, video):
    """崩溃恢复按上传预留回收未登记对象，保留已提交素材与未知文件。"""
    from mycomfy.database.models import User

    store = LocalStore(tmp_path)
    first = "up_" + "1" * 32
    second = "up_" + "2" * 32
    with mysql_sessions.begin() as db:
        db.add(User(id="u", username="u", password_hash="unused"))
    with video.open("rb") as f, store.prepare_upload(f, video.stat().st_size, owner=first) as saved:
        with mysql_sessions.begin() as db:
            saved.register(db, "u")
        saved.committed()
    key = "uploads/" + store.upload_id(second)
    store.path(key).write_bytes(b"unregistered")
    store.stage_path(key).write_bytes(b"partial")
    unknown = store.path("uploads/unknown")
    unknown.write_bytes(b"unknown")
    with mysql_sessions.begin() as db:
        store.cleanup_upload(db, first)
        store.cleanup_upload(db, second)
    assert store.path(saved.object_key).read_bytes() == video.read_bytes()
    assert not store.path(key).exists()
    assert not store.stage_path(key).exists()
    assert unknown.read_bytes() == b"unknown"
