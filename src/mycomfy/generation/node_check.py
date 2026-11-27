"""按 GPU 实例当前代次登记的节点类型核对工作流依赖。"""

from sqlalchemy import and_, select

from mycomfy.database.models import EngineInstance, EngineNodeCatalog
from mycomfy.errors import ApiError

from .workflow_nodes import workflow_node_references


def matches_environment(requirements, manifest):
    """比对策略声明的引擎、节点、模型版本与实例清单。"""
    required_commit = requirements.get("comfyui")
    if required_commit and required_commit != manifest.get("comfyui"):
        return False
    for group in ("nodes", "models"):
        actual = manifest.get(group, {})
        if any(actual.get(k) != version for k, version in requirements.get(group, {}).items()):
            return False
    return True


def _module_origin(module):
    """从 ComfyUI 注册的 Python 模块提取官方或第三方来源，兼容未提供模块的版本。"""
    if not module:
        return None
    root, _, package = module.partition(".")
    if root in {"nodes", "comfy_extras", "comfy_api_nodes"}:
        return "official", "comfy-core"
    if root == "custom_nodes" and package:
        return "custom", package
    return None


def check_workflow_nodes(db, raw):
    """逐实例比较完整节点集合，区分已支持、已确认缺失和等待引擎上报。"""
    references = workflow_node_references(raw)
    classes = {ref["class_type"] for ref in references}
    # 裸 API 图也允许名为 requirements 的普通节点。只有模板声明能约束适用实例。
    requirements = raw.get("requirements", {}) if isinstance(raw.get("strategy_key"), str) else {}
    if not isinstance(requirements, dict) or any(
        not isinstance(requirements.get(key, {}), dict) for key in ("models", "nodes")
    ):
        raise ApiError(422, "INVALID_STRATEGY", "模板的 requirements.nodes 和 models 需要是对象")
    instances = []
    origins = {key: set() for key in classes}
    query = (
        select(
            EngineInstance.id,
            EngineInstance.manifest,
            EngineInstance.runtime,
            EngineNodeCatalog.nodes,
        )
        .outerjoin(
            EngineNodeCatalog,
            and_(
                EngineNodeCatalog.instance_id == EngineInstance.id,
                EngineNodeCatalog.generation == EngineInstance.generation,
            ),
        )
        .where(EngineInstance.configured)
        .order_by(EngineInstance.id)
    )
    deployed = [
        (instance_id, (runtime or {}).get("configured_manifest", manifest), manifest, catalog)
        for instance_id, manifest, runtime, catalog in db.execute(query)
    ]
    eligible = [row for row in deployed if matches_environment(requirements, row[1])]
    # 模板可能将缺失插件本身列为版本要求。如果没有候选，也能检查是否所有现有引擎都缺类。
    for instance_id, configured, manifest, catalog in eligible or deployed:
        # 暂停或掉线不会改变已加载的类型。待切换部署的旧代次不能替新环境证明节点缺失。
        missing = []
        if catalog is None or configured != manifest:
            status = "unknown"
        else:
            missing = sorted(classes.difference(catalog))
            status = "missing" if missing else "ok"
            for key in classes.intersection(catalog):
                origin = _module_origin(catalog[key])
                if origin:
                    origins[key].add(origin)
        instances.append({"id": instance_id, "status": status, "missing": missing})

    # 多台 GPU 的节点不能拼在一起执行同一张图。只有全部候选实例都已证实缺少节点才拒绝。
    states = {instance["status"] for instance in instances}
    status = "unknown"
    if "ok" in states:
        status = "ok"
    elif states == {"missing"}:
        status = "missing"
    if not eligible and status != "missing":
        # 有节点的环境版本不匹配，不能据此宣称新环境可用，允许先准备模板再部署。
        status = "unknown"
        instances = []
    for ref in references:
        observed = origins[ref["class_type"]]
        if len(observed) == 1:
            ref["source"], ref["package"] = next(iter(observed))
        elif observed:
            ref["source"], ref["package"] = "unknown", None
    return {"status": status, "nodes": references, "instances": instances}


def require_workflow_nodes(db, content):
    """在写入模板版本前复核节点清单，给出每个缺失类型及对应的工作流节点位置。"""
    report = check_workflow_nodes(db, content)
    if report["status"] != "missing":
        return
    missing_on = {}
    for instance in report["instances"]:
        for class_type in instance["missing"]:
            missing_on.setdefault(class_type, []).append(instance["id"])
    details = []
    for ref in report["nodes"]:
        class_type = ref["class_type"]
        if class_type in missing_on:
            engines = "、".join(missing_on[class_type])
            for node_id in ref["node_ids"]:
                token = node_id.replace("~", "~0").replace("/", "~1")
                details.append(
                    {
                        "path": f"/workflow/{token}/class_type",
                        "code": "MISSING_NODE_TYPE",
                        "message": f"{engines} 缺少 {class_type}（节点 #{node_id}）",
                    }
                )
    raise ApiError(
        422, "WORKFLOW_NODES_MISSING", "没有一台匹配的引擎具备此工作流的全部节点", details
    )
