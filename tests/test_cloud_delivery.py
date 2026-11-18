"""用真实 MySQL、回环 HTTP 引擎替身和可解码 MP4 验证跨进程协议。"""

import hashlib
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from mycomfy.api import create_app
from mycomfy.config import Settings
from mycomfy.db import db_now
from mycomfy.models import EngineInstance, Execution, Job
from mycomfy.storage import LocalStore
from mycomfy.worker import Worker


@contextmanager
def engine_double(input_dir, output_dir, clip):
    """按 ComfyUI 的队列和历史协议保存结果，在首次受理后主动断开回执连接。"""
    accepted = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            """忽略测试引擎的逐请求日志，断言失败由 pytest 报告。"""

        def reply(self, data):
            """返回一个带长度的 UTF-8 JSON 响应。"""
            content = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            """检查工作流确实引用了上传素材，产出文件并模拟首个回执丢失。"""
            assert self.path == "/prompt"
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt_id = payload["prompt_id"]
            assert prompt_id not in accepted
            workflow = payload["prompt"]
            source = input_dir / workflow["1"]["inputs"]["video"]
            assert source.read_bytes() == clip.read_bytes()
            prefix = Path(workflow["2"]["inputs"]["filename_prefix"])
            path = output_dir / prefix.with_suffix(".mp4")
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, path)
            accepted[prompt_id] = {
                "status": {"status_str": "success", "completed": True},
                "outputs": {
                    "2": {
                        "videos": [
                            {
                                "subfolder": str(prefix.parent),
                                "filename": path.name,
                                "type": "output",
                            }
                        ]
                    }
                },
            }
            if len(accepted) == 1:
                # 引擎已经持久化结果，客户端只观察到连接中断。
                self.close_connection = True
            else:
                self.reply({"prompt_id": prompt_id})

        def do_GET(self):
            """按原持久标识返回实际完成记录，供重启后的协调器核对。"""
            if self.path == "/queue":
                self.reply({"queue_running": [], "queue_pending": []})
                return
            prompt_id = self.path.removeprefix("/history/")
            self.reply({prompt_id: accepted[prompt_id]} if prompt_id in accepted else {})

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", accepted
        finally:
            server.shutdown()
            thread.join()


def video_strategy():
    """构造使用素材输入和视频输出的两节点测试契约。"""
    return {
        "strategy_key": "cloud-delivery",
        "name": "云端协议测试",
        "workflow": {
            "1": {"class_type": "TestInput", "inputs": {"video": ""}},
            "2": {"class_type": "TestSave", "inputs": {"source": ["1", 0], "filename_prefix": ""}},
        },
        "input_schema": {
            "type": "object",
            "properties": {"source": {"type": "string"}},
            "required": ["source"],
        },
        "bindings": [{"input": "source", "node_id": "1", "node_input": "video", "kind": "asset"}],
        "requirements": {"comfyui": "cloud-test"},
        "progress_mapping": [],
        "output_contract": [
            {
                "role": "final_video",
                "node_id": "2",
                "width": 64,
                "height": 64,
                "media_types": ["video/mp4"],
                "require_decodable": True,
            }
        ],
        "scheduling": {"base_priority": 5},
        "retry": {"max_retries": 0, "interval_seconds": 0},
        "artifacts": {"expose_after_cancel": False},
    }


@pytest.mark.mysql
def test_http_batch_to_readable_mp4_with_lost_receipt(mysql_sessions, tmp_path):
    """两个版本的同一批次经并发重放、回执丢失和 Worker 重建后仍各交付一次。"""
    token_file = tmp_path / "maintenance"
    token_file.write_text("temporary-test-token")
    token_file.chmod(0o600)
    settings = Settings(
        database_url=mysql_sessions.kw["bind"].url.render_as_string(hide_password=False),
        data_root=tmp_path / "files",
        public_origin="https://testserver",
        maintenance_token_file=token_file,
        poll_seconds=0.05,
    )
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=64x64:rate=8",
            "-t",
            "0.5",
            "-c:v",
            "mpeg4",
            "-threads",
            "1",
            str(clip),
        ],
        check=True,
    )
    input_dir, output_dir = tmp_path / "in", tmp_path / "out"
    input_dir.mkdir()
    output_dir.mkdir()
    with (
        engine_double(input_dir, output_dir, clip) as (url, accepted),
        TestClient(
            create_app(settings, local=True, sessions=mysql_sessions),
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 40100),
        ) as local,
        TestClient(
            create_app(settings, sessions=mysql_sessions), base_url=settings.public_origin
        ) as public,
    ):
        with mysql_sessions.begin() as db:
            db.add(
                EngineInstance(
                    id="gpu-test",
                    generation=1,
                    manifest={"comfyui": "cloud-test"},
                    configured=True,
                    accepting=True,
                    heartbeat_at=db_now(db),
                    runtime={
                        "base_url": url,
                        "input_dir": str(input_dir),
                        "output_dir": str(output_dir),
                    },
                )
            )
        local.headers["Authorization"] = "Bearer temporary-test-token"
        versions = []
        for _ in range(2):
            result = local.post("/local/v1/strategy-versions", json=video_strategy())
            assert result.status_code == 201, result.text
            versions.append(result.json()["strategy_version_id"])

        public.headers["Origin"] = settings.public_origin
        account = {"username": "cloud-user", "password": "for-cloud-tests"}
        assert public.post("/api/v1/accounts", json=account).status_code == 201
        session = public.post("/api/v1/sessions", json=account)
        assert session.status_code == 200, session.text
        public.headers.update(
            {
                "X-Session-Generation": session.json()["session_generation"],
                "X-CSRF-Token": session.json()["csrf_token"],
            }
        )
        content = clip.read_bytes()
        upload = public.post("/api/v1/assets", files={"file": ("input.mp4", content, "video/mp4")})
        assert upload.status_code == 201, upload.text
        batch = {
            "client_batch_key": "cloud-batch",
            "tasks": [
                {
                    "client_task_key": str(i),
                    "strategy_version_id": version,
                    "inputs": {"source": upload.json()["asset_id"]},
                }
                for i, version in enumerate(versions)
            ],
        }

        def submit(_):
            """以同一 Cookie 和幂等键重放批次并保留该次响应。"""
            return public.post("/api/v1/batches", json=batch)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, replay = pool.map(submit, range(2))
        assert first.status_code == replay.status_code == 202, (first.text, replay.text)
        assert first.json() == replay.json()
        assert first.headers["X-Request-ID"] != replay.headers["X-Request-ID"]
        # 首次发送丢失回执后每轮重新创建 Worker，要求从 MySQL 恢复全部进度。
        for _ in range(10):
            Worker(settings, mysql_sessions, LocalStore(settings.data_root)).tick()
            with mysql_sessions() as db:
                if (
                    db.scalar(select(func.count()).select_from(Job).where(Job.state == "SUCCEEDED"))
                    == 2
                ):
                    break
        assert len(accepted) == 2
        with mysql_sessions() as db:
            assert db.scalar(select(func.count()).select_from(Execution)) == 2
        for task, version in zip(first.json()["tasks"], versions, strict=True):
            detail = public.get(f"/api/v1/jobs/{task['job_id']}").json()
            assert detail["state"] == "SUCCEEDED", detail
            assert detail["strategy_version_id"] == version
            (asset,) = detail["assets"]
            assert asset["digest"] == hashlib.sha256(content).hexdigest()
            assert public.get(asset["content_url"], params={"download": 1}).content == content
            partial = public.get(asset["content_url"], headers={"Range": "bytes=4-15"})
            assert partial.status_code == 206 and partial.content == content[4:16]
