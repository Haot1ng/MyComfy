"""验证节点清单未确认、失败退避及跨代次采集的边界。"""

from concurrent.futures import wait
from functools import partial

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mycomfy.database.models import Base, EngineInstance, EngineNodeCatalog
from mycomfy.engine import node_catalog


@pytest.mark.parametrize(
    ("payload", "limit"),
    [
        ({}, 256),
        ({"exec_info": {"queue_remaining": 0}}, 256),
        ({"Valid": {"input": {}}, "Broken": None}, 256),
        ({"Large": {"input": {"required": {"file": ["x" * 256]}}}}, 64),
    ],
)
def test_unusable_object_info_is_never_an_empty_or_partial_catalog(monkeypatch, payload, limit):
    """元数据缺失、响应格式错误和大小超限均不能冒充一份完整的节点清单。"""
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    monkeypatch.setattr(node_catalog.httpx, "Client", partial(httpx.Client, transport=transport))
    monkeypatch.setattr(node_catalog, "MAX_OBJECT_INFO_BYTES", limit)
    with pytest.raises(ValueError):
        node_catalog.fetch_node_catalog("http://127.0.0.1:8188")


def test_collector_retries_and_discards_late_results(monkeypatch):
    """短暂超时留待重试，采集过程中换代时只发布匹配当代的完整响应。"""
    sessions = sessionmaker(create_engine("sqlite://"), expire_on_commit=False)
    Base.metadata.create_all(sessions.kw["bind"])
    with sessions.begin() as db:
        db.add(EngineInstance(id="gpu0", generation=1, manifest={}))
    calls = []

    def fetch(url):
        """第一次超时，后续返回可区分的新旧代次清单。"""
        calls.append(url)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow startup")
        return {str(len(calls)): "custom_nodes.tests"}

    monkeypatch.setattr(node_catalog, "fetch_node_catalog", fetch)
    collector = node_catalog.NodeCatalogCollector(sessions)
    url = "http://127.0.0.1:8188"

    def start_and_wait(generation):
        """等待背景 HTTP 返回；保存结果仍要等下一轮管理循环。"""
        collector.observe("gpu0", generation, url)
        _, pending = wait([collector.pending["gpu0"][1]], timeout=2)
        assert not pending

    try:
        start_and_wait(1)
        collector.observe("gpu0", 1, url)
        collector.observe("gpu0", 1, url)
        assert len(calls) == 1 and not collector.pending
        with sessions() as db:
            assert db.get(EngineNodeCatalog, "gpu0") is None

        # 重试已完成，但虚拟 GPU 在管理器收割结果前切到了第二代。
        collector.next_attempt["gpu0"] = (1, 0)
        start_and_wait(1)
        with sessions.begin() as db:
            db.get(EngineInstance, "gpu0").generation = 2
        collector.observe("gpu0", 1, url)
        with sessions() as db:
            assert db.get(EngineNodeCatalog, "gpu0") is None

        start_and_wait(2)
        collector.observe("gpu0", 2, url)
        with sessions() as db:
            catalog = db.get(EngineNodeCatalog, "gpu0")
            assert catalog.generation == 2 and catalog.nodes == {"3": "custom_nodes.tests"}
    finally:
        collector.executor.shutdown()
        sessions.kw["bind"].dispose()
