"""用宿主 Python 标准库读取部署参数，为 Compose 和 GPU 安装器分别提供所需数据。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote, urlsplit


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


def _credentials(config, service):
    """保留初始化服务所需原值，并单独编码供客户端连接串使用的凭据。"""
    values = _object(config.get(service), service, {"user", "password"})
    user = _text(values.get("user"), f"{service}.user")
    password = _text(values.get("password"), f"{service}.password")
    if service == "mysql":
        # 官方 MySQL 8.4 初始化入口会将账号和密码嵌入单引号 SQL 字符串。
        for field, value in values.items():
            if "'" in value or "\\" in value:
                raise ValueError(f"mysql.{field} 含 MySQL 镜像初始化不支持的单引号或反斜杠")
        if len(user) > 32:
            raise ValueError("mysql.user 长度最多为 32 个字符")
    elif any(c.isspace() for c in user):
        raise ValueError("redis.user 需要填写不含空白的 Redis ACL 用户名")
    authority = quote(user, safe="") + ":" + quote(password, safe="")
    return user, password, authority


def read_config(path: Path, repo: Path, *, core_only: bool) -> tuple[dict[str, str], dict | None]:
    """读取单一配置，解析路径、端口和业务凭据，返回 Compose 变量及独立的引擎配置。"""
    config = _object(
        json.loads(path.read_text(encoding="utf-8-sig")),
        "部署配置",
        {"mysql", "redis", "paths", "ports", "public", "engine"},
    )
    mysql_user, mysql_password, mysql_auth = _credentials(config, "mysql")
    redis_user, redis_password, redis_auth = _credentials(config, "redis")

    # 用户指定的相对路径以配置文件为基准。省略时沿用仓库的 var 目录布局。
    paths = _object(config.get("paths", {}), "paths", {"data", "config", "deployment"})
    directories = {}
    for name in ("data", "config", "deployment"):
        if name in paths:
            target = path.parent / _text(paths[name], f"paths.{name}")
        else:
            target = repo / "var" / name
        directories[name] = str(target.resolve())

    defaults = {"mysql": 3306, "redis": 6379, "api": 8000, "admin": 8001}
    ports = {**defaults, **_object(config.get("ports", {}), "ports", defaults.keys())}
    for name, port in ports.items():
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError(f"ports.{name} 需要填写 1 到 65535 之间的整数")

    public = _object(config.get("public", {}), "public", {"host", "origin", "cookie_secure"})
    host = _text(public.get("host", "127.0.0.1"), "public.host")
    origin = _text(public.get("origin", f"http://127.0.0.1:{ports['api']}"), "public.origin")
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

    # core / migrate 通过宿主端口访问 MySQL；engine / worker 通过 Compose 内部端口访问。
    variables = {
        "MYSQL_USER": mysql_user,
        "MYSQL_PASSWORD": mysql_password,
        "REDIS_USER": redis_user,
        "REDIS_PASSWORD": redis_password,
        "MYCOMFY_DATABASE_URL": f"mysql+pymysql://{mysql_auth}@127.0.0.1:{ports['mysql']}/mycomfy",
        "MYCOMFY_ENGINE_DATABASE_URL": f"mysql+pymysql://{mysql_auth}@mysql:3306/mycomfy",
        "MYCOMFY_REDIS_URL": f"redis://{redis_auth}@127.0.0.1:{ports['redis']}/0",
        "MYCOMFY_DATA_ROOT": directories["data"],
        "MYCOMFY_CONFIG_DIR": directories["config"],
        "MYCOMFY_DEPLOYMENT_DIR": directories["deployment"],
        "MYCOMFY_MYSQL_PORT": str(ports["mysql"]),
        "MYCOMFY_REDIS_PORT": str(ports["redis"]),
        "MYCOMFY_API_PORT": str(ports["api"]),
        "MYCOMFY_LOCAL_PORT": str(ports["admin"]),
        "MYCOMFY_UID": str(os.getuid()),
        "MYCOMFY_GID": str(os.getgid()),
        "MYCOMFY_API_HOST": host,
        "MYCOMFY_PUBLIC_ORIGIN": origin.rstrip("/"),
        "MYCOMFY_COOKIE_SECURE": str(cookie_secure).lower(),
    }
    if core_only:
        return variables, None

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
    return variables, engine
