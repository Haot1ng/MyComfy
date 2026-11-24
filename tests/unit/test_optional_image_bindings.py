"""验证可选上传的发布约束，以及按本次实际上传内容裁剪工作流的行为。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from mycomfy.artifacts.storage import LocalStore
from mycomfy.engine.adapter import prepare_workflow
from mycomfy.errors import ApiError
from mycomfy.generation.strategy import validate_strategy


@pytest.fixture
def template():
    """构造一个有三路参考图、可选 mask 连线及普通数组参数的第三方消费节点。"""
    loaders = {
        key: {"class_type": "LoadImage", "inputs": {"image": "template.png"}}
        for key in ("a", "b", "c")
    }
    bindings = [
        {"input": key, "kind": "asset", "node_id": key, "node_input": "image"} for key in loaders
    ]
    for binding in bindings[1:]:
        binding["omit_node_if_missing"] = True
    return {
        "strategy_key": "test-reference",
        "name": "参考图",
        "workflow": {
            **loaders,
            "render": {
                "class_type": "ThirdPartyReferenceVideo",
                "inputs": {
                    **{key: [key, 0] for key in loaders},
                    "mask_b": ["b", 1],
                    "literal": [["b", 0]],
                    "switches": ["c", False],
                    "params": [],
                },
            },
            "save": {
                "class_type": "SaveVideo",
                "inputs": {"filename_prefix": "", "video": ["render", 0]},
            },
        },
        "input_schema": {
            "type": "object",
            "properties": {
                **{key: {"type": "string"} for key in loaders},
                "params": {"type": "array"},
            },
            "required": ["a"],
        },
        "bindings": bindings
        + [{"input": "params", "kind": "value", "node_id": "render", "node_input": "params"}],
        "output_contract": [{"node_id": "save", "role": "video", "media_types": ["video/mp4"]}],
        "requirements": {},
        "progress_mapping": [],
        "scheduling": {},
        "retry": {},
        "artifacts": {},
    }


@pytest.mark.parametrize("selected", [("a",), ("a", "b"), ("a", "c"), ("a", "b", "c")])
def test_prepare_prunes_only_omitted_loaders_and_direct_edges(template, tmp_path, selected):
    """逐组合检查节点、直接连线及实际文件，在另一执行重试时仍使用原版本。"""
    content = validate_strategy(template)
    frozen = deepcopy(content)
    store = LocalStore(tmp_path)
    store.root.mkdir()
    assets = {}
    inputs = {"params": ["b", 0]}  # 用户数组即使恰好长得像引用也必须保留。
    for key in selected:
        asset_id = f"asset-{key}"
        assets[asset_id] = SimpleNamespace(id=asset_id, object_key=asset_id, media_type="image/png")
        store.path(asset_id).write_bytes(key.encode())
        inputs[key] = asset_id

    workspace = {"input_dir": tmp_path / "in", "output_dir": tmp_path / "out"}
    for attempt in ("first", "retry"):
        execution = SimpleNamespace(id=attempt, workspace=workspace)
        graph = prepare_workflow(execution, content, inputs, assets, store)
        render = graph["render"]["inputs"]
        assert set(graph) == {"save", "render", *selected}
        assert {key: render[key] for key in ("a", "b", "c") if key in render} == {
            key: [key, 0] for key in selected
        }
        assert render.get("mask_b") == (["b", 1] if "b" in selected else None)
        assert render["literal"] == [["b", 0]] and render["switches"] == ["c", False]
        assert render["params"] == ["b", 0]
        assert graph["save"]["inputs"]["video"] == ["render", 0]
        # 只有本次提供的素材会物化，且图中指向本次执行实际可读的文件。
        for key in selected:
            dest = graph[key]["inputs"]["image"]
            assert dest == f"{attempt}/asset-{key}.png"
            assert (workspace["input_dir"] / dest).read_bytes() == key.encode()
        assert content == frozen


def test_legacy_bindings_keep_defaults_and_serialized_shape(template, tmp_path):
    """已有绑定缺省保留工作流原值，显式关闭裁剪也维持相同的规范内容。"""
    del template["bindings"][1]["omit_node_if_missing"]
    content = validate_strategy(template)
    assert "omit_node_if_missing" not in content["bindings"][1]
    template["bindings"][1]["omit_node_if_missing"] = False
    assert validate_strategy(template) == content
    store = LocalStore(tmp_path)
    store.root.mkdir()
    store.path("a").touch()
    execution = SimpleNamespace(
        id="first", workspace={"input_dir": tmp_path / "in", "output_dir": tmp_path / "out"}
    )
    assets = {"a": SimpleNamespace(id="a", object_key="a", media_type="image/png")}
    graph = prepare_workflow(execution, content, {"a": "a"}, assets, store)
    assert graph["b"]["inputs"]["image"] == "template.png"
    assert graph["render"]["inputs"]["b"] == ["b", 0]


@pytest.mark.parametrize(
    "invalid",
    [
        "required",
        "array",
        "boolean_schema",
        "value",
        "class",
        "socket",
        "shared_node",
        "output",
        "non_bool",
    ],
)
def test_invalid_omissions_are_rejected_at_publication(template, invalid):
    """拒绝会吞掉必填字段、其他绑定或输出的裁剪，以及非布尔开关。"""
    binding = template["bindings"][1]
    schema = template["input_schema"]
    if invalid == "required":
        schema["required"].append("b")
    elif invalid == "array":
        schema["properties"]["b"] = {"type": "array", "items": {"type": "string"}}
    elif invalid == "boolean_schema":
        schema["properties"]["b"] = True
    elif invalid == "value":
        binding["kind"] = "value"
    elif invalid == "class":
        template["workflow"]["b"]["class_type"] = "LoadImageWithEffects"
    elif invalid == "socket":
        template["workflow"]["b"]["inputs"]["other"] = ""
        binding["node_input"] = "other"
    elif invalid == "shared_node":
        template["workflow"]["b"]["inputs"]["other"] = ""
        template["bindings"].append(
            {**binding, "node_input": "other", "omit_node_if_missing": False}
        )
    elif invalid == "output":
        template["workflow"]["b"]["inputs"]["filename_prefix"] = ""
        template["output_contract"][0]["node_id"] = "b"
    else:
        binding["omit_node_if_missing"] = "true"
    with pytest.raises(ApiError) as error:
        validate_strategy(template)
    assert error.value.details[0]["path"].startswith("/bindings/1")
