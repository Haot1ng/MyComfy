"""Linux 下每个引擎代次的进程树监护程序。"""

import ctypes
import json
import logging
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .deploy import write_json


def process_identity(pid):
    """读取启动时刻和系统启动标识，避免 PID 复用误认。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[-1].split()
        if stat[0] == b"Z":
            return None
        return {
            "pid": pid,
            "ticks": int(stat[19]),
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except (FileNotFoundError, ProcessLookupError):
        return None


def descendants(parent):
    """从 Linux 任务的子进程列表收集所有仍归该代次管理的后代。"""
    by_parent = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            record = stat.read_bytes().rsplit(b")", 1)[1].split()
            by_parent.setdefault(int(record[1]), set()).add(int(stat.parent.name))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    pending = [parent]
    children = set()
    while pending:
        pids = by_parent.get(pending.pop(), set())
        pending.extend(pids - children)
        children.update(pids)
    return children


def signal_tree(parent, sig):
    """通过 pidfd 和二次所有权校验向当前代次的子进程发信号。"""
    for child in descendants(parent):
        try:
            fd = os.pidfd_open(child)
        except ProcessLookupError:
            continue
        try:
            if child in descendants(parent):
                signal.pidfd_send_signal(fd, sig)
        except ProcessLookupError:
            pass
        finally:
            os.close(fd)


SECRET_LINE = re.compile(
    r"(password|passwd|cookie|authorization|bearer\s|csrf|token|secret|"
    r"api[_ -]?key|private[_ -]?key|://\S+:[^/\s]+@)",
    re.IGNORECASE,
)


def log_output(log, buffer, chunk, discarding):
    """按行脱敏并写轮转日志，限制异常长行的内存占用。"""
    for part in chunk.splitlines(keepends=True):
        finished = part.endswith((b"\r", b"\n"))
        if not discarding:
            buffer.extend(part)
            if len(buffer) > 65536:
                log.info("[引擎长行已省略]")
                discarding = True
                buffer.clear()
        if finished:
            if not discarding:
                line = buffer.decode("utf-8", "replace").rstrip()
                log.info("%s", "[引擎凭据字段已脱敏]" if SECRET_LINE.search(line) else line)
            buffer.clear()
            discarding = False
    return discarding


def run(run_dir):
    """登记监护身份，收割包括脱离会话的子孙进程，并持久记录退出证据。"""
    launch = json.loads((run_dir / "launch.json").read_text())
    # 子收割器在启动引擎之前设置；后代即使 setsid 或双重 fork 也会重新归此进程。
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER")
    owner = os.getpid()
    write_json(run_dir / "identity.json", process_identity(owner))
    handler = RotatingFileHandler(run_dir / "engine.log", maxBytes=4 << 20, backupCount=3)
    formatter = logging.Formatter("%(asctime)s %(message)s")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    log = logging.getLogger("engine")
    log.setLevel(logging.INFO)
    log.propagate = False
    log.addHandler(handler)
    child = None
    code = None
    started = None
    reason = "engine exited"
    try:
        if (run_dir / "stop").exists():
            reason = "stop before launch"
        else:
            child = subprocess.Popen(
                launch["command"],
                cwd=launch["cwd"],
                env=launch["environment"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            write_json(run_dir / "child.json", process_identity(child.pid))
        if child:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                buffer = bytearray()
                discarding = False
                while True:
                    for key, _ in selector.select(timeout=0.1):
                        data = os.read(key.fd, 8192)
                        if data:
                            discarding = log_output(log, buffer, data, discarding)
                        else:
                            selector.unregister(key.fd)
                            if buffer:
                                log_output(log, buffer, b"\n", discarding)
                    # waitpid(-1) 得到 ECHILD 才能证明所有后代已经退出。
                    try:
                        while True:
                            pid, status = os.waitpid(-1, os.WNOHANG)
                            if pid == 0:
                                break
                            if pid == child.pid:
                                code = os.waitstatus_to_exitcode(status)
                                child.returncode = code
                    except ChildProcessError:
                        # 所有写端已关闭，读完管道中最后的日志并脱敏。
                        while chunk := child.stdout.read(8192):
                            discarding = log_output(log, buffer, chunk, discarding)
                        if buffer:
                            log_output(log, buffer, b"\n", discarding)
                        break
                    if (run_dir / "stop").exists() or code is not None:
                        if started is None:
                            started = time.monotonic()
                            reason = "requested stop" if (run_dir / "stop").exists() else reason
                        sig = signal.SIGKILL if time.monotonic() - started > 3 else signal.SIGTERM
                        signal_tree(owner, sig)
            child.stdout.close()
    except OSError as exc:
        log.error("引擎启动或监护失败 errno=%s", exc.errno)
        if child is not None:
            # 监护异常不能证明后代已经退出。缺失退出凭据时管理程序会隔离实例。
            raise
        reason = "launch failed"
    finally:
        log.removeHandler(handler)
        handler.close()
    write_json(
        run_dir / "ended.json", {"generation": launch["generation"], "code": code, "reason": reason}
    )


if __name__ == "__main__":
    run(Path(sys.argv[1]))
