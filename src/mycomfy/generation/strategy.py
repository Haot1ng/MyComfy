from collections import Counter
from itertools import batched
from typing import Annotated, Any, Literal

from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from mycomfy.database.models import EngineInstance, Incompatibility, StrategySeries, StrategyVersion
from mycomfy.database.prompt_models import TemplatePromptSettings
from mycomfy.database.session import db_now, new_id
from mycomfy.errors import ApiError

from .state import canonical_digest

Key = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=64)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Binding(ContractModel):
    input: str
    node_id: str
    node_input: str
    kind: Literal["value", "asset"]
    # 默认沿用工作流中的原值。只序列化显式启用的裁剪，维持旧模板的内容摘要。
    omit_node_if_missing: bool = Field(default=False, exclude_if=lambda value: not value)


class Output(ContractModel):
    role: Key
    node_id: str
    min_count: int = Field(1, ge=0, le=100)
    max_count: int = Field(1, ge=1, le=100)
    media_types: list[str] = Field(min_length=1)
    require_decodable: bool = True
    path_input: str = "filename_prefix"
    history_key: str | None = None
    width: int | None = Field(None, gt=0)
    height: int | None = Field(None, gt=0)
    min_duration: float | None = Field(None, ge=0, allow_inf_nan=False)
    max_duration: float | None = Field(None, gt=0, allow_inf_nan=False)
    fps: float | None = Field(None, gt=0, allow_inf_nan=False)


class Requirements(ContractModel):
    comfyui: str | None = None
    nodes: dict[str, str] = Field(default_factory=dict)
    models: dict[str, str] = Field(default_factory=dict)


class Progress(ContractModel):
    node_ids: list[str] = Field(min_length=1)
    stage: str = Field(min_length=1, max_length=80)


class Scheduling(ContractModel):
    base_priority: int = Field(0, ge=0, le=10)


class Retry(ContractModel):
    max_retries: int = Field(0, ge=0, le=2_147_483_646)
    interval_seconds: int = Field(0, ge=0, le=2_147_483_647)


class ArtifactRules(ContractModel):
    expose_after_cancel: bool = False


class PromptAssist(ContractModel):
    """保存本版本发给扩写模型的指令及允许用户扩写的文本字段。"""

    instructions: str = Field(min_length=1, max_length=8000)
    fields: list[str] = Field(min_length=1)


class Strategy(ContractModel):
    strategy_key: Key
    name: str = Field(min_length=1, max_length=180)
    workflow: dict[str, dict[str, Any]] = Field(min_length=1)
    input_schema: dict[str, Any]
    bindings: list[Binding]
    prompt_assist: PromptAssist | None = None
    requirements: Requirements
    progress_mapping: list[Progress]
    output_contract: list[Output] = Field(min_length=1)
    scheduling: Scheduling
    retry: Retry
    artifacts: ArtifactRules


def json_pointer(loc):
    """按 JSON Pointer 转义规则生成可定位的字段路径。"""
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in loc)


def check_schema_references(schema):
    """按草案定义遍历子 Schema，并在发布前验证所有引用能在当前文档内解析。"""
    root = DRAFT202012.create_resource(schema)
    pending = [(root, Registry().resolver_with_root(root))]
    while pending:
        resource, resolver = pending.pop()
        if not isinstance(resource.contents, dict):
            continue
        for key in ("$ref", "$dynamicRef"):
            if key in resource.contents:
                value = resource.contents[key]
                if not value.startswith("#"):
                    raise ValueError("Schema references must point inside the schema")
                resolver.lookup(value)
        # default 与 examples 是数据，不能按子 Schema 遍历其中同名的属性。
        pending.extend((sub, resolver.in_subresource(sub)) for sub in resource.subresources())


def prompt_text_fields(content):
    """从当前版本中取得可自由填写的已绑定文本字段，排除引用上传素材的字符串。"""
    assets = {b["input"] for b in content["bindings"] if b["kind"] == "asset"}
    return {
        name
        for name, rule in content["input_schema"].get("properties", {}).items()
        if name.strip()
        and len(name) <= 128
        and name not in assets
        and isinstance(rule, dict)
        and rule.get("type") == "string"
        and "enum" not in rule
        and "const" not in rule
    }


def validate_prompt_settings(content, config):
    """按目标版本校验文件携带的默认值和后台即时设置，返回适合存储的配置或空值。"""
    if config is None:
        return None
    try:
        assist = PromptAssist.model_validate(config)
    except ValidationError as exc:
        details = [
            {"path": json_pointer(("prompt_assist", *e["loc"])), "code": e["type"]}
            for e in exc.errors(include_input=False)
        ]
        raise ApiError(422, "INVALID_STRATEGY", "Invalid prompt settings", details) from exc
    eligible = prompt_text_fields(content)
    seen = set()
    issues = []
    if not assist.instructions.strip():
        issues.append({"path": "/prompt_assist/instructions", "code": "blank"})
    for index, name in enumerate(assist.fields):
        if name not in eligible or name in seen:
            issues.append({"path": f"/prompt_assist/fields/{index}", "code": "invalid_field"})
        seen.add(name)
    if issues:
        raise ApiError(422, "INVALID_STRATEGY", "Check the prompt instructions and fields", issues)
    return assist.model_dump()


def effective_prompt_assist(db, version, settings_by_key=None):
    """取模板当前生效的指令，并按任务所选版本的实际字段限制适用范围。"""
    key = version.strategy_key
    setting = (
        db.get(TemplatePromptSettings, key) if settings_by_key is None else settings_by_key.get(key)
    )
    if setting is None:
        return version.content.get("prompt_assist")
    if setting.config is None:
        return None
    # 同系列的旧版本可能把同名字段改成了素材，或没有这个字段。
    allowed = prompt_text_fields(version.content)
    fields = [name for name in setting.config["fields"] if name in allowed]
    return {**setting.config, "fields": fields} if fields else None


def validate_strategy(raw):
    """验证工作流、参数绑定、阶段、输出及调度契约并生成持久化内容。"""

    def invalid(path, message, code="INVALID_STRATEGY"):
        """以字段路径和错误码报告一项策略发布错误。"""
        raise ApiError(422, code, message, [{"path": path, "code": code}])

    try:
        version = Strategy.model_validate(raw)
    except ValidationError as exc:
        raise ApiError(
            422,
            "INVALID_STRATEGY",
            "Strategy contains invalid fields",
            [{"path": json_pointer(e["loc"]), "code": e["type"]} for e in exc.errors()],
        ) from exc

    schema = version.input_schema
    try:
        Draft202012Validator.check_schema(schema)
        check_schema_references(schema)
    except (ValueError, SchemaError, Unresolvable) as exc:
        invalid("/input_schema", str(exc).splitlines()[0])
    if schema.get("type") != "object":
        invalid("/input_schema/type", "Input schema must describe an object")
    properties = schema.get("properties", {})
    for name, rule in properties.items():
        if (
            isinstance(rule, dict)
            and "default" in rule
            and not Draft202012Validator(schema).evolve(schema=rule).is_valid(rule["default"])
        ):
            invalid(f"/input_schema/properties/{name}/default", "Invalid default")
    for node_id, node in version.workflow.items():
        if (
            not node_id
            or not isinstance(node.get("class_type"), str)
            or not isinstance(node.get("inputs"), dict)
        ):
            invalid(f"/workflow/{node_id}", "Workflow node requires class_type and inputs")
        for name, value in node["inputs"].items():
            # ComfyUI 用 [节点 ID, 输出序号] 表示边，先排除指向不存在节点的引用。
            if (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], str)
                and type(value[1]) is int
                and (value[0] not in version.workflow or value[1] < 0)
            ):
                invalid(json_pointer(("workflow", node_id, "inputs", name)), "Invalid node link")

    def check_target(node_id, node_input, path):
        """核对绑定或输出目标在工作流中确有对应节点及输入。"""
        if node_input not in version.workflow.get(node_id, {}).get("inputs", {}):
            invalid(path, "Target workflow node input does not exist")

    claimed_targets = set()
    bindings_per_node = Counter(binding.node_id for binding in version.bindings)
    output_nodes = {output.node_id for output in version.output_contract}
    required_fields = set(schema.get("required", []))
    for i, binding in enumerate(version.bindings):
        # 普通用户只提交表单字段，节点及目标输入完全由已发布版本决定。
        if binding.input not in properties:
            invalid(f"/bindings/{i}/input", "Binding does not refer to a declared input")
        target = (binding.node_id, binding.node_input)
        check_target(*target, f"/bindings/{i}")
        if binding.omit_node_if_missing:
            rule = properties[binding.input]
            if (
                binding.kind != "asset"
                or binding.input in required_fields
                or not isinstance(rule, dict)
                or rule.get("type") != "string"
                or version.workflow[binding.node_id]["class_type"] != "LoadImage"
                or binding.node_input != "image"
            ):
                invalid(
                    f"/bindings/{i}/omit_node_if_missing",
                    "Only an optional string asset bound to LoadImage.image can omit its node",
                )
            # 裁剪整节点时，其他绑定和输出契约必须仍有自己的目标。
            if bindings_per_node[binding.node_id] > 1 or binding.node_id in output_nodes:
                invalid(f"/bindings/{i}", "An omittable image loader must have a dedicated node")
        if target in claimed_targets:
            invalid(f"/bindings/{i}", "Binding target is already assigned")
        claimed_targets.add(target)
    if properties.keys() - {binding.input for binding in version.bindings}:
        invalid("/bindings", "Every declared input must be bound to the workflow")
    for i, progress in enumerate(version.progress_mapping):
        if any(node_id not in version.workflow for node_id in progress.node_ids):
            invalid(f"/progress_mapping/{i}/node_ids", "Progress refers to an unknown node")
    roles = set()
    for i, output in enumerate(version.output_contract):
        target = (output.node_id, output.path_input)
        check_target(*target, f"/output_contract/{i}/path_input")
        if output.role in roles or output.min_count > output.max_count or target in claimed_targets:
            invalid(f"/output_contract/{i}", "Invalid output role, count or target")
        if (
            output.min_duration is not None
            and output.max_duration is not None
            and output.min_duration > output.max_duration
        ):
            invalid(f"/output_contract/{i}", "Invalid duration range")
        roles.add(output.role)
    if not any(
        o.min_count and any(t.startswith("video/") for t in o.media_types)
        for o in version.output_contract
    ):
        invalid("/output_contract", "At least one required video output is necessary")
    # 老版本没有这一节，继续产生相同的持久化内容和摘要。
    content = version.model_dump(
        mode="json", exclude={"prompt_assist"} if version.prompt_assist is None else set()
    )
    if version.prompt_assist is not None:
        validate_prompt_settings(content, version.prompt_assist)
    return content


def sql_publish(db, raw, request_id, *, create_only=False):
    """原子创建策略系列或发布其下一版本，保存不可变内容、摘要及请求关联。"""
    content = validate_strategy(raw)
    key = content["strategy_key"]
    if create_only:
        # 数据库主键裁决并发创建；重复标识不会悄悄变成已有系列的新版本。
        series = StrategySeries(key=key)
        db.add(series)
        try:
            db.flush()
        except IntegrityError as exc:
            raise ApiError(409, "STRATEGY_ALREADY_EXISTS", "该模板标识已存在") from exc
    else:
        # 版本发布允许加入已有系列，使用保存点恢复首次发布的并发插入冲突。
        series = db.get(StrategySeries, key, with_for_update=True)
        if series is None:
            try:
                with db.begin_nested():
                    db.add(StrategySeries(key=key))
                    db.flush()
            except IntegrityError:
                pass
            series = db.get(StrategySeries, key, with_for_update=True)
    series.latest_version += 1
    version = StrategyVersion(
        id=new_id("sv"),
        strategy_key=key,
        version=series.latest_version,
        name=content["name"],
        content=content,
        content_digest=canonical_digest(content),
        published_at=db_now(db),
        request_id=request_id,
    )
    db.add(version)
    db.flush()
    return version


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


def allows_manifest(db, strategy, manifest):
    """检查一份环境清单是否满足固定版本且未经相同环境明确拒收。"""
    return (
        matches_environment(strategy.content["requirements"], manifest)
        and db.get(Incompatibility, (strategy.id, canonical_digest(manifest))) is None
    )


def compatible(db, strategy, instance):
    """领取时以运行代次的实际环境判断能否执行此版本。"""
    return instance.configured and allows_manifest(db, strategy, instance.manifest)


def _configured_manifests(instances):
    """逐个取出已配置实例下一次启动使用的环境清单。"""
    for instance in instances:
        if instance.configured:
            # 排空期间实际运行的环境与下一次启动配置的环境可能不同。
            yield (instance.runtime or {}).get("configured_manifest", instance.manifest)


def configured_for(db, strategy, instances=None):
    """判断是否仍存在兼容的部署配置，供新任务受理使用。"""
    if instances is None:
        instances = db.scalars(select(EngineInstance).where(EngineInstance.configured)).all()
    return any(
        allows_manifest(db, strategy, manifest) for manifest in _configured_manifests(instances)
    )


def configured_strategy_ids(db, strategies, instances):
    """批量读取本批版本的环境拒收记录，返回至少有一份兼容部署清单的版本 ID。"""
    requirements = {strategy.id: strategy.content["requirements"] for strategy in strategies}
    manifests = {canonical_digest(m): m for m in _configured_manifests(instances)}
    blocked = set()
    # 两个 IN 按版本和环境分别分块，避免把所有版本与环境的组合展开为 SQL 参数。
    for version_ids in batched(requirements, 400):
        for digests in batched(manifests, 400):
            blocked.update(
                db.execute(
                    select(Incompatibility.strategy_version_id, Incompatibility.environment_digest)
                    .where(Incompatibility.strategy_version_id.in_(version_ids))
                    .where(Incompatibility.environment_digest.in_(digests))
                )
            )

    return {
        version_id
        for version_id, required in requirements.items()
        if any(
            (version_id, digest) not in blocked and matches_environment(required, manifest)
            for digest, manifest in manifests.items()
        )
    }


def asset_field_names(version):
    """从版本绑定取得表单中引用素材的字段，排除重复绑定的名称。"""
    return {b["input"] for b in version.content["bindings"] if b["kind"] == "asset"}


def input_labels(schema, names):
    """读取固定版本输入的显示名，沿文档内引用取标题，缺失时保留参数键。"""
    root = Registry().resolver_with_root(DRAFT202012.create_resource(schema))
    properties = schema.get("properties", {})
    labels = {}
    for name in names:
        labels[name] = name
        rule = properties.get(name)
        resolver = root
        seen = set()
        while isinstance(rule, dict) and id(rule) not in seen:
            seen.add(id(rule))
            if rule.get("title"):
                labels[name] = rule["title"]
                break
            ref = rule.get("$ref")
            if not isinstance(ref, str) or not ref.startswith("#"):
                break
            resolver = resolver.in_subresource(DRAFT202012.create_resource(rule))
            try:
                target = resolver.lookup(ref)
            except Unresolvable:
                break
            rule, resolver = target.contents, target.resolver
    return labels


def strategy_payload(db, version, settings_by_key=None):
    """构造策略版本摘要、表单及可受理性等用户可见字段。"""
    accepted = configured_for(db, version)
    assist = effective_prompt_assist(db, version, settings_by_key)
    return {
        "strategy_version_id": version.id,
        "strategy_key": version.strategy_key,
        "version": version.version,
        "name": version.name,
        "content_digest": version.content_digest,
        "input_schema": version.content["input_schema"],
        "asset_fields": sorted(asset_field_names(version)),
        "prompt_assist": {"fields": assist["fields"]} if assist else None,
        "accepting": accepted,
        "reason": None if accepted else "NO_COMPATIBLE_DEPLOYMENT",
        "expose_after_cancel": version.content["artifacts"]["expose_after_cancel"],
    }
