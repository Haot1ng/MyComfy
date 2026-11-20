"""从用户填写的引擎版本、PyTorch 版本和模型来源生成固定清单。"""

import argparse
import fcntl
import hashlib
import json
import re
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field

from .deploy import (
    Deployment,
    EngineLayout,
    InstanceSpec,
    ModelFile,
    Name,
    Record,
    Repo,
    Sha256,
    checkout,
    file_digest,
    provision,
    write_json,
)
from .lockfile import generate_lock, validate_pytorch


class VersionedRepo(Record):
    """源仓库以及需要检出的 Git 标签或完整提交号。"""

    url: str = Field(min_length=1)
    version: str = Field(min_length=1)


class InputModel(ModelFile):
    """本地模型可从实际文件计算摘要，远程模型须提供期望摘要。"""

    sha256: Sha256 | None = None


class InputInstance(InstanceSpec):
    """使用单卡部署的默认时限，同时允许按实例覆盖。"""

    observation_seconds: int = Field(default=120, gt=0)
    timeout_seconds: int = Field(default=3600, gt=0)
    reconcile_seconds: int = Field(default=120, gt=0)


class Recipe(EngineLayout):
    """引擎容器收到的配置，只包含引擎部署所需参数。"""

    comfyui: VersionedRepo
    nodes: dict[Name, VersionedRepo] = Field(default_factory=dict)
    models: dict[Name, InputModel] = Field(default_factory=dict)
    pytorch: dict[str, str]
    instances: list[InputInstance] = Field(
        min_length=1, default_factory=lambda: [InputInstance(id="gpu0", gpu="0", port=8188)]
    )


def fingerprint(value):
    """以排序后的 JSON 计算持久缓存标识，使配置字段顺序不影响重复部署。"""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def locate(source, base):
    """保留 HTTPS 来源，将本地来源转换成部署挂载目录内的绝对路径。"""
    url = urlsplit(source)
    if url.scheme:
        if url.scheme != "https" or not url.hostname:
            raise ValueError("来源须为 HTTPS 地址或部署目录内的本地路径")
        return source
    local = (base / source).resolve()
    if not local.is_relative_to(base):
        raise ValueError(f"本地来源超出部署目录 {source}")
    return str(local)


def pin_model(model, base, objects):
    """把模型转换为安装器可验证的来源，并识别本地文件的内容变化。"""
    source = locate(model.source, base)
    digest = model.sha256
    if urlsplit(source).scheme:
        if digest is None:
            raise ValueError(f"HTTPS 模型需要提供 SHA-256：{model.path}")
    # 显式摘要的模型可从既有对象缓存恢复，即使原始导入文件已移走。
    elif Path(source).exists() or digest is None or not (objects / digest).is_file():
        actual = file_digest(Path(source))
        if digest is not None and digest != actual:
            raise ValueError(f"本地模型摘要不符 {model.path}")
        digest = actual
    return ModelFile(source=source, path=model.path, sha256=digest)


def resolve_version(url, version):
    """直接使用完整提交号，或把轻量、附注 Git 标签精确解析为提交号。"""
    if re.fullmatch(r"[0-9a-fA-F]{40}", version):
        return version.lower()
    ref = "refs/tags/" + version.removeprefix("refs/tags/")
    if subprocess.run(["git", "check-ref-format", ref], check=False).returncode:
        raise ValueError(f"无效的 Git 标签 {version}")
    # 附注标签有单独的标签对象，带 ^{} 的记录才是最终提交。
    output = subprocess.check_output(
        ["git", "ls-remote", "--tags", "--", url, ref, ref + "^{}"], text=True
    )
    refs = {name: oid for oid, name in (line.split("\t") for line in output.splitlines())}
    commit = refs.get(ref + "^{}") or refs.get(ref)
    if not commit:
        raise ValueError(f"找不到 Git 标签 {version}，请填写准确标签或完整提交号")
    return commit


def pin_repo(repo, source_dir, base):
    """记录标签首次解析的提交，缓存固定源码供依赖解析与后续安装复用。"""
    url = locate(repo.url, base)
    directory = source_dir / fingerprint([url, repo.version])
    record = directory / "origin.json"
    if not record.exists():
        resolved = Repo(url=url, commit=resolve_version(url, repo.version))
        write_json(record, resolved.model_dump())
    resolved = Repo.model_validate_json(record.read_bytes())
    checkout(resolved, directory / "repo", base)
    return Repo(url=str(directory / "repo"), commit=resolved.commit)


def resolve_lock(locked_repos, pytorch, directory):
    """为固定源码和当前 Python 平台生成一次带哈希锁，重试时核验已有结果。"""
    identity = {
        "repos": [repo.model_dump() for repo in locked_repos],
        "pytorch": pytorch,
        "python": list(sys.version_info[:3]),
        "platform": sysconfig.get_platform(),
    }
    target = directory / fingerprint(identity)
    lock = target / "requirements-gpu.lock"
    if not target.exists():
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as stage:
            # 只有解析成功并写入校验摘要后才整体发布目录。
            bundle = Path(stage) / "bundle"
            bundle.mkdir()
            reqs = [Path(repo.url) / "requirements.txt" for repo in locked_repos]
            generated = bundle / lock.name
            generate_lock(pytorch, [req for req in reqs if req.is_file()], generated)
            (bundle / "sha256").write_text(file_digest(generated), encoding="ascii")
            bundle.rename(target)
    expected = (target / "sha256").read_text(encoding="ascii")
    if file_digest(lock) != expected:
        raise ValueError(f"已保存的依赖锁摘要不符 {lock}")
    return lock, expected


def materialize(recipe, root, base):
    """从单份配置生成不可变部署清单，独立存放每次部署实际采用的锁和提交。"""
    validate_pytorch(recipe.pytorch)
    base = Path(base).resolve()
    root = Path(root).resolve()
    models = {
        name: pin_model(model, base, root / "model-objects")
        for name, model in recipe.models.items()
    }
    generated = root / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    # 串行化首次解析，确保并行启动遇到同一标签或依赖配置时使用相同快照。
    with (generated / "resolve.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        sources = generated / "sources"
        comfyui = pin_repo(recipe.comfyui, sources, base)
        nodes = {name: pin_repo(recipe.nodes[name], sources, base) for name in sorted(recipe.nodes)}
        lock, sha = resolve_lock([comfyui, *nodes.values()], recipe.pytorch, generated / "locks")
        spec = Deployment(
            comfyui=comfyui,
            nodes=nodes,
            models=models,
            instances=recipe.instances,
            requirements_lock=str(lock),
            requirements_sha256=sha,
        ).model_dump()
        manifest = generated / "manifests" / fingerprint(spec) / "deployment.json"
        write_json(manifest, spec)
    return manifest


def main():
    """从指定文件或标准输入读取引擎配置，生成清单后按需安装。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipe", help="引擎配置文件；- 表示标准输入")
    parser.add_argument("--base", type=Path, default=Path("/deployment"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="解析并校验配置和依赖锁")
    args = parser.parse_args()
    raw = sys.stdin.buffer.read() if args.recipe == "-" else Path(args.recipe).read_bytes()
    manifest = materialize(Recipe.model_validate_json(raw), args.root, args.base)
    print("部署清单", manifest, flush=True)
    print(
        "依赖锁",
        Deployment.model_validate_json(manifest.read_bytes()).requirements_lock,
        flush=True,
    )
    if not args.check:
        print(provision(manifest, args.root))


if __name__ == "__main__":
    main()
