"""从部署脚本入口验证角色选择、单机启动顺序和数据服务失败时的行为。"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def checkout(tmp_path):
    """复制部署入口和真实模板，隔离配置发现、首次初始化和生成的数据目录。"""
    source = Path(__file__).resolve().parents[2]
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("compose.yaml", "deploy.config.example.json", ".env.example"):
        shutil.copy(source / name, repo / name)
    shutil.copytree(
        source / "scripts", repo / "scripts", ignore=shutil.ignore_patterns("__pycache__")
    )
    module = Path("src/mycomfy/origins.py")
    (repo / module.parent).mkdir(parents=True)
    shutil.copy(source / module, repo / module)
    return repo


@pytest.fixture
def run_deploy(checkout, tmp_path, monkeypatch):
    """使用真实 Compose 解析配置，记录构建和启动命令，避免依赖 GPU 或修改运行中的服务。"""
    real_docker = shutil.which("docker")
    assert real_docker, "配置解析需要 Docker Compose CLI"
    monkeypatch.setenv("TEST_DOCKER", real_docker)
    log = tmp_path / "docker.jsonl"
    monkeypatch.setenv("TEST_DOCKER_LOG", str(log))
    for name in list(os.environ):
        if name.startswith(("COMPOSE_", "MYSQL_", "REDIS_", "MYCOMFY_")):
            monkeypatch.delenv(name)

    recorder = tmp_path / "docker"
    recorder.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['TEST_DOCKER_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(args) + '\\n')\n"
        "if 'config' in args:\n"
        "    os.execv(os.environ['TEST_DOCKER'], [os.environ['TEST_DOCKER'], *args])\n"
        "if '--recipe' in args:\n"
        "    with open(os.environ['TEST_RECIPE'], 'wb') as output:\n"
        "        output.write(sys.stdin.buffer.read())\n"
        "if 'up' in args and 'mysql' in args and os.environ.get('TEST_DB_FAILURE'):\n"
        "    sys.exit(41)\n"
        "if 'stop' in args and os.environ.get('TEST_STOP_FAILURE') in args:\n"
        "    sys.exit(42)\n"
        "if os.getenv('TEST_STOP_PROBE'):\n"
        "    folder = Path(os.environ['TEST_STOP_PROBE'])\n"
        "    if 'ps' in args and '--format' in args:\n"
        "        service = args[-1]\n"
        "        calls = Path(os.environ['TEST_DOCKER_LOG']).read_text().splitlines()\n"
        "        stopped = any('stop' in call and service in call for call in map(json.loads, calls))\n"
        "        stopped = stopped or (service == 'engine' and (folder / '.container-exited').exists())\n"
        "        code = 137 if stopped and os.getenv('TEST_BAD_EXIT') == service else 0\n"
        "        row = {'Service': service, 'State': 'exited' if stopped else 'running', 'ExitCode': code}\n"
        "        print(json.dumps([row] if service == 'worker' else row))\n"
        "    if 'exec' in args and 'engine' in args:\n"
        "        if os.getenv('TEST_SNAPSHOT_FAILURE'):\n"
        "            sys.exit(43)\n"
        "        print(json.dumps([{'generation': 7, 'run_dir': str(folder)}]))\n"
        "    if 'stop' in args and 'engine' in args:\n"
        "        (folder / '.container-exited').touch()\n"
        "        receipt = os.getenv('TEST_RECEIPT', 'correct')\n"
        "        if receipt != 'missing':\n"
        "            (folder / 'ended.json').write_text(json.dumps({'generation': 7 if receipt == 'correct' else 6}))\n"
    )
    recorder.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("TEST_RECIPE", str(tmp_path / "submitted-recipe.json"))

    def invoke(*options, db_failure=False):
        """返回真实 CLI 的退出状态和本次调用的全部 Docker 命令。"""
        log.unlink(missing_ok=True)
        result = subprocess.run(
            [sys.executable, str(checkout / "scripts/deploy.py"), *options],
            cwd=tmp_path,
            env={**os.environ, "TEST_DB_FAILURE": "1" if db_failure else ""},
            capture_output=True,
            text=True,
            check=False,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls

    return invoke


@pytest.fixture
def launch(checkout, run_deploy):
    """构造指定单机标志的个人配置，保留其他参数和真实模板一致。"""

    def invoke(*options, single_host=None, db_failure=False):
        """显式传给部署命令，或省略 single_host 模拟旧版个人配置。"""
        config = json.loads((checkout / "deploy.config.example.json").read_text())
        config["mysql"]["password"] = "test-mysql"
        config["redis"]["password"] = "test-redis"
        config.pop("single_host", None)
        if single_host is not None:
            config["single_host"] = single_host
        path = checkout / "deploy.config.json"
        path.write_text(json.dumps(config))
        return run_deploy("--config", str(path), *options, db_failure=db_failure)

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
    assert install < app and {"core", "gateway", "engine", "worker"} <= set(calls[app])
    db = [i for i, call in enumerate(calls) if "up" in call and {"mysql", "redis"} <= set(call)]
    if options or setting:
        assert len(db) == 1 and db[0] < install
        assert "--wait" in calls[db[0]]
        assert not {"core", "gateway", "engine", "worker", "migrate"}.intersection(calls[db[0]])
    else:
        assert not db


@pytest.mark.parametrize(
    ("options", "targets", "install"),
    [
        (("--db-only",), {"mysql", "redis"}, False),
        (("--core-only",), {"core", "gateway"}, False),
        (("--gpu-only",), {"engine", "worker"}, True),
        (("--compose", "ps"), set(), False),
    ],
)
def test_explicit_role_overrides_config(launch, checkout, options, targets, install):
    """已有的独立部署和运维命令优先，单机默认值不会让它们额外启动组件。"""
    result, calls = launch(*options, single_host=True)
    assert result.returncode == 0, result.stderr
    up = [call for call in calls if "up" in call]
    services = {"mysql", "redis", "gateway", "core", "migrate", "engine", "worker"}
    assert [services.intersection(call) for call in up] == ([targets] if targets else [])
    assert (checkout / "var/gateway").is_dir() == ("gateway" in targets)
    assert any("run" in call and "engine" in call for call in calls) is install
    if "--compose" in options:
        assert calls[-1][-1] == "ps"


@pytest.mark.parametrize(
    "change",
    [{"paths": {"gateway": "var/config"}}, {"ports": {"api": 8001}}, {"ports": {"redis": 8000}}],
)
def test_gateway_deployment_rejects_port_and_mount_conflicts(checkout, run_deploy, change):
    """单机部署在启动任何服务之前报告监听冲突或会把维护凭据暴露给网关的挂载。"""
    config = json.loads((checkout / "deploy.config.example.json").read_text())
    for key, value in change.items():
        config[key].update(value)
    (checkout / "deploy.config.json").write_text(json.dumps(config))
    result, calls = run_deploy("--single-host")
    assert result.returncode != 0
    assert all("config" in call for call in calls)
    assert not (checkout / "var").exists()


def test_database_failure_blocks_application(launch):
    """Compose 无法等到数据服务健康时，返回失败状态并跳过后续引擎安装和业务启动。"""
    result, calls = launch("--single-host", db_failure=True)
    assert result.returncode == 41
    up = [call for call in calls if "up" in call]
    assert len(up) == 1 and {"--wait", "mysql", "redis"} <= set(up[0])
    assert not any("run" in call for call in calls)


@pytest.mark.parametrize(
    ("roles", "single_host", "expected"),
    [
        ((), False, [{"worker"}, {"engine"}, {"core"}, {"gateway"}, {"mysql", "redis"}]),
        ((), True, [{"worker"}, {"engine"}, {"core"}, {"gateway"}, {"mysql", "redis"}]),
        (
            ("--single-host",),
            False,
            [{"worker"}, {"engine"}, {"core"}, {"gateway"}, {"mysql", "redis"}],
        ),
        (("--gpu-only",), True, [{"worker"}, {"engine"}]),
        (("--core-only",), True, [{"core"}, {"gateway"}]),
        (("--db-only",), True, [{"mysql", "redis"}]),
    ],
)
def test_stop_limits_shutdown_to_selected_role(launch, checkout, roles, single_host, expected):
    """各角色仅停指定服务，全栈先等待 Worker 退出，再停引擎、业务和本机数据服务。"""
    result, calls = launch(*roles, "--stop", single_host=single_host)
    assert result.returncode == 0, result.stderr
    assert not any(
        set(call) & {"up", "build", "run", "start", "restart", "down", "kill", "rm"}
        for call in calls
    )
    # 依赖仍运行时等待上一组退出。单机启动参数可能只在当时的命令行中指定。
    stops = [call for call in calls if "stop" in call]
    assert [set(call[call.index("stop") + 1 :]) for call in stops] == [
        {"--timeout", "-1", *targets} for targets in expected
    ]
    if "mysql" in expected[-1]:
        assert (
            "--profile" in stops[-1] and stops[-1][stops[-1].index("--profile") + 1] == "database"
        )
    assert not (checkout / "var").exists()


@pytest.mark.parametrize(
    ("failing", "remaining"),
    [
        ("worker", {"engine", "core", "gateway", "mysql", "redis"}),
        ("engine", {"core", "mysql", "redis"}),
    ],
)
def test_stop_failure_preserves_remaining_services(launch, monkeypatch, failing, remaining):
    """停止某个服务失败应返回原退出码，保留其后续对账所需的服务。"""
    monkeypatch.setenv("TEST_STOP_FAILURE", failing)
    result, calls = launch("--stop", "--single-host")
    assert result.returncode == 42
    failed = next(i for i, call in enumerate(calls) if "stop" in call and failing in call)
    assert not any("stop" in call for call in calls[failed + 1 :])
    assert not any("stop" in call and remaining.intersection(call) for call in calls)


@pytest.mark.parametrize(
    ("bad_exit", "receipt", "expect_success"),
    [
        ("", "correct", True),
        ("worker", "correct", False),
        ("engine", "correct", False),
        ("", "missing", False),
        ("", "wrong_generation", False),
    ],
)
def test_stop_verifies_container_exit_and_generation_receipt(
    launch, checkout, monkeypatch, bad_exit, receipt, expect_success
):
    """容器退出码和实际代次凭据均正常才继续停机，保留尚待核对的业务与数据库。"""
    # 模拟容器只在收到 stop 后落盘凭据，过时代次和没有凭据均不能证明本代已退出。
    run_dir = checkout / "var/data/engines/instances/gpu0/g7-fixture"
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("TEST_STOP_PROBE", str(run_dir))
    monkeypatch.setenv("TEST_BAD_EXIT", bad_exit)
    monkeypatch.setenv("TEST_RECEIPT", receipt)
    result, calls = launch("--stop", "--single-host")
    assert (result.returncode == 0) is expect_success, result.stderr
    stops = [call for call in calls if "stop" in call]
    snapshot = [i for i, call in enumerate(calls) if "exec" in call and "engine" in call]
    if bad_exit == "worker":
        assert all("engine" not in call for call in stops) and not snapshot
    else:
        assert len(snapshot) == 1
        assert snapshot[0] < next(
            i for i, call in enumerate(calls) if "stop" in call and "engine" in call
        )
    if not expect_success:
        assert not any({"core", "gateway", "mysql", "redis"}.intersection(call) for call in stops)


def test_stop_retry_rechecks_exited_engine_until_receipt_is_restored(
    checkout, run_deploy, monkeypatch
):
    """旧引擎已经退出时，重复停机仍需核对首次记录的同一代次，再关闭余下服务。"""
    run_dir = checkout / "var/data/engines/instances/gpu0/g7-legacy"
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("TEST_STOP_PROBE", str(run_dir))
    monkeypatch.setenv("TEST_RECEIPT", "missing")
    # 沿用同一旧部署和 Compose 项目，下一次调用不能从已经退出的 engine 重新取快照。
    (checkout / ".env").write_text("MYSQL_PASSWORD=existing\nREDIS_PASSWORD=existing\n")
    first, first_calls = run_deploy("--stop")
    assert first.returncode != 0
    assert any("exec" in call and "engine" in call for call in first_calls)
    assert (run_dir / ".container-exited").exists()

    second, second_calls = run_deploy("--stop")
    assert second.returncode != 0
    assert not any("exec" in call and "engine" in call for call in second_calls)
    for call in first_calls + second_calls:
        assert not ("stop" in call and {"core", "gateway", "mysql", "redis"}.intersection(call))

    # 模拟运维核实原代次后恢复退出凭据，只有此时重试才应完成后续停机。
    (run_dir / "ended.json").write_text(json.dumps({"generation": 7}))
    restored, calls = run_deploy("--stop")
    assert restored.returncode == 0, restored.stderr
    assert not any("exec" in call and "engine" in call for call in calls)
    stops = [call for call in calls if "stop" in call]
    assert [
        set(call).intersection({"core", "gateway", "mysql", "redis"}) for call in stops[-3:]
    ] == [
        {"core"},
        {"gateway"},
        {"mysql", "redis"},
    ]


def test_stop_rejects_conflicting_actions(run_deploy):
    """停机不能和原样转发的 Compose 操作混用。"""
    result, calls = run_deploy("--stop", "--compose", "ps")
    assert result.returncode == 2 and not calls


def test_unresolved_engine_snapshot_preserves_running_engine_and_database(
    launch, tmp_path, monkeypatch
):
    """引擎内的待核实执行拒绝停机时，只允许之前的 Worker 排空步骤完成。"""
    monkeypatch.setenv("TEST_STOP_PROBE", str(tmp_path))
    monkeypatch.setenv("TEST_SNAPSHOT_FAILURE", "1")
    result, calls = launch("--stop")
    assert result.returncode == 43
    stops = [call for call in calls if "stop" in call]
    assert len(stops) == 1 and stops[0][-1] == "worker"
    assert any(call[-5:] == ["exec", "-T", "engine", "python", "-"] for call in calls)


def test_stop_gpu_preserves_legacy_files_without_bootstrap(checkout, run_deploy):
    """只有预制引擎清单的旧部署可以停机，不生成新账号或维护凭据。"""
    manifest = checkout / "var/deployment/deployment.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    result, _ = run_deploy("--gpu-only", "--stop")
    assert result.returncode == 0, result.stderr
    assert not (checkout / ".env").exists() and not (checkout / "var/config").exists()
    assert manifest.read_text() == "{}"


def test_conflicting_cli_roles_do_not_start_services(launch):
    """两个互斥的启动选项必须在调用 Docker 前报告用法错误。"""
    result, calls = launch("--single-host", "--gpu-only")
    assert result.returncode == 2
    assert not calls


def test_fresh_checkout_uses_h3_template(checkout, tmp_path, run_deploy):
    """干净检出可解析默认账号配置，单机部署以模板中的 H3 参数调用引擎。"""
    template_path = checkout / "deploy.config.example.json"
    original = template_path.read_bytes()
    template = json.loads(original)
    assert template["mysql"]["password"] == template["redis"]["password"] == "123456"
    result, _ = run_deploy("--compose", "--profile", "database", "config", "--format", "json")
    assert result.returncode == 0, result.stderr
    assert "123456" not in result.stderr
    services = json.loads(result.stdout)["services"]
    assert services["mysql"]["environment"]["MYSQL_PASSWORD"] == "123456"
    assert services["redis"]["environment"]["REDISCLI_AUTH"] == "123456"

    # 捕获传入安装器的 stdin，验证真实参数选择，全程无需下载模型。
    result, calls = run_deploy("--single-host")
    assert result.returncode == 0, result.stderr
    assert any("run" in call and call[-2:] == ["--recipe", "-"] for call in calls)
    recipe = json.loads((tmp_path / "submitted-recipe.json").read_text())
    assert recipe == template["engine"] and "h3-fl2va" in recipe["models"]
    assert template_path.read_bytes() == original
    assert not (checkout / ".env").exists() and not (checkout / "deploy.config.json").exists()


def test_explicit_missing_config_uses_template(checkout, tmp_path, run_deploy):
    """显式路径无文件时回退到仓库模板，不创建用户指定的文件。"""
    missing = tmp_path / "personal.json"
    # 即使工作目录已有旧 .env，显式缺失的 JSON 也应走模板并保留旧文件。
    old_env = checkout / ".env"
    old_env.write_text("MYSQL_PASSWORD=old-mysql\nREDIS_PASSWORD=old-redis\n")
    result, _ = run_deploy(
        "--config", str(missing), "--compose", "--profile", "database", "config", "--format", "json"
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert services["mysql"]["environment"]["MYSQL_PASSWORD"] == "123456"
    assert services["redis"]["environment"]["REDISCLI_AUTH"] == "123456"
    assert not missing.exists()
    assert old_env.read_text() == "MYSQL_PASSWORD=old-mysql\nREDIS_PASSWORD=old-redis\n"


def test_existing_env_preserves_legacy_deployment(checkout, run_deploy):
    """旧部署的账号和安装清单继续生效，不会被模板的密码和 H3 配置替换。"""
    old_env = checkout / ".env"
    data_root = checkout / "old-data"
    saved = f"MYSQL_PASSWORD=old-mysql\nREDIS_PASSWORD=old-redis\nMYCOMFY_DATA_ROOT={data_root}\n"
    old_env.write_text(saved)
    manifest = checkout / "var/deployment/deployment.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")

    result, _ = run_deploy("--compose", "--profile", "database", "config", "--format", "json")
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert services["mysql"]["environment"]["MYSQL_PASSWORD"] == "old-mysql"
    assert services["redis"]["environment"]["REDISCLI_AUTH"] == "old-redis"
    result, calls = run_deploy("--single-host")
    assert result.returncode == 0, result.stderr
    install = next(call for call in calls if "run" in call and "engine" in call)
    assert "--recipe" not in install
    assert old_env.read_text() == saved and manifest.read_text() == "{}"


def test_existing_manifest_alone_selects_legacy(tmp_path, run_deploy, monkeypatch):
    """仅有预制清单的部署也沿用原安装入口，包括通过环境变量指定的目录。"""
    imports = tmp_path / "previous-imports"
    imports.mkdir()
    (imports / "deployment.json").write_text("{}")
    monkeypatch.setenv("MYCOMFY_DEPLOYMENT_DIR", str(imports))
    result, calls = run_deploy("--single-host")
    assert result.returncode == 0, result.stderr
    install = next(call for call in calls if "run" in call and "engine" in call)
    assert "--recipe" not in install


@pytest.mark.parametrize("explicit", [False, True])
def test_invalid_existing_config_is_reported(checkout, tmp_path, run_deploy, explicit):
    """已有 JSON 解析失败时报告错误，避免对错误配置静默应用默认参数。"""
    path = tmp_path / "custom.json" if explicit else checkout / "deploy.config.json"
    path.write_text("{broken")
    options = ["--config", str(path)] if explicit else []
    result, calls = run_deploy(*options, "--single-host")
    assert result.returncode != 0 and result.stderr
    assert not calls and path.read_text() == "{broken"
