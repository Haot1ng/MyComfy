"""通过 Compose 的真实配置解析器检查数据库和应用的启动边界。"""

import json
import os
import runpy
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
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps(
            {
                "mysql": {"user": "app", "password": "example", "host": "db.internal"},
                "redis": {"user": "default", "password": "example", "host": "cache.internal"},
                "public": {"origin": "https://workspace.example"},
            }
        )
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
    assert set(application) == {"core", "migrate", "engine", "worker"}
    assert set(application["core"]["depends_on"]) == {"migrate"}
    assert not application["migrate"].get("depends_on")
    for name in ("engine", "worker"):
        variables = application[name]["environment"]
        assert variables["MYCOMFY_PUBLIC_ORIGIN"] == "https://workspace.example"
        assert "@db.internal:3306/" in variables["MYCOMFY_DATABASE_URL"]

    # 显式启用数据库 profile 不会改变业务依赖；数据库可直接作为独立启动目标。
    combined = services("--profile", "database")
    assert set(combined) - set(application) == {"mysql", "redis"}
    for name in ("mysql", "redis"):
        assert not combined[name].get("depends_on")
        assert combined[name]["ports"][0]["host_ip"] == "127.0.0.1"


def test_legacy_single_host_uses_bundled_credentials(tmp_path, capsys):
    """用真实 Compose 读取旧 .env，验证强制单机时的密码解码、服务地址和发布端口。"""
    repo = Path(__file__).resolve().parents[2]
    legacy_settings = runpy.run_path(str(repo / "scripts" / "deployment_config.py"))[
        "legacy_single_host_environment"
    ]
    mysql_user, mysql_password = "studio$@admin", "mysql$$@host#%/"
    redis_user, redis_password = "redis$@cache", "redis$$@host#%/"
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"MYSQL_USER='{mysql_user}'\nMYSQL_PASSWORD='{mysql_password}'\n"
        f"REDIS_USER='{redis_user}'\nREDIS_PASSWORD='{redis_password}'\n"
        "MYCOMFY_MYSQL_PORT=13306\nMYCOMFY_REDIS_PORT=16379\n"
        "MYCOMFY_MYSQL_BIND_HOST=0.0.0.0\nMYCOMFY_REDIS_BIND_HOST=0.0.0.0\n"
        "MYCOMFY_DATABASE_URL=mysql+pymysql://remote@remote.database:5000/mycomfy\n"
        "MYCOMFY_ENGINE_DATABASE_URL=mysql+pymysql://remote@remote.gpu-db:5000/mycomfy\n"
        "MYCOMFY_REDIS_URL=redis://remote.cache:6381/0\n"
    )
    environment = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("COMPOSE_", "MYSQL_", "REDIS_", "MYCOMFY_"))
    }
    cmd = [
        "docker",
        "compose",
        "-f",
        str(repo / "compose.yaml"),
        "--env-file",
        str(env_file),
        "--profile",
        "database",
        "config",
        "--format",
        "json",
    ]

    def render():
        """让 Compose 在隔离目录中解析 .env 与本次调用的覆盖值，配置内容仅保存在内存中。"""
        result = subprocess.run(cmd, cwd=tmp_path, env=environment, capture_output=True, check=True)
        return json.loads(result.stdout)["services"]

    original = render()
    assert "remote.database" in original["core"]["environment"]["MYCOMFY_DATABASE_URL"]
    environment.update(legacy_settings(original))
    services = render()
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
    assert capsys.readouterr() == ("", "")
