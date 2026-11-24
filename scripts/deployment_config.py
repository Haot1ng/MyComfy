"""用宿主 Python 标准库读取部署参数，为 Compose 和 GPU 安装器分别提供所需数据。"""

from __future__ import annotations

import json
import os
import re
import runpy
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import quote, urlsplit

# 宿主 Python 无需安装业务包，仍与容器进程使用同一个标准库校验器。
normalize_origins = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "src/mycomfy/origins.py")
)["normalize_origins"]


def _object(value, field, allowed):
    """检查配置分组及其键名，在应用默认值前报告拼写错误。"""
    if not isinstance(value, dict):
        raise ValueError(f"{field} 需要填写 JSON 对象")  # noqa: TRY004 - 用户配置校验错误
    unknown = value.keys() - allowed
    if unknown:
        raise ValueError(f"{field} 含有未支持的字段：{', '.join(sorted(unknown))}")
    return value


def _text(value, field):
    """要求可传入 Compose 的非空单行字符串，错误消息只显示字段名。"""
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise ValueError(f"{field} 需要填写非空单行文本")
    return value


def _credentials(values, service, database_init):
    """保留初始化服务所需原值，并单独编码供客户端连接串使用的凭据。"""
    user = _text(values.get("user"), f"{service}.user")
    password = _text(values.get("password"), f"{service}.password")
    if service == "mysql":
        if database_init:
            # 镜像首次建库时会拼接 SQL。已有数据库的连接密码只需在 URL 中编码。
            for field, value in (("user", user), ("password", password)):
                if "'" in value or "\\" in value:
                    raise ValueError(f"mysql.{field} 含 MySQL 镜像初始化不支持的单引号或反斜杠")
        if len(user) > 32:
            raise ValueError("mysql.user 长度最多为 32 个字符")
    elif any(c.isspace() for c in user):
        raise ValueError("redis.user 需要填写不含空白的 Redis ACL 用户名")
    authority = quote(user, safe="") + ":" + quote(password, safe="")
    return user, password, authority


def _host(value, field, *, bind=False):
    """校验单个主机地址，返回未带方括号的主机和回环标记。发布端口只能绑定 IP。"""
    name = _text(value, field)
    # 同时支持 JSON 中常见的裸 IPv6 地址和 URL 风格的方括号 IPv6 地址。
    if name.startswith("[") and name.endswith("]") and ":" in name:
        name = name[1:-1]
    try:
        address = ip_address(name)
    except ValueError:
        if bind:
            raise ValueError(f"{field} 需要填写本机绑定端口的 IP 地址") from None
        labels = name.removesuffix(".")
        valid_label = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        if (
            len(labels) > 253
            or re.fullmatch(r"[0-9.]+", labels)
            or not all(re.fullmatch(valid_label, label) for label in labels.split("."))
        ):
            raise ValueError(f"{field} 需要填写单个 IP 地址或 DNS 主机名") from None
        return name, labels.lower() == "localhost"
    if "%" in name:
        raise ValueError(f"{field} 不支持带网络接口标识的 IPv6 地址")
    if not bind and address.is_unspecified:
        raise ValueError(f"{field} 需要填写客户端可连接的具体地址")
    return str(address), address.is_loopback


def _port(value, field):
    """检查 TCP 端口范围，防止布尔值被 Python 当成整数端口。"""
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError(f"{field} 需要填写 1 到 65535 之间的整数")
    return value


def _endpoint(host, port):
    """为连接串格式化主机和端口，IPv6 主机必须用方括号包围。"""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _connection_urls(mysql_auth, redis_auth, mysql_endpoint, engine_endpoint, redis_endpoint):
    """用已编码的凭据生成业务端、GPU 端和缓存连接串。"""
    return {
        "MYCOMFY_DATABASE_URL": f"mysql+pymysql://{mysql_auth}@{mysql_endpoint}/mycomfy",
        "MYCOMFY_ENGINE_DATABASE_URL": f"mysql+pymysql://{mysql_auth}@{engine_endpoint}/mycomfy",
        "MYCOMFY_REDIS_URL": f"redis://{redis_auth}@{redis_endpoint}/0" if redis_auth else "",
    }


def legacy_single_host_environment(services):
    """从 Compose 已解析的旧版 .env 提取内置账号、密码和端口，为单机生成本地连接。"""

    def authority(service, username, password):
        """还原 Compose 输出中的字面量 $，用与 JSON 部署相同的规则校验并编码凭据。"""
        values = services[service]["environment"]
        credentials = {
            "user": values[username].replace("$$", "$"),
            "password": values[password].replace("$$", "$"),
        }
        return _credentials(credentials, service, database_init=True)[2]

    # 宿主网络里的业务端使用发布端口，GPU 容器用 Compose 服务名直连 MySQL。
    loopback = "127.0.0.1"
    urls = _connection_urls(
        authority("mysql", "MYSQL_USER", "MYSQL_PASSWORD"),
        authority("redis", "REDIS_USER", "REDISCLI_AUTH"),
        _endpoint(loopback, services["mysql"]["ports"][0]["published"]),
        "mysql:3306",
        _endpoint(loopback, services["redis"]["ports"][0]["published"]),
    )
    return {**urls, "MYCOMFY_MYSQL_BIND_HOST": loopback, "MYCOMFY_REDIS_BIND_HOST": loopback}


def read_config(
    path: Path,
    repo: Path,
    *,
    core_only: bool,
    database_init: bool = False,
    redis_optional: bool = False,
    single_host: bool = False,
) -> tuple[dict[str, str], dict | None, bool]:
    """读取一次 JSON，返回 Compose 变量、当前角色所需的引擎配置和生效的单机标记。"""
    config = _object(
        json.loads(path.read_text(encoding="utf-8-sig")),
        "部署配置",
        {
            "single_host",
            "manual_retry_limit",
            "mysql",
            "redis",
            "paths",
            "ports",
            "public",
            "engine",
        },
    )
    retry_limit = config.get("manual_retry_limit", 3)
    if type(retry_limit) is not int or retry_limit < 0:
        raise ValueError("manual_retry_limit 需要填写非负整数，0 表示关闭手动重试")
    configured_single_host = config.get("single_host", False)
    if not isinstance(configured_single_host, bool):
        raise ValueError("single_host 需要填写 JSON 布尔值 true 或 false")  # noqa: TRY004
    single_host = single_host or configured_single_host
    # 单机全栈会创建内置库。按角色执行的命令仍只校验其所需的凭据与引擎参数。
    if single_host and not core_only and not redis_optional:
        database_init = True
    service_fields = {"user", "password", "host", "bind_host"}
    mysql = _object(config.get("mysql"), "mysql", service_fields | {"engine_host", "engine_port"})
    redis = _object(config.get("redis", {}), "redis", service_fields)
    mysql_user, mysql_password, mysql_auth = _credentials(mysql, "mysql", database_init)
    if redis_optional and not database_init and redis.get("password", "") == "":
        # GPU 进程只读写 MySQL。显式清空这些键，避免继承调用终端里的 Redis 凭据。
        redis_user, redis_password, redis_auth = "", "", ""
    else:
        redis_user, redis_password, redis_auth = _credentials(redis, "redis", database_init)

    # 用户指定的相对路径以配置文件为基准。省略时沿用仓库的 var 目录布局。
    paths = _object(config.get("paths", {}), "paths", {"data", "config", "deployment", "gateway"})
    directories = {}
    for name in ("data", "config", "deployment", "gateway"):
        if name in paths:
            target = path.parent / _text(paths[name], f"paths.{name}")
        else:
            target = repo / "var" / name
        directories[name] = str(target.resolve())

    defaults = {"mysql": 3306, "redis": 6379, "api": 8000, "admin": 8001}
    ports = {**defaults, **_object(config.get("ports", {}), "ports", defaults.keys())}
    for name, port in ports.items():
        _port(port, f"ports.{name}")

    if single_host:
        # 业务进程在宿主机网络中，GPU 进程走 Compose 内网。两端使用同一套内置服务。
        mysql_host = redis_host = mysql_bind = redis_bind = "127.0.0.1"
        engine_host, engine_port = "mysql", 3306
    else:
        mysql_host, local_mysql = _host(mysql.get("host", "127.0.0.1"), "mysql.host")
        redis_host, _ = _host(redis.get("host", "127.0.0.1"), "redis.host")
        mysql_bind, _ = _host(mysql.get("bind_host", "127.0.0.1"), "mysql.bind_host", bind=True)
        redis_bind, _ = _host(redis.get("bind_host", "127.0.0.1"), "redis.bind_host", bind=True)

        # 分机时允许单独指定 GPU 可达的地址，省略时按业务端 MySQL 位置推导。
        if "engine_host" in mysql:
            engine_host, _ = _host(mysql["engine_host"], "mysql.engine_host")
            engine_port = ports["mysql"]
        elif local_mysql:
            engine_host, engine_port = "mysql", 3306
        else:
            engine_host, engine_port = mysql_host, ports["mysql"]
        engine_port = _port(mysql.get("engine_port", engine_port), "mysql.engine_port")
    mysql_endpoint = _endpoint(mysql_host, ports["mysql"])
    engine_endpoint = _endpoint(engine_host, engine_port)
    redis_endpoint = _endpoint(redis_host, ports["redis"])

    public = _object(
        config.get("public", {}),
        "public",
        {
            "host",
            "origin",
            "cookie_secure",
            "admin_origin",
            "admin_test_origin",
            "trusted_proxy_cidrs",
        },
    )
    proxies = public.get("trusted_proxy_cidrs", [])
    if not isinstance(proxies, list) or not all(isinstance(value, str) for value in proxies):
        raise ValueError("public.trusted_proxy_cidrs 需要填写 IP 或 CIDR 字符串数组")
    try:
        proxy_cidrs = ",".join(str(ip_network(value)) for value in proxies)
    except ValueError:
        raise ValueError("public.trusted_proxy_cidrs 含有无效的 IP 或 CIDR") from None
    host = _text(public.get("host", "127.0.0.1"), "public.host")
    origin = _text(public.get("origin", f"http://127.0.0.1:{ports['api']}"), "public.origin")
    # 只在读取旧 JSON 时识别曾使用的字段名；新字段显式设为 null 会关闭该管理来源。
    admin = public.get("admin_origin", public.get("admin_test_origin"))
    origin, admin_origin = normalize_origins(origin, admin)
    try:
        address = urlsplit(origin)
        port = address.port
    except ValueError:
        raise ValueError("public.origin 需要填写有效的主机地址和端口") from None
    if (
        address.scheme not in {"http", "https"}
        or not address.hostname
        or address.username is not None
        or port == 0
        or address.path not in {"", "/"}
        or address.query
        or address.fragment
    ):
        raise ValueError(
            "public.origin 需要填写网页的 HTTP 或 HTTPS 来源，例如 https://video.example"
        )
    cookie_secure = public.get("cookie_secure", address.scheme == "https")
    if not isinstance(cookie_secure, bool):
        raise ValueError("public.cookie_secure 需要填写 true 或 false")  # noqa: TRY004

    # 客户端连接地址与数据库宿主机用于发布端口的监听地址分别传递。
    variables = {
        "MYSQL_USER": mysql_user,
        "MYSQL_PASSWORD": mysql_password,
        "REDIS_USER": redis_user,
        "REDIS_PASSWORD": redis_password,
        "MYCOMFY_MYSQL_HOST": mysql_host,
        "MYCOMFY_REDIS_HOST": redis_host,
        "MYCOMFY_MYSQL_BIND_HOST": mysql_bind,
        "MYCOMFY_REDIS_BIND_HOST": redis_bind,
        **_connection_urls(mysql_auth, redis_auth, mysql_endpoint, engine_endpoint, redis_endpoint),
        "MYCOMFY_DATA_ROOT": directories["data"],
        "MYCOMFY_CONFIG_DIR": directories["config"],
        "MYCOMFY_DEPLOYMENT_DIR": directories["deployment"],
        "MYCOMFY_GATEWAY_STATE_DIR": directories["gateway"],
        "MYCOMFY_TRUSTED_PROXY_CIDRS": proxy_cidrs,
        "MYCOMFY_MYSQL_PORT": str(ports["mysql"]),
        "MYCOMFY_REDIS_PORT": str(ports["redis"]),
        "MYCOMFY_API_PORT": str(ports["api"]),
        "MYCOMFY_LOCAL_PORT": str(ports["admin"]),
        "MYCOMFY_UID": str(os.getuid()),
        "MYCOMFY_GID": str(os.getgid()),
        "MYCOMFY_API_HOST": host,
        "MYCOMFY_MANUAL_RETRY_LIMIT": str(retry_limit),
        "MYCOMFY_PUBLIC_ORIGIN": origin.rstrip("/"),
        "MYCOMFY_ADMIN_ORIGIN": admin_origin or "",
        "MYCOMFY_COOKIE_SECURE": str(cookie_secure).lower(),
    }
    if core_only:
        return variables, None, single_host

    # 两个目录均会挂载到 GPU 容器。凭据文件需要留在这些目录以外。
    config_path = path.resolve()
    for name in ("data", "deployment"):
        if Path(directories[name]) in config_path.parents:
            raise ValueError(f"请将包含数据库密码的配置文件移出 paths.{name} 挂载目录")

    # 仅传递引擎分组。安装器在准备版本、模型和依赖锁前进一步校验其结构。
    engine = _object(
        config.get("engine"), "engine", {"comfyui", "pytorch", "nodes", "models", "instances"}
    )
    for required in ("comfyui", "pytorch"):
        if required not in engine:
            raise ValueError(f"engine.{required} 为全栈部署必填项")
    return variables, engine, single_host
