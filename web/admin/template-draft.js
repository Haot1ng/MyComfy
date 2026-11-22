import {parseWorkflow} from "./workflow-graph.js";

/** 读取 JSON 文件，兼容 UTF-8 BOM，并以页面可展示的文字报告错误。 */
function readDocument(text) {
  const source = text.trim();
  try {
    return {source, value: JSON.parse(source)};
  } catch {
    throw new Error("请先提供有效的工作流 JSON。");
  }
}

/** 从可执行工作流中列出可绑定的标量输入，节点间连线由预览解析器统一校验。 */
export function readApiInputs(workflowText) {
  const {value} = readDocument(workflowText);
  if (Array.isArray(value?.nodes)) throw new Error("请在 ComfyUI 中导出 API 格式的工作流。");
  const {kind, graph} = parseWorkflow(value);
  if (kind !== "api") throw new Error("自定义工作流需要 ComfyUI API 节点 JSON。");

  return Array.from(graph.nodes.values(), node => {
    const fields = [];
    for (const [name, value] of node.fields) {
      const type = typeof value;
      if (type === "string" || type === "boolean" || Number.isFinite(value)) {
        fields.push({name, type: Number.isInteger(value) ? "integer" : type});
      }
    }
    return {id: node.id, type: node.type, fields};
  });
}

/** 只替换内置策略最外层的两个字符串，使其他字段及大整数保留原始 JSON 表示。 */
function copyPreset(text, identity) {
  const {value, source} = readDocument(text);
  if (typeof value?.strategy_key !== "string" || typeof value?.name !== "string" || !value.workflow) {
    throw new Error("内置模板缺少策略定义。");
  }
  if (value.strategy_key === identity.strategy_key) throw new Error("请为新模板填写不同于原模板的标识。");
  let depth = 0;
  let field = null;
  // 已解析成功的 JSON 可按结构符号遍历；将完整字符串作为一个词元可跳过其中的逗号和括号。
  return source.replace(/"(?:[^"\\]|\\.)*"|[[\]{}:,]/g, token => {
    if (token === "{" || token === "[") depth++;
    else if (token === "}" || token === "]") depth--;
    else if (depth === 1) {
      if (token === ",") field = null;
      else if (token !== ":") {
        if (field === null) field = JSON.parse(token);
        else if (Object.hasOwn(identity, field)) return JSON.stringify(identity[field]);
      }
    }
    return token;
  });
}

/** 按节点及输入名称寻找可编辑的字面值；目标缺失或连线已占用时停止构建。 */
function targetField(nodes, nodeId, fieldName) {
  const field = nodes.find(node => node.id === nodeId)?.fields.find(item => item.name === fieldName);
  if (!field) throw new Error("请选择节点中存在的标量输入。");
  return field;
}

/** 构造新系列的完整策略 JSON，共享一个表单字段的多处绑定必须约定相同的类型。 */
export function buildTemplateDraft({name, key, presetText, workflowText, bindings = [], output}) {
  const identity = {name: name.trim(), strategy_key: key.trim()};
  if (!identity.name || Array.from(identity.name).length > 180) throw new Error("模板名称需要 1 到 180 个字符。");
  if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(identity.strategy_key)) {
    throw new Error("模板标识最多 64 位，请以小写字母或数字开头，可包含连字符和下划线。");
  }
  if (presetText != null) return copyPreset(presetText, identity);

  const nodes = readApiInputs(workflowText);
  const pathField = targetField(nodes, output?.node_id, output?.path_input);
  if (pathField.type !== "string" || !/^video\/[a-z0-9][a-z0-9.+-]*$/.test(output.media_type)) {
    throw new Error("请指定保存节点的字符串路径输入和视频媒体类型。");
  }
  const used = new Set([pathField]);
  const fields = new Map();
  const mappings = [];
  for (const binding of bindings) {
    const target = targetField(nodes, binding.node_id, binding.node_input);
    if (used.has(target)) throw new Error("字段绑定或视频输出重复使用了同一节点输入。");
    used.add(target);
    const input = binding.input.trim();
    if (!input) throw new Error("请输入表单字段名。");
    const kind = binding.kind;
    if (kind !== "value" && kind !== "asset") throw new Error("请选择参数或素材绑定。");
    if (kind === "asset" && target.type !== "string") throw new Error("素材必须绑定到字符串输入。");
    const title = binding.title?.trim() || input;
    const previous = fields.get(input);
    if (previous && (previous.kind !== kind || previous.type !== target.type || previous.title !== title)) {
      throw new Error("同名表单字段需要相同的类型、用途和标题。");
    }
    fields.set(input, {type: target.type, title, kind});
    mappings.push({input, kind, node_id: binding.node_id, node_input: binding.node_input});
  }

  const properties = Object.fromEntries(Array.from(fields, ([input, {type, title}]) => [input, {type, title}]));
  const metadata = {
    ...identity,
    input_schema: {type: "object", properties, required: [...fields.keys()], additionalProperties: false},
    bindings: mappings,
    output_contract: [{role: "video", node_id: output.node_id, path_input: output.path_input, media_types: [output.media_type]}],
    requirements: {}, progress_mapping: [], scheduling: {}, retry: {}, artifacts: {},
  };
  // 配置字段由 JSON.stringify 负责转义，工作流原文经校验后嵌入以保留 64 位种子。
  return '{"workflow":' + workflowText.trim() + ',' + JSON.stringify(metadata).slice(1);
}
