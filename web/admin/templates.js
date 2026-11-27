import { renderWorkflowPreview } from "./workflow-graph.js";
import { mountNodeCheck } from "./workflow-node-check.js";
import { renderAdminNav } from "./navigation.js";
import { stripJsonComments } from "./template-draft.js";
import { mountPromptAssist, mountTemplateEditor } from "./template-editor.js";
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
const assistPanel = byId("prompt-assist");
const saveAssistButton = byId("save-assist");
const savedDrafts = new Map();
const requiredFields = [
  "strategy_key", "name", "workflow", "input_schema", "bindings", "requirements",
  "progress_mapping", "output_contract", "scheduling", "retry", "artifacts",
];
let loadNumber = 0;
let publishText = null;
let previewedApi = null;
let publishing = false;
let savingAssist = false;
let liveSettings = null;
let creating = false;
let catalog = builtins;
let selectedUrl = null;
let catalogRequest = null;
const nodeCheck = mountNodeCheck(byId("workflow-node-check"), updateControls);
const editor = mountTemplateEditor(byId("new-template-editor"), {
  onDirty: resetPreview,
  onDraft: text => loadPreview("新增模板", () => text),
});
const assist = mountPromptAssist(assistPanel, assistEdited);

/** 有尚未保存的系列设置时，让浏览器在离开页面前提醒。 */
function protectDrafts(event) {
  event.preventDefault();
  event.returnValue = "";
}

/** 在状态区显示可朗读的纯文本，文件内容和服务端消息均不插入 HTML。 */
function message(id, text, tone = "") {
  const target = byId(id);
  target.textContent = text;
  target.dataset.tone = tone;
}

/** 同步发布条件，提交期间锁住来源，确保发送的就是屏幕上的模板。 */
function updateControls() {
  const busy = publishing || savingAssist;
  const draft = liveSettings ? savedDrafts.get(liveSettings.url) : undefined;
  const dirty = Boolean(draft);
  const review = dirty && liveSettings.loaded && draft.etag !== liveSettings.etag;
  if (savedDrafts.size) window.addEventListener("beforeunload", protectDrafts);
  else window.removeEventListener("beforeunload", protectDrafts);
  for (const control of [filePicker, pasteToggle, jsonText, byId("preview-paste"), byId("new-template")]) {
    control.disabled = busy;
  }
  for (const button of choices.children) {
    button.disabled = busy;
    button.setAttribute("aria-pressed", String(button.dataset.url === selectedUrl));
  }
  editor.setBusy(busy);
  nodeCheck.setBusy(busy);
  assistPanel.disabled = busy || publishText === null || (liveSettings !== null && !liveSettings.loaded);
  byId("assist-live-actions").hidden = liveSettings === null;
  byId("discard-assist").hidden = !dirty;
  byId("discard-assist").disabled = busy;
  byId("reload-assist").hidden = !liveSettings?.readFailed;
  byId("reload-assist").disabled = busy;
  byId("assist-review").hidden = !review;
  byId("keep-assist").disabled = busy;
  if (review) {
    const latest = liveSettings.current;
    byId("assist-current").textContent = latest ? "字段：" + latest.fields.join("、") + "\n" + latest.instructions : "已关闭扩写";
  }
  saveAssistButton.disabled = busy || !liveSettings?.loaded || !dirty || review || !assist.valid() || publishText === null;
  saveAssistButton.textContent = savingAssist ? "正在保存…" : "保存扩写设置";
  // 发布始终针对图本身；先处理本地扩写草稿，以免误将“发布”当作“保存”。
  publishButton.disabled = busy || publishText === null || !nodeCheck.allowsPublish() || (liveSettings ? dirty : !assist.valid());
  publishButton.textContent = publishing ? "正在发布…" : creating ? "创建并发布" : "发布新版本";
}

/** 切换或修改模板时清除待发布内容，同时让迟到的旧读取结果失效。 */
function resetPreview() {
  loadNumber += 1;
  publishText = null;
  previewedApi = null;
  selectedUrl = null;
  liveSettings = null;
  nodeCheck.clear();
  byId("assist-section").hidden = true;
  assistPanel.hidden = true;
  message("assist-save-status", "");
  graph.replaceChildren();
  sourceLabel.textContent = "";
  byId("publish-description").textContent = creating ? "请先预览新模板。" : "选择完整策略模板后可发布新版本。";
  message("preview-message", "");
  message("publish-result", "");
  updateControls();
  return loadNumber;
}

/** 读取一个模板来源，渲染节点图，并确定该文件是否具备策略发布字段。 */
async function loadPreview(label, read, sourceUrl = null, publishedTemplate = null) {
  if (publishing || savingAssist) return;
  const request = resetPreview();
  selectedUrl = sourceUrl;
  updateControls();
  message("preview-message", "正在读取模板…");
  sourceLabel.textContent = label;
  try {
    const original = await read();
    if (request !== loadNumber) return;
    // 图形消费解析后的对象，发布直接发送剥除注释的原文，以保全 ComfyUI 的大整数种子。
    const text = stripJsonComments(original);
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
    // 先显示连线图，独立检查已部署的节点。缺失提示保留在预览旁，便于按 ID 查找。
    void nodeCheck.check(text);
    if (kind !== "strategy") {
      // 从当前预览继续创建模板时，维护者仍可在编辑区看到原来的注释。
      if (kind === "api") previewedApi = original;
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
    byId("assist-section").hidden = false;
    if (publishedTemplate) {
      liveSettings = {
        url: sourceUrl + "/prompt-assist-settings", template: publishedTemplate,
        strategy: raw, loaded: false, readFailed: false,
      };
      byId("assist-scope").textContent = "保存后立即应用于" + identity + "系列。历史版本使用各自仍可填写的字段。";
      await loadAssistSettings(liveSettings);
    } else {
      byId("assist-scope").textContent = "这些默认值随模板一起发布。已单独保存的系列扩写设置优先。";
      assist.open(raw);
    }
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
  }, template.url, template.version ? template : null);
}

/** 从回执中读取完整配置，避免将响应不完整误当作关闭扩写。 */
function responseConfig(data) {
  if (!Object.hasOwn(data ?? {}, "prompt_assist")) throw new Error("服务未返回扩写设置。");
  const config = data.prompt_assist;
  if (config === null) return null;
  if (typeof config?.instructions !== "string" || !Array.isArray(config.fields) ||
      !config.fields.every(name => typeof name === "string")) throw new Error("扩写设置响应无效。");
  return {instructions: config.instructions, fields: config.fields};
}

/** 读取版本当前生效的系列规则，切换模板后忽略旧响应，返回原模板时恢复未保存的草稿。 */
async function loadAssistSettings(target) {
  target.loaded = false;
  target.readFailed = false;
  message("assist-save-status", "正在读取扩写设置…");
  updateControls();
  try {
    const response = await fetch(target.url, {
      mode: "same-origin", credentials: "omit", redirect: "error", cache: "no-store",
      headers: {"X-MyComfy-Admin": "templates"},
    });
    if (!response.ok) throw new Error("HTTP " + response.status);
    const current = responseConfig(await response.json());
    const etag = response.headers.get("ETag");
    if (!etag) throw new Error("服务未返回扩写设置的版本标识。");
    if (liveSettings !== target) return;
    target.current = current;
    target.etag = etag;
    target.loaded = true;
    if (JSON.stringify(savedDrafts.get(target.url)?.config) === JSON.stringify(current)) {
      savedDrafts.delete(target.url);
    }
    const draft = savedDrafts.get(target.url);
    assist.open(target.strategy, draft ? draft.config : current);
    if (draft && draft.etag !== etag) {
      message("assist-save-status", "设置已在其他页面更新。请核对最新设置，或撤销本页的修改。", "error");
    } else {
      message("assist-save-status", draft ? "已恢复本页中未保存的修改。" : "");
    }
  } catch (error) {
    if (liveSettings !== target) return;
    target.readFailed = true;
    message("assist-save-status", "无法读取当前扩写设置。 " + error.message, "error");
  } finally {
    if (liveSettings === target) updateControls();
  }
}

/** 按所选版本暂存编辑，保留空配置和未填完的规则，目录切换或刷新列表不会串用草稿。 */
function assistEdited() {
  if (liveSettings?.loaded) {
    const current = assist.config();
    const changed = JSON.stringify(current) !== JSON.stringify(liveSettings.current);
    const etag = savedDrafts.get(liveSettings.url)?.etag ?? liveSettings.etag;
    if (changed) savedDrafts.set(liveSettings.url, {config: current, etag, template: liveSettings.template});
    else savedDrafts.delete(liveSettings.url);
    message("assist-save-status", changed ? "有未保存的扩写设置。" : "");
  }
  updateControls();
}

/** 只向设置端点写入已核对的规则，保存失败时保留修改以便重试。 */
async function saveAssistSettings() {
  const target = liveSettings;
  if (!target || saveAssistButton.disabled) return;
  savingAssist = true;
  const draft = savedDrafts.get(target.url);
  updateControls();
  message("assist-save-status", "正在保存扩写设置…");
  try {
    const response = await fetch(target.url, {
      method: "PUT", mode: "same-origin", credentials: "omit", redirect: "error", cache: "no-store",
      headers: {"Content-Type": "application/json", "X-MyComfy-Admin": "templates", "If-Match": draft.etag},
      body: JSON.stringify({prompt_assist: draft.config}),
    });
    const data = await response.json().catch(() => null);
    // 并发修改后先读最新值，与本地草稿并排供维护者核对，不能用新标识自动覆盖。
    if (response.status === 412) {
      if (liveSettings === target) await loadAssistSettings(target);
      return;
    }
    if (data?.error?.code === "PROMPT_SETTINGS_VERSION_CONFLICT") {
      throw new Error("请刷新模板列表，打开最新版本后再调整系列字段。");
    }
    if (!response.ok) throw new Error(data?.error?.message || "HTTP " + response.status);
    const current = responseConfig(data);
    const etag = response.headers.get("ETag");
    if (!etag) throw new Error("请重新打开模板核对生效的设置。");
    savedDrafts.delete(target.url);
    if (liveSettings !== target) return;
    target.current = current;
    target.etag = etag;
    assist.open(target.strategy, current);
    message("assist-save-status", "扩写设置已保存并立即生效。", "success");
  } catch (error) {
    if (liveSettings === target) message("assist-save-status", "未能确认保存结果，修改已保留。 " + error.message, "error");
  } finally {
    savingAssist = false;
    updateControls();
  }
}

/** 展示接口状态和校验错误的 JSON 字段路径，便于定位失败输入。 */
function rejection(status, data) {
  const error = data?.error;
  const lines = ["发布失败（HTTP " + status + "）"];
  if (error?.code === "STRATEGY_ALREADY_EXISTS") lines.push("模板标识已存在。请换一个标识，再预览并创建。");
  else if (status === 403) lines.push("请从管理后台重新打开模板页，再次发布。");
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
  if (publishButton.disabled) return;
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
      body: liveSettings ? publishText : assist.publishBody(publishText),
    });
    const data = await response.json().catch(() => null);
    if (response.status >= 400 && response.status < 500) {
      message("publish-result", rejection(response.status, data), "error");
      // 导入之后引擎可能换代。按发布时发现的变化重新读取可用节点，保留草稿与诊断。
      if (data?.error?.code === "WORKFLOW_NODES_MISSING") void nodeCheck.check(publishText);
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
  const visible = new Map(catalog.map(template => [template.url, template]));
  // 刷新目录时系列可能已发布更新版本，保留未保存草稿原本对应的版本入口。
  for (const {template} of savedDrafts.values()) visible.set(template.url, template);
  for (const template of visible.values()) {
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
saveAssistButton.addEventListener("click", saveAssistSettings);
byId("reload-assist").addEventListener("click", () => loadAssistSettings(liveSettings));
byId("keep-assist").addEventListener("click", () => {
  // 维护者已核对当前服务端内容，下次保存仍需与这个版本精确匹配。
  savedDrafts.get(liveSettings.url).etag = liveSettings.etag;
  message("assist-save-status", "已保留本页修改。点击「保存扩写设置」后生效。");
  updateControls();
});
byId("discard-assist").addEventListener("click", () => {
  savedDrafts.delete(liveSettings.url);
  if (liveSettings.loaded) {
    assist.open(liveSettings.strategy, liveSettings.current);
    message("assist-save-status", "");
  } else {
    assistPanel.hidden = true;
  }
  updateControls();
});
publishButton.addEventListener("click", publish);
byId("refresh-published").addEventListener("click", () => loadPublished());
renderAdminNav(byId("nav"), "templates");
renderChoices();
void loadPublished();
