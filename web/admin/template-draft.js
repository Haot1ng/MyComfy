import {parseWorkflow} from "./workflow-graph.js";

/** 将 JSONC 注释和文件头 BOM 换成空白，保留数值原文、字符串、换行和错误位置。 */
export function stripJsonComments(text) {
  const source = text.replace(/^\uFEFF/, " ");
  const parts = [];
  let start = 0;
  let quoted = false;
  for (let i = 0; i < source.length; i++) {
    const char = source[i];
    // 转义字符与其后一个字符同属字符串，提示词里的 URL、引号和注释标记均照原样保留。
    if (quoted) {
      if (char === "\\") i++;
      else if (char === '"') quoted = false;
    } else if (char === '"') {
      quoted = true;
    } else if (char === "/" && (source[i + 1] === "/" || source[i + 1] === "*")) {
      let end = i + 2;
      if (source[i + 1] === "/") {
        while (end < source.length && source[end] !== "\r" && source[end] !== "\n") end++;
      } else {
        const close = source.indexOf("*/", end);
        if (close === -1) {
          const line = source.slice(0, i).split(/\r\n|[\r\n]/).length;
          throw new Error("第 " + line + " 行的块注释缺少结束标记 */。");
        }
        end = close + 2;
      }
      // 注释相邻的两个值仍需由 JSON 语法分隔；用空格占位也保留 UTF-16 列偏移。
      parts.push(source.slice(start, i), source.slice(i, end).replace(/[^\r\n]/g, " "));
      start = end;
      i = end - 1;
    }
  }
  return parts.length ? parts.join("") + source.slice(start) : source;
}

/** 读取标准或带注释的 JSON 文档，返回严格 JSON 正文及供预览使用的解析值。 */
function readDocument(text) {
  const source = stripJsonComments(text);
  try {
    return {source, value: JSON.parse(source)};
  } catch {
    throw new Error("请先提供有效的工作流 JSON。");
  }
}

/** 从已解析的工作流中列出可绑定的标量输入，节点间连线由预览解析器统一校验。 */
function apiInputs(value) {
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

/** 在新增模板表单中读取 API 工作流的可绑定节点。 */
export function readApiInputs(workflowText) {
  return apiInputs(readDocument(workflowText).value);
}

/** 修改已解析 JSON 的顶层成员。以原文保留其余成员，包括嵌套工作流的 64 位种子。 */
function editRootFields(source, changes) {
  const members = [];
  const seen = new Set();
  let start = source.indexOf("{") + 1;
  let depth = 0;
  // 整段字符串是单个词元，提示词中的引号、逗号和括号不会被当作对象边界。
  for (const match of source.matchAll(/"(?:[^"\\]|\\.)*"|[[\]{},]/g)) {
    const token = match[0];
    if (depth === 1 && (token === "," || token === "}")) {
      const member = source.slice(start, match.index);
      const header = /^\s*("(?:[^"\\]|\\.)*")\s*:\s*/.exec(member);
      if (header) {
        const key = JSON.parse(header[1]);
        if (!Object.hasOwn(changes, key)) {
          members.push(member);
        } else {
          seen.add(key);
          if (changes[key] !== undefined) {
            members.push(header[0] + JSON.stringify(changes[key]) + member.match(/\s*$/)[0]);
          }
        }
      }
      start = match.index + 1;
    }
    if (token === "{" || token === "[") depth++;
    if (token === "}" || token === "]") depth--;
  }
  // undefined 表示移除可选字段；新配置追加到同一顶层对象。
  for (const [key, value] of Object.entries(changes)) {
    if (value !== undefined && !seen.has(key)) members.push(JSON.stringify(key) + ":" + JSON.stringify(value));
  }
  return "{" + members.join(",") + "}";
}

/** 列出工作台允许辅助填写的文本字段，素材、枚举和固定值不接受自由文本。 */
export function promptTextFields(strategy) {
  const bindings = Array.isArray(strategy.bindings) ? strategy.bindings : [];
  const assets = new Set(bindings.filter(binding => binding?.kind === "asset").map(binding => binding.input));
  const values = new Set(bindings.filter(binding => binding?.kind === "value").map(binding => binding.input));
  return Object.entries(strategy.input_schema?.properties ?? {}).filter(([name, rule]) =>
    name.trim() && Array.from(name).length <= 128 && values.has(name) && !assets.has(name) &&
    rule?.type === "string" && !Object.hasOwn(rule, "enum") && !Object.hasOwn(rule, "const")
  ).map(([name, rule]) => ({name, title: typeof rule.title === "string" ? rule.title : name}));
}

/** 只更新策略的提示词辅助配置，关闭时移除可选字段。 */
export function writePromptAssist(source, config) {
  return editRootFields(source, {prompt_assist: config ?? undefined});
}

/** 复制内置策略到新的系列，保留未修改参数的原始 JSON 表示。 */
function copyPreset(text, identity) {
  const {value, source} = readDocument(text);
  if (typeof value?.strategy_key !== "string" || typeof value?.name !== "string" || !value.workflow) {
    throw new Error("内置模板缺少策略定义。");
  }
  if (value.strategy_key === identity.strategy_key) throw new Error("请为新模板填写不同于原模板的标识。");
  return editRootFields(source, identity);
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

  const {source, value} = readDocument(workflowText);
  const nodes = apiInputs(value);
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
  // 配置字段由 JSON.stringify 负责转义，工作流嵌入已剥除注释的正文并保留 64 位种子。
  return '{"workflow":' + source.trim() + ',' + JSON.stringify(metadata).slice(1);
}
