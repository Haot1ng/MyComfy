"""本地存储提供器，以排他创建的方式在目标卷发布正式对象。"""

import errno
import hashlib
import json
import logging
import math
import os
import re
import stat
import subprocess
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from threading import Lock
from typing import BinaryIO

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from mycomfy.database.models import Asset
from mycomfy.database.session import db_now, new_id
from mycomfy.errors import ApiError, not_found

BLOCK = 1024 * 1024
MEDIA_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/mp4": ".m4a",
    "audio/flac": ".flac",
    "audio/wav": ".wav",
}


def materialized_input_name(asset):
    """由素材标识与已检测的类型导出引擎输入名，供物化和回收共同使用。"""
    return asset.id + MEDIA_EXTENSIONS.get(asset.media_type, "")


class MediaInvalid(ValueError):
    """源文件的完整字节不符合媒体契约。"""


class ObjectConflict(OSError):
    """同一正式对象键已存在摘要不同的文件。"""


def fsync_directory(path):
    """刷新目录项，使正式文件链接可在掉电后恢复。"""
    fd = os.open(path, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_directory(path):
    """递归建立目录，并将每个新增的目录项持久化。"""
    if path.is_dir():
        return
    durable_directory(path.parent)
    path.mkdir(exist_ok=True)
    fsync_directory(path.parent)


def copy_bytes(source, destination, limit=None):
    """在字节上限内流式复制，计算 SHA-256 并刷新目标文件。"""
    size = 0
    digest = hashlib.sha256()
    while chunk := source.read(BLOCK):
        size += len(chunk)
        if limit is not None and size > limit:
            raise ApiError(413, "UPLOAD_TOO_LARGE", "Upload exceeds the configured limit")
        digest.update(chunk)
        destination.write(chunk)
    destination.flush()
    os.fsync(destination.fileno())
    return size, digest.hexdigest()


def file_signature(path):
    """读取同一文件描述符上的大小和 SHA-256，用于比对不可变对象。"""
    with path.open("rb") as f:
        return os.fstat(f.fileno()).st_size, hashlib.file_digest(f, "sha256").hexdigest()


def media_format(header):
    """根据前缀识别受支持的媒体封装与初步 MIME 类型。"""
    # 根据文件头指定解复用器，避免将上传内容解析为外部播放列表。
    if header[4:8] == b"ftyp":
        kind = "video/quicktime" if header[8:12] == b"qt  " else "video/mp4"
        return "mov", kind
    for magic, demuxer, mime in (
        (b"\x89PNG\r\n\x1a\n", "png_pipe", "image/png"),
        (b"\xff\xd8\xff", "jpeg_pipe", "image/jpeg"),
        (b"GIF8", "gif", "image/gif"),
        (b"fLaC", "flac", "audio/flac"),
        (
            b"\x1a\x45\xdf\xa3",
            "matroska",
            "video/webm" if b"webm" in header else "video/x-matroska",
        ),
    ):
        if header.startswith(magic):
            return demuxer, mime
    if header.startswith(b"RIFF"):
        formats = {
            b"WAVE": ("wav", "audio/wav"),
            b"WEBP": ("webp_pipe", "image/webp"),
            b"AVI ": ("avi", "video/x-msvideo"),
        }
        if header[8:12] in formats:
            return formats[header[8:12]]
    raise MediaInvalid("UNSUPPORTED_MEDIA_TYPE")


def media_tool(args):
    """运行媒体检测命令，区分数据无效与暂时的环境故障。"""
    try:
        result = subprocess.run(
            args, stdin=subprocess.DEVNULL, capture_output=True, timeout=300, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError(errno.ETIMEDOUT, "Media validation timed out") from exc
    if result.returncode < 0:
        raise OSError(errno.EAGAIN, "Media tool interrupted")
    if result.returncode:
        if any(
            message in result.stderr
            for message in (b"Input/output error", b"Cannot allocate memory")
        ):
            raise OSError(errno.EIO, "Media inspection temporarily unavailable")
        raise MediaInvalid("UNDECODABLE_MEDIA")
    return result.stdout


def inspect_media(path, contract):
    """检查实际音视频流、尺寸、时长及策略约束，并按需完整解码。"""
    with path.open("rb") as source:
        demuxer, kind = media_format(source.read(512))
    input_args = ["-protocol_whitelist", "file,pipe", "-f", demuxer, "-i", str(path)]
    document = json.loads(
        media_tool(
            [
                "ffprobe",
                "-v",
                "error",
                *input_args,
                "-show_entries",
                "stream=codec_type,width,height,avg_frame_rate:format=duration",
                "-of",
                "json",
            ]
        )
    )
    streams = document.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = any(stream.get("codec_type") == "audio" for stream in streams)
    if kind == "video/mp4" and audio and video is None:
        kind = "audio/mp4"
    if (kind.startswith(("video/", "image/")) and video is None) or (
        kind.startswith("audio/") and not audio
    ):
        raise MediaInvalid("MISSING_MEDIA_STREAM")
    if contract.get("media_types") and kind not in contract["media_types"]:
        raise MediaInvalid("MEDIA_TYPE_MISMATCH")
    media = {"width": None, "height": None, "duration": None, "fps": None}
    duration = document.get("format", {}).get("duration")
    if duration and duration != "N/A":
        media["duration"] = float(duration)
    if video:
        media.update(width=video.get("width"), height=video.get("height"))
        try:
            media["fps"] = float(Fraction(video.get("avg_frame_rate", "0/0")))
        except (ZeroDivisionError, ValueError):
            pass
    for key in ("width", "height", "fps"):
        if contract.get(key) is not None and (
            media[key] is None or not math.isclose(media[key], contract[key], rel_tol=1e-4)
        ):
            raise MediaInvalid(f"{key.upper()}_MISMATCH")
    for bound in ("min_duration", "max_duration"):
        threshold = contract.get(bound)
        if threshold is not None and (
            media["duration"] is None
            or (
                media["duration"] < threshold
                if bound == "min_duration"
                else media["duration"] > threshold
            )
        ):
            raise MediaInvalid("DURATION_MISMATCH")
    if contract.get("require_decodable", True):
        media_tool(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                *input_args,
                "-map",
                "0:v?",
                "-map",
                "0:a?",
                "-f",
                "null",
                "-",
            ]
        )
    return kind, media


@dataclass
class PreparedUpload:
    asset_id: str
    object_key: str
    verification: dict
    database_bind: object | None = None
    saved: bool = False

    def register(self, db, user_id):
        """在调用方短事务内将已持久化的素材登记到所属用户。"""
        asset = Asset(
            id=self.asset_id,
            user_id=user_id,
            role="input",
            provider="local",
            object_key=self.object_key,
            created_at=db_now(db),
            **self.verification,
        )
        db.add(asset)
        db.flush()
        self.database_bind = db.get_bind()
        return asset

    def retained(self):
        """提交回执缺失时用独立会话核对登记结果，失联时保留字节待查。"""
        if self.saved:
            return True
        if self.database_bind is None:
            return False
        try:
            with Session(self.database_bind) as db:
                return db.get(Asset, self.asset_id) is not None
        except SQLAlchemyError:
            logging.getLogger(__name__).warning(
                "upload %s awaits database reconciliation", self.asset_id
            )
            return True

    def committed(self):
        """在素材事务成功提交后标记正式对象应当保留。"""
        self.saved = True


class LocalStore:
    def __init__(self, data_root):
        """定位当前部署存放正式对象的根目录。"""
        self.root = (Path(data_root) / "objects").resolve()
        self._verified = OrderedDict()
        self._verify_lock = Lock()

    def path(self, key):
        """解析平台对象键，并校验规范路径位于对象根下。"""
        candidate = (self.root / key).resolve()
        if (
            not key
            or Path(key).is_absolute()
            or candidate == self.root
            or not candidate.is_relative_to(self.root)
        ):
            raise ValueError("Invalid local object key")
        return candidate

    def verify_reader(self, reader, asset):
        """对当前打开的文件校验摘要，相同 inode 且未变化的后续范围读取复用结果。"""
        info = os.fstat(reader.fileno())
        if info.st_size != asset.size:
            return False
        signature = (
            asset.digest,
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        with self._verify_lock:
            if signature in self._verified:
                self._verified.move_to_end(signature)
                return True
        if hashlib.file_digest(reader, "sha256").hexdigest() != asset.digest:
            return False
        with self._verify_lock:
            self._verified[signature] = True
            if len(self._verified) > 256:
                self._verified.popitem(last=False)
        return True

    def readable(self, key, size):
        """检查正式路径指向与登记大小一致的普通文件。"""
        try:
            info = self.path(key).stat()
            return stat.S_ISREG(info.st_mode) and info.st_size == size
        except FileNotFoundError:
            return False

    def stage_path(self, key):
        """在正式对象所在文件系统生成可重复定位的暂存路径。"""
        target = self.path(key)
        return target.with_name(f".{target.name}.stage")

    def publish(self, key, size, digest):
        """排他链接已校验的暂存文件，同键重试须通过摘要核对。"""
        target = self.path(key)
        try:
            os.link(self.stage_path(key), target)
        except (FileExistsError, FileNotFoundError):
            if not target.exists():
                raise
            if file_signature(target) != (size, digest):
                raise ObjectConflict("OBJECT_DIGEST_CONFLICT")
        fsync_directory(target.parent)

    @staticmethod
    def upload_id(owner):
        """由持久化上传预留标识推导唯一素材 ID，用于崩溃后核对。"""
        if not re.fullmatch(r"up_[0-9a-f]{32}", owner):
            raise ValueError("Invalid upload reservation identity")
        return "a_" + owner[3:]

    def cleanup_upload(self, db, owner):
        """调用方持有已失效上传的独占锁时，只回收该上传可证明的临时字节。"""
        try:
            asset_id = self.upload_id(owner)
        except ValueError:
            return
        directory = self.root / "uploads"
        if not directory.is_dir() or directory.resolve() != directory:
            return
        # 锁定读取会等待同一主键上已发送到数据库的登记事务结束。
        asset = db.get(Asset, asset_id, with_for_update=True)
        paths = [directory / f".{asset_id}.stage"]
        if asset is None:
            paths.append(directory / asset_id)
        for path in paths:
            if not path.is_symlink() and path.is_file():
                path.unlink(missing_ok=True)

    @contextmanager
    def prepare_upload(self, source: BinaryIO, max_bytes: int, *, owner=None):
        """限额读取并验证上传文件，在数据库登记前持久化字节。"""
        asset_id = self.upload_id(owner) if owner is not None else new_id("a")
        key = f"uploads/{asset_id}"
        target = self.path(key)
        stage = self.stage_path(key)
        prepared = None
        linked = False
        try:
            durable_directory(target.parent)
            with stage.open("xb") as destination:
                size, digest = copy_bytes(source, destination, max_bytes)
            kind, media = inspect_media(stage, {})
            prepared = PreparedUpload(
                asset_id,
                key,
                {
                    "size": size,
                    "digest": digest,
                    "media_type": kind,
                    "media": media,
                    "complete": True,
                    "available": True,
                },
            )
            self.publish(key, size, digest)
            linked = True
        except MediaInvalid as exc:
            raise ApiError(415, "INVALID_MEDIA", "Unsupported or invalid media") from exc
        except OSError as exc:
            raise ApiError(
                503, "STORAGE_UNAVAILABLE", "Upload storage is temporarily unavailable"
            ) from exc
        finally:
            stage.unlink(missing_ok=True)
        try:
            yield prepared
        finally:
            # 登记事务的提交结果不明时保留正式对象，供恢复器核对。
            if linked and not prepared.retained():
                target.unlink(missing_ok=True)


def parse_range(header, size):
    """将一个 HTTP 字节范围解析为左闭右开区间，越界返回 416。"""
    if header is None:
        return 0, size
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", header)
    if match and size > 0:
        first, last = match.groups()

        # 大于整个文件的十进制数只需按文件大小截断，不必构造任意精度整数。
        def offset(value):
            """将无符号十进制字节偏移限制到当前文件末尾。"""
            digits = value.lstrip("0") or "0"
            return size if len(digits) > len(str(size)) else min(int(digits), size)

        if first:
            start = offset(first)
            stop = min(offset(last) + 1, size) if last else size
            if start < stop:
                return start, stop
        elif last and offset(last) > 0:
            return size - offset(last), size
    raise ApiError(
        416,
        "INVALID_RANGE",
        "Range is not satisfiable",
        headers={"Content-Range": f"bytes */{size}"},
    )


def content_response(store, asset, range_header=None, download=False):
    """从已授权文件描述符传输预览、下载或指定范围的内容。"""
    if not asset.available or asset.provider != "local" or (not asset.complete and not download):
        raise not_found()
    try:
        reader = store.path(asset.object_key).open("rb")
    except OSError as exc:
        raise ApiError(503, "OBJECT_UNAVAILABLE", "Stored object is unavailable") from exc
    try:
        if not store.verify_reader(reader, asset):
            raise ApiError(503, "OBJECT_UNAVAILABLE", "Stored object failed integrity verification")
        start, stop = parse_range(range_header, asset.size)
        reader.seek(start)
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(stop - start),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if download:
            headers["Content-Disposition"] = (
                f'attachment; filename="{materialized_input_name(asset)}"'
            )
        if range_header is not None:
            headers["Content-Range"] = f"bytes {start}-{stop - 1}/{asset.size}"

        def chunks():
            """按指定范围分块发送内容，并在迭代结束时关闭文件。"""
            remaining = stop - start
            try:
                while remaining:
                    block = reader.read(min(remaining, BLOCK))
                    if not block:
                        raise OSError("Object changed during read")
                    remaining -= len(block)
                    yield block
            finally:
                reader.close()

        return StreamingResponse(
            chunks(),
            status_code=206 if range_header is not None else 200,
            headers=headers,
            media_type=asset.media_type,
            background=BackgroundTask(reader.close),
        )
    except BaseException:
        reader.close()
        raise
