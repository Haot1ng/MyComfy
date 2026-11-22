import { renderWorkflowPreview } from "./workflow-graph.js";
import { renderAdminNav } from "./navigation.js";
import { mountTemplateEditor } from "./template-editor.js";
import { builtins, readTemplateCatalog } from "./template-catalog.js";

/** 取得模板维护页中的固定控件。 */
const byId = id => document.getElementById(id);
const choices = byId("template-choices");
const filePicker = byId("template-file");
const publishButton = byId("publish-template");
const pasteToggle = byId("paste-toggle");
const pasteEditor = byId("paste-editor");
const jsonText = byId("json-text");
const graph = byId("workflow-preview");
const sourceLabel = byId("preview-source");
const requiredFields = [
  "strategy_key", "name", "workflow", "input_schema", "bindings", "requirements",
  "progress_mapping", "output_contract", "scheduling", "retry", "artifacts",
];
let loadNumber = 0;
let publishText = null;
let previewedApi = null;
let publishing = false;
let creating = false;
let catalog = builtins;
let selectedUrl = null;
let catalogRequest = null;
const editor = mountTemplateEditor(byId("new-template-editor"), {
  onDirty: resetPreview,
  onDraft: text => loadPreview("新增模板", () => text),
});

/** 在状态区显示可朗读的纯文本，文件内容和服务端消息均不插入 HTML。 */
function message(id, text, tone = "") {
  const target = byId(id);
  target.textContent = text;
  target.dataset.tone = tone;
}

/** 同步发布条件，提交期间锁住来源，确保发送的就是屏幕上的模板。 */
function updateControls() {
  for (const control of [filePicker, pasteToggle, jsonText, byId("preview-paste"), byId("new-template")]) {
    control.disabled = publishing;
  }
  for (const button of choices.children) {
    button.disabled = publishing;
    button.setAttribute("aria-pressed", String(button.dataset.url === selectedUrl));
  }
  editor.setBusy(publishing);
  publishButton.disabled = publishing || publishText === null;
  publishButton.textContent = publishing ? "正在发布…" : creating ? "创建并发布" : "发布新版本";
}

/** 切换或修改模板时清除待发布内容，同时让迟到的旧读取结果失效。 */
function resetPreview() {
  loadNumber += 1;
  publishText = null;
  previewedApi = null;
  selectedUrl = null;
  graph.replaceChildren();
  sourceLabel.textContent = "";
  byId("publish-description").textContent = creating ? "请先预览新模板。" : "选择完整策略模板后可发布新版本。";
  message("preview-message", "");
  message("publish-result", "");
  updateControls();
  return loadNumber;
}

/** 读取一个模板来源，渲染节点图，并确定该文件是否具备策略发布字段。 */
async function loadPreview(label, read, sourceUrl = null) {
  if (publishing) return;
  const request = resetPreview();
  selectedUrl = sourceUrl;
  updateControls();
  message("preview-message", "正在读取模板…");
  sourceLabel.textContent = label;
  try {
    // 图形只消费解析后的对象，发布保留原文以保全 ComfyUI 的大整数种子。
    const text = (await read()).replace(/^\uFEFF/, "");
    if (request !== loadNumber) return;
    let raw;
    try {
      // 新浏览器提供数值原文。旧浏览器只能显示舍入值时，在节点详情标明近似值。
      raw = JSON.parse(text, (key, value, context) => {
        if (typeof value === "number" && Number.isInteger(value) && !Number.isSafeInteger(value)) {
          return context?.source ?? ("约 " + value + "（原文将在发布时保留）");
        }
        return value;
      });
    } catch {
      throw new Error("JSON 解析失败，请检查文件中的引号、逗号和括号。");
    }
    const kind = renderWorkflowPreview(graph, raw);
    message("preview-message", "");
    if (kind !== "strategy") {
      if (kind === "api") previewedApi = text;
      byId("publish-description").textContent = kind === "canvas"
        ? "ComfyUI 画布可在本页预览。请从 ComfyUI 导出 API 格式，再通过「新增模板」绑定输入和视频输出。"
        : "点击「新增模板」，可以从这份 API 工作流创建自己的策略。";
      return;
    }
    // 后端仍负责完整的 Schema、节点绑定、输出契约和调度校验。
    const missing = requiredFields.filter(field => !Object.hasOwn(raw, field));
    if (missing.length) {
      byId("publish-description").textContent = "模板缺少发布字段：" + missing.join("、");
      return;
    }
    publishText = text;
    const identity = "「" + raw.name + "」（" + raw.strategy_key + "）";
    byId("publish-description").textContent = creating
      ? "将创建 " + identity + " 并发布首个版本 v1。"
      : "每次发布都会为 " + identity + " 创建一个新版本。";
  } catch (error) {
    if (request !== loadNumber) return;
    graph.replaceChildren();
    message("preview-message", error.message || "读取模板失败。", "error");
  } finally {
    if (request === loadNumber) updateControls();
  }
}

/** 按卡片上的版本读取完整策略，发布直接使用响应原文以保留大整数种子。 */
function selectTemplate(template) {
  filePicker.value = "";
  pasteEditor.hidden = true;
  pasteToggle.setAttribute("aria-expanded", "false");
  const label = template.name + (template.version ? " · v" + template.version : " · 内置");
  void loadPreview(label, async () => {
    const response = await fetch(template.url, {
      mode: "same-origin", credentials: "omit", redirect: "error", cache: "no-store",
    });
    if (!response.ok) throw new Error("模板读取失败（HTTP " + response.status + "）。");
    return response.text();
  }, template.url);
}

/** 展示接口状态和校验错误的 JSON 字段路径，便于定位失败输入。 */
function rejection(status, data) {
  const error = data?.error;
  const lines = ["发布失败（HTTP " + status + "）"];
  if (error?.code === "STRATEGY_ALREADY_EXISTS") lines.push("模板标识已存在。请换一个标识，再预览并创建。");
  else if (status === 403) lines.push("请从本机管理地址或部署时启用的 HTTPS 后台链接打开模板页，再次发布。");
  else if (error?.message) lines.push(error.message);
  if (Array.isArray(error?.details)) {
    for (const field of error.details) {
      lines.push([field.path, field.message || field.code].filter(Boolean).join(" · "));
    }
  }
  if (data?.request_id) lines.push("请求 ID " + data.request_id);
  return lines.join("\n");
}

/** 回执不确定时停止同一草稿的发布，交由维护者核对已创建的版本。 */
function markUnconfirmed(status) {
  publishText = null;
  message("publish-result", (status ? "HTTP " + status + "。" : "") +
    "未能确认发布结果。请在「选择模板」中刷新列表，核对版本后再决定是否重试。", "error");
}

/** 从同源管理页发起一次明确的创建或发布，并显示数据库回执。 */
async function publish() {
  if (publishing || publishText === null) return;
  publishing = true;
  updateControls();
  message("publish-result", "正在发布模板…");
  try {
    // 由浏览器发送真实 Origin；新增要求在数据库中原子确认模板标识未被占用。
    const path = "/local/v1/strategy-versions" + (creating ? "?create_only=true" : "");
    const response = await fetch(path, {
      method: "POST", mode: "same-origin", credentials: "omit", referrerPolicy: "same-origin",
      cache: "no-store", redirect: "error",
      headers: {"Content-Type": "application/json", "X-MyComfy-Admin": "templates"},
      body: publishText,
    });
    const data = await response.json().catch(() => null);
    if (response.status >= 400 && response.status < 500) {
      message("publish-result", rejection(response.status, data), "error");
      return;
    }
    if (response.status !== 201 || !data?.strategy_version_id || !Number.isInteger(data.version)) {
      markUnconfirmed(response.status);
      return;
    }
    // 已收到回执，禁用当前草稿，刷新来自数据库的模板列表。
    publishText = null;
    message("publish-result", "已发布 " + data.strategy_key + " · v" + data.version +
      "\n策略版本 ID " + data.strategy_version_id, "success");
    void loadPublished();
  } catch {
    markUnconfirmed();
  } finally {
    publishing = false;
    updateControls();
  }
}

/** 切换创建和导入视图，两种发布操作始终对应各自的已预览草稿。 */
function toggleEditor() {
  const apiText = previewedApi;
  creating = !creating;
  const toggle = byId("new-template");
  toggle.setAttribute("aria-expanded", String(creating));
  toggle.textContent = creating ? "返回模板选择" : "新增模板";
  byId("template-sources").hidden = creating;
  resetPreview();
  if (creating) editor.open(apiText);
  else {
    editor.close();
    selectTemplate(catalog[0]);
  }
}

/** 在同一个选择区展示所有模板；异步刷新后仍按不可变版本标识当前预览。 */
function renderChoices() {
  const cards = document.createDocumentFragment();
  for (const template of catalog) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.url = template.url;
    const name = document.createElement("strong");
    name.textContent = template.name;
    const details = document.createElement("span");
    details.textContent = template.key + (template.version ? " · v" + template.version : " · 内置");
    button.append(name, details);
    button.addEventListener("click", () => selectTemplate(template));
    cards.append(button);
  }
  choices.replaceChildren(cards);
  updateControls();
}

/** 完整读完目录再替换卡片。若刷新失败，仍可使用上一次成功读取的目录。 */
async function loadPublished() {
  catalogRequest?.abort();
  const request = new AbortController();
  catalogRequest = request;
  message("catalog-message", "正在读取已发布模板…");
  try {
    const templates = await readTemplateCatalog(request.signal);
    if (request !== catalogRequest) return;
    catalog = templates;
    renderChoices();
    message("catalog-message", "");
  } catch (error) {
    if (request !== catalogRequest) return;
    message("catalog-message", "读取已发布模板失败。 " + error.message, "error");
  }
  // 首次打开默认显示第一个模板。等待目录期间的文件导入、粘贴或点击优先。
  if (loadNumber === 0) {
    selectTemplate(catalog[0]);
  }
}

filePicker.addEventListener("change", () => {
  const file = filePicker.files[0];
  if (!file) return;
  pasteEditor.hidden = true;
  pasteToggle.setAttribute("aria-expanded", "false");
  void loadPreview(file.name, () => file.text());
  filePicker.value = "";
});
pasteToggle.addEventListener("click", () => {
  pasteEditor.hidden = !pasteEditor.hidden;
  pasteToggle.setAttribute("aria-expanded", String(!pasteEditor.hidden));
  if (!pasteEditor.hidden) {
    resetPreview();
    message("preview-message", "粘贴模板后点击「预览 JSON」。");
    jsonText.focus();
  }
});
jsonText.addEventListener("input", () => {
  resetPreview();
  message("preview-message", "点击「预览 JSON」查看当前内容。");
});
byId("preview-paste").addEventListener("click", () => {
  void loadPreview("粘贴的 JSON", () => jsonText.value);
});
byId("new-template").addEventListener("click", toggleEditor);
publishButton.addEventListener("click", publish);
byId("refresh-published").addEventListener("click", () => loadPublished());
renderAdminNav(byId("nav"), "templates");
renderChoices();
void loadPublished();
