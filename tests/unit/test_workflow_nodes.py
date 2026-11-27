import pytest

from mycomfy.errors import ApiError
from mycomfy.generation import workflow_nodes
from mycomfy.generation.workflow_nodes import workflow_node_references


def test_api_node_names_and_strategy_wrapper():
    """API 图里的 workflow 与 nodes 可以是普通节点 ID，名称相同的执行类型合并显示。"""
    graph = {
        "workflow": {"class_type": "LoadImage", "inputs": {}},
        "nodes": {"class_type": "LoadImage", "inputs": {}},
        # API 图中的同名节点仍是引擎执行类型，虚拟节点过滤仅用于画布。
        "3": {"class_type": "Note", "inputs": {}},
    }
    expected = [
        {
            "class_type": "LoadImage",
            "node_ids": ["workflow", "nodes"],
            "source": "unknown",
            "package": None,
        },
        {"class_type": "Note", "node_ids": ["3"], "source": "unknown", "package": None},
    ]
    assert (
        workflow_node_references(graph) == workflow_node_references({"workflow": graph}) == expected
    )


def test_referenced_subgraphs_and_package_hints():
    """嵌套子图按实例标识定位，禁用实例、纯编辑器节点和未使用的子图都不引入依赖。"""
    graph = {
        "nodes": [
            {"id": 1, "type": "MarkdownNote"},
            {"id": 7, "type": "first"},
            {"id": 9, "type": "first"},
            {"id": 15, "type": "first", "mode": 2},
            {"id": 20, "type": "MissingBypass", "mode": 4},
            {
                "id": 29,
                "type": "PathchSageAttentionKJ",
                "properties": {"cnr_id": "comfyui-kjnodes"},
            },
            {"id": 40, "type": "SaveVideo", "properties": {"cnr_id": "comfy-core"}},
        ],
        "definitions": {
            "subgraphs": [
                {
                    "id": "first",
                    "nodes": [{"id": 2, "type": "second"}, {"id": 3, "type": "Reroute"}],
                },
                {
                    "id": "second",
                    "inputNode": {"id": -10},
                    "outputNode": {"id": -20},
                    "nodes": [
                        {
                            "id": 1,
                            "type": "MiniMaxH3TimelinePlanner",
                            "properties": {"aux_id": "Songssx/ComfyUI-MiniMaxH3-TimelineDirector"},
                        },
                    ],
                },
                {"id": "unused", "nodes": [{"id": 2, "type": "NotRequired"}]},
            ]
        },
    }
    assert workflow_node_references(graph) == [
        {
            "class_type": "MiniMaxH3TimelinePlanner",
            "node_ids": ["7:2:1", "9:2:1"],
            "source": "custom",
            "package": "Songssx/ComfyUI-MiniMaxH3-TimelineDirector",
        },
        {
            "class_type": "PathchSageAttentionKJ",
            "node_ids": ["29"],
            "source": "custom",
            "package": "comfyui-kjnodes",
        },
        {
            "class_type": "SaveVideo",
            "node_ids": ["40"],
            "source": "official",
            "package": "comfy-core",
        },
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"nodes": [{"id": 1, "type": []}]},
        {"nodes": [{"id": 1, "type": "a"}, {"id": "1", "type": "a"}]},
        {"1": {"inputs": {}, "class_type": ""}},
        {
            "nodes": [{"id": 1, "type": "loop"}],
            "definitions": {
                "subgraphs": [
                    {"id": "loop", "nodes": [{"id": 1, "type": "loop"}]},
                ]
            },
        },
    ],
)
def test_invalid_workflow_returns_client_error(payload):
    """无效节点、重复编号和子图环都作为输入错误返回。"""
    with pytest.raises(ApiError) as exc:
        workflow_node_references(payload)
    assert exc.value.status == 422


def test_bounds_repeated_subgraph_expansion(monkeypatch):
    """同一个定义的多次实例化共用节点数量上限。"""
    monkeypatch.setattr(workflow_nodes, "MAX_EXPANDED_NODES", 3)
    graph = {
        "nodes": [{"id": 1, "type": "group"}, {"id": 2, "type": "group"}],
        "definitions": {
            "subgraphs": [{"id": "group", "nodes": [{"id": 1, "type": "Custom"}]}],
        },
    }
    with pytest.raises(ApiError, match="工作流过大"):
        workflow_node_references(graph)
