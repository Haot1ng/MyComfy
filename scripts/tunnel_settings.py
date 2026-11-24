"""在部署主机上加载独立的外网测试配置，并与 Tailscale 的当前设备身份核对。"""

from __future__ import annotations

import os
import re
import runpy
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile

# 使用部署入口的文档解析器，从仓库的任意工作目录启动都无需预装 YAML 包。
load_document = runpy.run_path(str(Path(__file__).with_name("config_document.py")))["load_document"]


def _device_name(value, field):
    """校验完整的 ts.net 设备 DNS 名称，返回小写且无末尾点的形式。"""
    if isinstance(value, str) and value.isascii():
        name = value.removesuffix(".").lower()
        label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        if (
            len(name) <= 253
            and name.endswith(".ts.net")
            and all(re.fullmatch(label, part) for part in name.split("."))
        ):
            return name
    raise ValueError(
        f"{field} 需要完整的 Tailscale 设备 DNS 名称，例如 workstation.example-tailnet.ts.net"
    )


@dataclass(frozen=True)
class TunnelSettings:
    """两条公开入口共用的设备身份，以及各自的外部 HTTPS 端口。"""

    machine_name: str = "auto"
    public_https_port: int = 443
    admin_https_port: int = 8443
    _read_personal: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self):
        """统一设备名格式，要求两个入口分别使用 Tailscale Funnel 支持的端口。"""
        if isinstance(self.machine_name, str) and self.machine_name.lower() == "auto":
            object.__setattr__(self, "machine_name", "auto")
        else:
            object.__setattr__(
                self, "machine_name", _device_name(self.machine_name, "machine_name")
            )
        for port_name in ("public_https_port", "admin_https_port"):
            value = getattr(self, port_name)
            if type(value) is not int or value not in {443, 8443, 10000}:
                raise ValueError(f"{port_name} 需要填写整数 443、8443 或 10000")
        if self.public_https_port == self.admin_https_port:
            raise ValueError("public_https_port 和 admin_https_port 需要使用不同的端口")

    def resolve_machine_name(self, actual_dns_name: str) -> str:
        """以 Tailscale Self.DNSName 为准解析 auto，并拒绝错误设备上的显式配置。"""
        actual = _device_name(actual_dns_name, "Tailscale Self.DNSName")
        if self.machine_name != "auto" and self.machine_name != actual:
            raise ValueError("machine_name 与当前 Tailscale 登录设备不一致，请核对设备或设为 auto")
        return actual


def load_tunnel_settings(repo: Path, requested: Path | None = None) -> TunnelSettings:
    """优先读取显式路径或个人设置，缺少个人设置时读取模板；旧业务配置保留原样。"""
    personal = Path(repo) / "deploy.test.yaml"
    if requested is not None:
        path = Path(requested)
    elif personal.exists() or personal.is_symlink():
        path = personal
    else:
        path = Path(repo) / "deploy.test.example.yaml"
    try:
        config = load_document(path)
    except FileNotFoundError:
        raise ValueError(f"找不到测试配置文件 {path}，请核对路径。") from None
    except IsADirectoryError:
        raise ValueError(f"测试配置路径 {path} 指向目录，请指定文件。") from None
    # 旧操作指引曾让用户把整份业务配置复制成 deploy.test.yaml；保留其中的数据库和部署参数。
    if config.keys() & {
        "single_host",
        "manual_retry_limit",
        "mysql",
        "redis",
        "paths",
        "ports",
        "public",
        "engine",
    }:
        raise ValueError(
            f"{path} 是业务部署配置。请用 --config 指定该文件，并用 "
            "--test-config deploy.test.example.yaml 或单独的测试配置文件指定测试入口。"
        )
    if config.keys() - {"machine_name", "public_https_port", "admin_https_port"}:
        raise ValueError("测试配置仅支持 machine_name、public_https_port 和 admin_https_port")
    return TunnelSettings(**config, _read_personal=requested is None and path == personal)


def remember_machine_name(
    repo: Path, requested: Path | None, settings: TunnelSettings, resolved: str
) -> bool:
    """两个入口都已就绪时，仅为首次隐式使用模板的会话创建已注明用途的个人配置。"""
    personal = Path(repo) / "deploy.test.yaml"
    # 显式指定的文件保持不变；个人文件在启动后被主动删除时也应尊重这次删除。
    if (
        requested is not None
        or settings._read_personal
        or personal.exists()
        or personal.is_symlink()
    ):
        return False
    name = settings.resolve_machine_name(resolved)
    content = (
        "# 本次成功建立两个测试入口后记录的个人设置。可直接编辑此文件。\n"
        "# 设备名会与 Tailscale Self.DNSName 核对；改为 auto 可随当前登录设备切换。\n"
        f'machine_name: "{name}"\n\n'
        "# 工作台和管理后台各使用一个外部 HTTPS 端口，可选 443、8443 或 10000。\n"
        f"public_https_port: {settings.public_https_port}  # 工作台\n"
        f"admin_https_port: {settings.admin_https_port}  # 管理后台\n"
    )
    # 在同一目录写完再创建硬链接，其他进程只能读到完整内容；抢先出现的个人文件会保留。
    with NamedTemporaryFile(dir=personal.parent, mode="w", encoding="utf-8") as draft:
        draft.write(content)
        draft.flush()
        os.fsync(draft.fileno())
        try:
            os.link(draft.name, personal)
        except FileExistsError:
            return False
    return True
