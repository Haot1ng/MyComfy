"""从部署脚本入口验证角色选择、单机启动顺序和数据服务失败时的行为。"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def launch(tmp_path, monkeypatch):
    """使用真实 Compose 解析配置，记录构建和启动命令，避免依赖 GPU 或修改运行中的服务。"""
    repo = Path(__file__).resolve().parents[2]
    real_docker = shutil.which("docker")
    assert real_docker, "配置解析需要 Docker Compose CLI"
    monkeypatch.setenv("TEST_DOCKER", real_docker)
    log = tmp_path / "docker.jsonl"
    monkeypatch.setenv("TEST_DOCKER_LOG", str(log))
    for name in ("COMPOSE_FILE", "COMPOSE_PROFILES"):
        monkeypatch.delenv(name, raising=False)

    recorder = tmp_path / "docker"
    recorder.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['TEST_DOCKER_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(args) + '\\n')\n"
        "if 'config' in args:\n"
        "    os.execv(os.environ['TEST_DOCKER'], [os.environ['TEST_DOCKER'], *args])\n"
        "if 'up' in args and 'mysql' in args and os.environ.get('TEST_DB_FAILURE'):\n"
        "    sys.exit(41)\n"
    )
    recorder.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    def invoke(*options, single_host=None, db_failure=False):
        """将测试配置写入独立目录，返回 CLI 的退出状态和本次调用的全部 Docker 参数。"""
        config = json.loads((repo / "deploy.config.example.json").read_text())
        config["mysql"]["password"] = "test-mysql"
        config["redis"]["password"] = "test-redis"
        config["paths"] = {name: str(tmp_path / name) for name in ("data", "config", "deployment")}
        # 可以验证升级前没有 single_host 字段的配置，即使模板已经带默认值。
        config.pop("single_host", None)
        if single_host is not None:
            config["single_host"] = single_host
        path = tmp_path / "deploy.config.json"
        path.write_text(json.dumps(config))
        log.unlink(missing_ok=True)
        result = subprocess.run(
            [sys.executable, str(repo / "scripts/deploy.py"), "--config", str(path), *options],
            cwd=tmp_path,
            env={**os.environ, "TEST_DB_FAILURE": "1" if db_failure else ""},
            capture_output=True,
            text=True,
            check=False,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls

    return invoke


@pytest.mark.parametrize(
    ("options", "setting"),
    [(("--single-host",), None), ((), True), ((), None), ((), False)],
)
def test_full_deployment_order(launch, options, setting):
    """显式或配置启用的单机部署等待数据库健康，旧配置和显式关闭沿用外部数据服务。"""
    result, calls = launch(*options, single_host=setting)
    assert result.returncode == 0, result.stderr
    # install 即引擎容器的安装命令，应用 up 时 Compose 根据依赖先执行迁移。
    install = next(i for i, call in enumerate(calls) if "run" in call and "engine" in call)
    app = next(i for i, call in enumerate(calls) if "up" in call and "core" in call)
    assert install < app and {"core", "engine", "worker"} <= set(calls[app])
    db = [i for i, call in enumerate(calls) if "up" in call and {"mysql", "redis"} <= set(call)]
    if options or setting:
        assert len(db) == 1 and db[0] < install
        assert "--wait" in calls[db[0]]
        assert not {"core", "engine", "worker", "migrate"}.intersection(calls[db[0]])
    else:
        assert not db


@pytest.mark.parametrize(
    ("options", "targets", "install"),
    [
        (("--db-only",), {"mysql", "redis"}, False),
        (("--core-only",), {"core"}, False),
        (("--gpu-only",), {"engine", "worker"}, True),
        (("--compose", "ps"), set(), False),
    ],
)
def test_explicit_role_overrides_config(launch, options, targets, install):
    """已有的独立部署和运维命令优先，单机默认值不会让它们额外启动组件。"""
    result, calls = launch(*options, single_host=True)
    assert result.returncode == 0, result.stderr
    up = [call for call in calls if "up" in call]
    services = {"mysql", "redis", "core", "migrate", "engine", "worker"}
    assert [services.intersection(call) for call in up] == ([targets] if targets else [])
    assert any("run" in call and "engine" in call for call in calls) is install
    if "--compose" in options:
        assert calls[-1][-1] == "ps"


def test_database_failure_blocks_application(launch):
    """Compose 无法等到数据服务健康时，返回失败状态并跳过后续引擎安装和业务启动。"""
    result, calls = launch("--single-host", db_failure=True)
    assert result.returncode == 41
    up = [call for call in calls if "up" in call]
    assert len(up) == 1 and {"--wait", "mysql", "redis"} <= set(up[0])
    assert not any("run" in call for call in calls)


def test_conflicting_cli_roles_do_not_start_services(launch):
    """两个互斥的启动选项必须在调用 Docker 前报告用法错误。"""
    result, calls = launch("--single-host", "--gpu-only")
    assert result.returncode == 2
    assert not calls
