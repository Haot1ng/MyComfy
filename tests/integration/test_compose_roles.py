"""通过 Compose 的真实配置解析器检查数据库和应用的启动边界。"""

import json
import subprocess
import sys
from pathlib import Path


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
