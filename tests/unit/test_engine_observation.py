"""核对 ComfyUI 历史和队列端点独立可用时的执行判定。"""

import httpx
import pytest

from mycomfy.engine.adapter import ComfyAdapter, Observation, Unconfirmed


@pytest.fixture
def engine():
    """以可配置响应替代 HTTP 服务，并记录 Worker 发出的查询。"""
    replies = {}
    paths = []

    def handle(request):
        """按路径返回测试响应或模拟传输故障。"""
        path = request.url.path
        paths.append(path)
        reply = replies[path]
        if isinstance(reply, Exception):
            raise reply
        return reply

    adapter = ComfyAdapter(
        {"base_url": "http://127.0.0.1:8188"}, transport=httpx.MockTransport(handle)
    )
    try:
        yield adapter, replies, paths
    finally:
        adapter.close()


@pytest.mark.parametrize(
    ("history", "error_type", "queue_field", "stage"),
    [
        (httpx.ReadTimeout("history timed out"), "ReadTimeout", "queue_running", "running"),
        (httpx.Response(503), "HTTPStatusError", "queue_pending", "engine_queue"),
        (httpx.Response(200, text="invalid JSON"), "JSONDecodeError", "queue_running", "running"),
        (httpx.Response(200, json={"p": None}), "TypeError", "queue_running", "running"),
    ],
)
def test_queue_can_confirm_activity_after_history_fails(
    engine, monkeypatch, history, error_type, queue_field, stage
):
    """历史超时、HTTP 错误或无效 JSON 都可由队列中同一个 prompt 的记录补充。"""
    adapter, replies, paths = engine
    replies["/history/p"] = history
    replies["/queue"] = httpx.Response(200, json={queue_field: [[0, "other"], [1, "p"]]})
    ticks = iter([10, 10.3, 10.5, 10.52])
    monkeypatch.setattr("mycomfy.engine.adapter.monotonic", lambda: next(ticks))
    observation = adapter.observe("p")
    assert observation == Observation("RUNNING", stage=stage)
    # 完整比对摘要确保没有保存请求、响应正文或异常文本。
    history_detail = {"duration_ms": 300, "status": "error", "error": error_type}
    if isinstance(history, httpx.Response):
        history_detail["http_status"] = history.status_code
    assert observation.lookup == {
        "history": history_detail,
        "queue": {
            "duration_ms": 20,
            "status": queue_field.replace("queue_", "matched_"),
            "http_status": 200,
        },
    }
    assert paths == ["/history/p", "/queue"]


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ({"completed": True, "status_str": "success"}, "SUCCEEDED"),
        ({"status_str": "error"}, "FAILED"),
        ({"status_str": "error", "messages": [["execution_interrupted", {}]]}, "INTERRUPTED"),
    ],
)
def test_terminal_history_completes_observation(engine, status, outcome):
    """已经确认终态时直接返回对应产物，即使队列暂时不可读。"""
    adapter, replies, paths = engine
    outputs = {"node": {"videos": []}}
    replies["/history/p"] = httpx.Response(200, json={"p": {"status": status, "outputs": outputs}})
    observation = adapter.observe("p")
    assert observation == Observation(outcome, outputs)
    assert observation.lookup.keys() == {"history"}
    assert observation.lookup["history"]["status"] == outcome.lower()
    assert observation.lookup["history"]["http_status"] == 200
    assert observation.lookup["history"]["duration_ms"] >= 0
    assert paths == ["/history/p"]


@pytest.mark.parametrize(
    ("history_code", "queue_code"), [(503, 200), (200, 503), (503, 503), (200, 200)]
)
def test_absent_prompt_distinguishes_lookup_failure_from_unknown(engine, history_code, queue_code):
    """其他任务的队列记录不能确认本执行，任何端点失败都应保留查询异常。"""
    adapter, replies, paths = engine
    replies["/history/p"] = httpx.Response(history_code, json={})
    replies["/queue"] = httpx.Response(queue_code, json={"queue_running": [[0, "other"]]})
    if history_code == queue_code == 200:
        observation = adapter.observe("p")
        assert observation == Observation("UNKNOWN")
        lookup = observation.lookup
    else:
        with pytest.raises(Unconfirmed, match="ENGINE_LOOKUP_UNAVAILABLE") as error:
            adapter.observe("p")
        lookup = error.value.lookup
    for name, code in (("history", history_code), ("queue", queue_code)):
        detail = lookup[name]
        assert detail["http_status"] == code and detail["duration_ms"] >= 0
        assert detail["status"] == ("missing" if code == 200 else "error")
        assert detail.get("error") == (None if code == 200 else "HTTPStatusError")
    assert paths == ["/history/p", "/queue"]


@pytest.mark.parametrize("queue", [[], {}, {"queue_running": {}}, {"queue_pending": [0]}])
def test_invalid_queue_does_not_turn_incomplete_history_into_absence(engine, queue):
    """两端均无决定性记录时区分历史尚未完成与队列结构损坏。"""
    adapter, replies, _ = engine
    replies["/history/p"] = httpx.Response(200, json={"p": {"status": {"completed": False}}})
    replies["/queue"] = httpx.Response(200, json=queue)
    with pytest.raises(Unconfirmed) as error:
        adapter.observe("p")
    assert error.value.lookup["history"]["status"] == "incomplete"
    assert error.value.lookup["queue"]["status"] == "error"
    assert error.value.lookup["queue"]["error"] == (
        "ValueError" if isinstance(queue, dict) else "TypeError"
    )
