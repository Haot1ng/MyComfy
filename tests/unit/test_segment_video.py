"""以真实 FFmpeg 验证五图工作流的流式分段编码、拼接顺序和异常清理。"""

import json
import subprocess
from itertools import repeat

import pytest

from mycomfy.engine.comfy_nodes.video import concat_segments, encode_segment


def test_encode_four_segments_and_copy_video_streams(tmp_path):
    """编码 480 帧不同亮度的测试图像，逐帧解码核验时长、帧率、片段顺序和无音轨。"""
    width, height = 32, 16
    rgb_size = width * height * 3
    # 每段起始亮度不同，段内按时间渐变，同时检出错序、边界掉帧和重复帧。
    levels = [32 + section * 48 + frame // 4 for section in range(4) for frame in range(120)]
    segments = []
    for section in range(4):
        path = tmp_path / f"测试 '片段' {section}.mp4"
        frames = (bytes([v]) * rgb_size for v in levels[section * 120 : (section + 1) * 120])
        encode_segment(path, width, height, frames)
        segments.append(path)
    output = tmp_path / "ready.mp4"
    concat_segments(segments, output)

    probe = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(output),
        ]
    )
    metadata = json.loads(probe)
    (video,) = metadata["streams"]
    assert video["codec_name"] == "h264" and video["pix_fmt"] == "yuv420p"
    assert (video["width"], video["height"]) == (width, height)
    assert video["avg_frame_rate"] == "24/1" and int(video["nb_frames"]) == 480
    assert float(metadata["format"]["duration"]) == 20.0
    decoded = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(output),
            "-threads",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ]
    )
    assert len(decoded) == len(levels) * rgb_size
    assert max(abs(actual - expected) for actual, expected in zip(decoded[::rgb_size], levels)) <= 3


@pytest.mark.parametrize(("frame_count", "frame_bytes"), [(119, 48), (121, 48), (120, 47)])
def test_incomplete_or_invalid_frame_stream_removes_partial_output(
    tmp_path, frame_count, frame_bytes
):
    """不足、多余或字节长度错误的帧都要终止编码并删掉无法交付的片段。"""
    output = tmp_path / "segment.mp4"
    with pytest.raises(ValueError):
        encode_segment(output, 4, 4, repeat(bytes(frame_bytes), frame_count))
    assert not output.exists()


def test_generator_interruption_is_propagated_and_existing_output_is_preserved(tmp_path):
    """上游采样中止可穿透管道；重试遇到已有文件时保留其内容。"""

    def interrupted():
        """先写一帧，再模拟渲染线程取消本段。"""
        yield bytes(48)
        raise RuntimeError("cancelled")

    output = tmp_path / "segment.mp4"
    with pytest.raises(RuntimeError, match="cancelled"):
        encode_segment(output, 4, 4, interrupted())
    assert not output.exists()
    output.write_bytes(b"previous-render")
    with pytest.raises(FileExistsError):
        encode_segment(output, 4, 4, interrupted())
    assert output.read_bytes() == b"previous-render"


def test_damaged_segment_cannot_publish_a_partial_join(tmp_path):
    """后续分段损坏时，即使 FFmpeg 把它当作正常输入结束，截短成品也须清理。"""
    segment = tmp_path / "sample.mp4"
    encode_segment(segment, 4, 4, repeat(bytes(48), 120))
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"broken segment")
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        concat_segments([segment, segment, broken, segment], tmp_path / "ready.mp4")
    assert set(tmp_path.iterdir()) == {segment, broken}


def test_encoder_early_exit_reports_reason_and_removes_output(tmp_path, monkeypatch):
    """用提前关闭 stdin 的编码器复现断管，保留 stderr 诊断并清除不完整片段。"""
    encoder = tmp_path / "ffmpeg"
    encoder.write_text("#!/bin/sh\necho encoder-unavailable >&2\nexit 7\n")
    encoder.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    output = tmp_path / "part.mp4"
    with pytest.raises(RuntimeError, match="encoder-unavailable"):
        encode_segment(output, 128, 128, repeat(bytes(128 * 128 * 3), 120))
    assert not output.exists()
