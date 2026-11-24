"""在没有项目虚拟环境的部署主机上读取配置文档，使用随仓库分发的 YAML 安全加载器。"""

import json
import sys
from pathlib import Path
from typing import ClassVar

# 使用私有包名避免与操作系统自带的 yaml 冲突；加载所需模块后还原 Python 搜索路径。
sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor" / "pyyaml-6.0.3.zip"))
try:
    from _mycomfy_yaml.error import YAMLError
    from _mycomfy_yaml.loader import SafeLoader
finally:
    sys.path.pop(0)


class _ConfigError(ValueError):
    """可直接反馈给部署者的错误，消息只包含文件位置和原因。"""


class _ConfigLoader(SafeLoader):
    """使用可传递给 Compose 和引擎的基本类型，拒绝会被悄然覆盖的同名配置项。"""

    # 模型版本可能长得像日期，保留为字符串。显式的非配置类型和自定义标签走未知标签报错。
    yaml_implicit_resolvers: ClassVar[dict] = {
        key: [(tag, regex) for tag, regex in entries if tag != "tag:yaml.org,2002:timestamp"]
        for key, entries in SafeLoader.yaml_implicit_resolvers.items()
    }
    yaml_constructors: ClassVar[dict] = {
        tag: fn
        for tag, fn in SafeLoader.yaml_constructors.items()
        if tag is None
        or tag.rsplit(":", 1)[-1] in {"map", "seq", "str", "int", "bool", "float", "null"}
    }

    def construct_document(self, node):
        """递归完成每个节点再返回文档，以便在构建容器前发现无法传给引擎的循环引用。"""
        return self.construct_object(node, deep=True)

    def compose_mapping_node(self, anchor):
        """在展开合并键之前检查原始映射，每份锚点配置的显式字段只校验一次。"""
        node = super().compose_mapping_node(anchor)
        seen = set()
        for key, _ in node.value:
            line = key.start_mark.line + 1
            if key.tag not in {"tag:yaml.org,2002:str", "tag:yaml.org,2002:merge"}:
                raise _ConfigError(f"YAML 第 {line} 行的配置项名称必须为字符串")
            identity = key.tag, key.value
            if identity in seen:
                raise _ConfigError(f"YAML 第 {line} 行有重复配置项")
            seen.add(identity)
        return node


def load_document(path):
    """按后缀读取单份 YAML 或旧 JSON；报错定位行列，同时避免输出含密码的原文。"""
    path = Path(path)
    content = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            loader = _ConfigLoader(content)
            try:
                document = loader.get_single_data()
            finally:
                loader.dispose()
        except _ConfigError:
            raise
        except YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f"第 {mark.line + 1} 行，第 {mark.column + 1} 列" if mark else "文档"
            raise ValueError(f"YAML {where}解析失败，请检查缩进、引号或标签") from None
        except (ValueError, LookupError, OverflowError, RecursionError):
            # 显式类型转换的底层错误可能包含标量原文；部署输出仅保留错误类别。
            raise ValueError("YAML 标量类型或嵌套结构无效") from None
    else:
        # 旧版 --config 允许自定义文件名，未声明 YAML 后缀时沿用 JSON 读取方式。
        document = json.loads(content)
    if not isinstance(document, dict):
        raise _ConfigError("部署配置必须是键值映射")
    return document
