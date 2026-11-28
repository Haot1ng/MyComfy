"""为宿主部署脚本和引擎安装器原子发布已验证的模型对象，仅使用标准库。"""

import hashlib
import os
import tempfile
from itertools import chain
from pathlib import Path


def _matches_digest(path: Path, digest: str):
    """读取一个普通文件，核对配置的 SHA-256 后才允许作为权重使用。"""
    if not path.is_file() or path.is_symlink():
        return False
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest() == digest


def reuse_model_object(target: Path, digest: str, model_path: str, *, context_file=None):
    """验证持久对象，若路径缺失则从构建上下文或任一已安装版本恢复同摘要内容。"""
    if target.is_symlink() or target.exists():
        return _matches_digest(target, digest)

    # 仅在持久路径缺失时查找模型。名称用于定位候选，摘要才是复用依据。
    engines = target.parent.parent
    previous = (models / model_path for models in engines.glob("releases/*/models"))
    candidates = chain((context_file,), previous) if context_file is not None else previous
    checked = set()
    for path in candidates:
        if not path.is_file() or path.resolve() != engines.resolve() / path.relative_to(engines):
            continue
        # 不重复扫描多次发布时共享同一 inode 的旧版本。
        info = path.stat()
        inode = (info.st_dev, info.st_ino)
        if inode in checked:
            continue
        checked.add(inode)
        if _matches_digest(path, digest):
            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(path, target)
            print("从本机已验证副本恢复权重", model_path, flush=True)
            return True
    return False


def relink_model(source: Path, target: Path):
    """原子切换一个已发布的模型路径，新打开文件的读者始终得到完整内容。"""
    with tempfile.TemporaryDirectory(dir=target.parent) as staging:
        link = Path(staging) / "model"
        os.link(source, link)
        os.replace(link, target)


def publish_verified_object(staged: Path, target: Path):
    """持有 deploy.lock 时发布已校验的文件，并修复各版本中指向损坏对象的硬链接。"""
    if not target.is_symlink() and target.is_file() and target.stat().st_nlink > 1:
        # 只有替换损坏的共享对象才遍历受管目录；先修复各别名，最后切换缓存路径。
        # 中途失败时原缓存仍标记为损坏，显式重试可继续修复剩余的链接。
        for models in target.parent.parent.glob("releases/*/models"):
            for path in models.rglob("*"):
                if not path.is_symlink() and path.is_file() and path.samefile(target):
                    relink_model(staged, path)
    os.replace(staged, target)
