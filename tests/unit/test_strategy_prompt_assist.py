"""核对可选提示词配置的旧版本兼容性和可外发字段的发布约束。"""

import json
from pathlib import Path

import pytest

from mycomfy.errors import ApiError
from mycomfy.generation.strategy import validate_strategy

PRESET = Path(__file__).resolve().parents[2] / "web/admin/presets/h3-i2v.strategy.json"


def test_absent_instructions_preserve_legacy_strategy_content():
    """未配置或显式关闭时，规范化结果与旧策略格式一致，可继续按原摘要寻址。"""
    raw = json.loads(PRESET.read_text())
    assert validate_strategy(raw)["prompt_assist"]["fields"] == ["prompt"]
    del raw["prompt_assist"]
    original = validate_strategy(raw)
    assert "prompt_assist" not in original
    assert validate_strategy({**raw, "prompt_assist": None}) == original


@pytest.mark.parametrize(
    "invalid", ["missing", "asset", "repeated", "choices", "constant", "number", "blank", "empty"]
)
def test_assist_rejects_unusable_fields_and_instructions(invalid):
    """发布时拒绝不存在、绑定为素材或不可自由编辑的字段及空白配置。"""
    raw = json.loads(PRESET.read_text())
    assist = raw["prompt_assist"]
    if invalid == "asset":
        assist["fields"] = [next(b["input"] for b in raw["bindings"] if b["kind"] == "asset")]
    elif invalid == "missing":
        assist["fields"] = ["does_not_exist"]
    elif invalid == "repeated":
        assist["fields"] = ["prompt", "prompt"]
    elif invalid == "blank":
        assist["instructions"] = "  "
    elif invalid == "empty":
        assist["fields"] = []
    else:
        property_schema = {
            "choices": {"type": "string", "enum": ["a"]},
            "constant": {"type": "string", "const": "a"},
            "number": {"type": "integer"},
        }
        raw["input_schema"]["properties"]["prompt"] = property_schema[invalid]
    with pytest.raises(ApiError) as error:
        validate_strategy(raw)
    assert error.value.details[0]["path"].startswith("/prompt_assist/")
