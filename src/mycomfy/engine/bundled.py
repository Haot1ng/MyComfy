"""固定随 MyComfy 镜像分发的 H3 节点，并为可复用的 ComfyUI 环境安装独立的节点目录。"""

import hashlib
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

H3_NODE = "MyComfySegmentedH3Video"
H3_COMFYUI = "2f35f4a08176d993cded35dac3332be4f7287f41"
H3_FILES = ("__init__.py", "segment.py", "video.py")
SOURCE = Path(__file__).with_name("comfy_nodes")


def node_digest(directory):
    """按文件名和内容计算可移植的节点版本，不加载 ComfyUI，也不包含运行时字节码。"""
    digest = hashlib.sha256()
    for name in H3_FILES:
        digest.update(name.encode("ascii") + b"\0")
        digest.update(hashlib.sha256((directory / name).read_bytes()).digest())
    return digest.hexdigest()


def media_ready():
    """检查引擎进程可调用的 FFprobe 和 FFmpeg H.264 编码器。"""
    try:
        subprocess.run(["ffprobe", "-version"], check=True, capture_output=True, timeout=10)
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"], check=True, capture_output=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[1:2] == [b"libx264"] for line in result.stdout.splitlines())


def select_nodes(comfy_commit, external_nodes, pinned=None):
    """根据精确的引擎提交和媒体工具选择可提供的节点，校验清单中已有的版本约束。"""
    if pinned == {}:
        return {}
    # 老版本、使用同名第三方节点或缺少编码工具的部署仍可运行原有工作流。
    if comfy_commit != H3_COMFYUI or H3_NODE in external_nodes:
        if pinned:
            raise ValueError("内置 H3 节点与 ComfyUI 提交或同名外部节点不兼容")
        return {}
    if pinned is not None and set(pinned) != {H3_NODE}:
        raise ValueError("部署清单指定了未知的内置节点")
    if not media_ready():
        message = "分段 H3 节点需要可用的 ffprobe 和带 libx264 编码器的 ffmpeg"
        if pinned:
            raise ValueError(message)
        logging.getLogger(__name__).warning(message)
        return {}
    return pinned if pinned is not None else {H3_NODE: node_digest(SOURCE)}


def install_node(root, version):
    """原子安装或核验指定内容版本，返回只含该版本节点包的 ComfyUI 搜索目录。"""
    directory = root / "bundled-nodes" / version
    package = directory / H3_NODE
    if not directory.exists():
        directory.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory.parent) as staging:
            staged = Path(staging) / "nodes"
            target = staged / H3_NODE
            target.mkdir(parents=True)
            for name in H3_FILES:
                shutil.copyfile(SOURCE / name, target / name)
            # 只有整包与清单锁定版本相符时才发布，旧清单可直接复用以前部署的版本。
            if node_digest(target) != version:
                raise ValueError("当前 MyComfy 包未提供清单指定的内置 H3 节点版本")
            staged.rename(directory)
    else:
        # ComfyUI 扫描整个搜索目录；额外模块和包内导入源都不能混入已登记版本。
        if (
            set(directory.iterdir()) != {package}
            or not package.is_dir()
            or {p.name for p in package.iterdir()} - {"__pycache__"} != set(H3_FILES)
            or node_digest(package) != version
        ):
            raise ValueError(f"已安装的内置 H3 节点内容与摘要不符 {directory}")
    return directory
