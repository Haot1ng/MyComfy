import { renderWorkflowPreview } from "./workflow-graph.js";
import { renderAdminNav } from "./navigation.js";
import { mountTemplateEditor } from "./template-editor.js";

/** 取得模板维护页中的固定控件。 */
const byId = id => document.getElementById(id);
const presets = Array.from(document.querySelectorAll("[data-preset]"));
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
let nextCursor = null;
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
  for (const control of [...presets, filePicker, pasteToggle, jsonText, byId("preview-paste"), byId("new-template")]) {
    control.disabled = publishing;
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
  graph.replaceChildren();
  sourceLabel.textContent = "";
  byId("publish-description").textContent = creating ? "请先预览新模板。" : "选择完整策略模板后可发布新版本。";
  message("preview-message", "");
  message("publish-result", "");
  for (const preset of presets) preset.setAttribute("aria-pressed", "false");
  updateControls();
  return loadNumber;
}

/** 读取一个模板来源，渲染节点图，并确定该文件是否具备策略发布字段。 */
async function loadPreview(label, read, presetButton = null) {
  if (publishing) return;
  const request = resetPreview();
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
    if (presetButton) presetButton.setAttribute("aria-pressed", "true");
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

/** 从同源的固定资源读取内置示例，预览时无需维护凭据。 */
function selectPreset(button) {
  filePicker.value = "";
  pasteEditor.hidden = true;
  pasteToggle.setAttribute("aria-expanded", "false");
  void loadPreview(button.querySelector("strong").textContent, async () => {
    const response = await fetch("./presets/" + button.dataset.preset + ".strategy.json", {
      credentials: "omit", redirect: "error",
    });
    if (!response.ok) throw new Error("内置模板读取失败（HTTP " + response.status + "）。");
    return response.text();
  }, button);
}

/** 展示接口状态和校验错误的 JSON 字段路径，便于定位失败输入。 */
function rejection(status, data) {
  const error = data?.error;
  const lines = ["发布失败（HTTP " + status + "）"];
  if (error?.code === "STRATEGY_ALREADY_EXISTS") lines.push("模板标识已存在。请换一个标识，再预览并创建。");
  else if (status === 403) lines.push("请在业务机本机的管理页面中发布。");
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
    "未能确认发布结果。请先刷新已发布模板，核对版本后再决定是否重试。", "error");
}

/** 从同源本机管理页发起一次明确的创建或发布，并显示数据库回执。 */
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
    selectPreset(presets[0]);
  }
}

/** 按页读取每个模板的最新版本，刷新时淘汰在途的旧响应。 */
async function loadPublished(cursor = null) {
  catalogRequest?.abort();
  const request = new AbortController();
  catalogRequest = request;
  const list = byId("published-templates");
  const more = byId("more-published");
  more.disabled = true;
  const query = new URLSearchParams({limit: "10"});
  if (cursor) query.set("cursor", cursor);
  try {
    const response = await fetch("/local/v1/strategies?" + query, {
      mode: "same-origin", credentials: "omit", cache: "no-store", signal: request.signal,
    });
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    if (request !== catalogRequest) return;
    if (!cursor) list.replaceChildren();
    for (const item of data.items) {
      const row = document.createElement("li");
      const name = document.createElement("strong");
      name.textContent = item.name;
      const key = document.createElement("span");
      key.className = "template-key";
      key.textContent = item.strategy_key;
      const version = document.createElement("span");
      version.className = "template-version";
      version.textContent = "v" + item.version;
      row.append(name, key, version);
      list.append(row);
    }
    nextCursor = data.next_cursor;
    more.hidden = !nextCursor;
    message("catalog-message", list.children.length ? "" : "暂无已发布模板。");
  } catch (error) {
    if (request === catalogRequest) message("catalog-message", "读取模板列表失败。 " + error.message, "error");
  } finally {
    if (request === catalogRequest) more.disabled = false;
  }
}

for (const button of presets) button.addEventListener("click", () => selectPreset(button));
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
byId("more-published").addEventListener("click", () => loadPublished(nextCursor));
renderAdminNav(byId("nav"), "templates");
selectPreset(presets[0]);
void loadPublished();
