import { renderWorkflowPreview } from "./workflow-graph.js";
import { renderAdminNav } from "./navigation.js";

/** 取得模板维护页中的固定控件。 */
const byId = id => document.getElementById(id);
const presets = Array.from(document.querySelectorAll("[data-preset]"));
const filePicker = byId("template-file");
const tokenPicker = byId("maintenance-token");
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
let publishing = false;

/** 在状态区显示可朗读的纯文本，文件内容和服务端消息均不插入 HTML。 */
function message(id, text, tone = "") {
  const target = byId(id);
  target.textContent = text;
  target.dataset.tone = tone;
}

/** 同步发布条件，提交期间锁住来源，确保发送的就是屏幕上的模板。 */
function updateControls() {
  for (const control of [...presets, filePicker, pasteToggle, jsonText, byId("preview-paste")]) {
    control.disabled = publishing;
  }
  tokenPicker.disabled = publishing || publishText === null;
  publishButton.disabled = tokenPicker.disabled || !tokenPicker.files.length;
  publishButton.textContent = publishing ? "正在发布…" : "发布新版本";
}

/** 切换模板时丢弃旧凭据及待发布内容，同时让迟到的旧读取结果失效。 */
function resetPreview() {
  loadNumber += 1;
  publishText = null;
  tokenPicker.value = "";
  graph.replaceChildren();
  sourceLabel.textContent = "";
  byId("publish-description").textContent = "选择完整策略模板后可发布新版本。";
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
      const format = kind === "canvas" ? "ComfyUI 画布" : "ComfyUI API 工作流";
      byId("publish-description").textContent =
        format + "可在本页预览。发布时请打开包含 API 工作流、输入绑定和输出契约的 MyComfy 策略 JSON。";
      return;
    }
    // 后端仍负责完整的 Schema、节点绑定、输出契约和调度校验。
    const missing = requiredFields.filter(field => !Object.hasOwn(raw, field));
    if (missing.length) {
      byId("publish-description").textContent = "模板缺少发布字段：" + missing.join("、");
      return;
    }
    publishText = text;
    byId("publish-description").textContent =
      "每次发布都会为「" + raw.name + "」系列（" + raw.strategy_key + "）创建一个新版本。";
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
  if (error?.code === "MAINTENANCE_FORBIDDEN") lines.push("凭据无效，请重新选择本机维护凭据文件。");
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
    "未能确认发布结果。请先在工作台核对策略版本，确认需要重试后再重新选择模板。", "error");
}

/** 使用本机凭据单次发布，保留服务端返回的不可变版本号及资源 ID。 */
async function publish(event) {
  event.preventDefault();
  const file = tokenPicker.files[0];
  if (publishing || publishText === null || !file) return;
  publishing = true;
  tokenPicker.value = "";
  updateControls();
  message("publish-result", "正在读取凭据…");
  try {
    // 服务端令牌最多 4096 字节，凭据选择器仅用于当前操作。
    if (file.size > 4097) throw new Error("凭据文件过大，请选择 maintenance-token。");
    const token = (await file.text()).trim();
    if (!token || /[^\x21-\x7E]/.test(token)) throw new Error("凭据格式无效，请选择 maintenance-token。");
    message("publish-result", "正在发布模板…");
    let response;
    try {
      response = await fetch("/local/v1/strategy-versions", {
        method: "POST", credentials: "omit", cache: "no-store", redirect: "error",
        headers: {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        body: publishText,
      });
    } catch {
      markUnconfirmed();
      return;
    }
    const data = await response.json().catch(() => null);
    if (response.status >= 400 && response.status < 500) {
      message("publish-result", rejection(response.status, data), "error");
      return;
    }
    if (response.status !== 201 || !data?.strategy_version_id || !Number.isInteger(data.version)) {
      markUnconfirmed(response.status);
      return;
    }
    // 已收到回执，禁用当前草稿，避免双击或重新选择凭据重复发布。
    publishText = null;
    message("publish-result", "已发布 " + data.strategy_key + " · v" + data.version +
      "\n策略版本 ID " + data.strategy_version_id, "success");
  } catch {
    // 凭据读取错误仅显示本地指引，避免浏览器异常将请求头带入页面。
    message("publish-result", "无法读取有效凭据，请重新选择项目的 maintenance-token 文件。", "error");
  } finally {
    publishing = false;
    updateControls();
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
tokenPicker.addEventListener("change", updateControls);
byId("publish-form").addEventListener("submit", publish);
window.addEventListener("pagehide", () => { tokenPicker.value = ""; });
window.addEventListener("pageshow", updateControls);
renderAdminNav(byId("nav"), "templates");
selectPreset(presets[0]);
