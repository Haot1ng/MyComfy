"""核对可导入的 H3 分段模板、部署清单和节点接口。"""

import json
from pathlib import Path

import pytest

from mycomfy.engine import bundled
from mycomfy.engine.comfy_nodes import NODE_CLASS_MAPPINGS
from mycomfy.generation.strategy import validate_strategy


@pytest.mark.parametrize("size", [384, 640])
def test_segmented_template_matches_deployed_node(size):
    """验证每个分辨率都能发布，且部署的节点能消费五图及全套分镜提示词。"""
    examples = Path(__file__).resolve().parents[2] / "examples" / "strategies"
    key = f"h3-five-image-20s-segmented-{size}"
    strategy = validate_strategy(json.loads((examples / f"{key}.strategy.json").read_bytes()))
    assert strategy["strategy_key"] == key
    # 调度器按精确版本匹配；节点代码改动后必须同步更新两个模板。
    requirements = strategy["requirements"]
    assert requirements["comfyui"] == bundled.H3_COMFYUI
    assert requirements["nodes"] == {bundled.H3_NODE: bundled.node_digest(bundled.SOURCE)}

    graph = strategy["workflow"]
    (output,) = strategy["output_contract"]
    assert (output["width"], output["height"], output["fps"]) == (size, size, 24)
    assert output["min_duration"] == output["max_duration"] == 20
    node_id = output["node_id"]
    node = graph[node_id]
    assert node["class_type"] == bundled.H3_NODE
    implementation = NODE_CLASS_MAPPINGS[bundled.H3_NODE]
    assert implementation.OUTPUT_NODE
    inputs = node["inputs"]
    assert inputs.keys() == implementation.INPUT_TYPES()["required"].keys()
    assert output["path_input"] == "filename_prefix"
    assert inputs["seed"] == 2026112301

    schema = strategy["input_schema"]
    images = [f"image_{i}" for i in range(1, 6)]
    prompts = ["prompt", *(f"shot_{i}_prompt" for i in range(1, 5))]
    assert schema["required"] == images
    assert set(schema["properties"]) == set(images + prompts)
    bindings = {entry["input"]: entry for entry in strategy["bindings"]}
    for field in images:
        loader = bindings[field]["node_id"]
        assert bindings[field]["kind"] == "asset" and bindings[field]["node_input"] == "image"
        assert graph[loader]["class_type"] == "LoadImage"
        scale = graph[inputs[field][0]]
        assert scale["class_type"] == "ImageScale"
        assert scale["inputs"]["width"] == scale["inputs"]["height"] == size
        assert scale["inputs"]["crop"] == "center"
        first = graph[scale["inputs"]["image"][0]]
        assert first["class_type"] == "ImageFromBatch"
        assert first["inputs"] == {"image": [loader, 0], "batch_index": 0, "length": 1}
    for field in prompts:
        assert inputs[field] and inputs[field] == schema["properties"][field]["default"]
        assert bindings[field] == {
            "input": field,
            "kind": "value",
            "node_id": node_id,
            "node_input": field,
        }
