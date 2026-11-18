"""无需数据库或 GPU 的协议级回归测试。"""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from referencing.exceptions import Unresolvable

from mycomfy.state import canonical_digest
from mycomfy.strategy import check_schema_references, compatible, configured_for


def test_batch_digest_is_stable_and_preserves_task_order():
    """验证对象键顺序不影响重放，而任务顺序改变时产生不同摘要。"""
    first = {"tasks": ["left", "right"], "settings": {"seed": 1, "fps": 24}}
    reordered = {"settings": {"fps": 24, "seed": 1}, "tasks": ["left", "right"]}
    assert canonical_digest(first) == canonical_digest(reordered)
    assert canonical_digest(first) != canonical_digest({**reordered, "tasks": ["right", "left"]})


def test_schema_references_are_validated_before_publication():
    """本地指针可解析，悬空指针会被拒绝，默认值里的引用字样按数据处理。"""
    schema = {"$defs": {"id": {"type": "string"}}, "properties": {"asset": {"$ref": "#/$defs/id"}}}
    check_schema_references({**schema, "default": {"$ref": "sample text"}})
    with pytest.raises(Unresolvable):
        check_schema_references({"properties": {"asset": {"$ref": "#/$defs/missing"}}})


def test_draining_generation_uses_separate_admission_manifest():
    """部署换版时，新受理与旧执行分别使用配置清单和实际运行清单。"""
    db = Mock()
    db.get.return_value = None
    instance = SimpleNamespace(
        configured=True,
        manifest={"comfyui": "old"},
        runtime={"configured_manifest": {"comfyui": "new"}},
    )
    for version, expected_admission in (("new", True), ("old", False)):
        strategy = SimpleNamespace(id=version, content={"requirements": {"comfyui": version}})
        assert configured_for(db, strategy, [instance]) is expected_admission
        assert compatible(db, strategy, instance) is not expected_admission


def test_python_functions_have_documentation():
    """检查项目方法、命令和测试的功能简介，报告遗漏的文件与行号。"""
    root = Path(__file__).resolve().parents[2]
    missing = []
    for folder in ("src", "scripts", "migrations", "tests"):
        for path in (root / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and not ast.get_docstring(node):
                    missing.append(f"{path.relative_to(root)}:{node.lineno} {node.name}")
    assert not missing, "\n".join(missing)
