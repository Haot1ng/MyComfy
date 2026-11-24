"""事务式消费 Nginx 脱敏分钟日志，使进程重启后可以从同一字节位置恢复计量。"""

import sqlite3
import time

COLUMNS = (
    "requests",
    "status_4xx",
    "status_5xx",
    "duration_ms",
    "max_duration_ms",
    "rejected",
    "gateway_failures",
)


def open_metrics(shared):
    """初始化 WAL 统计库和日志游标；读者可在后台聚合期间查询已提交的分钟。"""
    db = sqlite3.connect(shared / "edge.sqlite3", timeout=2)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("""CREATE TABLE IF NOT EXISTS edge_minutes (
        minute INTEGER NOT NULL, listener TEXT NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL,
        requests INTEGER NOT NULL,status_4xx INTEGER NOT NULL,status_5xx INTEGER NOT NULL,
        duration_ms INTEGER NOT NULL,max_duration_ms INTEGER NOT NULL,
        rejected INTEGER NOT NULL,gateway_failures INTEGER NOT NULL,
        PRIMARY KEY(minute,listener,method,path)) WITHOUT ROWID""")
    db.execute(
        "CREATE TABLE IF NOT EXISTS edge_offsets (filename TEXT PRIMARY KEY, position INTEGER NOT NULL)"
    )
    return db


def _collect(db, path, now):
    """在同一事务内提交一批计数与新的字节游标；部分尾行留待下次读取。"""
    row = db.execute("SELECT position FROM edge_offsets WHERE filename=?", (path.name,)).fetchone()
    position = row[0] if row else 0
    counts = {}
    with path.open("rb") as source:
        source.seek(position)
        # 限制每批的内存，并让处理积压时仍可及时读取新规则和处理停机。
        for line in source.readlines(1024 * 1024):
            if not line.endswith(b"\n"):
                break
            position += len(line)
            stamp, listener, method, route, status, limiting, duration, header = (
                line.rstrip(b"\n").decode("ascii").split("|")
            )
            minute = int(stamp.split(".")[0]) // 60 * 60
            status = int(status)
            seconds, _, fraction = duration.partition(".")
            ms = int(seconds) * 1000 + int(fraction.ljust(3, "0")[:3])
            key = (minute, listener, method, route)
            acc = counts.setdefault(key, [0] * len(COLUMNS))
            acc[0] += 1
            acc[1] += 400 <= status < 500
            acc[2] += 500 <= status < 600
            acc[3] += ms
            acc[4] = max(acc[4], ms)
            acc[5] += limiting == "REJECTED"
            # 真实上游 HTTP 响应有响应头耗时（包括 0.000 秒），连接拒绝或等头超时为 "-"。
            acc[6] += status >= 500 and header == "-"
    if counts:
        columns = ",".join(COLUMNS)
        update = ",".join(
            f"{c}=max({c},excluded.{c})" if c == "max_duration_ms" else f"{c}={c}+excluded.{c}"
            for c in COLUMNS
        )
        with db:
            db.executemany(
                f"""INSERT INTO edge_minutes(minute,listener,method,path,{columns})
                VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(minute,listener,method,path)
                DO UPDATE SET {update}""",
                [(*k, *v) for k, v in counts.items()],
            )
            db.execute("INSERT OR REPLACE INTO edge_offsets VALUES(?,?)", (path.name, position))
    stat = path.stat()
    if position == stat.st_size and stat.st_mtime < now - 180:
        # Nginx 每条请求按完成时钟打开对应分钟文件；保留三个分钟来包容在途 write。
        path.unlink()
        with db:
            db.execute("DELETE FROM edge_offsets WHERE filename=?", (path.name,))
    return position < stat.st_size and bool(counts)


def ingest(db, journal):
    """消费各分钟文件并报告是否还有已写的积压，供控制循环及时追赶。"""
    now = time.time()
    more = False
    for path in sorted(journal.glob("*.log")):
        more |= _collect(db, path, now)
    return more


def prune(db):
    """保留近 25 小时的分钟汇总，覆盖用户可查询的完整 24 小时窗口。"""
    with db:
        db.execute("DELETE FROM edge_minutes WHERE minute < ?", (int(time.time()) - 25 * 3600,))
