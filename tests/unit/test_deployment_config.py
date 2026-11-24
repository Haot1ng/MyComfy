"""验证部署配置向 Docker 和应用传递相同的账号、地址与挂载目录。"""

import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
from redis import Redis
from sqlalchemy.engine import make_url

from mycomfy.config import Settings

REPO = Path(__file__).resolve().parents[2]
read_config = runpy.run_path(str(REPO / "scripts/deployment_config.py"))["read_config"]


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
                "paths": {"data": "files", "gateway": "edge", "deployment": "/srv/engine-import"},
                "public": {
                    "origin": "https://video.example/",
                    "admin_origin": "HTTPS://ADMIN.Example:443/",
                    "trusted_proxy_cidrs": ["192.0.2.10", "2001:db8::/48"],
                },
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
    assert values["MYCOMFY_GATEWAY_STATE_DIR"] == str(config_dir / "edge")
    assert values["MYCOMFY_TRUSTED_PROXY_CIDRS"] == "192.0.2.10/32,2001:db8::/48"
    assert values["MYCOMFY_DEPLOYMENT_DIR"] == "/srv/engine-import"
    assert values["MYCOMFY_PUBLIC_ORIGIN"] == "https://video.example"
    assert values["MYCOMFY_ADMIN_ORIGIN"] == "https://admin.example"
    assert values["MYCOMFY_COOKIE_SECURE"] == "true"
    assert values["MYCOMFY_MYSQL_HOST"] == values["MYCOMFY_REDIS_HOST"] == "127.0.0.1"
    assert values["MYCOMFY_MYSQL_BIND_HOST"] == values["MYCOMFY_REDIS_BIND_HOST"] == "127.0.0.1"
    assert (values["MYCOMFY_UID"], values["MYCOMFY_GID"]) == (str(os.getuid()), str(os.getgid()))


@pytest.mark.parametrize("extension", [".yaml", ".yml"])
def test_yaml_values_reach_compose_and_gpu_recipe(tmp_path, monkeypatch, extension):
    """核对凭据原文、YAML 标量类型、路径及嵌套的模型与 GPU 实例列表。"""
    monkeypatch.setenv("BUILD_TOKEN", "must-not-be-expanded")
    password = '001 $$ ${BUILD_TOKEN} # 缓存: [x] "带引号"'
    path = tmp_path / ("deployment" + extension)
    path.write_text(
        """# 自定义配置可加入整行和行末注释。
single_host: false
manual_retry_limit: 0
mysql:
  user: studio
  password: '00123'
redis:
  user: cache
  password: '001 $$ ${BUILD_TOKEN} # 缓存: [x] "带引号"' # 留在引号外的说明
ports: {api: 8031, admin: 8032}
paths:
  data: 'data #1'
public:
  origin: 'http://localhost:8031'
  admin_origin: null
  trusted_proxy_cidrs:
    - 192.0.2.10
    - '2001:db8::/48'
engine:
  comfyui: {url: 'https://example.invalid/ComfyUI.git', version: '1.0'}
  pytorch: {torch: '2.11.0+cu130'}
  nodes: {}
  models:
    demo:
      path: 'vae/a #1.safetensors'
  instances:
    - id: left
      gpu: '0'
      port: 8188
    - id: right
      gpu: '1'
      port: 8189
""",
        encoding="utf-8",
    )
    values, recipe, single_host = read_config(path, tmp_path, core_only=False)
    assert single_host is False
    assert values["MYSQL_PASSWORD"] == make_url(values["MYCOMFY_DATABASE_URL"]).password == "00123"
    assert values["REDIS_PASSWORD"] == password
    with Redis.from_url(values["MYCOMFY_REDIS_URL"]) as cache:
        assert cache.connection_pool.connection_kwargs["password"] == password
    assert values["MYCOMFY_MANUAL_RETRY_LIMIT"] == "0"
    assert values["MYCOMFY_API_PORT"] == "8031" and values["MYCOMFY_LOCAL_PORT"] == "8032"
    assert values["MYCOMFY_ADMIN_ORIGIN"] == ""
    assert values["MYCOMFY_TRUSTED_PROXY_CIDRS"] == "192.0.2.10/32,2001:db8::/48"
    assert values["MYCOMFY_DATA_ROOT"] == str(tmp_path / "data #1")
    assert recipe == {
        "comfyui": {"url": "https://example.invalid/ComfyUI.git", "version": "1.0"},
        "pytorch": {"torch": "2.11.0+cu130"},
        "nodes": {},
        "models": {"demo": {"path": "vae/a #1.safetensors"}},
        "instances": [
            {"id": "left", "gpu": "0", "port": 8188},
            {"id": "right", "gpu": "1", "port": 8189},
        ],
    }


def test_commented_yaml_preserves_published_defaults(tmp_path):
    """以发布过的 JSON 为基线，核对整份模板、Compose 参数和原样送往 GPU 的配方。"""
    template = REPO / "deploy.config.example.yaml"
    load_document = runpy.run_path(str(REPO / "scripts/config_document.py"))["load_document"]
    data = load_document(template)
    # c852c8cf562d 的 deploy.config.example.json，经下式排序及压缩后取得 SHA-256。
    canonical = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode()).hexdigest() == (
        "31dab648e3ca97d66eb759dbb7332aebe4a1a787431a56d5ac02e9c41e32493d"
    )
    assert data["manual_retry_limit"] == 3
    # 在同一个目录读两种格式，使相对挂载路径可直接比较。
    yaml_path, json_path = tmp_path / "settings.yaml", tmp_path / "old-settings.json"
    yaml_path.write_bytes(template.read_bytes())
    json_path.write_text(canonical)
    result = read_config(yaml_path, tmp_path, core_only=False)
    assert result == read_config(json_path, tmp_path, core_only=False)
    assert result[1] == data["engine"]


def test_host_yaml_needs_no_installed_packages(tmp_path):
    """关闭 Python 的 site、用户环境和当前目录导入后，宿主仍可读取仓库携带的 YAML。"""
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            """import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from config_document import load_document
assert load_document(Path(sys.argv[2]))['engine']['instances'][0]['gpu'] == '0'
""",
            str(REPO / "scripts"),
            str(REPO / "deploy.config.example.yaml"),
        ],
        cwd=tmp_path,
        check=True,
    )


@pytest.mark.parametrize(
    "text",
    [
        "mysql:\n  password: private-test-credential\n  password: duplicate\n",
        "mysql:\n  password: !unknown private-test-credential\n",
        "mysql:\n  password: private-test-credential\n---\nsecond: document\n",
        "mysql:\n  password: [private-test-credential\n",
    ],
)
def test_yaml_parse_errors_hide_credentials(tmp_path, text):
    """重复键、未知标签、多文档和语法错误均须在配置解析阶段失败且不回显行内凭据。"""
    path = tmp_path / "broken.yaml"
    path.write_text(text)
    load_document = runpy.run_path(str(REPO / "scripts/config_document.py"))["load_document"]
    with pytest.raises(ValueError) as error:
        load_document(path)
    assert "private-test-credential" not in str(error.value)


@pytest.mark.parametrize(
    ("extra", "field"),
    [("single_host: 'false'", "single_host"), ("ports: {api: true}", r"ports\.api")],
)
def test_yaml_schema_rejects_wrong_scalar_types(tmp_path, extra, field):
    """YAML 语法虽正确，带引号的布尔值和作为端口的布尔值仍应明确报字段类型错误。"""
    path = tmp_path / "wrong.yaml"
    path.write_text(
        "mysql: {user: db, password: '0123456'}\nredis: {user: cache, password: '0123456'}\n"
        + extra
    )
    with pytest.raises(ValueError, match=field):
        read_config(path, tmp_path, core_only=True)


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
    assert values["MYCOMFY_ADMIN_ORIGIN"] == ""
    assert values["MYCOMFY_TRUSTED_PROXY_CIDRS"] == ""
    with pytest.raises(ValueError, match="engine"):
        read_config(path, tmp_path, core_only=False)
    data["mysql"]["password"] = "credential'not-for-logs"
    path.write_text(json.dumps(data))
    values, _, _ = read_config(path, tmp_path, core_only=True)
    assert make_url(values["MYCOMFY_DATABASE_URL"]).password == data["mysql"]["password"]
    with pytest.raises(ValueError, match=r"mysql\.password") as error:
        read_config(path, tmp_path, core_only=True, database_init=True)
    assert data["mysql"]["password"] not in str(error.value)


def test_old_json_admin_origin_is_mapped_only_when_new_key_is_absent(tmp_path):
    """读取旧 JSON 时映射旧键，显式配置的新值（包括关闭）始终优先。"""
    config = {
        "mysql": {"user": "db", "password": "example"},
        "redis": {"user": "cache", "password": "example"},
    }
    public = {"origin": "https://studio.example", "admin_test_origin": "https://old-admin.example"}
    config["public"] = public
    path = tmp_path / "deploy.json"
    for overrides, expected in (
        ({}, "https://old-admin.example"),
        ({"admin_origin": "https://admin.example"}, "https://admin.example"),
        ({"admin_origin": None}, ""),
    ):
        config["public"] = {**public, **overrides}
        path.write_text(json.dumps(config))
        values, _, _ = read_config(path, tmp_path, core_only=True)
        assert values["MYCOMFY_ADMIN_ORIGIN"] == expected
        assert "MYCOMFY_ADMIN_TEST_ORIGIN" not in values


@pytest.mark.parametrize(
    ("admin", "expected"),
    [
        (None, None),
        ("", None),
        ("HTTPS://FRONT.example:8443/", "https://front.example:8443"),
        ("https://127.0.0.1", "https://127.0.0.1"),
        ("https://[2001:0DB8::0001]:443/", "https://[2001:db8::1]"),
    ],
)
def test_admin_origin_from_environment(monkeypatch, admin, expected):
    """直接启动服务也规范化 HTTPS 来源，缺省或空变量均关闭公网后台。"""
    monkeypatch.setenv("MYCOMFY_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("MYCOMFY_PUBLIC_ORIGIN", "https://front.example")
    if admin is None:
        monkeypatch.delenv("MYCOMFY_ADMIN_ORIGIN", raising=False)
    else:
        monkeypatch.setenv("MYCOMFY_ADMIN_ORIGIN", admin)
    assert Settings.from_env().admin_origin == expected


@pytest.mark.parametrize(
    ("public", "admin", "field"),
    [
        ("https://front.example", value, r"public\.admin_origin")
        for value in (
            False,
            "*",
            "admin.example",
            "http://admin.example",
            "https://*.example",
            "https://u@admin.example",
            "https://admin.example?",
            "https://admin.example#",
            "https://admin.example/path",
            "https://admin.example//",
            "https://admin.example:",
            "https://admin.example:0",
            "https://admin.example:65536",
            "https://bad_host",
            "https://[face]suffix",
            "https://127.1",
            "https://0x7f000001",
            "https://admin.example\n",
            "HTTPS://FRONT.EXAMPLE:443/",
        )
    ]
    + [
        ("http://front.example", "https://admin.example", r"public\.origin"),
        ("https://front.example//", "https://admin.example", r"public\.origin"),
    ],
)
def test_invalid_test_origins_fail_before_deployment_and_startup(tmp_path, public, admin, field):
    """两种部署入口都拒绝歧义、通配、非 HTTPS 或合并为同一来源的配置。"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "mysql": {"user": "app", "password": "p"},
                "redis": {"user": "default", "password": "p"},
                "public": {"origin": public, "admin_origin": admin},
            }
        )
    )
    with pytest.raises(ValueError, match=field):
        read_config(path, tmp_path, core_only=True)
    with pytest.raises(ValueError, match=field):
        Settings("sqlite://", tmp_path, public_origin=public, admin_origin=admin)


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


@pytest.mark.parametrize("limit", [0, 1, 5])
def test_manual_retry_limit_reaches_core(tmp_path, monkeypatch, limit):
    """验证部署 YAML 的手动重试次数经环境变量进入业务端，包括关闭重试。"""
    path = tmp_path / "deploy.yaml"
    path.write_text(
        f"manual_retry_limit: {limit}\nmysql: {{user: db, password: example}}\n"
        "redis: {user: cache, password: example}\n"
    )
    variables, _, _ = read_config(path, tmp_path, core_only=True)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    assert Settings.from_env().manual_retry_limit == limit


@pytest.mark.parametrize("limit", [-1, True, "3", 1.5])
def test_invalid_manual_retry_limit(tmp_path, limit):
    """错误配置在部署阶段就给出具体参数名。"""
    path = tmp_path / "deploy.json"
    path.write_text(json.dumps({"manual_retry_limit": limit}))
    with pytest.raises(ValueError, match="manual_retry_limit"):
        read_config(path, tmp_path, core_only=True)
