"""验证部署配置向 Docker 和应用传递相同的账号、地址与挂载目录。"""

import json
import os
import runpy
from pathlib import Path

import pytest
from redis import Redis
from sqlalchemy.engine import make_url

read_config = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts" / "deployment_config.py")
)["read_config"]


def test_custom_accounts_and_paths(tmp_path):
    """用实际驱动解析连接串，验证特殊字符、容器内外端口和配置相对路径。"""
    repo = tmp_path / "application"
    config_dir = tmp_path / "settings"
    config_dir.mkdir()
    path = config_dir / "config.json"
    recipe = {"comfyui": {"url": "engine", "version": "1.0"}, "pytorch": {"torch": "2.6.0"}}
    mysql_password = "My$@:/#% 密码"
    redis_password = "R'$@:/#% 密码"
    path.write_text(
        json.dumps(
            {
                "mysql": {"user": "a@b", "password": mysql_password},
                "redis": {"user": "cache", "password": redis_password},
                "ports": {"mysql": 3333, "redis": 6380, "api": 8080, "admin": 8081},
                "paths": {"data": "files", "deployment": "/srv/engine-import"},
                "public": {"origin": "https://video.example/"},
                "engine": recipe,
            }
        )
    )
    values, inputs, single_host = read_config(path, repo, core_only=False)
    assert single_host is False
    assert inputs == recipe
    assert values["MYSQL_PASSWORD"] == mysql_password and values["REDIS_PASSWORD"] == redis_password
    for key, host, port in (
        ("MYCOMFY_DATABASE_URL", "127.0.0.1", 3333),
        ("MYCOMFY_ENGINE_DATABASE_URL", "mysql", 3306),
    ):
        url = make_url(values[key])
        assert (url.username, url.password) == ("a@b", mysql_password)
        assert (url.host, url.port, url.database) == (host, port, "mycomfy")
    with Redis.from_url(values["MYCOMFY_REDIS_URL"]) as client:
        options = client.connection_pool.connection_kwargs
        assert (options["username"], options["password"]) == ("cache", redis_password)
        assert (options["host"], options["port"], options["db"]) == ("127.0.0.1", 6380, 0)
    assert values["MYCOMFY_DATA_ROOT"] == str(config_dir / "files")
    assert values["MYCOMFY_CONFIG_DIR"] == str(repo / "var/config")
    assert values["MYCOMFY_DEPLOYMENT_DIR"] == "/srv/engine-import"
    assert values["MYCOMFY_PUBLIC_ORIGIN"] == "https://video.example"
    assert values["MYCOMFY_COOKIE_SECURE"] == "true"
    assert values["MYCOMFY_MYSQL_HOST"] == values["MYCOMFY_REDIS_HOST"] == "127.0.0.1"
    assert values["MYCOMFY_MYSQL_BIND_HOST"] == values["MYCOMFY_REDIS_BIND_HOST"] == "127.0.0.1"
    assert (values["MYCOMFY_UID"], values["MYCOMFY_GID"]) == (str(os.getuid()), str(os.getgid()))


def test_core_only_and_secret_errors(tmp_path):
    """业务端独立于 GPU 配置，错误反馈不会包含密码。"""
    path = tmp_path / "config.json"
    data = {
        "mysql": {"user": "mycomfy", "password": "mysql-local"},
        "redis": {"user": "default", "password": "redis-local"},
        "engine": "尚未配置",
    }
    path.write_text(json.dumps(data))
    values, recipe, _ = read_config(path, tmp_path, core_only=True)
    assert recipe is None
    assert values["MYCOMFY_PUBLIC_ORIGIN"] == "http://127.0.0.1:8000"
    with pytest.raises(ValueError, match="engine"):
        read_config(path, tmp_path, core_only=False)
    data["mysql"]["password"] = "credential'not-for-logs"
    path.write_text(json.dumps(data))
    values, _, _ = read_config(path, tmp_path, core_only=True)
    assert make_url(values["MYCOMFY_DATABASE_URL"]).password == data["mysql"]["password"]
    with pytest.raises(ValueError, match=r"mysql\.password") as error:
        read_config(path, tmp_path, core_only=True, database_init=True)
    assert data["mysql"]["password"] not in str(error.value)


def test_remote_endpoints_and_publish_addresses(tmp_path):
    """远端 MySQL 与 Redis 可在不同地址，GPU 能默认共用或显式覆盖端点。"""
    path = tmp_path / "config.json"
    config = {
        "mysql": {"host": "db.internal", "bind_host": "10.0.1.2", "user": "db", "password": "p"},
        "redis": {"host": "[fd00::7]", "bind_host": "::", "user": "cache", "password": "p"},
        "ports": {"mysql": 13306, "redis": 16379},
    }
    path.write_text(json.dumps(config))
    values, _, _ = read_config(path, tmp_path, core_only=True)
    db = make_url(values["MYCOMFY_DATABASE_URL"])
    assert (db.host, db.port) == ("db.internal", 13306)
    assert values["MYCOMFY_ENGINE_DATABASE_URL"] == values["MYCOMFY_DATABASE_URL"]
    assert values["MYCOMFY_MYSQL_BIND_HOST"] == "10.0.1.2"
    assert values["MYCOMFY_REDIS_BIND_HOST"] == "::"
    with Redis.from_url(values["MYCOMFY_REDIS_URL"]) as client:
        options = client.connection_pool.connection_kwargs
        assert (options["host"], options["port"]) == ("fd00::7", 16379)

    config["mysql"]["engine_host"] = "fd00::2"
    path.write_text(json.dumps(config))
    values, _, _ = read_config(path, tmp_path, core_only=True)
    db = make_url(values["MYCOMFY_ENGINE_DATABASE_URL"])
    assert (db.host, db.port) == ("fd00::2", 13306)

    config["mysql"]["engine_port"] = 33306
    path.write_text(json.dumps(config))
    values, _, _ = read_config(path, tmp_path, core_only=True)
    assert make_url(values["MYCOMFY_ENGINE_DATABASE_URL"]).port == 33306
    assert make_url(values["MYCOMFY_DATABASE_URL"]).port == 13306


@pytest.mark.parametrize(("configured", "forced"), [(True, False), (False, True)])
def test_single_host_uses_local_databases_for_all_services(tmp_path, configured, forced):
    """单机 JSON 和命令行选项都固定回环监听与各网络的访问路径，并沿用自定义宿主端口。"""
    path = tmp_path / "config.json"
    recipe = {"comfyui": {"url": "engine", "version": "1.0"}, "pytorch": {"torch": "2.6.0"}}
    config = {
        "single_host": configured,
        "mysql": {
            "host": "other-db",
            "bind_host": "0.0.0.0",
            "engine_host": "remote-db",
            "engine_port": 4000,
            "user": "db",
            "password": "pass",
        },
        "redis": {"host": "other-cache", "bind_host": "::", "user": "cache", "password": "pass"},
        "ports": {"mysql": 13306, "redis": 16379},
        "engine": recipe,
    }
    path.write_text(json.dumps(config))
    values, inputs, single_host = read_config(path, tmp_path, core_only=False, single_host=forced)
    assert single_host is True and inputs == recipe
    assert values["MYCOMFY_MYSQL_HOST"] == values["MYCOMFY_MYSQL_BIND_HOST"] == "127.0.0.1"
    assert values["MYCOMFY_REDIS_HOST"] == values["MYCOMFY_REDIS_BIND_HOST"] == "127.0.0.1"
    assert values["MYCOMFY_DATABASE_URL"] == "mysql+pymysql://db:pass@127.0.0.1:13306/mycomfy"
    assert values["MYCOMFY_ENGINE_DATABASE_URL"] == "mysql+pymysql://db:pass@mysql:3306/mycomfy"
    assert values["MYCOMFY_REDIS_URL"] == "redis://cache:pass@127.0.0.1:16379/0"

    # 隐式全栈必须能建库。纯业务启动和诊断允许沿用已有账号，且无需准备引擎输入。
    config["mysql"]["password"] = "existing'password"
    del config["engine"]
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match=r"mysql\.password"):
        read_config(path, tmp_path, core_only=False, single_host=forced)
    values, inputs, single_host = read_config(path, tmp_path, core_only=True, single_host=forced)
    assert single_host is True and inputs is None
    assert make_url(values["MYCOMFY_DATABASE_URL"]).password == "existing'password"
    config["mysql"]["password"] = "pass"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="engine"):
        read_config(path, tmp_path, core_only=False, single_host=forced)


@pytest.mark.parametrize("value", ["true", 1, None])
def test_single_host_accepts_json_booleans_only(tmp_path, value):
    """拒绝可能被误解成启用整机部署的字符串、数字和空值。"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"single_host": value}))
    with pytest.raises(ValueError, match="single_host"):
        read_config(path, tmp_path, core_only=True)


@pytest.mark.parametrize("redis", [None, {"user": "cache", "password": ""}])
def test_gpu_only_omits_redis_without_inheriting_password(tmp_path, monkeypatch, redis):
    """GPU 机可以只提供 MySQL 和引擎参数，运维查看配置时也能使用同一个文件。"""
    path = tmp_path / "gpu.json"
    recipe = {"comfyui": {"url": "engine", "version": "1.0"}, "pytorch": {"torch": "2.6.0"}}
    config = {"mysql": {"host": "db.internal", "user": "db", "password": "mysql"}, "engine": recipe}
    if redis is not None:
        config["redis"] = redis
    path.write_text(json.dumps(config))
    monkeypatch.setenv("REDIS_PASSWORD", "unrelated-ambient-secret")
    monkeypatch.setenv("MYCOMFY_REDIS_URL", "redis://other.internal")
    values, inputs, _ = read_config(path, tmp_path, core_only=False, redis_optional=True)
    assert inputs == recipe
    assert make_url(values["MYCOMFY_ENGINE_DATABASE_URL"]).host == "db.internal"
    assert values["MYCOMFY_PUBLIC_ORIGIN"] == "http://127.0.0.1:8000"
    assert values["REDIS_PASSWORD"] == values["REDIS_USER"] == values["MYCOMFY_REDIS_URL"] == ""
    diagnostics, _, _ = read_config(path, tmp_path, core_only=True, redis_optional=True)
    assert diagnostics == values
    # 业务端和配套数据库的正常启动入口仍要求有效的 Redis 配置。
    with pytest.raises(ValueError, match="redis"):
        read_config(path, tmp_path, core_only=True)
    with pytest.raises(ValueError, match="redis"):
        read_config(path, tmp_path, core_only=True, database_init=True, redis_optional=True)
    config["redis"] = {"user": "active", "password": "configured"}
    path.write_text(json.dumps(config))
    active, _, _ = read_config(path, tmp_path, core_only=True, redis_optional=True)
    assert active["REDIS_USER"] == "active" and active["REDIS_PASSWORD"] == "configured"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", "mysql://10.0.1.2:3306"),
        ("host", "db.internal/schema"),
        ("host", "user@db.internal"),
        ("engine_host", ""),
        ("engine_host", "::"),
        ("host", "0.0.0.0"),
        ("bind_host", "host.example"),
        ("engine_port", 65536),
    ],
)
def test_invalid_service_endpoint(tmp_path, field, value):
    """区分主机、端口和发布 IP 的错误，避免生成指向其他库或路径的 URL。"""
    path = tmp_path / "config.json"
    config = {
        "mysql": {"user": "db", "password": "p", field: value},
        "redis": {"user": "cache", "password": "p"},
    }
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match=rf"mysql\.{field}"):
        read_config(path, tmp_path, core_only=True)


def test_credential_file_outside_engine_mounts(tmp_path):
    """全栈模式拒绝将整个含密码的用户配置挂载进 GPU 容器。"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "mysql": {"user": "mycomfy", "password": "mysql-local"},
                "redis": {"user": "default", "password": "redis-local"},
                "engine": {"comfyui": {}, "pytorch": {}},
                "paths": {"deployment": "."},
            }
        )
    )
    with pytest.raises(ValueError, match=r"paths\.deployment"):
        read_config(path, tmp_path, core_only=False)
