import { renderAdminNav } from "./navigation.js";

/** 获取服务商页面的固定控件。 */
const byId = id => document.getElementById(id);
const dialog = byId("editor");
const form = byId("provider-form");
const keyInput = byId("api-key");
let providers = [];
let editing = null;
let busy = false;

/** 通过同源维护接口读写配置，页面只展示固定的错误说明，避免回显请求中的密钥。 */
async function request(suffix = "", method = "GET", body) {
  const headers = {"X-MyComfy-Admin": "prompt-providers"};
  if (body) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch("/local/v1/prompt-providers" + suffix, {
      method, headers, body: body ? JSON.stringify(body) : undefined,
      credentials: "omit", redirect: "error", cache: "no-store",
    });
  } catch {
    throw new Error("无法连接管理接口，请重试。");
  }
  const data = await response.json().catch(() => null);
  if (response.ok && data) return data;
  if (data?.error?.code === "PROVIDER_KEY_REQUIRED") {
    throw new Error("更换 API 基址时，请填写新密钥或选择清除已保存的密钥。");
  }
  if (response.status === 422) throw new Error("请检查名称、API 基址、模型标识和密钥格式。");
  if (response.status === 404) throw new Error("服务商已不存在，请刷新列表。");
  if (response.status === 403) throw new Error("请从配置的管理后台地址打开此页。");
  throw new Error("管理接口响应异常（HTTP " + response.status + "）。");
}

/** 在对应状态区发布可由辅助技术朗读的操作反馈。 */
function announce(id, message, tone = "") {
  byId(id).textContent = message;
  byId(id).dataset.tone = tone;
}

/** 在请求期间锁定表单和行操作，避免双击造成重复创建或并发切换。 */
function setBusy(value) {
  busy = value;
  for (const id of ["refresh", "add", "fields", "save", "cancel"]) byId(id).disabled = value;
  byId("providers").inert = value;
}

/** 用纯文本展示配置，每行只显示密钥是否已配置。 */
function render() {
  const rows = document.createDocumentFragment();
  for (const provider of providers) {
    const row = document.createElement("tr");
    row.insertCell().textContent = provider.name;
    row.insertCell().textContent = provider.model;
    row.insertCell().textContent = provider.base_url;
    const key = row.insertCell();
    key.className = "key-state";
    key.textContent = provider.has_api_key ? "已配置" : "未配置";
    const state = document.createElement("span");
    state.className = provider.active ? "state good" : "state";
    state.textContent = provider.active ? "已启用" : "未启用";
    row.insertCell().append(state);
    const actions = document.createElement("div");
    actions.className = "toolbar";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.textContent = "编辑";
    edit.dataset.edit = provider.id;
    edit.setAttribute("aria-label", "编辑 " + provider.name);
    edit.addEventListener("click", () => openEditor(provider));
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.textContent = provider.active ? "停用" : "启用";
    toggle.dataset.toggle = provider.id;
    toggle.setAttribute("aria-label", toggle.textContent + " " + provider.name);
    toggle.addEventListener("click", () => setActive(provider));
    actions.append(edit, toggle);
    row.insertCell().append(actions);
    rows.append(row);
  }
  byId("providers").replaceChildren(rows);
  byId("empty").hidden = providers.length > 0;
}

/** 刷新成功后替换列表，网络失败时保留当前可见配置。 */
async function load() {
  if (busy) return;
  setBusy(true);
  announce("list-status", "正在读取服务商…");
  try {
    providers = (await request()).items;
    render();
    announce("list-status", "");
  } catch (error) {
    announce("list-status", error.message, "error");
  } finally {
    setBusy(false);
  }
}

/** 用服务端确认的配置更新列表，激活新服务商时同步显示旧服务商已停用。 */
function applySaved(saved, focusAction) {
  const existing = providers.findIndex(item => item.id === saved.id);
  if (saved.active) providers = providers.map(item => ({...item, active: false}));
  if (existing < 0) providers.push(saved);
  else providers[existing] = saved;
  render();
  const buttons = byId("providers").querySelectorAll("button");
  Array.from(buttons).find(button => button.dataset[focusAction] === saved.id)?.focus();
}

/** 单独启停当前服务商，提交成功后再更新可见状态。 */
async function setActive(provider) {
  if (busy) return;
  const action = provider.active ? "disable" : "activate";
  setBusy(true);
  let saved;
  try {
    saved = await request("/" + encodeURIComponent(provider.id) + "/" + action, "POST", {});
  } catch (error) {
    announce("list-status", error.message, "error");
    return;
  } finally {
    setBusy(false);
  }
  applySaved(saved, "toggle");
  announce("list-status", (saved.active ? "已启用 " : "已停用 ") + saved.name + "。", "success");
}

/** 初始化新增或编辑弹窗，已保存的密钥只以状态提示表示。 */
function openEditor(provider = null) {
  if (busy) return;
  form.reset();
  editing = provider;
  byId("editor-title").textContent = provider ? "编辑服务商" : "添加服务商";
  byId("name").value = provider?.name ?? "";
  byId("base-url").value = provider?.base_url ?? "";
  byId("model").value = provider?.model ?? "";
  keyInput.value = "";
  keyInput.disabled = false;
  byId("clear-option").hidden = !provider?.has_api_key;
  byId("key-help").textContent = provider?.has_api_key
    ? "留空沿用已保存的密钥。更换 API 基址时，请重新填写或清除原密钥。"
    : "服务商要求验证时填写，无需密钥时可留空。";
  announce("form-status", "");
  dialog.showModal();
}

/** 保存表单。空密钥省略，清除操作必须由管理员明确勾选。 */
async function save(event) {
  event.preventDefault();
  if (busy) return;
  const body = {
    name: byId("name").value.trim(), base_url: byId("base-url").value.trim(),
    model: byId("model").value.trim(),
  };
  // 地址规范化和并发更新均由后端判定。旧密钥从不返回浏览器或随表单重发。
  if (byId("clear-key").checked) body.clear_api_key = true;
  else if (keyInput.value) body.api_key = keyInput.value;

  setBusy(true);
  announce("form-status", "正在保存…");
  let saved;
  try {
    saved = await request(editing ? "/" + encodeURIComponent(editing.id) : "",
      editing ? "PUT" : "POST", body);
  } catch (error) {
    announce("form-status", error.message, "error");
    return;
  } finally {
    setBusy(false);
  }
  keyInput.value = "";
  dialog.close();
  applySaved(saved, "edit");
  announce("list-status", "已保存 " + saved.name + "。", "success");
}

renderAdminNav(byId("nav"), "providers");
byId("refresh").addEventListener("click", load);
byId("add").addEventListener("click", () => openEditor());
byId("cancel").addEventListener("click", () => dialog.close());
dialog.addEventListener("cancel", event => { if (busy) event.preventDefault(); });
dialog.addEventListener("close", () => { form.reset(); editing = null; });
byId("clear-key").addEventListener("change", event => {
  // 替换密钥和清除密钥分别提交，勾选清除时立即擦掉先前输入的文本。
  keyInput.value = "";
  keyInput.disabled = event.target.checked;
});
window.addEventListener("pagehide", () => { keyInput.value = ""; });
form.addEventListener("submit", save);
void load();
