"""从部署脚本入口验证角色选择、单机启动顺序和数据服务失败时的行为。"""

import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture
def checkout(tmp_path):
    """复制部署入口和真实模板，隔离配置发现、首次初始化和生成的数据目录。"""
    source = Path(__file__).resolve().parents[2]
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("compose.yaml", "deploy.config.example.yaml"):
        shutil.copy(source / name, repo / name)
    shutil.copytree(
        source / "scripts", repo / "scripts", ignore=shutil.ignore_patterns("__pycache__")
    )
    for resource in (
        Path("src/mycomfy/origins.py"),
        Path("src/mycomfy/engine/model_objects.py"),
        Path("examples/deployment/h3-ref2va.models.yaml"),
    ):
        (repo / resource.parent).mkdir(parents=True)
        shutil.copy(source / resource, repo / resource)
    return repo


@pytest.fixture
def example_config(checkout):
    """使用检出目录随附的解析器读取示例，保留 GPU 配方和各项配置的原始类型。"""
    load_document = runpy.run_path(str(checkout / "scripts/config_document.py"))["load_document"]
    return load_document(checkout / "deploy.config.example.yaml")


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
        "if 'run' in args and 'deploy' in args:\n"
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
def launch(checkout, run_deploy, example_config):
    """根据真实模板生成个人 YAML，用同一份业务配置测试各种部署角色。"""

    def invoke(*options, single_host=None, db_failure=False):
        """以显式配置启动，既可提供 single_host 值，也可验证其默认行为。"""
        config = deepcopy(example_config)
        config["mysql"]["password"] = "test-mysql"
        config["redis"]["password"] = "test-redis"
        config.pop("single_host", None)
        if single_host is not None:
            config["single_host"] = single_host
        path = checkout / "deploy.config.yaml"
        path.write_text(json.dumps(config))
        return run_deploy("--config", str(path), *options, db_failure=db_failure)

    return invoke


@pytest.mark.parametrize(
    ("options", "setting"),
    [(("--single-host",), None), ((), True), ((), None), ((), False)],
)
def test_full_deployment_order(launch, options, setting):
    """显式或配置启用的单机部署等待数据库健康，省略或关闭该选项时使用外部数据服务。"""
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


def test_single_host_no_build_starts_from_existing_images(run_deploy, example_config, tmp_path):
    """复用自定义数据目录中的引擎，等待内置数据库健康后，用本机镜像启动应用。"""
    config_dir = tmp_path / "site"
    data = config_dir / "existing-data"
    instances = data / "engines" / "engines.json"
    instances.parent.mkdir(parents=True)
    existing = '{"instances": []}'
    instances.write_text(existing)
    example_config["single_host"] = False
    # 已安装的 GPU 实例来自持久清单，启动时只需用镜像选择项定位本地标签。
    example_config["engine"] = {
        "ref2va_in_image": True,
        "sageattention": {"wheel": "wheels/moved.whl", "sha256": "0" * 64},
    }
    example_config["paths"]["data"] = "existing-data"
    example_config["ports"].update(api=18000, admin=18001)
    config = config_dir / "local.yaml"
    config.write_text(json.dumps(example_config))
    result, calls = run_deploy("--config", str(config), "--single-host", "--no-build")
    assert result.returncode == 0, result.stderr

    # 数据层先就绪；后续 Compose 依赖包含 migrate，更新过的本地镜像允许替换旧容器。
    services = {"mysql", "redis", "migrate", "core", "gateway", "engine", "worker"}
    starts = [call for call in calls if "up" in call]
    assert [services.intersection(call) for call in starts] == [
        {"mysql", "redis"},
        {"core", "gateway", "engine", "worker"},
    ]
    for call in starts:
        assert {"--no-build", "-d", "--wait"} <= set(call)
        assert call[call.index("--pull") + 1] == "never"
        assert call[call.index("--profile") + 1] == "database"
        assert "--no-recreate" not in call
    assert "--no-deps" not in starts[1]
    assert not any({"build", "pull", "run"}.intersection(call) for call in calls)
    assert instances.read_text() == existing
    assert "工作台地址 http://127.0.0.1:18000" in result.stdout
    assert "管理后台地址 http://127.0.0.1:18001" in result.stdout


def test_single_host_no_build_requires_installed_engine(launch):
    """缺少已安装的引擎清单时指出缺失文件，保持所有服务尚未启动。"""
    result, calls = launch("--single-host", "--no-build")
    assert result.returncode != 0
    assert "engines.json" in result.stderr
    assert not any({"up", "build", "pull", "run"}.intersection(call) for call in calls)


@pytest.mark.parametrize(
    "option",
    [
        {"version": "1.0.6"},
        {"wheel": "wheels/sageattention-2.2.0-cp312-cp312-linux_x86_64.whl", "sha256": "1" * 64},
    ],
)
def test_gpu_deployment_forwards_optional_sageattention(
    checkout, run_deploy, example_config, tmp_path, option
):
    """GPU 部署将附加依赖随配方提交给安装器，安装器在目标容器中解析与验证。"""
    example_config["engine"]["sageattention"] = option
    (checkout / "deploy.config.yaml").write_text(json.dumps(example_config))
    result, _ = run_deploy("--gpu-only")
    assert result.returncode == 0, result.stderr
    submitted = json.loads((tmp_path / "submitted-recipe.json").read_text())
    assert submitted["sageattention"] == option


@pytest.mark.parametrize("action", ["--stop", "--test"])
def test_no_build_rejects_non_start_actions(run_deploy, action):
    """停机和临时隧道都不启动镜像，参数冲突时不调用 Docker。"""
    result, calls = run_deploy("--no-build", action)
    assert result.returncode == 2 and "--no-build" in result.stderr
    assert not calls


@pytest.mark.parametrize(
    "action", ["--no-build", "--core-only", "--db-only", "--test", "--stop", "--compose"]
)
def test_refresh_weights_requires_engine_install(run_deploy, action):
    """复用镜像、只操作业务服务或数据库时拒绝强制刷新，且不接触 Docker。"""
    result, calls = run_deploy("--refresh-weights", action)
    assert result.returncode == 2 and "--refresh-weights" in result.stderr
    assert not calls


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
def test_gateway_deployment_rejects_port_and_mount_conflicts(
    checkout, run_deploy, example_config, change
):
    """单机部署在启动任何服务之前报告监听冲突或会把维护凭据暴露给网关的挂载。"""
    config = example_config
    for key, value in change.items():
        config[key].update(value)
    (checkout / "deploy.config.yaml").write_text(json.dumps(config))
    result, calls = run_deploy("--single-host")
    assert result.returncode != 0
    assert all("config" in call for call in calls)
    assert not (checkout / "var").exists()


@pytest.mark.parametrize("options", [(), ("--no-build",)])
def test_database_failure_blocks_application(launch, checkout, options):
    """Compose 无法等到数据服务健康时，返回失败状态并跳过后续引擎安装和业务启动。"""
    if options:
        instances = checkout / "var/data/engines/engines.json"
        instances.parent.mkdir(parents=True)
        instances.write_text('{"instances": []}')
    result, calls = launch("--single-host", *options, db_failure=True)
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
    """引擎已经退出时，重复停机仍需核对首次记录的同一代次，再关闭余下服务。"""
    run_dir = checkout / "var/data/engines/instances/gpu0/g7-fixture"
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("TEST_STOP_PROBE", str(run_dir))
    monkeypatch.setenv("TEST_RECEIPT", "missing")
    # 同一项目下一次调用不能从已经退出的 engine 重新取快照。
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
    assert any(
        call[-8:]
        == ["exec", "-T", "engine", "python", "-m", "mycomfy.cli", "runtime", "stop-snapshot"]
        for call in calls
    )


def test_stop_gpu_skips_bootstrap(checkout, run_deploy):
    """GPU 停机只操作已部署容器，无需为关机初始化配置或安装目录。"""
    result, _ = run_deploy("--gpu-only", "--stop")
    assert result.returncode == 0, result.stderr
    assert not (checkout / "var").exists()


def test_conflicting_cli_roles_do_not_start_services(launch):
    """两个互斥的启动选项必须在调用 Docker 前报告用法错误。"""
    result, calls = launch("--single-host", "--gpu-only")
    assert result.returncode == 2
    assert not calls


def test_fresh_checkout_uses_h3_template(checkout, tmp_path, run_deploy, example_config):
    """干净检出可解析默认账号配置，单机部署以模板中的 H3 参数调用引擎。"""
    template_path = checkout / "deploy.config.example.yaml"
    original = template_path.read_bytes()
    template = example_config
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
    install = next(call for call in calls if "run" in call and "engine" in call)
    assert install[install.index("engine") :] == [
        "engine",
        "deploy",
        "-",
        "--base",
        "/deployment",
        "--root",
        str(checkout / "var/data/engines"),
    ]
    recipe = json.loads((tmp_path / "submitted-recipe.json").read_text())
    assert template["engine"].pop("ref2va_in_image") is False
    assert recipe == template["engine"] and "h3-fl2va" in recipe["models"]
    assert services["engine"]["build"]["target"] == "engine" and "h3-ref2va" not in recipe["models"]
    assert template_path.read_bytes() == original
    assert not any(
        (checkout / name).exists()
        for name in (".env", "deploy.config.yaml", "deploy.config.yml", "deploy.config.json")
    )


@pytest.mark.parametrize("suffix", ["yaml", "yml"])
def test_missing_explicit_yaml_selects_template(checkout, tmp_path, run_deploy, suffix):
    """常规部署的显式 YAML 路径缺失时提示并回退示例，即使仓库有个人配置。"""
    path = tmp_path / f"missing.{suffix}"
    write_personal(checkout / "deploy.config.yaml", "repo-password")
    result, _ = run_deploy(
        "--config", str(path), "--compose", "--profile", "database", "config", "--format", "json"
    )
    assert result.returncode == 0 and str(path) in result.stderr
    services = json.loads(result.stdout)["services"]
    assert services["mysql"]["environment"]["MYSQL_PASSWORD"] == "123456"
    assert services["redis"]["environment"]["REDISCLI_AUTH"] == "123456"


def test_missing_explicit_json_is_rejected(tmp_path, run_deploy):
    """显式非 YAML 路径即使不存在，也在触发模板回退之前报告格式错误。"""
    path = tmp_path / "missing.json"
    result, calls = run_deploy("--config", str(path), "--compose", "ps")
    assert result.returncode != 0 and ".yaml" in result.stderr and not calls


def write_personal(path, label):
    """写入可区分来源的账号和相对数据路径，保留原生 YAML 缩进和注释。"""
    path.write_text(
        "# 此配置中的相对路径以当前文件为基准\n"
        f"mysql:\n  user: db_user\n  password: '{label}'\n"
        f"redis:\n  user: cache_user\n  password: '{label}'\n"
        "paths:\n  data: objects\n"
    )


def compose_services(run_deploy, *options):
    """从 CLI 的标准输出读取实际 Compose 服务配置，以容器收到的参数判定配置来源。"""
    result, _ = run_deploy(
        *options, "--compose", "--profile", "database", "config", "--format", "json"
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["services"]


def test_ref2va_build_and_gpu_install_use_one_config(
    checkout, run_deploy, example_config, tmp_path
):
    """从相同配置渲染 Compose 和 GPU 安装清单，核对角色模型和原有首帧模型同时注册。"""
    # 使用带真实摘要的小文件贯通构建和安装入口，测试机不下载生产模型。
    preset_file = checkout / "examples/deployment/h3-ref2va.models.yaml"
    load_document = runpy.run_path(str(checkout / "scripts/config_document.py"))["load_document"]
    preset = load_document(preset_file)
    model = preset["engine"]["models"]["h3-ref2va"]
    source = checkout / "var/deployment/test.safetensors"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"small test weight")
    model.update(source=source.name, sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    preset_file.write_text(json.dumps(preset))
    example_config["engine"]["ref2va_in_image"] = True
    (checkout / "deploy.config.yaml").write_text(json.dumps(example_config))
    services = compose_services(run_deploy)
    assert services["engine"]["build"]["target"] == "engine-ref2va"
    assert services["engine"]["image"] == "mycomfy-engine-ref2va:local"
    assert services["worker"]["build"]["target"] == "worker"

    # 只构建业务镜像不应准备 GPU 权重。单独构建 engine 时先准备本地命名上下文。
    context = checkout / "var/data/engines/build-contexts/ref2va"
    for args in (("build", "core"), ("build", "worker"), ("build", "--print", "engine")):
        result, _ = run_deploy("--compose", *args)
        assert result.returncode == 0 and not context.exists(), result.stderr
    result, _ = run_deploy("--compose", "--profile", "database", "build", "engine")
    assert result.returncode == 0, result.stderr
    assert (context / model["sha256"]).read_bytes() == source.read_bytes()
    result, calls = run_deploy("--gpu-only", "--refresh-weights")
    assert result.returncode == 0, result.stderr
    install = next(call for call in calls if "deploy" in call)
    assert "--refresh-weights" in install
    assert install[install.index("--prepared-weight") + 1] == model["sha256"]
    # 删除本地来源，验证下次部署复用同摘要的缓存，无需再次打开来源文件。
    source.unlink()
    result, calls = run_deploy("--gpu-only")
    assert result.returncode == 0, result.stderr
    assert all("--refresh-weights" not in call for call in calls)
    recipe = json.loads((tmp_path / "submitted-recipe.json").read_text())
    assert "ref2va_in_image" not in recipe
    assert recipe["models"] == {**example_config["engine"]["models"], "h3-ref2va": model}


@pytest.mark.parametrize(
    ("role", "ports", "workbench_port"),
    [
        ("--single-host", {"api": 18000, "admin": 18001}, 18000),
        ("--core-only", {"admin": 18001}, 8000),
    ],
)
def test_deploy_prints_addresses_for_configured_ports(
    checkout, run_deploy, example_config, role, ports, workbench_port
):
    """从示例复制配置并只改端口，核对部署输出、网关映射和 Core 的工作台来源。"""
    example_config["ports"].update(ports)
    (checkout / "deploy.config.yaml").write_text(json.dumps(example_config))
    workbench = f"http://127.0.0.1:{workbench_port}"
    admin = f"http://127.0.0.1:{ports['admin']}"

    services = compose_services(run_deploy)
    published = {p["target"]: int(p["published"]) for p in services["gateway"]["ports"]}
    assert published == {8000: workbench_port, 8001: ports["admin"]}
    assert services["core"]["environment"]["MYCOMFY_PUBLIC_ORIGIN"] == workbench

    result, _ = run_deploy(role)
    assert result.returncode == 0, result.stderr
    assert f"工作台地址 {workbench}" in result.stdout.splitlines()
    assert f"管理后台地址 {admin}" in result.stdout.splitlines()


@pytest.mark.parametrize("role", ["--core-only", "--gpu-only"])
def test_deploy_prints_explicit_proxy_origins(checkout, run_deploy, example_config, role):
    """使用 HTTPS 代理时保留外部地址，GPU 机只显示远端工作台。"""
    workbench = "https://video.example.com"
    admin = "https://admin.example.com:8443"
    example_config["public"].update(origin=workbench, admin_origin=admin)
    (checkout / "deploy.config.yaml").write_text(json.dumps(example_config))
    services = compose_services(run_deploy)
    assert services["core"]["environment"]["MYCOMFY_PUBLIC_ORIGIN"] == workbench
    assert services["core"]["environment"]["MYCOMFY_ADMIN_ORIGIN"] == admin

    result, _ = run_deploy(role)
    assert result.returncode == 0, result.stderr
    assert f"工作台地址 {workbench}" in result.stdout.splitlines()
    if role == "--core-only":
        assert f"管理后台地址 {admin}" in result.stdout.splitlines()
    else:
        assert "管理后台地址" not in result.stdout


@pytest.mark.parametrize("suffixes", [("yaml", "yml"), ("yml",)])
def test_personal_yaml_precedes_template(checkout, run_deploy, suffixes):
    """仓库中的 YAML 优先于 YML，缺少两者时才读取内置示例。"""
    for suffix in suffixes:
        write_personal(checkout / f"deploy.config.{suffix}", f"source-{suffix}")
    services = compose_services(run_deploy)
    expected = f"source-{suffixes[0]}"
    assert services["mysql"]["environment"]["MYSQL_PASSWORD"] == expected
    assert services["redis"]["environment"]["REDISCLI_AUTH"] == expected
    assert services["core"]["environment"]["MYCOMFY_DATA_ROOT"] == str(checkout / "objects")


@pytest.mark.parametrize("suffix", ["yaml", "yml"])
def test_explicit_relative_config_resolves_from_caller(checkout, tmp_path, run_deploy, suffix):
    """从仓库外执行时，--config 以调用目录查找文件，配置中的数据路径以文件目录为基准。"""
    relative_path = Path("profiles") / f"personal.{suffix}"
    personal = tmp_path / relative_path
    personal.parent.mkdir()
    write_personal(personal, "selected-profile")
    write_personal(checkout / "deploy.config.yaml", "repo-default")
    services = compose_services(run_deploy, "--config", str(relative_path))
    assert services["mysql"]["environment"]["MYSQL_PASSWORD"] == "selected-profile"
    assert services["core"]["environment"]["MYCOMFY_DATA_ROOT"] == str(personal.parent / "objects")


def test_only_yaml_can_configure_deployment(checkout, run_deploy):
    """仓库即使残留 .env、JSON 配置或固定清单，也从 YAML 示例取账号和引擎配方。"""
    old_env = checkout / ".env"
    old_env.write_text("MYSQL_PASSWORD=discarded\nREDIS_PASSWORD=discarded\n")
    old_json = checkout / "deploy.config.json"
    old_json.write_text('{"mysql": {"password": "discarded"}}')
    manifest = checkout / "var/deployment/deployment.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")

    services = compose_services(run_deploy)
    assert services["mysql"]["environment"]["MYSQL_PASSWORD"] == "123456"
    assert services["redis"]["environment"]["REDISCLI_AUTH"] == "123456"
    result, calls = run_deploy("--single-host")
    assert result.returncode == 0, result.stderr
    assert any(
        "run" in call and call[call.index("engine") + 1 :][:2] == ["deploy", "-"] for call in calls
    )
    assert old_env.is_file() and old_json.is_file() and manifest.read_text() == "{}"

    # 即使是无需读取凭据的停机操作，也只接受本版的配置文件格式。
    result, calls = run_deploy("--config", str(old_json), "--stop")
    assert result.returncode != 0 and ".yaml" in result.stderr and not calls


@pytest.mark.parametrize("explicit", [False, True])
def test_invalid_existing_config_is_reported(checkout, tmp_path, run_deploy, explicit):
    """选中的 YAML 解析失败时停止，避免静默改用其他配置部署。"""
    path = tmp_path / "custom.yaml" if explicit else checkout / "deploy.config.yaml"
    path.write_text("mysql: [")
    options = ["--config", str(path)] if explicit else []
    result, calls = run_deploy(*options, "--single-host")
    assert result.returncode != 0 and result.stderr and not calls
