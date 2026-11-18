"""使用用户 API 替身验证目标机验收脚本的证据和失败报告。"""

import hashlib
import importlib
import json
from functools import partial
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError


@pytest.fixture
def tool(monkeypatch):
    """按脚本在工作区的路径载入可测试的命令入口。"""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    return importlib.import_module("accept_gpu")


def case_data():
    """构造明确标识为测试替身的用例清单。"""
    return {
        "environment": {
            "gpu_model": "test-fixture",
            "vram_mib": 1,
            "driver": "fake",
            "comfyui_commit": "fake",
            "node_versions": {},
            "model_sha256": {"fixture": "0" * 64},
            "disk_type": "test",
            "instance_count": 1,
            "condition": "warm",
            "preparation": "fixture",
        },
        "tasks": [
            {
                "strategy_version_id": "sv_fixture",
                "inputs": {"seed": 1},
                "uploads": {"reference_asset_id": "reference.bin"},
                "frames": 1,
                "input_dimensions": "1x1",
                "encoding": "test",
                "seed": 1,
            }
        ],
    }


@pytest.mark.parametrize("corrupt", [False, True])
def test_target_runner_records_real_hash_and_protocol(tmp_path, tool, monkeypatch, corrupt):
    """模拟完整 API 交互，验证首字节探测、摘要失配、错误样本清理及报告脱敏。"""
    case_path = tmp_path / "case.json"
    case_path.write_text(json.dumps(case_data()))
    (tmp_path / "reference.bin").write_bytes(b"fixture-input")
    output, media = tmp_path / "report.json", tmp_path / "media"
    fixture_video = b"sample video fixture"
    expected_sha = hashlib.sha256(fixture_video).hexdigest()
    operations = []
    observed_key = []

    def reply(request):
        """以固定响应实现脚本所需的用户 API，记录实际收到的请求顺序。"""
        path = request.url.path
        operations.append((path, request.method, request.headers.get("range")))
        assert request.headers["origin"] == "https://test.local"
        if path == "/api/v1/sessions":
            return httpx.Response(
                200,
                json={"csrf_token": "secret-for-fixture", "session_generation": "7"},
                headers={"Set-Cookie": "mycomfy_session=test-cookie; Path=/; Secure"},
            )
        assert "mycomfy_session=test-cookie" in request.headers["cookie"]
        if request.method != "GET":
            assert request.headers["x-csrf-token"] == "secret-for-fixture"
            assert request.headers["x-session-generation"] == "7"
        if path == "/api/v1/strategy-versions/sv_fixture":
            return httpx.Response(
                200,
                json={
                    "strategy_version_id": "sv_fixture",
                    "content_digest": "fake-version",
                    "version": 3,
                    "accepting": True,
                },
            )
        if path == "/api/v1/assets":
            return httpx.Response(
                201,
                json={
                    "asset_id": "a_input",
                    "digest": hashlib.sha256(b"fixture-input").hexdigest(),
                },
            )
        if path == "/api/v1/batches":
            data = json.loads(request.content)
            assert data["tasks"][0]["inputs"] == {"seed": 1, "reference_asset_id": "a_input"}
            observed_key.append(data["client_batch_key"])
            # 发起真正的受理之前，用户已经有可追踪的幂等键。
            assert (
                json.loads(output.read_bytes())["batches"][0]["client_batch_key"] == observed_key[0]
            )
            return httpx.Response(
                202,
                json={"batch_id": "b1", "tasks": [{"job_id": "j1", "client_task_key": "task-1"}]},
                headers={"X-Request-ID": "req_batch"},
            )
        if path == "/api/v1/jobs/j1":
            return httpx.Response(
                200,
                json={
                    "state": "SUCCEEDED",
                    "strategy_version_id": "sv_fixture",
                    "current_execution_id": "e1",
                    "attempt_no": 1,
                    "assets": [
                        {
                            "asset_id": "a_output",
                            "media_type": "video/mp4",
                            "complete": True,
                            "available": True,
                            "size": len(fixture_video),
                            "digest": expected_sha,
                        }
                    ],
                },
            )
        if path == "/api/v1/assets/a_output/content":
            if request.headers.get("range") == "bytes=0-0":
                return httpx.Response(
                    206,
                    headers={"content-range": f"bytes 0-0/{len(fixture_video)}"},
                    content=fixture_video[:1],
                )
            assert request.url.params["download"] == "1"
            return httpx.Response(200, content=b"bad" if corrupt else fixture_video)
        assert path == "/api/v1/sessions/current" and request.method == "DELETE"
        return httpx.Response(204)

    monkeypatch.setenv("MYCOMFY_ACCEPT_PASSWORD", "fixture-password")
    monkeypatch.setattr(
        tool.httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(reply))
    )
    code = tool.main(
        [
            "--url",
            "https://test.local",
            "--username",
            "fixture",
            "--case",
            str(case_path),
            "--output",
            str(output),
            "--media-dir",
            str(media),
        ]
    )
    report = json.loads(output.read_bytes())
    (batch,) = report["batches"]
    (sample,) = batch["jobs"]
    (record,) = sample["files"]
    assert batch["request_id"] == "req_batch" and observed_key == [batch["client_batch_key"]]
    assert sample["timeline"][0]["execution_id"] == "e1"
    assert report["requires_target_evidence"]
    for secret in ("fixture-password", "test-cookie", "secret-for-fixture"):
        assert secret not in output.read_text()
    if corrupt:
        assert code == 1 and report["status"] == "failed"
        assert record["download"]["verified"] is False
        assert record["download"]["sha256"] == hashlib.sha256(b"bad").hexdigest()
        assert list(media.iterdir()) == [] and report["summary"]["verified_successes"] == 0
    else:
        assert code == 0 and report["status"] == "delivery_verified"
        assert record["download"]["sha256"] == expected_sha and record["download"]["verified"]
        assert (media / record["download"]["local_sample"]).read_bytes() == fixture_video
        assert report["summary"]["delivery_p95_seconds"] >= 0
        assert report["summary"]["verified_successes"] == 1
    assert operations[-1][0] == "/api/v1/sessions/current"


def test_cold_trials_require_fresh_environment(tool):
    """防止将同一连续运行里的预热样本误记为冷启动样本。"""
    raw = case_data()
    raw["environment"]["condition"] = "cold"
    raw["repetitions"] = 2
    with pytest.raises(ValidationError, match="单独准备"):
        tool.Case.model_validate(raw)
