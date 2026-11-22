"""部署脚本和服务进程共同使用的 HTTPS 测试入口校验，仅依赖标准库。"""

import re
from ipaddress import IPv4Address, IPv6Address


def _https_origin(value, field):
    """检查完整 HTTPS 来源，按浏览器格式返回协议、主机和非默认端口。"""
    # 仅匹配来源本身，以免 URL 解析器吞掉控制符、空查询、用户信息或多余路径。
    pattern = r"https://(\[[a-f0-9:.]+\]|[a-z0-9.-]+)(?::([0-9]{1,5}))?/?"
    match = (
        re.fullmatch(pattern, value, re.ASCII | re.IGNORECASE) if isinstance(value, str) else None
    )
    if match is None:
        raise ValueError(f"{field} 需要填写完整的 HTTPS 来源，例如 https://admin.example")
    host, port = match[1].lower(), int(match[2] or "443")
    if not 1 <= port <= 65535:
        raise ValueError(f"{field} 的端口需要在 1 到 65535 之间")
    try:
        if host.startswith("["):
            host = f"[{IPv6Address(host[1:-1])}]"
        elif re.fullmatch(r"(?:[0-9]+|0x[a-f0-9]*)", host.rsplit(".", 1)[-1]):
            # 浏览器会把末段为数字的主机当作 IPv4。只接受标准的四段十进制形式。
            host = str(IPv4Address(host))
        else:
            label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            if len(host) > 253 or not all(re.fullmatch(label, item) for item in host.split(".")):
                raise ValueError
    except ValueError:
        raise ValueError(f"{field} 需要填写单个 DNS 主机名或 IP 地址") from None
    return f"https://{host}" + (f":{port}" if port != 443 else "")


def normalize_test_origins(public, admin):
    """启用公网管理测试时校验两个独立入口，关闭时保留工作台的原有配置。"""
    if admin is None or admin == "":
        return public, None
    admin = _https_origin(admin, "public.admin_test_origin")
    public = _https_origin(public, "public.origin")
    if public == admin:
        raise ValueError("public.admin_test_origin 和 public.origin 必须使用两个不同的来源")
    return public, admin
