"""将四段 RGB24 帧逐段编码成 H.264，再无损拼接视频流。仅依赖宿主提供的 FFmpeg 工具。"""

import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

FPS = 24
SEGMENT_FRAMES = 120
SEGMENT_COUNT = 4


@contextmanager
def _new_output(path: Path) -> Iterator[None]:
    """独占新建临时输出，编码、拼接或取消失败时回收本次产生的残片。"""
    path.touch(mode=0o600, exist_ok=False)
    try:
        yield
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def encode_segment(destination: Path, width: int, height: int, frames: Iterable[bytes]) -> None:
    """按迭代顺序写入恰好 120 帧 RGB24，生成 5 秒、24 fps 的无声 H.264 视频。"""
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise ValueError("H.264 yuv420p 的宽、高必须为正偶数")
    frame_size = width * height * 3
    command = [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-filter_threads",
        "1",
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgb24",
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        str(FPS),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libx264",
        "-threads",
        "2",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "mp4",
        str(destination.resolve()),
    ]
    # 用文件接收诊断，避免 stderr 管道塞满后妨碍 stdin 继续送帧。
    with (
        _new_output(destination),
        tempfile.TemporaryFile() as errors,
        subprocess.Popen(command, stdin=subprocess.PIPE, stderr=errors) as encoder,
    ):
        broken_pipe = False
        try:
            assert encoder.stdin is not None
            with encoder.stdin as stream:
                count = 0
                for frame in frames:
                    if count == SEGMENT_FRAMES or len(frame) != frame_size:
                        raise ValueError("视频分段帧数超限或 RGB24 单帧字节数不符")
                    stream.write(frame)
                    count += 1
                if count != SEGMENT_FRAMES:
                    raise ValueError(f"视频分段需要 {SEGMENT_FRAMES} 帧，实际收到 {count} 帧")
        except BrokenPipeError:
            # 即使提前关闭输入的一方正常退出，尚未写完的分段也必须失败。
            broken_pipe = True
            encoder.kill()
        except BaseException:
            # 生成器中断时主动结束、回收编码子进程，再把异常交给外层清理。
            encoder.kill()
            raise
        if encoder.wait() or broken_pipe:
            errors.seek(0)
            detail = errors.read(4096).decode(errors="replace").strip()
            raise RuntimeError(f"FFmpeg 分段编码失败：{detail}")


def concat_segments(segments: Sequence[Path], destination: Path) -> None:
    """按传入顺序复制四段 H.264 视频流，核对最终 MP4 样本数后交给调用方原子发布。"""
    if len(segments) != SEGMENT_COUNT:
        raise ValueError(f"需要 {SEGMENT_COUNT} 个视频分段")
    with _new_output(destination), tempfile.TemporaryDirectory(dir=destination.parent) as directory:
        work = Path(directory)
        # 清单仅引用私有目录内自动命名的链接，宿主路径的引号、空格不参与清单解析。
        entries = []
        for number, segment in enumerate(segments):
            alias = work / f"segment-{number}.mp4"
            alias.symlink_to(segment.resolve(strict=True))
            entries.append(f"file {alias.name}\n")
        manifest = work / "segments.ffconcat"
        manifest.write_text("".join(entries), encoding="ascii")
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                "-xerror",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(manifest.resolve()),
                "-map",
                "0:v:0",
                "-an",
                "-c:v",
                "copy",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(destination.resolve()),
            ],
            check=True,
        )
        # 有些损坏的后续分段会被 concat 当作输入结束。读取 MP4 帧数即可识别截短的成品。
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_frames",
                "-of",
                "default=nw=1:nk=1",
                str(destination.resolve()),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if probe.stdout.strip() != str(SEGMENT_COUNT * SEGMENT_FRAMES):
            raise ValueError("拼接结果帧数不符，预期 480 帧")
