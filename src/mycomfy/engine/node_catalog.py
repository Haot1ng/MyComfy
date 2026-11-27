"""采集 ComfyUI 实际注册的节点，供业务机核对工作流。"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import select

from mycomfy.database.models import EngineInstance, EngineNodeCatalog

MAX_OBJECT_INFO_BYTES = 32 * 1024 * 1024
CATALOG_RETRY_SECONDS = 60


def fetch_node_catalog(base_url):
    """从本代次的 ComfyUI 获取完整节点表，只保留类型名和 Python 来源模块。"""
    # /object_info 还包含模型名和节点参数，限制读取时间和大小，仅在 GPU 机内处理原文。
    deadline = time.monotonic() + 30
    data = bytearray()
    with (
        httpx.Client(trust_env=False, timeout=httpx.Timeout(15, connect=2)) as client,
        client.stream(
            "GET", base_url + "/object_info", headers={"Accept-Encoding": "identity"}
        ) as response,
    ):
        response.raise_for_status()
        for chunk in response.iter_bytes():
            if len(data) + len(chunk) > MAX_OBJECT_INFO_BYTES or time.monotonic() > deadline:
                raise ValueError("节点元数据超过采集上限")
            data.extend(chunk)
    definitions = json.loads(data)
    if not isinstance(definitions, dict) or not definitions:
        raise ValueError("引擎未返回节点定义")
    nodes = {}
    for name, definition in definitions.items():
        if (
            not name
            or not isinstance(definition, dict)
            or not isinstance(definition.get("input"), dict)
        ):
            raise ValueError("引擎返回的节点定义不完整")
        module = definition.get("python_module")
        nodes[name] = module if isinstance(module, str) and module else None
    return nodes


class NodeCatalogCollector:
    def __init__(self, sessions):
        """限制并行采集数量，每个实例维护独立的进行中请求和重试时间。"""
        self.sessions = sessions
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="comfy-node-catalog")
        self.pending = {}
        self.next_attempt = {}

    def close(self):
        """取消尚未开始的采集，让进行中的限时请求随引擎停机结束。"""
        self.executor.shutdown(wait=False, cancel_futures=True)

    def observe(self, instance_id, generation, base_url):
        """在管理循环中发起或收割采集，避免节点元数据读取阻塞心跳和就绪探测。"""
        pending = self.pending.get(instance_id)
        if pending:
            sampled_generation, future = pending
            if not future.done():
                return
            del self.pending[instance_id]
            if sampled_generation == generation:
                try:
                    nodes = future.result()
                except (httpx.HTTPError, ValueError, RecursionError) as exc:
                    # 读取失败不发布空清单；保留“尚未验证”，到期再试。
                    if self.next_attempt.get(instance_id, (None, 0))[0] != generation:
                        logging.getLogger(__name__).warning(
                            "实例 %s 第 %s 代的节点清单暂不可用：%s，稍后自动重试",
                            instance_id,
                            generation,
                            type(exc).__name__,
                        )
                    self.next_attempt[instance_id] = (
                        generation,
                        time.monotonic() + CATALOG_RETRY_SECONDS,
                    )
                    return
                with self.sessions.begin() as db:
                    # 管理器或实例可能在采集期间重建，旧响应不能覆盖新代次。
                    current = db.scalar(
                        select(EngineInstance.generation)
                        .where(EngineInstance.id == instance_id)
                        .with_for_update()
                    )
                    if current != generation:
                        return
                    db.merge(
                        EngineNodeCatalog(
                            instance_id=instance_id, generation=generation, nodes=nodes
                        )
                    )
                self.next_attempt[instance_id] = (generation, float("inf"))
                return

        last_generation, due = self.next_attempt.get(instance_id, (None, 0))
        if last_generation == generation:
            if time.monotonic() < due:
                return
        else:
            # 管理器进程重启时复用已持久化的同代清单；单列查询不加载节点 JSON。
            with self.sessions() as db:
                persisted = db.scalar(
                    select(EngineNodeCatalog.generation).where(
                        EngineNodeCatalog.instance_id == instance_id
                    )
                )
            if persisted == generation:
                self.next_attempt[instance_id] = (generation, float("inf"))
                return
        self.pending[instance_id] = (generation, self.executor.submit(fetch_node_catalog, base_url))
