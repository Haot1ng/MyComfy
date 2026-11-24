"""用临时 CLI 和本地状态验证双端口 Funnel，不连接 Tailscale 服务。"""

import base64
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    import funnel_client as fc
finally:
    sys.path.remove(str(SCRIPTS))

HOST = "desktop.example.ts.net"

# 每个前台进程只持有一个状态文件；测试可模拟已有配置、子进程退出或并发规则变更。
FAKE = """#!/usr/bin/env python3
import fcntl, json, os, signal, sys, time
from pathlib import Path
root = Path(os.environ["MYCOMFY_FAKE_FUNNEL"])
args = sys.argv[1:]
with (root / "calls").open("a") as log:
    print(json.dumps(args), file=log)
host = "desktop.example.ts.net"
if args == ["status", "--json"]:
    print(json.dumps({"BackendState": "Running", "Self": {"DNSName": host.upper() + "."}}))
    sys.exit(0)
if args in (["serve", "status", "--json"], ["funnel", "status", "--json"]):
    if args[0] == "serve" and (root / "fallback").exists():
        sys.exit(1)
    config = json.loads((root / "baseline").read_text())
    fg = config.setdefault("Foreground", {})
    for path in root.glob("rule-*"):
        with path.open() as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fg[path.name] = json.load(lease)
    print(json.dumps(config))
    sys.exit(0)
assert len(args) == 3 and args[0] == "funnel" and args[1].startswith("--https=")
port = args[1].split("=")[1]
target = args[2]
pid = str(os.getpid())
rule = root / ("rule-" + pid)
def stop(*_):
    rule.unlink(missing_ok=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print("Authorize at https://login.example.invalid/approve", flush=True)
if (root / ("fail-" + port)).exists():
    sys.exit(31)
hp = host + ":" + port
if (root / ("mismatch-" + port)).exists():
    target += "/other"
cfg = {"TCP": {port: {"HTTPS": True}}, "AllowFunnel": {hp: True},
       "Web": {hp: {"Handlers": {"/": {"Proxy": target}}}}}
tmp = root / ("draft-" + pid)
lease = tmp.open("w")
fcntl.flock(lease, fcntl.LOCK_EX)
json.dump(cfg, lease)
lease.flush()
tmp.replace(rule)
if not (root / ("quiet-" + port)).exists():
    print("https://" + host + ("" if port == "443" else ":" + port) + "/", flush=True)
while not (root / ("stop-" + port)).exists(): time.sleep(.02)
stop()
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """准备本机可执行的 CLI 替身，并保留一条测试不拥有的后台规则。"""
    path = tmp_path / "tailscale"
    path.write_text(FAKE)
    path.chmod(0o700)
    (tmp_path / "baseline").write_text(
        json.dumps({"TCP": {"10000": {"TCPForward": "localhost:9"}}})
    )
    monkeypatch.setenv("MYCOMFY_FAKE_FUNNEL", str(tmp_path))
    return fc.FunnelClient.discover(path)


def test_two_live_forwards_and_original_authorization_output(fake, tmp_path, capsys):
    """验证双会话真实启动、逐条确认、授权链接透传和正常返回后的定向清理。"""
    assert fake.hostname == HOST and fake.occupied_ports() == {10000}
    with fake.forward({443: 8123, 8443: 8124}, startup_timeout=3) as session:
        session.check()
        assert fake.occupied_ports() == {443, 8443, 10000}
        assert "https://login.example.invalid/approve" in capsys.readouterr().err
    assert fake.occupied_ports() == {10000}
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert [args for args in calls if args[0] == "funnel"] == [
        ["funnel", "--https=443", "http://127.0.0.1:8123"],
        ["funnel", "--https=8443", "http://127.0.0.1:8124"],
    ]


@pytest.mark.parametrize(
    ("signal_file", "error"),
    [("fail-8443", "31"), ("mismatch-8443", "目标"), ("quiet-8443", "超时")],
)
def test_second_failed_forward_cleans_both(fake, tmp_path, signal_file, error):
    """第二条退出、被换目标或一直未由该进程确认时都要回收第一条。"""
    (tmp_path / signal_file).touch()
    with (
        pytest.raises(fc.FunnelError, match=error),
        fake.forward({443: 7001, 8443: 7002}, startup_timeout=1),
    ):
        pytest.fail("两条配置未通过确认")
    assert fake.occupied_ports() == {10000}


def test_existing_foreground_and_background_are_never_overwritten(fake, tmp_path):
    """某一选定端口仅有 Web 或 Funnel 权限也被识别为占用，冲突时不启动第一条。"""
    baseline = {
        "AllowFunnel": {HOST + ":10000": True},
        "Foreground": {"other": {"Web": {HOST + ":8443": {"Handlers": {"/": {"Text": "keep"}}}}}},
    }
    (tmp_path / "baseline").write_text(json.dumps(baseline))
    (tmp_path / "fallback").touch()
    with pytest.raises(fc.FunnelError, match="8443"), fake.forward({443: 8000, 8443: 8001}):
        pytest.fail("覆盖了现有配置")
    assert fake.occupied_ports() == {8443, 10000}
    assert all("--https=" not in line for line in (tmp_path / "calls").read_text().splitlines())


def test_runtime_failure_closes_sibling(fake, tmp_path):
    """启动完毕后一条前台进程退出时，wait 及时抛错并触发另一条的清理。"""
    with (
        pytest.raises(fc.FunnelError, match="443"),
        fake.forward({443: 8000, 8443: 8001}) as session,
    ):
        (tmp_path / "stop-443").touch()
        session.wait()
    assert fake.occupied_ports() == {10000}


def test_real_keyboard_interrupt_during_second_registration(fake, tmp_path):
    """向测试启动器发送 SIGINT，验证等待第二条授权期间也能正常回收两个真实子进程。"""
    (tmp_path / "quiet-8443").touch()
    runner = "from funnel_client import FunnelClient; import sys\n" + (
        "try:\n with FunnelClient.discover(sys.argv[1]).forward({443: 7001, 8443: 7002}): pass\n"
        "except KeyboardInterrupt: sys.exit(130)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", runner, fake.executable],
        env={**os.environ, "PYTHONPATH": str(SCRIPTS)},
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while len(list(tmp_path.glob("rule-*"))) != 2:
            assert time.monotonic() < deadline and process.poll() is None
            time.sleep(0.02)
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=5) == 130
        assert fake.occupied_ports() == {10000}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_killing_linux_launcher_drops_both_owned_leases(fake, tmp_path):
    """直接 SIGKILL 启动器，无 Python finally 时也必须由内核关闭两条前台连接。"""
    code = (
        "from funnel_client import FunnelClient; import sys\n"
        "with FunnelClient.discover(sys.argv[1]).forward({443: 7001, 8443: 7002}) as s:\n"
        " print('READY', flush=True); s.wait()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, fake.executable],
        env={**os.environ, "PYTHONPATH": str(SCRIPTS)},
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        import select

        assert select.select([process.stdout], [], [], 6)[0]
        assert process.stdout.readline().strip() == "READY"
        assert fake.occupied_ports() == {443, 8443, 10000}
        process.kill()
        assert process.wait(timeout=3) == -signal.SIGKILL
        # CLI 对替身 daemon 的两份文件锁，与实际 WatchIPNBus 连接一样随进程退出释放。
        deadline = time.monotonic() + 3
        while fake.occupied_ports() != {10000}:
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()


def test_discovery_prefers_logged_in_windows_then_linux(tmp_path, monkeypatch):
    """模拟 WSL 的 PATH 和标准目录顺序；首选客户端未登录时使用下一候选。"""
    for name in ("tailscale.exe", "standard.exe", "tailscale"):
        (tmp_path / name).touch(mode=0o700)
    monkeypatch.setenv("WSL_INTEROP", "test")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(fc, "WINDOWS_TAILSCALE", str(tmp_path / "standard.exe"))
    candidates = fc._candidates()
    assert candidates == [
        str(tmp_path / name) for name in ("tailscale.exe", "standard.exe", "tailscale")
    ]
    monkeypatch.setattr(
        fc,
        "_json_command",
        lambda path, args: {
            "BackendState": "Running" if path == candidates[1] else "NeedsLogin",
            "Self": {"DNSName": HOST},
        },
    )
    assert fc.FunnelClient.discover().executable == candidates[1]
    with pytest.raises(fc.FunnelError, match="登录"):
        fc.FunnelClient.discover(candidates[0])


def test_windows_broker_uses_wsl_mapping_and_private_windows_job(tmp_path, monkeypatch):
    """核对 Windows 启动契约，包括原生 Job、自身 stdin 租约及带空格和引号的路径。"""
    monkeypatch.setattr(fc.shutil, "which", lambda name: sys.executable)
    winpath = "C:\\Program Files\\O'Neil\\tailscale.exe"
    monkeypatch.setattr(fc.subprocess, "check_output", lambda *args, **kwargs: winpath)
    cmd = fc._windows_command(
        tmp_path / "tailscale.exe", ["funnel", "--https=8443", "http://127.0.0.1:8001"]
    )
    assert cmd[:-1] == [sys.executable, "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    script = base64.b64decode(cmd[-1]).decode("utf-16le")
    assert "'C:\\Program Files\\O''Neil\\tailscale.exe'" in script
    assert "'funnel --https=8443 http://127.0.0.1:8001'" in script
    assert script.index("[FunnelJob]::Guard()") < script.index("$p.Start()")
    assert "AssignProcessToJobObject" in script and "$lease.BeginRead" in script


@pytest.mark.parametrize(
    "value",
    [
        None,
        "short",
        "https://x.example.ts.net",
        "x.ts.net:99",
        "evil.ts.net.example",
        "-bad.ts.net",
    ],
)
def test_reject_invalid_device_identity(value):
    """拒绝无效或与 ts.net 不同的设备标识，避免生成错误入口。"""
    with pytest.raises(fc.FunnelError):
        fc.normalize_hostname(value)
