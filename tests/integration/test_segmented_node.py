"""用 CPU 替身验证分段节点、ComfyUI 历史回执和 Worker 恢复目录之间的约定。"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mycomfy.engine.adapter import completed_outputs, node_directory, output_paths, stopped_outputs
from mycomfy.engine.comfy_nodes import MyComfySegmentedH3Video, segment, video

NODE_ID = "202"
CONTRACT = [
    {"node_id": NODE_ID, "role": "video", "media_types": ["video/mp4"], "history_key": "images"}
]


class Interrupted(BaseException):
    """ComfyUI 的用户中断也是 BaseException，用于覆盖普通异常之外的清理路径。"""


class Probe:
    def __init__(self, root):
        """为每个测试分配引擎输出、临时目录和独立的帧生命周期计数器。"""
        self.output = root / "output"
        self.temp = root / "temp"
        self.output.mkdir()
        self.temp.mkdir()
        self.models = (object(), object(), object())
        self.images = [SimpleNamespace(index=i, shape=(1, 384, 384, 3)) for i in range(5)]
        self.calls = []
        self.encoded = []
        self.concatenations = 0
        self.live = 0
        self.peak = 0
        self.failure = None
        self.execution = None
        self.node = MyComfySegmentedH3Video()

    def save_path(self, filename_prefix, output_dir, width, height):
        """提供隔离的 ComfyUI 保存路径，计数器按执行目录独立计算。"""
        prefix = Path(filename_prefix)
        directory = Path(output_dir) / prefix.parent
        directory.mkdir(parents=True, exist_ok=True)
        counter = 1 + len(list(directory.glob("output*.mp4")))
        return str(directory), prefix.name, counter, str(prefix.parent), filename_prefix

    def generate(
        self, model, clip, vae, first, last, prompt, seed, *, width, height, final=False, steps=20
    ):
        """用可跟踪的单字节帧代替 H3 输出，检查上一段已释放且本段按需消费。"""
        assert (model, clip, vae) == self.models
        assert self.live == 0
        assert (width, height) == (384, 384)
        self.calls.append((first.index, last.index, prompt, seed, final))
        probe = self

        class Frame(bytes):
            """记录何时释放帧，检测提前积累整段或全部视频的回归。"""

            def __del__(self):
                """释放该帧时从存活计数器中扣除。"""
                probe.live -= 1

        for number in range(120):
            if self.failure == "sampling" and first.index == 2 and number == 1:
                raise RuntimeError("模拟第三段采样失败")
            self.live += 1
            self.peak = max(self.live, self.peak)
            yield Frame(bytes([first.index]))

    def encode(self, path, width, height, frames):
        """模拟流式编码，把四个不同标记的帧序列写入真实临时文件。"""
        path = Path(path)
        assert path.is_relative_to(self.temp) and not path.is_relative_to(self.output)
        assert (width, height) == (384, 384)
        with path.open("xb") as target:
            for frame in frames:
                target.write(frame)
        assert path.stat().st_size == 120
        self.encoded.append(path)
        self.assert_unpublished()

    def concat(self, paths, path):
        """在合并前后检查 Worker 都读不到半成品，验证各段交给 FFmpeg 的顺序。"""
        self.concatenations += 1
        assert list(paths) == self.encoded[-4:]
        result = b"".join(segment_path.read_bytes() for segment_path in paths)
        assert result == b"".join(bytes([i]) * 120 for i in range(4))
        Path(path).write_bytes(result)
        self.assert_unpublished()

    def interrupt(self):
        """在段间和最终发布前模拟用户中断。"""
        if self.failure == "between" and len(self.encoded) == 2:
            raise Interrupted()
        if self.failure == "before_publish" and self.concatenations:
            raise Interrupted()

    def assert_unpublished(self):
        """没有可恢复的正式文件时，Worker 不能将已生成的片段误报为成品。"""
        assert not stopped_outputs(self.execution, CONTRACT).get("video")

    def invoke(self, execution_id, prompt):
        """以与 Worker 相同的输出前缀运行一个独立任务，并返回 ComfyUI 节点回执。"""
        self.execution = SimpleNamespace(
            id=execution_id, workspace={"output_dir": str(self.output)}
        )
        prefix = f"{execution_id}/{node_directory(NODE_ID)}/output"
        return self.node.run(
            *self.models, *self.images, prompt, "one", "two", "three", "four", 2**64 - 2, prefix
        )


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """仅替换 ComfyUI 环境与计算引擎，保留真实节点、文件操作及平台产物检索代码。"""
    probe = Probe(tmp_path)
    paths = SimpleNamespace(
        get_output_directory=lambda: str(probe.output),
        get_temp_directory=lambda: str(probe.temp),
        get_save_image_path=probe.save_path,
    )
    manager = SimpleNamespace(throw_exception_if_processing_interrupted=probe.interrupt)
    monkeypatch.setitem(sys.modules, "folder_paths", paths)
    monkeypatch.setitem(sys.modules, "comfy", SimpleNamespace(model_management=manager))
    monkeypatch.setitem(sys.modules, "comfy.model_management", manager)
    monkeypatch.setattr(segment, "generate_segment", probe.generate)
    monkeypatch.setattr(video, "encode_segment", probe.encode)
    monkeypatch.setattr(video, "concat_segments", probe.concat)
    return probe


def test_segmented_outputs_are_streamed_and_scoped_to_each_execution(probe):
    """连续运行两个提示词仅各返回一个本次执行的成品，并保持关键帧、种子和四段顺序。"""
    published = []
    for index, prompt in enumerate(("morning", "evening"), start=1):
        receipt = probe.invoke(f"execution_{index}", prompt)
        assert probe.calls[-4:] == [
            (i, i + 1, f"{prompt} {shot}", (2**64 - 2 + i) % 2**64, i == 3)
            for i, shot in enumerate(("one", "two", "three", "four"))
        ]
        assert receipt["result"] == ()
        ui = receipt["ui"]
        assert ui["animated"] == [True]
        (item,) = ui["images"]
        assert item["type"] == "output"
        history = {NODE_ID: ui}
        reported = output_paths(probe.execution, CONTRACT, history)
        assert reported == completed_outputs(probe.execution, CONTRACT, history)
        assert reported == stopped_outputs(probe.execution, CONTRACT)
        (path,) = reported["video"]
        assert path == probe.output / item["subfolder"] / item["filename"]
        assert path.read_bytes() == b"".join(bytes([i]) * 120 for i in range(4))
        published.append(path)
        assert probe.concatenations == index
        assert 0 < probe.peak <= 2 and probe.live == 0
        assert {p for p in probe.output.rglob("*") if p.is_file()} == set(published)
        assert not any(p.is_file() for p in probe.temp.rglob("*"))
    assert published[0] != published[1]


@pytest.mark.parametrize(
    ("failure", "error", "encoded", "joined"),
    [
        ("sampling", RuntimeError, 2, 0),
        ("between", Interrupted, 2, 0),
        ("before_publish", Interrupted, 4, 1),
    ],
)
def test_segmented_failure_cleans_partial_work_without_publishing(
    probe, failure, error, encoded, joined
):
    """后段异常或用户中断时，删除已生成的分段和正在发布的临时成品。"""
    probe.failure = failure
    with pytest.raises(error):
        probe.invoke("execution_failed", "fixture")
    assert len(probe.encoded) == encoded and probe.concatenations == joined
    probe.assert_unpublished()
    for root in (probe.temp, probe.output):
        assert not any(p.is_file() for p in root.rglob("*"))
