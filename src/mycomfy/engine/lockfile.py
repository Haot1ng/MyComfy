"""在引擎容器的实际 Python 和 Linux 平台上解析 GPU 依赖，生成 pip 可重放的哈希锁。"""

import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

TORCH_PACKAGES = ("torch", "torchvision", "torchaudio")
EXACT_VERSION = re.compile(
    r"[0-9]+(?:![0-9]+)?(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?"
    r"(?:\.dev[0-9]+)?(?:\+[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*)?"
)


def validate_pytorch(pytorch):
    """校验用户选择的索引与精确版本，避免把凭据或范围表达式写进锁文件。"""
    if not isinstance(pytorch, dict) or set(pytorch) - {"index_url", *TORCH_PACKAGES}:
        raise ValueError("pytorch 仅支持 index_url、torch、torchvision、torchaudio")
    index = pytorch.get("index_url")
    parsed = urlsplit(index) if isinstance(index, str) else urlsplit("")
    public = parsed.scheme == "https" and parsed.hostname and parsed.username is None
    local = parsed.scheme == "file" and not parsed.netloc and Path(parsed.path).is_absolute()
    if not (local or public) or parsed.query or parsed.fragment or any(c.isspace() for c in index):
        raise ValueError("pytorch.index_url 需要无凭据的 HTTPS 索引或本地 file:/// 索引")
    for name in TORCH_PACKAGES:
        if name != "torch" and name not in pytorch:
            continue
        version = pytorch.get(name)
        if not isinstance(version, str) or not EXACT_VERSION.fullmatch(version):
            raise ValueError(f"pytorch.{name} 需要一个精确版本，例如 2.7.1+cu126")


def generate_lock(pytorch, requirements, target):
    """合并各仓库依赖并解析传递依赖，只在全部包都有可安装文件和 SHA-256 后发布锁。"""
    validate_pytorch(pytorch)
    target = Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    pins = {name: pytorch[name] for name in TORCH_PACKAGES if name in pytorch}
    with tempfile.TemporaryDirectory(dir=target.parent) as scratch:
        work = Path(scratch)
        # uv 的显式索引只给指定的 PyTorch 包使用。普通依赖继续从其原索引解析。
        deps = [f"{name}=={version}" for name, version in pins.items()]
        sources = "\n".join(f'{name} = {{ index = "gpu" }}' for name in pins)
        project = work / "pyproject.toml"
        project.write_text(
            '[project]\nname = "mycomfy-engine-pins"\nversion = "0"\n'
            f"dependencies = {json.dumps(deps)}\n[tool.uv.sources]\n{sources}\n"
            f'[[tool.uv.index]]\nname = "gpu"\nurl = {json.dumps(pytorch["index_url"])}\n'
            "explicit = true\n",
            encoding="utf-8",
        )
        result = subprocess.check_output(
            [
                "uv",
                "pip",
                "compile",
                "--python",
                sys.executable,
                "--no-python-downloads",
                "--system-certs",
                "--format",
                "pylock.toml",
                "--generate-hashes",
                "--no-header",
                "--only-binary",
                ",".join(pins),
                str(project),
                *(str(Path(path).resolve()) for path in requirements),
            ],
            cwd=work,
        )
        packages = tomllib.loads(result.decode("utf-8"))["packages"]
        # 非通用解析已按当前解释器、架构和平台过滤 wheel。记录实际 URL，pip 安装时无需再猜索引。
        draft = work / "requirements-gpu.lock"
        with draft.open("w", encoding="utf-8") as stream:
            for package in packages:
                name = package["name"]
                wheels = package.get("wheels", [])
                if name in TORCH_PACKAGES and (name not in pins or not wheels):
                    raise ValueError(f"请在 pytorch 配置中固定 {name} 的索引 wheel 版本")
                artifact = wheels[0] if wheels else package.get("archive") or package.get("sdist")
                digest = (artifact or {}).get("hashes", {}).get("sha256", "")
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError(f"{name} 缺少可哈希的安装包，请使用固定的 wheel 或源码归档")
                url = artifact.get("url") or (work / artifact["path"]).resolve().as_uri()
                if urlsplit(url).username is not None or any(c.isspace() for c in url):
                    raise ValueError(f"{name} 的安装包地址含凭据或空白字符")
                stream.write(f"{name} @ {url} --hash=sha256:{digest}\n")
            stream.flush()
            os.fsync(stream.fileno())
        draft.replace(target)
