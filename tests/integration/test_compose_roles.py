"""通过 Compose 的真实配置解析器检查数据库和应用的启动边界。"""

import json
import os
import subprocess
import sys
from pathlib import Path

from redis import Redis
from sqlalchemy.engine import make_url


def test_database_profile_is_independent(tmp_path, monkeypatch):
    """业务与 GPU 的依赖图不引用随附数据库，启用 database 后可单独启动两个数据服务。"""
    repo = Path(__file__).resolve().parents[2]
    for name in ("COMPOSE_FILE", "COMPOSE_PROFILES"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "settings.yaml"
    config.write_text(
        "mysql: {user: app, password: example, host: db.internal}\n"
        "redis: {user: default, password: example, host: cache.internal}\n"
        "public:\n"
        "  origin: https://workspace.example\n"
        "  admin_origin: https://admin.example:443/\n"
    )
    command = [
        sys.executable,
        str(repo / "scripts/deploy.py"),
        "--config",
        str(config),
        "--compose",
    ]

    def services(*profile):
        """读取指定 profile 下的服务定义，仅解析 Compose 配置。"""
        output = subprocess.check_output([*command, *profile, "config", "--format", "json"])
        return json.loads(output)["services"]

    application = services()
    assert set(application) == {"gateway", "core", "migrate", "engine", "worker"}
    gateway = application["gateway"]
    assert gateway["depends_on"]["core"]["condition"] == "service_healthy"
    assert gateway["network_mode"] == application["core"]["network_mode"] == "host"
    assert application["core"]["command"] == ["api", "--socket-dir", "/run/mycomfy-api"]
    assert gateway["user"] == application["core"]["user"]
    mounts = {mount["target"]: mount for mount in gateway["volumes"]}
    assert set(mounts) == {"/run/mycomfy-api", "/var/lib/mycomfy-gateway"}
    assert mounts["/run/mycomfy-api"]["read_only"]
    assert set(gateway["environment"]) == {
        "MYCOMFY_API_HOST",
        "MYCOMFY_API_PORT",
        "MYCOMFY_LOCAL_PORT",
        "MYCOMFY_GATEWAY_DIR",
        "MYCOMFY_TRUSTED_PROXY_CIDRS",
    }
    assert set(application["core"]["depends_on"]) == {"migrate"}
    assert not application["migrate"].get("depends_on")
    for name in ("engine", "worker"):
        variables = application[name]["environment"]
        assert variables["MYCOMFY_PUBLIC_ORIGIN"] == "https://workspace.example"
        assert "@db.internal:3306/" in variables["MYCOMFY_DATABASE_URL"]

    # 显式启用数据库 profile 不会改变业务依赖；数据库可直接作为独立启动目标。
    combined = services("--profile", "database")
    assert set(combined) - set(application) == {"mysql", "redis"}
    # 只有处理管理请求的 core 获得该来源，其他进程照常使用原有配置。
    assert combined["core"]["environment"]["MYCOMFY_ADMIN_ORIGIN"] == "https://admin.example"
    for name, service in combined.items():
        if name != "core":
            assert "MYCOMFY_ADMIN_ORIGIN" not in service["environment"]
    for name in ("mysql", "redis"):
        assert not combined[name].get("depends_on")
        assert combined[name]["ports"][0]["host_ip"] == "127.0.0.1"


def test_single_host_uses_configured_credentials_and_ports(tmp_path, monkeypatch):
    """用部署命令渲染单机服务，核对特殊字符凭据、服务间地址和宿主发布端口。"""
    repo = Path(__file__).resolve().parents[2]
    for key in ("COMPOSE_FILE", "COMPOSE_PROFILES"):
        monkeypatch.delenv(key, raising=False)
    mysql_user, mysql_password = "studio$@admin", "mysql$$@host#%/"
    redis_user, redis_password = "redis$@cache", "redis$$@host#%/"
    config = tmp_path / "deploy.yaml"
    config.write_text(
        "single_host: true\n"
        f"mysql: {{user: '{mysql_user}', password: '{mysql_password}', host: db.internal}}\n"
        f"redis: {{user: '{redis_user}', password: '{redis_password}', host: cache.internal}}\n"
        "ports: {mysql: 13306, redis: 16379}\n"
    )
    # 经正式 CLI 加载业务配置，让 Compose 验证宿主与 GPU 容器实际获得的环境。
    output = subprocess.check_output(
        [
            sys.executable,
            str(repo / "scripts/deploy.py"),
            "--config",
            str(config),
            "--compose",
            "config",
            "--format",
            "json",
        ],
        cwd=tmp_path,
    )
    services = json.loads(output)["services"]
    for service, host, port in (
        ("core", "127.0.0.1", 13306),
        ("migrate", "127.0.0.1", 13306),
        ("engine", "mysql", 3306),
        ("worker", "mysql", 3306),
    ):
        database = make_url(services[service]["environment"]["MYCOMFY_DATABASE_URL"])
        assert (database.host, database.port, database.database) == (host, port, "mycomfy")
        assert (database.username, database.password) == (mysql_user, mysql_password)
    with Redis.from_url(services["core"]["environment"]["MYCOMFY_REDIS_URL"]) as client:
        options = client.connection_pool.connection_kwargs
        assert (options["host"], options["port"], options["db"]) == ("127.0.0.1", 16379, 0)
        assert (options["username"], options["password"]) == (redis_user, redis_password)
    for service, port in (("mysql", "13306"), ("redis", "16379")):
        published = services[service]["ports"][0]
        assert (published["host_ip"], published["published"]) == ("127.0.0.1", port)


def test_stop_configuration_does_not_invent_connection_urls(monkeypatch):
    """停机上下文可渲染容器定义，单独的数据服务初始化参数不会被拼成业务连接串。"""
    repo = Path(__file__).resolve().parents[2]
    for name in ("MYCOMFY_DATABASE_URL", "MYCOMFY_ENGINE_DATABASE_URL", "MYCOMFY_REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MYSQL_PASSWORD", "db-init-only")
    monkeypatch.setenv("REDIS_PASSWORD", "cache-init-only")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(repo / "compose.yaml"),
            "--env-file",
            os.devnull,
            "--profile",
            "database",
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(result.stdout)["services"]
    assert services["mysql"]["environment"]["MYSQL_PASSWORD"] == "db-init-only"
    assert services["redis"]["environment"]["REDISCLI_AUTH"] == "cache-init-only"
    for name in ("core", "migrate", "engine", "worker"):
        assert services[name]["environment"]["MYCOMFY_DATABASE_URL"] == ""
    assert services["core"]["environment"]["MYCOMFY_REDIS_URL"] == ""
