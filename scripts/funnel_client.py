"""用已登录的本机 Tailscale 客户端管理本次外网测试的前台 Funnel。"""

from __future__ import annotations

import base64
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from tunnel_settings import TunnelSettings

HTTPS_PORTS = {443, 8443, 10000}
WINDOWS_TAILSCALE = "/mnt/c/Program Files/Tailscale/tailscale.exe"
WINDOWS_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


class FunnelError(RuntimeError):
    """本机无法发现、启动或继续维持所请求的测试转发。"""


def normalize_hostname(value):
    """用测试配置相同的规则规范化客户端返回的完整设备域名。"""
    try:
        return TunnelSettings().resolve_machine_name(value)
    except ValueError as exc:
        raise FunnelError(str(exc)) from exc


def _candidates():
    """WSL 优先使用 Windows 桌面登录会话，其次查找 Linux 原生客户端。"""
    candidates = []
    if os.environ.get("WSL_INTEROP") or "microsoft" in platform.release().lower():
        candidates.extend([shutil.which("tailscale.exe"), WINDOWS_TAILSCALE])
    candidates.append(shutil.which("tailscale"))
    # Windows 程序目录可能已在 WSL PATH 内，避免重复探测同一文件。
    return list(dict.fromkeys(path for path in candidates if path and os.access(path, os.X_OK)))


def _json_command(executable, args):
    """读取一次只读 CLI 的 JSON 结果；无法判定状态时拒绝继续。"""
    try:
        result = subprocess.run(
            [executable, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.decode(errors="replace").strip()[:400]
            raise FunnelError(f"{' '.join(args)} 失败（{result.returncode}） {detail}")
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise FunnelError(f"无法读取 {' '.join(args)} 的 JSON 状态：{exc}") from exc


def _table(config, key):
    """取出一个状态映射，拒绝把客户端不兼容的数据当成空配置。"""
    value = config.get(key, {})
    if not isinstance(value, dict):
        raise FunnelError(f"Tailscale 状态中的 {key} 不是对象，无法安全检查转发")
    return value


def _owners(config):
    """扫描设备本身的后台和前台规则，按远端端口列出拥有者及其配置。"""
    owners = {}
    for session_id, rules in [(None, config), *_table(config, "Foreground").items()]:
        if not isinstance(rules, dict):
            raise FunnelError("Tailscale 前台转发状态无效")
        # 新版独立服务有自己的地址；未知状态结构不能证明设备端口空闲。
        if rules.keys() - {"TCP", "Web", "AllowFunnel", "Services", "Foreground"}:
            raise FunnelError("Tailscale 转发配置包含未识别的字段，无法安全检查端口")
        # TCP 原始转发、Web 挂载及开放 Funnel 的条目都可能单独占用所选端口。
        keys = set(_table(rules, "TCP"))
        keys.update(key.rsplit(":", 1)[-1] for key in _table(rules, "Web"))
        keys.update(
            key.rsplit(":", 1)[-1]
            for key, enabled in _table(rules, "AllowFunnel").items()
            if enabled
        )
        for key in keys:
            if not key.isascii() or not key.isdigit() or not 1 <= int(key) <= 65535:
                raise FunnelError("Tailscale 转发状态包含无法识别的端口")
            owners.setdefault(int(key), []).append((session_id, rules))
    return owners


def _matching_session(owners, hostname, remote, local):
    """核对该端口只对应一个前台会话，且 HTTPS、公开权限、根路径目标全部匹配。"""
    matches = owners.get(remote, [])
    if not matches:
        return None
    endpoint = f"{hostname}:{remote}"
    if len(matches) == 1:
        session_id, rules = matches[0]
        tcp = rules.get("TCP", {}).get(str(remote), {})
        web = rules.get("Web", {}).get(endpoint, {})
        handlers = _table(web, "Handlers") if isinstance(web, dict) else {}
        root = handlers.get("/")
        target = root.get("Proxy") if isinstance(root, dict) else None
        if (
            session_id
            and isinstance(tcp, dict)
            and tcp.get("HTTPS") is True
            and rules.get("AllowFunnel", {}).get(endpoint) is True
            and target in (f"http://127.0.0.1:{local}", f"http://127.0.0.1:{local}/")
        ):
            return session_id
    raise FunnelError(f"HTTPS {remote} 出现了其他转发或目标与本次启动不符")


# 在新的单线程解释器内把 CLI 的寿命绑定到启动器，然后原地 exec，保留同一个 PID。
# 如果父进程在设置 prctl 之前已死亡，复核 PPID 可避免孤儿进程误开公网入口。
_LINUX_EXEC = """
import ctypes, os, signal, sys
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
    raise OSError(ctypes.get_errno(), "PR_SET_PDEATHSIG")
if os.getppid() != int(sys.argv[1]):
    sys.exit(1)
os.execvp(sys.argv[2], sys.argv[2:])
"""


# Windows 客户端属于 Windows 进程树。WSL 侧只向此私有 stdin 租约发 EOF；
# Windows 监护进程持有真实 Process 对象，并把自身与子进程放入单独的内核 Job。
# 即便监护进程意外结束，最后一个 Job 句柄关闭时 Windows 也会终止其子进程。
_WINDOWS_BROKER = r"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class FunnelJob {
    [StructLayout(LayoutKind.Sequential)]
    struct Basic {
        public long perProcessTime, perJobTime;
        public uint flags;
        public UIntPtr minSet, maxSet;
        public uint maxProcesses;
        public UIntPtr affinity;
        public uint priority, schedule;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct Extended {
        public Basic basic;
        public ulong readOps, writeOps, otherOps, readBytes, writeBytes, otherBytes;
        public UIntPtr processMemory, jobMemory, peakProcess, peakJob;
    }
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetInformationJobObject(IntPtr job, int type, ref Extended info, uint size);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();

    // Create the kill-on-close job before any Tailscale child can exist.
    public static void Guard() {
        var job = CreateJobObject(IntPtr.Zero, null);
        var limits = new Extended();
        limits.basic.flags = 0x2000;
        if (job == IntPtr.Zero ||
            !SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(typeof(Extended))) ||
            !AssignProcessToJobObject(job, GetCurrentProcess()))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        // This non-inheritable handle stays open until this dedicated broker exits.
    }
}
'@
[FunnelJob]::Guard()
$lease = [Console]::OpenStandardInput()
$buffer = New-Object byte[] 1
$ending = $lease.BeginRead($buffer, 0, 1, $null, $null)
$p = New-Object System.Diagnostics.Process
$p.StartInfo.FileName = __EXECUTABLE__
$p.StartInfo.Arguments = '__ARGUMENTS__'
$p.StartInfo.UseShellExecute = $false
$p.StartInfo.RedirectStandardInput = $true
$p.StartInfo.CreateNoWindow = $true
$started = $false
try {
    if ($ending.IsCompleted) { exit 0 }
    $started = $p.Start()
    $p.StandardInput.Close()
    while (!$p.WaitForExit(150) -and !$ending.IsCompleted) {}
    if ($p.HasExited) { exit $p.ExitCode }
} finally {
    if ($started -and !$p.HasExited) {
        $p.Kill()
        $p.WaitForExit()
    }
    $p.Dispose()
}
"""


def _windows_command(executable, args):
    """组装 WSL 可启动的 Windows 监护命令，文件名作为 PowerShell 字符串字面量传入。"""
    powershell = shutil.which("powershell.exe") or WINDOWS_POWERSHELL
    if not os.access(powershell, os.X_OK):
        raise FunnelError("找不到 Windows PowerShell，无法从 WSL 管理本次 Windows 转发")
    try:
        windows_path = subprocess.check_output(
            ["wslpath", "-w", str(Path(executable).resolve())],
            timeout=5,
            text=True,
        ).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise FunnelError("无法通过 wslpath 获取 Windows Tailscale 客户端路径") from exc
    quoted = "'" + windows_path.replace("'", "''") + "'"
    # 仅数值端口参与生成命令行参数，路径和值永远不会被当成 shell 命令执行。
    script = _WINDOWS_BROKER.replace("__ARGUMENTS__", " ".join(args)).replace(
        "__EXECUTABLE__", quoted
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


class _Listener:
    """持有一个前台 CLI 或 Windows 监护进程，并实时转发原生授权提示。"""

    def __init__(self, executable, hostname, remote, local):
        """启动隔离的前台进程，通过其标准输出独立确认由该进程公布的地址。"""
        self.remote = remote
        self.announced = threading.Event()
        suffix = "" if remote == 443 else f":{remote}"
        self.origin = f"https://{hostname}{suffix}"
        args = ["funnel", f"--https={remote}", f"http://127.0.0.1:{local}"]
        windows = str(executable).lower().endswith(".exe")
        if windows:
            cmd = _windows_command(executable, args)
        else:
            cmd = [sys.executable, "-c", _LINUX_EXEC, str(os.getpid()), str(executable), *args]
        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if windows else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except OSError as exc:
            raise FunnelError(f"无法启动 HTTPS {remote} 的 Tailscale 客户端：{exc}") from exc
        self.output = threading.Thread(target=self._read_output, daemon=True)
        try:
            self.output.start()
        except BaseException:
            self.stop()
            raise

    def _read_output(self):
        """逐行透传输出，用独立一行的预期 URL 识别自己的 CLI 已完成注册。"""
        with self.process.stdout as stream:
            for line in stream:
                print(line, end="", file=sys.stderr, flush=True)
                if line.strip().rstrip("/") == self.origin:
                    self.announced.set()

    def check(self):
        """检查本次前台进程是否异常退出，保留 CLI 已输出的故障或授权指引。"""
        code = self.process.poll()
        if code is not None:
            raise FunnelError(f"HTTPS {self.remote} 的 Tailscale 前台转发已退出（{code}）")

    def stop(self):
        """关闭本条 Windows 租约或终止自己的 Linux 子进程，回收输出线程。"""
        if self.process.stdin is not None:
            # Windows 收到 EOF 后关闭本机持有的 Process，不能将 Linux PID 当作 Windows PID。
            self.process.stdin.close()
        elif self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if self.process.stdin is not None:
                raise FunnelError(
                    f"Windows 未确认关闭 HTTPS {self.remote} 的前台进程，请核对 tailscale serve status"
                ) from None
            self.process.kill()
            self.process.wait(timeout=5)
        if self.output.ident is not None:
            self.output.join(timeout=2)


class FunnelSession:
    """管理本次测试创建的全部前台转发。"""

    def __init__(self):
        """创建空会话；每次成功派生子进程即登记，以覆盖中途失败。"""
        self.listeners = []

    def launch(self, executable, hostname, remote, local):
        """将前台子进程登记后再处理本次 Ctrl+C 或终端关闭，避免留下半启动进程。"""
        previous = {}
        pending = []

        def defer(signum, frame):
            """暂存启动器捕获的退出信号，待成功登记资源后交还原处理器。"""
            pending.append(signum)

        try:
            # 暂换 Python 处理器，不改变子进程的 POSIX 信号掩码；SIGTERM 仍能关闭 Linux CLI。
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    if callable(signal.getsignal(signum)):
                        previous[signum] = signal.signal(signum, defer)
            listener = _Listener(executable, hostname, remote, local)
            self.listeners.append(listener)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            if pending:
                signal.raise_signal(pending[0])
        return listener

    def check(self):
        """核实所有已经启动的子进程存活。"""
        for listener in self.listeners:
            listener.check()

    def wait(self):
        """等待 Ctrl+C，同时及时报告任何一个前台转发意外退出。"""
        while True:
            self.check()
            time.sleep(0.25)


class FunnelClient:
    """在已登录的 Windows 或 Linux 客户端上读取规则并启动测试会话。"""

    def __init__(self, executable, hostname):
        """记录本次选中的可执行文件和已登录设备名称。"""
        self.executable = executable
        self.hostname = hostname

    @classmethod
    def discover(cls, executable=None):
        """从显式路径或各平台候选项选择第一个处于 Running 的登录设备。"""
        errors = []
        for path in [executable] if executable else _candidates():
            try:
                state = _json_command(path, ["status", "--json"])
                if not isinstance(state, dict) or state.get("BackendState") != "Running":
                    raise FunnelError("Tailscale 尚未登录或尚未连接（需要 Running）")
                hostname = normalize_hostname(_table(state, "Self").get("DNSName"))
                return cls(path, hostname)
            except FunnelError as exc:
                errors.append(f"{path}: {exc}")
        raise FunnelError("未找到已连接的 Tailscale 客户端。请先安装并登录。 " + "；".join(errors))

    def _owners(self):
        """读取 Serve 与 Funnel 共用的 daemon 状态，兼容两个只读 CLI 子命令。"""
        try:
            config = _json_command(self.executable, ["serve", "status", "--json"])
        except FunnelError:
            config = _json_command(self.executable, ["funnel", "status", "--json"])
        if config is None:
            return {}
        if not isinstance(config, dict):
            raise FunnelError("Tailscale 转发状态不是配置对象")
        return _owners(config)

    def occupied_ports(self):
        """返回当前设备由后台或前台 Serve/Funnel 占用的端口号。"""
        return set(self._owners())

    def _require_free(self, ports):
        """遇到任何已配置的目标端口即停止，不修改已有后台或前台转发。"""
        busy = self.occupied_ports().intersection(ports)
        if busy:
            existing = ", ".join(map(str, sorted(busy)))
            raise FunnelError(f"远端 HTTPS 端口 {existing} 已被 Serve 或 Funnel 占用")

    @contextmanager
    def forward(self, targets, *, startup_timeout=300):
        """逐条注册并确认两条临时转发，出现异常或退出上下文时定向清理本次子进程。"""
        targets = dict(targets)
        if len(targets) != 2 or any(
            type(remote) is not int
            or remote not in HTTPS_PORTS
            or type(local) is not int
            or not 1 <= local <= 65535
            for remote, local in targets.items()
        ):
            raise FunnelError("需要两个不同的 Funnel HTTPS 端口以及对应的本机端口")
        self._require_free(targets)
        session = FunnelSession()
        session_ids = {}
        try:
            for remote, local in targets.items():
                self._require_free([remote])
                # 两个 CLI 依次修改同一个 daemon 配置，第一条被确认后才启动第二条。
                listener = session.launch(self.executable, self.hostname, remote, local)
                deadline = time.monotonic() + startup_timeout
                while True:
                    session.check()
                    if listener.announced.is_set():
                        owners = self._owners()
                        owner = _matching_session(owners, self.hostname, remote, local)
                        if owner:
                            session_ids[remote] = owner
                            break
                    if time.monotonic() >= deadline:
                        raise FunnelError(
                            f"等待 HTTPS {remote} 前台转发就绪超时，请核对授权和 CLI 输出"
                        )
                    time.sleep(0.2)
            # 第二条注册后再核对第一条的会话身份，避免把中途消失的规则报告为就绪。
            for remote, local in targets.items():
                if _matching_session(owners, self.hostname, remote, local) != session_ids[remote]:
                    raise FunnelError(f"HTTPS {remote} 的前台转发会话已变化")
            session.check()
            yield session
        finally:
            # ExitStack 保证即使一条清理报错，另一条同样会被回收。
            with ExitStack() as cleanup:
                for listener in session.listeners:
                    cleanup.callback(listener.stop)
