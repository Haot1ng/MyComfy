"""根据本容器协调器的主循环进度判断 Worker 是否健康。"""

import fcntl
import math
import os
import struct
import time
from contextlib import contextmanager

# 一次观察可能先后发起两个 10 秒的引擎请求，再等待代次停止。
POLL_GRACE = 45
# 文件检验最多运行三次 300 秒的媒体命令，另留时间用于复制和登记。
FILE_GRACE = 20 * 60
_DEADLINE = struct.Struct("d")


def ignore_progress(seconds):
    """供未启用容器探针的调用者运行同一套协调循环。"""


@contextmanager
def report_health(path):
    """在协调器持锁期间提供进度回调，进程退出时内核会释放该锁。"""
    if not path:
        yield ignore_progress
        return
    # 心跳放在容器自己的文件系统中，避免共享数据卷上的其他 Worker 回应探针。
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        # 协调器已持有独占调度锁。若启动恰遇探针短暂持有共享锁，等待读取结束。
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.ftruncate(fd, 0)

        def report(seconds):
            """以单次写入更新单调时钟期限，传零立即报告不可用。"""
            deadline = time.monotonic() + seconds if seconds > 0 else 0
            os.pwrite(fd, _DEADLINE.pack(deadline), 0)

        yield report
    finally:
        os.close(fd)


def unhealthy_reason(path):
    """校验活着的持锁者和最近进度，为 Docker 健康检查返回失败原因。"""
    if not path:
        return "未配置 Worker 心跳文件"
    try:
        with open(path, "rb", buffering=0) as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                # 只读取当前协调器持有的文件。未完成首个检查点时内容为空。
                record = stream.read(_DEADLINE.size)
                deadline = _DEADLINE.unpack(record)[0] if len(record) == _DEADLINE.size else 0
                if math.isfinite(deadline) and deadline > time.monotonic():
                    return None
                return "Worker 未在期限内取得调度进展"
            return "Worker 协调器已退出"
    except OSError as exc:
        return f"无法读取 Worker 心跳文件：{exc.strerror}"


if __name__ == "__main__":
    raise SystemExit(unhealthy_reason(os.environ.get("MYCOMFY_WORKER_HEALTH_FILE")))
