"""检查第二个公网入口启动失败时，PowerShell 会回收已就绪的第一个入口。"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.name != "posix" or not shutil.which("pwsh"),
    reason="requires PowerShell and a POSIX shell for the fake native client",
)
def test_second_funnel_failure_stops_first_process(tmp_path, monkeypatch):
    """模拟 443 已成功而 8443 启动失败，验证会话退出后第一个进程已经结束。"""
    script = Path(__file__).resolve().parents[2] / "scripts" / "funnel-session.ps1"
    # 让假客户端按真实 CLI 的格式报告就绪，并用同一 PID 保持前台会话。
    client = tmp_path / "tailscale.exe"
    client.write_text(
        """#!/bin/sh
case "$2" in
    --https=443)
        echo "$$" > public.pid
        echo 'Available on the internet:'
        echo 'https://gpu.example.ts.net/'
        exec sleep 20 ;;
    --https=8443)
        echo 'simulated admin startup failure' >&2
        exit 1 ;;
    *) exit 2 ;;
esac
"""
    )
    client.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    # 输出重定向到文件，即使第一个子进程意外存活，也能立即检查它的 PID。
    log = tmp_path / "session.log"
    with log.open("w") as output:
        result = subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-File",
                str(script),
                "-DeviceName",
                "gpu.example.ts.net",
                "-PublicPort",
                "8000",
                "-AdminPort",
                "8001",
            ],
            cwd=tmp_path,
            check=False,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
    assert result.returncode != 0
    assert "simulated admin startup failure" in log.read_text()
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "public.pid").read_text()), 0)
