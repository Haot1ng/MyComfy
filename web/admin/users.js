import { renderAdminNav } from "./navigation.js";

/** 获取用户页的固定控件。 */
const byId = id => document.getElementById(id);
const dialog = byId("editor");
const form = byId("group-form");
const pageCursors = [null];
const permissionNames = {
  "strategies.view": "查看工作流模板", "jobs.submit": "提交任务", "jobs.view": "查看任务",
  "jobs.cancel": "取消任务", "assets.upload": "上传素材", "assets.download": "下载文件",
  "notifications.view": "查看通知", "notifications.mark_read": "标记通知已读",
};
let pageIndex = 0;
let nextCursor = null;
let loadingPage = false;
let editing = null;

/** 使用同源后台接口读取 JSON，在失败时为当前操作提供可显示的错误。 */
async function request(path, options = {}) {
  const headers = {"X-MyComfy-Admin": "users"};
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch("/local/v1/" + path, {
    credentials: "omit", redirect: "error", cache: "no-store", headers, ...options,
  });
  if (!response.ok) {
    const detail = (await response.json().catch(() => null))?.error;
    if (detail?.code === "UNKNOWN_GROUP") throw new Error("可选分组已变更，请重新打开编辑窗口。");
    if (response.status === 404) throw new Error("记录已不存在，请刷新列表。");
    throw new Error(detail?.message || "请求失败（HTTP " + response.status + "）。");
  }
  return response.json();
}

/** 通过状态区朗读操作进度，安全显示账户名与服务端错误信息。 */
function announce(id, text, tone = "") {
  const status = byId(id);
  status.textContent = text;
  status.dataset.tone = tone;
}

/** 在用户行中显示服务端确认的所有分组，保留没有分组的用户。 */
function renderGroups(cell, groups) {
  if (!groups.length) {
    cell.textContent = "未分组";
    return;
  }
  const list = document.createElement("div");
  list.className = "group-list";
  for (const group of groups) {
    const label = document.createElement("span");
    label.className = "state";
    label.textContent = group.name;
    label.title = group.key;
    list.append(label);
  }
  cell.replaceChildren(list);
}

/** 展开时列出所有分组合并后的实际权限，遇到新权限点时直接显示其代码。 */
function renderPermissions(cell, permissions) {
  if (!permissions.length) {
    cell.textContent = "无权限";
    return;
  }
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = permissions.length + " 项权限";
  const list = document.createElement("ul");
  for (const code of permissions) {
    const item = document.createElement("li");
    item.textContent = permissionNames[code] ?? code;
    item.title = code;
    list.append(item);
  }
  details.append(summary, list);
  cell.replaceChildren(details);
}

/** 渲染当前页，每个编辑按钮绑定所在行，保存后只更新该行的分组。 */
function renderUsers(users) {
  const rows = document.createDocumentFragment();
  for (const user of users) {
    const row = document.createElement("tr");
    row.insertCell().textContent = user.username;
    const id = row.insertCell();
    id.textContent = user.user_id;
    id.className = "code";
    const cells = {groups: row.insertCell(), permissions: row.insertCell()};
    renderGroups(cells.groups, user.groups);
    renderPermissions(cells.permissions, user.permissions);
    const edit = document.createElement("button");
    edit.type = "button";
    edit.textContent = "编辑分组";
    edit.setAttribute("aria-label", "编辑 " + user.username + " 的分组");
    edit.addEventListener("click", () => openEditor(user, cells));
    row.insertCell().append(edit);
    rows.append(row);
  }
  byId("users").replaceChildren(rows);
  byId("list-empty").textContent = "暂无用户";
  byId("list-empty").hidden = users.length > 0;
}

/** 翻页成功后才提交游标和页码，请求失败时保留可重试的原页面。 */
async function loadPage(index, cursor) {
  if (loadingPage || editing) return;
  loadingPage = true;
  byId("previous").disabled = byId("next").disabled = byId("refresh").disabled = true;
  byId("users").inert = true;
  announce("list-status", "正在加载用户…");
  const query = new URLSearchParams({limit: "50"});
  if (cursor) query.set("cursor", cursor);
  try {
    const page = await request("users?" + query);
    renderUsers(page.items);
    pageCursors.splice(index, pageCursors.length - index, cursor);
    pageIndex = index;
    nextCursor = page.next_cursor;
    byId("page-number").textContent = "第 " + (index + 1) + " 页 · 本页 " + page.items.length + " 人";
    announce("list-status", "");
  } catch (error) {
    announce("list-status", "加载用户失败。 " + error.message, "error");
  } finally {
    loadingPage = false;
    byId("users").inert = false;
    byId("refresh").disabled = false;
    byId("previous").disabled = pageIndex === 0;
    byId("next").disabled = !nextCursor;
  }
}

/** 根据原有成员关系生成复选框，组名与稳定组键始终以纯文本显示。 */
function renderChoices(groups, original) {
  const options = document.createDocumentFragment();
  for (const group of groups) {
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.name = "group";
    box.value = group.key;
    box.checked = original.has(group.key);
    const key = document.createElement("small");
    key.textContent = group.key;
    label.append(box, document.createTextNode(group.name), key);
    options.append(label);
  }
  byId("options").replaceChildren(options);
}

/** 仅允许保存有变化的完整组集合，清空时提示权限将随之撤销。 */
function updateSelection() {
  const boxes = [...form.querySelectorAll('input[name="group"]')];
  const changed = boxes.some(box => box.checked !== editing.original.has(box.value));
  byId("save").disabled = !changed;
  const empty = changed && !boxes.some(box => box.checked);
  announce("edit-status", empty ? "保存后，该用户将失去所有分组授予的权限。" : "");
}

/** 打开弹窗时重新读取组目录和用户成员关系，忽略已关闭窗口的迟到响应。 */
async function openEditor(user, cells) {
  if (editing || loadingPage) return;
  const session = {path: "users/" + encodeURIComponent(user.user_id) + "/groups",
    user, cells, controller: new AbortController(), saving: false};
  editing = session;
  byId("account").textContent = user.username + " · " + user.user_id;
  byId("choices").hidden = byId("choices").disabled = true;
  byId("save").disabled = true;
  byId("cancel").disabled = false;
  byId("options").replaceChildren();
  announce("edit-status", "正在读取分组…");
  dialog.showModal();
  try {
    const options = {signal: session.controller.signal};
    const [catalog, profile] = await Promise.all([request("groups", options), request(session.path, options)]);
    if (editing !== session) return;
    session.original = new Set(profile.groups.map(group => group.key));
    // 读取期间成员关系可能变化，先把已确认的现状同步回列表。
    renderGroups(cells.groups, profile.groups);
    renderPermissions(cells.permissions, profile.permissions);
    // 若两个并发查询之间新增了成员组，阻止提交不完整的集合，重新打开即可取到新目录。
    const available = new Set(catalog.items.map(group => group.key));
    if (profile.groups.some(group => !available.has(group.key))) {
      throw new Error("分组目录已更新，请重新打开编辑窗口。");
    }
    renderChoices(catalog.items, session.original);
    byId("choices").hidden = byId("choices").disabled = false;
    announce("edit-status", catalog.items.length ? "" : "暂无可分配的权限组。");
  } catch (error) {
    if (editing === session) announce("edit-status", "读取分组失败。 " + error.message, "error");
  }
}

/** 关闭编辑器并取消尚未完成的查询，保存事务结束前保留操作现场。 */
function closeEditor() {
  if (!editing || editing.saving) return;
  editing.controller.abort();
  editing = null;
  dialog.close();
}

/** 提交完整组键列表；失败保留草稿，成功后显示服务器返回的实际成员关系。 */
async function saveGroups(event) {
  event.preventDefault();
  if (!editing || byId("save").disabled) return;
  const session = editing;
  const groupKeys = new FormData(form).getAll("group");
  session.saving = true;
  byId("save").disabled = byId("cancel").disabled = byId("choices").disabled = true;
  announce("edit-status", "正在保存分组…");
  try {
    const profile = await request(session.path, {method: "PUT", body: JSON.stringify({group_keys: groupKeys})});
    renderGroups(session.cells.groups, profile.groups);
    renderPermissions(session.cells.permissions, profile.permissions);
    session.saving = false;
    closeEditor();
    announce("list-status", "已保存 " + session.user.username + " 的权限组。", "success");
  } catch (error) {
    session.saving = false;
    byId("choices").disabled = byId("cancel").disabled = byId("save").disabled = false;
    announce("edit-status", "未能确认保存结果。 " + error.message, "error");
  }
}

renderAdminNav(byId("nav"), "users");
byId("refresh").addEventListener("click", () => loadPage(pageIndex, pageCursors[pageIndex]));
byId("next").addEventListener("click", () => loadPage(pageIndex + 1, nextCursor));
byId("previous").addEventListener("click", () => loadPage(pageIndex - 1, pageCursors[pageIndex - 1]));
byId("choices").addEventListener("change", updateSelection);
byId("cancel").addEventListener("click", closeEditor);
dialog.addEventListener("cancel", event => { event.preventDefault(); closeEditor(); });
form.addEventListener("submit", saveGroups);
void loadPage(0, null);
