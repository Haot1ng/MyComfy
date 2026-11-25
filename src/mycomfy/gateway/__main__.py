"""运行非特权 Nginx 网关，应用 Core 快照并向管理后台报告实际生效的版本。"""

import fcntl
import http.client
import json
import os
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from .metrics import ingest, open_metrics, prune
from .nginx import read_snapshot, render


def paths():
    """返回与 Core 共享的统计目录及仅在本网关容器中使用的运行目录。"""
    return (
        Path(os.environ.get("MYCOMFY_GATEWAY_DIR", "/var/lib/mycomfy-gateway")).resolve(),
        Path(os.environ.get("MYCOMFY_GATEWAY_RUN_DIR", "/tmp/mycomfy-gateway")).resolve(),
    )


def atomic_file(path, content):
    """在同一目录写完并刷盘，再原子替换规则、配置或状态文件。"""
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as draft:
        name = Path(draft.name)
        try:
            draft.write(content)
            draft.flush()
            os.fsync(draft.fileno())
            os.replace(name, path)
        finally:
            name.unlink(missing_ok=True)


def served_revision(work):
    """只从私有 Unix 套接字读取当前 Nginx worker 真实响应的配置版本。"""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        sock.connect(str(work / "health.sock"))
        sock.sendall(b"GET /revision HTTP/1.0\r\nHost: localhost\r\n\r\n")
        with http.client.HTTPResponse(sock) as response:
            response.begin()
            if response.status == 200:
                return response.read(65).decode("ascii")
    return None


def clear_stale_health_socket(path):
    """冷启动持有网关独占锁时，仅清理已无监听进程的私有健康套接字。"""
    try:
        # 用 lstat 保留符号链接和普通文件，让异常的运行目录有明确的报错。
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise RuntimeError(f"Expected a Unix socket at {path}")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            probe.connect(str(path))
    except FileNotFoundError:
        return
    except ConnectionRefusedError:
        # 容器强停可能留下路径。只有内核明确拒绝连接时才允许释放它。
        path.unlink(missing_ok=True)
        return
    raise RuntimeError(f"Gateway health socket already has a listener at {path}")


def health():
    """验证运行心跳、服务中的规则版本，以及统计数据库能否读取。"""
    shared, work = paths()
    status = json.loads((shared / "status.json").read_bytes())
    age = (datetime.now(UTC) - datetime.fromisoformat(status["updated_at"])).total_seconds()
    if status["error"] or not 0 <= age < 60 or not status["revision"]:
        return False
    if served_revision(work) != status["revision"]:
        return False
    uri = (shared / "edge.sqlite3").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
        db.execute("SELECT requests, rejected FROM edge_minutes LIMIT 1").fetchone()
    return True


class Gateway:
    """维护一份已核准的规则和一个 Nginx 主进程，拒绝无效更新并保留已生效的配置。"""

    def __init__(self, shared, work):
        """创建日志目录和控制文件名；不可从公网访问的健康套接字位于私有运行目录。"""
        self.shared = shared
        self.work = work
        self.work.mkdir(parents=True, exist_ok=True, mode=0o700)
        clear_stale_health_socket(work / "health.sock")
        self.journal = shared / "journal"
        self.journal.mkdir(mode=0o700, exist_ok=True)
        self.proc = None
        self.revision = None
        self.signature = None
        self.next_retry = 0
        self.error = None
        self.command = ["nginx", "-e", "stderr", "-p", str(work), "-c", str(work / "nginx.conf")]

    def apply(self, snapshot):
        """先测试完整配置，再启动或平滑重载，成功收到新版本的 HTTP 回执后才确认生效。"""
        candidate = self.work / "candidate.conf"
        config = self.work / "nginx.conf"
        atomic_file(candidate, render(snapshot, self.shared, self.work).encode())
        subprocess.run(
            [*self.command[:-1], str(candidate), "-t", "-q"],
            check=True,
            stdout=subprocess.DEVNULL,
            timeout=8,
        )
        previous = config.read_bytes() if self.proc is not None else None
        os.replace(candidate, config)
        if self.proc is None:
            self.proc = subprocess.Popen(
                self.command,
                stdout=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            self.proc.send_signal(signal.SIGHUP)
        deadline = time.monotonic() + 5
        while self.proc.poll() is None and time.monotonic() < deadline:
            try:
                if served_revision(self.work) == snapshot["revision"]:
                    self.revision = snapshot["revision"]
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.05)
        if previous is not None and self.proc.poll() is None:
            atomic_file(config, previous)
            self.proc.send_signal(signal.SIGHUP)
        raise RuntimeError("Nginx did not acknowledge rules")

    def sync(self):
        """规则文件原子替换时才重新编译；已成功应用的快照可用于下次冷启动。"""
        desired = self.shared / "rules.json"
        saved = self.shared / "applied.json"
        try:
            stat = desired.stat()
            signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if self.signature == signature and (
                not self.error or time.monotonic() < self.next_retry
            ):
                return
            self.signature = signature
            snapshot = read_snapshot(desired)
            if self.revision != snapshot["revision"]:
                self.apply(snapshot)
                atomic_file(saved, json.dumps(snapshot, separators=(",", ":")).encode())
            self.error = None
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError):
            self.error = "Could not apply Core gateway rules"
            self.next_retry = time.monotonic() + 10
            if self.revision is None and self.proc is None and saved.is_file():
                self.apply(read_snapshot(saved))

    def status(self, error):
        """发布心跳、已生效的摘要和脱敏故障类别，不暴露配置内容或请求原文。"""
        value = {
            "revision": self.revision,
            "updated_at": datetime.now(UTC).isoformat(),
            "error": error,
        }
        atomic_file(self.shared / "status.json", json.dumps(value).encode())

    def stop_nginx(self):
        """仅在关闭容器时有界排空长期订阅；规则热更新继续保留原有连接。"""
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGQUIT)
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # 活跃 SSE 可持续数小时。超过排空期限后让 master 结束这些连接。
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # 若 worker 本身无响应，对这个私有进程组执行最后的清理。
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=2)

    def run(self, stop):
        """定期应用配置及提交访问计数。Nginx 异常退出时让容器重启并从持久游标恢复。"""
        with closing(open_metrics(self.shared)) as db:
            next_heartbeat = next_prune = 0
            last_status = None
            try:
                while not stop.is_set():
                    if self.proc is not None and self.proc.poll() is not None:
                        raise RuntimeError(f"Nginx exited with return code {self.proc.returncode}")
                    self.sync()
                    error, backlog = self.error, False
                    try:
                        backlog = ingest(db, self.journal)
                        if time.monotonic() >= next_prune:
                            prune(db)
                            next_prune = time.monotonic() + 600
                        if self.revision and served_revision(self.work) != self.revision:
                            error = "Nginx revision unavailable"
                    except (sqlite3.Error, OSError, ValueError, http.client.HTTPException):
                        error = "Gateway statistics or Nginx unavailable"
                    current = (self.revision, error)
                    if current != last_status or time.monotonic() >= next_heartbeat:
                        self.status(error)
                        last_status = current
                        next_heartbeat = time.monotonic() + 10
                    stop.wait(0.05 if backlog else 1)
            finally:
                self.stop_nginx()
                # 先结束所有 Nginx 子进程，确保最后一个请求的日志也已写入。
                while ingest(db, self.journal):
                    pass
                self.status("Gateway stopped")


def main():
    """作为容器入口启动单实例协调进程，或为 Docker 返回健康检查退出码。"""
    if sys.argv[1:] == ["health"]:
        try:
            return 0 if health() else 1
        except (OSError, ValueError, KeyError, sqlite3.Error, http.client.HTTPException):
            return 1
    if sys.argv[1:]:
        raise ValueError("Expected no arguments or health")
    shared, work = paths()
    shared.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (shared / "gateway.lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        Gateway(shared, work).run(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
