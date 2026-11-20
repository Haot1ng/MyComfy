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
    values, inputs = read_config(path, repo, core_only=False)
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
    values, recipe = read_config(path, tmp_path, core_only=True)
    assert recipe is None
    assert values["MYCOMFY_PUBLIC_ORIGIN"] == "http://127.0.0.1:8000"
    with pytest.raises(ValueError, match="engine"):
        read_config(path, tmp_path, core_only=False)
    data["mysql"]["password"] = "credential'not-for-logs"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=r"mysql\.password") as error:
        read_config(path, tmp_path, core_only=True)
    assert data["mysql"]["password"] not in str(error.value)


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
