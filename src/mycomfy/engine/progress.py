"""从 ComfyUI 的每执行 WebSocket 采集可验证的节点阶段。"""

import json
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect


class ProgressChannel:
    def __init__(self, runtime, execution_id):
        """保存执行专属会话地址，在首次读取时连接并在断线后延迟重连。"""
        endpoint = urlsplit(runtime["base_url"])
        if endpoint.scheme != "http" or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("ComfyUI progress endpoint must be on loopback")
        self.url = urlunsplit(
            ("ws", endpoint.netloc, "/ws", urlencode({"clientId": execution_id}), "")
        )
        self.socket = None
        self.next_connect = 0.0

    def close(self):
        """关闭属于原执行的连接，防止迟到事件进入另一个执行。"""
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def sample(self, prompt_id, mapping):
        """返回当前 prompt 最新的已映射节点采样，忽略二进制预览和无归属消息。"""
        if self.socket is None and time.monotonic() < self.next_connect:
            return None
        latest = None
        try:
            if self.socket is None:
                self.socket = connect(
                    self.url,
                    proxy=None,
                    open_timeout=1,
                    close_timeout=0.25,
                    max_queue=16,
                    max_size=4 * 1024 * 1024,
                )
            # 限制单轮读取次数，繁忙引擎不会让其他实例失去调度机会。
            for _ in range(128):
                try:
                    raw = self.socket.recv(timeout=0)
                except TimeoutError:
                    break
                if not isinstance(raw, str) or not prompt_id:
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(message, dict) or message.get("type") not in {
                    "executing",
                    "progress",
                }:
                    continue
                data = message.get("data")
                if not isinstance(data, dict) or data.get("prompt_id") != prompt_id:
                    continue
                node_id = data.get("node")
                if not isinstance(node_id, (int, str)):
                    continue
                stage = mapping.get(str(node_id))
                if stage:
                    latest = {"stage": stage, "node_id": str(node_id)}
                    value, total = data.get("value"), data.get("max")
                    if (
                        type(value) in (int, float)
                        and type(total) in (int, float)
                        and 0 <= value <= total
                        and total > 0
                    ):
                        # 这是节点自身进度，不能当作整个视频任务的百分比。
                        latest["node_progress"] = value / total
        except (OSError, ValueError, WebSocketException, TimeoutError):
            self.close()
            self.next_connect = time.monotonic() + 5
        return latest
