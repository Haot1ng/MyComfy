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

from . import bundled
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
from .lockfile import EXACT_VERSION, generate_lock, validate_pytorch
from .model_objects import reuse_model_object


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


class SageAttentionVersion(Record):
    """从 Python 包索引解析指定版本的 SageAttention wheel。"""

    version: str = Field(pattern=f"^{EXACT_VERSION.pattern}$")


class SageAttentionWheel(Record):
    """从部署输入目录读取与 GPU、Torch、CUDA 匹配的已知 wheel。"""

    wheel: str = Field(min_length=1)
    sha256: Sha256


class Recipe(EngineLayout):
    """引擎容器收到的配置，只包含引擎部署所需参数。"""

    comfyui: VersionedRepo
    nodes: dict[Name, VersionedRepo] = Field(default_factory=dict)
    models: dict[Name, InputModel] = Field(default_factory=dict)
    pytorch: dict[str, str]
    sageattention: SageAttentionVersion | SageAttentionWheel | None = None
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
    elif Path(source).exists() or digest is None:
        actual = file_digest(Path(source))
        if digest is not None and digest != actual:
            raise ValueError(f"本地模型摘要不符 {model.path}")
        digest = actual
    elif not (objects / digest).is_file():
        # 原始导入文件已移走时，也允许从历史版本恢复已验证的本地对象。
        if not objects.parent.is_dir():
            raise FileNotFoundError(source)
        with (objects.parent / "deploy.lock").open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            if not reuse_model_object(objects / digest, digest, model.path):
                raise FileNotFoundError(source)
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


def sageattention_requirement(spec, base):
    """生成解析器的受限输入；本地 wheel 即使复用旧锁也要核对配置摘要。"""
    if spec is None:
        return None
    if isinstance(spec, SageAttentionVersion):
        return f"sageattention=={spec.version}", None
    if Path(spec.wheel).is_absolute() or urlsplit(spec.wheel).scheme:
        raise ValueError("sageattention.wheel 请填写部署目录内的相对路径")
    wheel = (base / spec.wheel).resolve()
    if not wheel.is_relative_to(base):
        raise ValueError("sageattention.wheel 超出部署目录")
    if wheel.suffix != ".whl" or wheel.name.partition("-")[0].lower() != "sageattention":
        raise ValueError("sageattention.wheel 需要 sageattention 包的 .whl 文件")
    if not wheel.is_file() or file_digest(wheel) != spec.sha256:
        raise ValueError(f"SageAttention wheel 缺失或 SHA-256 不匹配：{wheel.name}")
    return f"sageattention @ {wheel.as_uri()}", spec.sha256


def resolve_lock(locked_repos, pytorch, directory, *, sageattention=None):
    """为固定源码和当前 Python 平台生成一次带哈希锁，重试时核验已有结果。"""
    identity = {
        "repos": [repo.model_dump() for repo in locked_repos],
        "pytorch": pytorch,
        "python": list(sys.version_info[:3]),
        "platform": sysconfig.get_platform(),
    }
    if sageattention is not None:
        # 未启用时原样复用历史锁；启用或切换 wheel 时生成独立快照。
        identity["sageattention"] = sageattention
    target = directory / fingerprint(identity)
    lock = target / "requirements-gpu.lock"
    if not target.exists():
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as stage:
            # 只有解析成功并写入校验摘要后才整体发布目录。
            bundle = Path(stage) / "bundle"
            bundle.mkdir()
            reqs = [Path(repo.url) / "requirements.txt" for repo in locked_repos]
            if sageattention is not None:
                requirement, pinned_hash = sageattention
                extra = Path(stage) / "sageattention.txt"
                extra.write_text(f"--only-binary sageattention\n{requirement}\n", encoding="utf-8")
                reqs.append(extra)
            generated = bundle / lock.name
            generate_lock(pytorch, [req for req in reqs if req.is_file()], generated)
            if sageattention is not None and pinned_hash is not None:
                # uv 将实际解析到的 URL 和摘要写进锁，二次比对后再发布给 pip --require-hashes。
                expected_line = f"{requirement} --hash=sha256:{pinned_hash}"
                if expected_line not in generated.read_text(encoding="utf-8").splitlines():
                    raise ValueError("解析到的 SageAttention wheel 与配置的文件或摘要不符")
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
    sageattention = sageattention_requirement(recipe.sageattention, base)
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
        lock, sha = resolve_lock(
            [comfyui, *nodes.values()],
            recipe.pytorch,
            generated / "locks",
            sageattention=sageattention,
        )
        spec = Deployment(
            comfyui=comfyui,
            nodes=nodes,
            bundled_nodes=bundled.select_nodes(comfyui.commit, nodes),
            models=models,
            instances=recipe.instances,
            requirements_lock=str(lock),
            requirements_sha256=sha,
            verify_sageattention=sageattention is not None,
        ).model_dump(exclude={"verify_sageattention"} if sageattention is None else set())
        manifest = generated / "manifests" / fingerprint(spec) / "deployment.json"
        write_json(manifest, spec)
    return manifest


def main(argv=None):
    """执行 mycomfy deploy，从文件或标准输入读取引擎配置，生成固定清单后按需安装。"""
    parser = argparse.ArgumentParser(prog="mycomfy deploy", description=__doc__)
    parser.add_argument("recipe", help="业务配置中 engine 分组的 JSON 文件，- 表示标准输入")
    parser.add_argument(
        "--base", type=Path, default=Path("/deployment"), help="本地模型与源码的基准目录"
    )
    parser.add_argument("--root", type=Path, required=True, help="保存引擎及依赖锁的持久目录")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="解析并校验配置和依赖锁")
    mode.add_argument(
        "--refresh-weights", action="store_true", help="从各模型的配置来源显式刷新权重并校验"
    )
    parser.add_argument("--prepared-weight", metavar="SHA256", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.prepared_weight is not None and not args.refresh_weights:
        parser.error("--prepared-weight 需要 --refresh-weights")
    raw = sys.stdin.buffer.read() if args.recipe == "-" else Path(args.recipe).read_bytes()
    manifest = materialize(Recipe.model_validate_json(raw), args.root, args.base)
    print("部署清单", manifest, flush=True)
    print(
        "依赖锁",
        Deployment.model_validate_json(manifest.read_bytes()).requirements_lock,
        flush=True,
    )
    # 同一份生成的清单用于校验和安装，避免锁定后重新解析标签或依赖。
    if not args.check:
        print(
            provision(
                manifest,
                args.root,
                refresh_weights=args.refresh_weights,
                prepared_weight=args.prepared_weight,
            )
        )
