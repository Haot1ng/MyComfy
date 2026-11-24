"""由真实 Nginx 代理两个测试 Unix 套接字，验证热加载、拒绝来源及可恢复的脱敏计量。"""

import hashlib
import http.client
import json
import os
import shutil
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler

import pytest

from mycomfy.gateway.metrics import ingest, open_metrics


def snapshot(routes):
    """按 Core 的格式构造签名快照。"""
    raw = json.dumps(routes, sort_keys=True, separators=(",", ":")).encode()
    return {"revision": hashlib.sha256(raw).hexdigest(), "routes": routes}


def test_replay_commits_counts_and_offsets_together(tmp_path):
    """重复消费、重启以及不完整尾行都不会漏记或重复计数。"""
    journal = tmp_path / "journal"
    journal.mkdir()
    log = journal / "sample.log"
    now = int(time.time())
    line = f"{now}.000|public|GET|/api/v1/jobs|429|REJECTED|0.001|-\n".encode()
    log.write_bytes(line + line[:-1])
    db = open_metrics(tmp_path)
    try:
        ingest(db, journal)
        ingest(db, journal)
        assert db.execute("SELECT requests,rejected FROM edge_minutes").fetchall() == [(1, 1)]
    finally:
        db.close()
    with log.open("ab") as stream:
        stream.write(b"\n")
    db = open_metrics(tmp_path)
    try:
        ingest(db, journal)
        assert db.execute(
            "SELECT requests,rejected,duration_ms,max_duration_ms FROM edge_minutes"
        ).fetchall() == [(2, 2, 2, 1)]
    finally:
        db.close()


def unused_port():
    """分配一个短期空闲的本机监听端口。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(check):
    """等待子进程通过可观察的网络或持久化断言，超时保留最后的失败原因。"""
    end = time.monotonic() + 15
    while True:
        try:
            return check()
        except (AssertionError, ValueError, OSError, sqlite3.Error):
            if time.monotonic() >= end:
                raise
            time.sleep(0.08)


@contextmanager
def upstream(directory):
    """为公开和管理端准备独立上游，同时记录到达 Core 的参数。"""
    records = []
    release = threading.Event()
    close_held_stream = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            """测试日志不输出浏览器请求路径。"""

        def do_GET(self):
            """支持立即输出 SSE，以及能与网关 429 区分的业务拒绝。"""
            records.append(
                (
                    self.path,
                    self.headers.get("X-Forwarded-For"),
                    self.headers.get("Host"),
                    self.headers.get("Origin"),
                    self.headers.get("Forwarded"),
                )
            )
            if self.path == "/api/v1/disconnected":
                return
            if self.path in ("/api/v1/events", "/api/v1/hold-open"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"data: live\n\n")
                self.wfile.flush()
                if self.path == "/api/v1/events":
                    if release.wait(30):
                        self.wfile.write(b"data: after-reload\n\n")
                        self.wfile.flush()
                else:
                    close_held_stream.wait(30)
                return
            self.send_response(429 if self.path == "/api/v1/business-limit" else 200)
            self.end_headers()
            self.wfile.write(b"upstream")

        def do_POST(self):
            """读取透传的大于 Nginx 默认上限的上传。"""
            length = int(self.headers["Content-Length"])
            assert len(self.rfile.read(length)) == length
            self.send_response(201)
            self.end_headers()

    servers = []
    try:
        for name in ("public", "admin"):
            s = socketserver.ThreadingUnixStreamServer(str(directory / (name + ".sock")), Handler)
            servers.append(s)
            threading.Thread(target=s.serve_forever, daemon=True).start()
        yield records, release
    finally:
        release.set()
        close_held_stream.set()
        for s in servers:
            s.shutdown()
            s.server_close()


@pytest.mark.skipif(shutil.which("nginx") is None, reason="需要真实 Nginx")
def test_live_edge_routes_reload_proxy_and_accounting(tmp_path, monkeypatch):
    """覆盖真实 Nginx 限流、可信代理、流式转发，并区分上游 429 和网关 502。"""
    public, local = unused_port(), unused_port()
    shared, work = tmp_path / "shared", tmp_path / "work"
    shared.mkdir()
    monkeypatch.setenv("MYCOMFY_GATEWAY_DIR", str(shared))
    monkeypatch.setenv("MYCOMFY_GATEWAY_RUN_DIR", str(work))
    monkeypatch.setenv("MYCOMFY_CORE_SOCKET_DIR", str(tmp_path))
    monkeypatch.setenv("MYCOMFY_TRUSTED_PROXY_CIDRS", "127.0.0.1/32")
    monkeypatch.setenv("MYCOMFY_API_PORT", str(public))
    monkeypatch.setenv("MYCOMFY_LOCAL_PORT", str(local))
    rules = [
        {"listener": listener, "method": method, "path": route, "edge_rpm": rate, "edge_burst": 0}
        for listener, method, route, rate in [
            ("public", "GET", "/api/v1/jobs/{id}", 1),
            ("public", "GET", "/api/v1/jobs/list", 0),
            ("local", "GET", "/local/v1/jobs/{id}", 1),
            ("public", "GET", "/api/v1/events", 0),
            ("public", "POST", "/api/v1/assets", 0),
            ("public", "GET", "/api/v1/business-limit", 0),
            ("public", "GET", "/api/v1/disconnected", 0),
        ]
    ]

    def publish():
        """模拟 Core 原子发布更新，返回可在状态心跳中观察的摘要。"""
        value = snapshot(rules)
        candidate = shared / ".write"
        candidate.write_text(json.dumps(value))
        os.replace(candidate, shared / "rules.json")
        return value["revision"]

    def applied(revision):
        """从实际应用状态确认规则生效。"""
        status = json.loads((shared / "status.json").read_bytes())
        assert status["revision"] == revision and status["error"] is None, status

    def request(path, *, port=public, source="127.0.0.1", xff="192.0.2.1", method="GET", body=None):
        """向实际对外监听端口请求，可控制 TCP 连接来源和转发头。"""
        authority = "studio.example:9443" if port == public else "admin.example:9443"
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3, source_address=(source, 0))
        try:
            conn.request(
                method,
                path,
                body,
                {
                    "X-Forwarded-For": xff,
                    "Host": authority,
                    "Origin": f"https://{authority}",
                },
            )
            res = conn.getresponse()
            return res.status, dict(res.getheaders()), res.read()
        finally:
            conn.close()

    first = publish()
    with upstream(tmp_path) as (records, release):
        proc = subprocess.Popen([sys.executable, "-m", "mycomfy.gateway"])
        try:
            wait_for(lambda: applied(first))
            assert request("/api/v1/jobs/secret-id?q=secret-token")[0] == 200
            status, headers, body = request("/api/v1/jobs/another-id")
            assert status == 429 and json.loads(body)["error"]["code"] == "EDGE_RATE_LIMIT"
            assert int(headers["Retry-After"]) == 60
            assert request("/api/v1/jobs/list")[0] == 200
            assert request("/api/v1/jobs/distinct", xff="192.0.2.2")[0] == 200
            assert records[-1][1:] == (
                "192.0.2.2",
                "studio.example:9443",
                "https://studio.example:9443",
                None,
            )
            # 非信任的实际对端不能用自填的 XFF 绕过同一桶。
            assert request("/api/v1/jobs/direct", source="127.0.0.2", xff="192.0.2.11")[0] == 200
            assert records[-1][1] == "127.0.0.2"
            assert request("/api/v1/jobs/direct", source="127.0.0.2", xff="192.0.2.12")[0] == 429
            assert request("/local/v1/jobs/1", port=local)[0] == 200
            assert records[-1][1:] == (
                None,
                "admin.example:9443",
                "https://admin.example:9443",
                "for=unknown",
            )
            assert request("/local/v1/jobs/2", port=local)[0] == 429
            assert request("/local/v1/jobs/3", port=local, xff="192.0.2.3")[0] == 200
            assert request("/api/v1/business-limit")[2] == b"upstream"
            assert request("/api/v1/disconnected")[0] == 502
            assert request("/api/v1/assets", method="POST", body=b"x" * 1_100_000)[0] == 201
            conn = http.client.HTTPConnection("127.0.0.1", public, timeout=2)
            conn.request("GET", "/api/v1/events")
            events = conn.getresponse()
            assert events.read(len(b"data: live\n\n")) == b"data: live\n\n"
            # 即使新快照无效也继续执行原规则。修复快照后热加载，不再阻挡该 URL。
            (shared / "rules.json").write_text("{}")

            def rejected_snapshot():
                """核对无效快照未取代运行中的修订，并已向管理端报告失败。"""
                state = json.loads((shared / "status.json").read_bytes())
                assert state["revision"] == first and state["error"]

            wait_for(rejected_snapshot)
            assert request("/api/v1/jobs/another-id")[0] == 429
            rules[0]["edge_rpm"] = 0
            last = publish()
            wait_for(lambda: applied(last))
            assert request("/api/v1/jobs/another-id")[0] == 200
            # 新 worker 已确认运行新规则，此时旧 worker 上同一条 SSE 无需重连即可收到下一事件。
            release.set()
            assert events.read(len(b"data: after-reload\n\n")) == b"data: after-reload\n\n"
            events.close()
            conn.close()
            subprocess.run([sys.executable, "-m", "mycomfy.gateway", "health"], check=True)
            # 上游这条 SSE 会保持 30 秒，即使它继续工作，网关也必须在容器停机期限内退出。
            hold = http.client.HTTPConnection("127.0.0.1", public, timeout=10)
            hold.request("GET", "/api/v1/hold-open")
            ongoing = hold.getresponse()
            assert ongoing.read(len(b"data: live\n\n")) == b"data: live\n\n"
            proc.terminate()
            proc.wait(timeout=8)
            ongoing.close()
            hold.close()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
        assert proc.returncode == 0
    with sqlite3.connect(shared / "edge.sqlite3") as db:
        assert db.execute(
            "SELECT SUM(rejected),SUM(gateway_failures) FROM edge_minutes"
        ).fetchone() == (4, 1)
        assert db.execute(
            "SELECT requests,status_4xx,rejected FROM edge_minutes WHERE path='/api/v1/business-limit'"
        ).fetchone() == (1, 1, 0)
        assert db.execute(
            "SELECT requests FROM edge_minutes WHERE path='/api/v1/events'"
        ).fetchone() == (1,)
    contents = b"".join(p.read_bytes() for p in (shared / "journal").glob("*.log"))
    assert contents and b"secret-id" not in contents and b"secret-token" not in contents


@pytest.mark.skipif(shutil.which("nginx") is None, reason="需要真实 Nginx")
def test_live_admin_blocks_proxy_downgrade(tmp_path, monkeypatch):
    """通过真实 Nginx 和 Core 私有套接字核对本地直连、HTTPS 管理来源及回环 Host 降级。"""
    from mycomfy.api.server import api_server
    from mycomfy.config import Settings
    from mycomfy.database.schema import migrate

    public, local = unused_port(), unused_port()
    shared, sockets = tmp_path / "shared", tmp_path / "sockets"
    monkeypatch.setenv("MYCOMFY_GATEWAY_DIR", str(shared))
    monkeypatch.setenv("MYCOMFY_GATEWAY_RUN_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("MYCOMFY_CORE_SOCKET_DIR", str(sockets))
    monkeypatch.setenv("MYCOMFY_API_PORT", str(public))
    monkeypatch.setenv("MYCOMFY_LOCAL_PORT", str(local))
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/core.sqlite",
        data_root=tmp_path / "data",
        public_origin="https://studio.example",
        admin_origin="https://admin.example",
    )
    migrate(settings.database_url)

    def get(headers):
        """对相同的管理监听器发送指定头，检查 Core 最终授予或拒绝的真实 HTTP 响应。"""
        connection = http.client.HTTPConnection("127.0.0.1", local, timeout=4)
        try:
            connection.request("GET", "/local/v1/strategies", headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    with api_server(settings, "127.0.0.1", 0, 0, socket_dir=sockets) as (core, listeners):
        thread = threading.Thread(target=core.run, kwargs={"sockets": listeners}, daemon=True)
        thread.start()
        gateway = subprocess.Popen([sys.executable, "-m", "mycomfy.gateway"])
        try:
            local_host = {"Host": f"localhost:{local}"}

            def direct_admin_ready():
                """等待全链路就绪并确认普通本机访问可读模板列表。"""
                status, body = get(local_host)
                assert status == 200 and body["items"] == []

            wait_for(direct_admin_ready)

            proxy_headers = {
                "Forwarded": "for=192.0.2.2;proto=https;host=admin.example",
                "X-Forwarded-For": "192.0.2.2",
                "X-Forwarded-Host": "admin.example",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Port": "443",
                "X-Real-IP": "192.0.2.2",
            }
            # 即使只携带任意一个转发头，外层代理改写成 localhost 后也无法取得直连权限。
            for name, value in proxy_headers.items():
                status, body = get({**local_host, name: value})
                assert status == 403 and body["error"]["code"] == "LOCAL_ONLY", (name, body)
            assert (
                get({**proxy_headers, "Host": "admin.example", "Origin": "https://admin.example"})[
                    0
                ]
                == 200
            )
            assert (
                get({**proxy_headers, "Host": "admin.example", "Origin": "https://other.example"})[
                    0
                ]
                == 403
            )
        finally:
            gateway.terminate()
            try:
                gateway.wait(timeout=8)
            finally:
                core.should_exit = True
                thread.join(8)
