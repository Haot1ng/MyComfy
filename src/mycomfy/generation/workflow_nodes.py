"""提取 ComfyUI 画布、API 工作流及平台策略中需要引擎注册的节点类型。"""

from mycomfy.errors import ApiError

# 这些节点由 ComfyUI 前端在导出 API 工作流时处理，服务端不会注册同名节点。
CANVAS_ONLY_TYPES = {"Note", "MarkdownNote", "PrimitiveNode", "Reroute"}
MAX_EXPANDED_NODES = 20_000
MAX_SUBGRAPH_DEPTH = 64
MAX_EXPANDED_ID_CHARS = 2_000_000


def _invalid(message):
    """生成导入预检的格式错误，调用方可将可读原因显示在原预览旁。"""
    return ApiError(422, "INVALID_WORKFLOW", message)


def _package_hint(node):
    """读取画布自带的包标记供定位插件，实际节点可用性仍须与引擎清单比对。"""
    properties = node.get("properties")
    if isinstance(properties, dict):
        for key in ("cnr_id", "aux_id"):
            package = properties.get(key)
            if isinstance(package, str) and package.strip():
                return package.strip()
    return None


def _canvas_nodes(canvas):
    """展开被使用的子图实例，并返回带实例路径的节点 ID、执行类型和来源提示。"""
    definitions = canvas.get("definitions", {})
    if not isinstance(definitions, dict) or not isinstance(definitions.get("subgraphs", []), list):
        raise _invalid("画布的 definitions.subgraphs 须为数组")
    subgraphs = {}
    for group in definitions.get("subgraphs", []):
        if not isinstance(group, dict) or not isinstance(group.get("id"), str) or not group["id"]:
            raise _invalid("子图定义缺少有效的 ID")
        if group["id"] in subgraphs:
            raise _invalid("画布中存在重复的子图定义 ID")
        subgraphs[group["id"]] = group

    expanded_count = expanded_id_chars = 0

    def visit(scope, prefix, ancestors):
        """按实际使用路径展开，检测递归引用并限制重复实例化造成的放大。"""
        nonlocal expanded_count, expanded_id_chars
        if not isinstance(scope.get("nodes"), list):
            raise _invalid("画布或子图缺少 nodes 数组")
        local_ids = set()
        for node in scope["nodes"]:
            if not isinstance(node, dict) or type(node.get("id")) not in (int, str):
                raise _invalid("画布节点缺少有效的 ID")
            node_id = str(node["id"])
            class_type = node.get("type")
            if not node_id or node_id in local_ids:
                raise _invalid("同一画布内的节点 ID 为空或重复")
            if not isinstance(class_type, str) or not class_type.strip():
                raise _invalid("画布节点缺少有效的类型")
            local_ids.add(node_id)
            scoped_id = prefix + node_id
            expanded_count += 1
            expanded_id_chars += len(scoped_id)
            if expanded_count > MAX_EXPANDED_NODES or expanded_id_chars > MAX_EXPANDED_ID_CHARS:
                raise _invalid("子图展开后的工作流过大，请拆分后检查")
            # LiteGraph 模式 2（Never）与 4（Bypass）不会导出可执行节点。
            if node.get("mode") in (2, 4) or class_type in CANVAS_ONLY_TYPES:
                continue
            if class_type in subgraphs:
                if class_type in ancestors or len(ancestors) >= MAX_SUBGRAPH_DEPTH:
                    raise _invalid("工作流存在循环子图引用或嵌套过深")
                yield from visit(subgraphs[class_type], scoped_id + ":", ancestors | {class_type})
            else:
                yield scoped_id, class_type, _package_hint(node)

    yield from visit(canvas, "", set())


def _api_nodes(document):
    """读取裸 API 节点图或策略中的 workflow，保留命名为 workflow 的合法节点。"""
    candidate = document.get("workflow")
    if isinstance(candidate, dict) and not (
        isinstance(candidate.get("class_type"), str) and isinstance(candidate.get("inputs"), dict)
    ):
        document = candidate
    if not document:
        raise _invalid("API 工作流中没有节点")
    for node_id, node in document.items():
        if not isinstance(node_id, str) or not node_id or not isinstance(node, dict):
            raise _invalid("API 工作流节点需要有效的 ID 和对象")
        class_type = node.get("class_type")
        if (
            not isinstance(class_type, str)
            or not class_type.strip()
            or not isinstance(node.get("inputs"), dict)
        ):
            raise _invalid(f"API 节点 {node_id} 缺少有效的 class_type 或 inputs")
        yield node_id, class_type, None


def workflow_node_references(raw):
    """按实际执行类型合并节点编号，保留一致的包提示用于缺失节点的诊断。"""
    if not isinstance(raw, dict):
        raise _invalid("工作流须为 JSON 对象")
    entries = _canvas_nodes(raw) if isinstance(raw.get("nodes"), list) else _api_nodes(raw)
    grouped = {}
    for node_id, class_type, package in entries:
        ids, hints = grouped.setdefault(class_type, ([], set()))
        ids.append(node_id)
        if package:
            hints.add(package)
    references = []
    for class_type, (ids, hints) in grouped.items():
        # 同一类型的元数据若相互矛盾，保留类型并交给引擎确定归属。
        package = next(iter(hints)) if len(hints) == 1 else None
        if package is None:
            source = "unknown"
        elif package.casefold() in {"comfy-core", "comfy-org/comfyui", "comfyanonymous/comfyui"}:
            source = "official"
        else:
            source = "custom"
        references.append(
            {"class_type": class_type, "node_ids": ids, "source": source, "package": package}
        )
    return references
