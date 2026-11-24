"""验证独立测试配置的查找、设备身份及 Funnel 端口限制。"""

import runpy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

REPO = Path(__file__).resolve().parents[2]
module = runpy.run_path(str(REPO / "scripts/tunnel_settings.py"))
load_settings = module["load_tunnel_settings"]
remember_machine_name = module["remember_machine_name"]


def test_default_template_and_selection_order(tmp_path, monkeypatch):
    """首次使用仓库模板，此后优先个人文件，显式文件相对于调用者的当前目录。"""
    template = tmp_path / "deploy.test.example.yaml"
    template.write_bytes((REPO / template.name).read_bytes())
    settings = load_settings(tmp_path)
    assert (settings.machine_name, settings.public_https_port, settings.admin_https_port) == (
        "auto",
        443,
        8443,
    )
    assert settings.resolve_machine_name("Lab.Example.TS.NET.") == "lab.example.ts.net"
    personal = tmp_path / "deploy.test.yaml"
    personal.write_text("machine_name: Lab.Example.TS.NET.\nadmin_https_port: 10000\n")
    settings = load_settings(tmp_path)
    assert settings.machine_name == "lab.example.ts.net"
    assert settings.public_https_port == 443 and settings.admin_https_port == 10000
    assert settings.resolve_machine_name("lab.example.ts.net") == "lab.example.ts.net"
    with pytest.raises(ValueError, match="不一致"):
        settings.resolve_machine_name("other.example.ts.net")
    # 明确指定错误路径时不能悄悄回退到个人设置；从别处运行也能读取指定文件。
    other = tmp_path / "outside"
    other.mkdir()
    monkeypatch.chdir(other)
    with pytest.raises(ValueError, match="missing.yaml"):
        load_settings(tmp_path, Path("missing.yaml"))
    Path("other.yml").write_text("public_https_port: 10000\nadmin_https_port: 443\n")
    explicit = load_settings(tmp_path, Path("other.yml"))
    assert explicit.public_https_port == 10000 and explicit.admin_https_port == 443


def test_legacy_business_file_is_preserved(tmp_path):
    """识别旧同名业务配置时给出分开传参的指引，原凭据既不输出也不改写。"""
    personal = tmp_path / "deploy.test.yaml"
    original = b"public:\n  host: 127.0.0.1\nmysql:\n  password: fixture-credential\n"
    personal.write_bytes(original)
    with pytest.raises(ValueError, match="业务部署配置") as error:
        load_settings(tmp_path)
    assert "--config" in str(error.value) and "--test-config" in str(error.value)
    assert "fixture-credential" not in str(error.value)
    assert personal.read_bytes() == original


def test_remember_publishes_one_complete_personal_config(tmp_path):
    """两个首次会话竞争时只保存一个已核对名称，个人文件可直接读回且包含实际端口。"""
    template = tmp_path / "deploy.test.example.yaml"
    template.write_text("public_https_port: 10000\nadmin_https_port: 443\n")
    settings = load_settings(tmp_path)
    hosts = ["First.Example.TS.NET.", "Second.Example.TS.NET."]
    barrier = Barrier(2)

    def publish(host):
        """同时尝试创建首次成功会话的个人配置，记录实际赢得创建权的设备。"""
        barrier.wait(timeout=5)
        return remember_machine_name(tmp_path, None, settings, host)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(publish, hosts))
    assert sorted(results) == [False, True]
    saved = load_settings(tmp_path)
    assert saved.machine_name == hosts[results.index(True)].lower().removesuffix(".")
    assert (saved.public_https_port, saved.admin_https_port) == (10000, 443)
    assert "# 工作台" in (tmp_path / "deploy.test.yaml").read_text()
    # 清理临时文件后，目录只剩原模板和最终文件。
    assert {p.name for p in tmp_path.iterdir()} == {template.name, "deploy.test.yaml"}


def test_remember_skips_explicit_or_operator_owned_settings(tmp_path):
    """显式配置、既有文件、悬空符号链接和管理员删除的个人配置均受到保护。"""
    template = tmp_path / "deploy.test.example.yaml"
    template.write_text("machine_name: auto\n")
    settings = load_settings(tmp_path)
    name = "me.example.ts.net"
    personal = tmp_path / "deploy.test.yaml"
    assert not remember_machine_name(tmp_path, template, settings, name)
    assert not personal.exists() and template.read_text() == "machine_name: auto\n"
    personal.symlink_to(tmp_path / "missing")
    assert not remember_machine_name(tmp_path, None, settings, name)
    assert personal.is_symlink() and not (tmp_path / "missing").exists()
    personal.unlink()
    personal.write_text("# 我自己的配置\nmachine_name: auto\n")
    assert not remember_machine_name(tmp_path, None, settings, name)
    assert personal.read_text() == "# 我自己的配置\nmachine_name: auto\n"
    loaded_personal = load_settings(tmp_path)
    personal.unlink()
    assert not remember_machine_name(tmp_path, None, loaded_personal, name)
    assert not personal.exists()


@pytest.mark.parametrize(
    "content,field",
    [
        ("public_https_port: true", "public_https_port"),
        ('admin_https_port: "10000"', "admin_https_port"),
        ("public_https_port: 8444", "public_https_port"),
        ("admin_https_port: 443", "不同的端口"),
        ("machine_name: host.example.com", "machine_name"),
        ("machine_name: host..ts.net", "machine_name"),
        ("machine_name: ts.net", "machine_name"),
        ("machine_name: https://host.example.ts.net", "machine_name"),
        ("machine_name: host.example.ts.net..", "machine_name"),
        ("machine_name: null", "machine_name"),
        ("machine_name: -bad.example.ts.net", "machine_name"),
        ("admin_port: 8443", "仅支持"),
    ],
)
def test_bad_test_settings_report_field(tmp_path, content, field):
    """在启动前发现拼写、类型、重复端口及非法设备名，不接受含协议的 URL。"""
    path = tmp_path / "deploy.test.yaml"
    path.write_text(content)
    with pytest.raises(ValueError, match=field):
        load_settings(tmp_path)


@pytest.mark.parametrize(
    "dns", ["", "auto", "host.ts.net:8443", "host.ts.net\n", None, "a" * 64 + ".ts.net"]
)
def test_auto_requires_valid_reported_identity(dns):
    """自动模式也只采用 Tailscale 实际返回的合法设备域名。"""
    settings = load_settings(REPO, REPO / "deploy.test.example.yaml")
    with pytest.raises(ValueError, match="Self.DNSName"):
        settings.resolve_machine_name(dns)
