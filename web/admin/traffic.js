import {renderAdminNav} from "./navigation.js";

/** 查找流量页已定义的控件。 */
const byId = id => document.getElementById(id);
const dialog = byId("rule-editor");
const fieldKeys = ["edge_rpm", "edge_burst", "user_rpm", "provider_concurrency"];
const labels = {public: "业务端", local: "管理端"};
const counts = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2});
let snapshot = null;
let reading = null;
let polling = null;
let refreshTimer;
let editing = null;
let writing = false;

/** 集中处理后端缺失的指标，未知值显示为破折号。 */
function number(value) {
  return Number.isFinite(value) ? counts.format(value) : "—";
}

/** 低频接口在长时窗内仍有访问时，避免让两位小数把速率显示为零。 */
function rate(value, requests) {
  return Number.isFinite(value) && value < 0.01 && requests > 0 ? "< 0.01" : number(value);
}

/** 在正确的朗读区域呈现操作结果。 */
function announce(id, message, tone = "") {
  if (byId(id).textContent !== message) byId(id).textContent = message;
  byId(id).dataset.tone = tone;
}

/** 调用受同源管理页约束的流量接口，HTTP 错误保留状态码以便区分。 */
async function request(path, options = {}) {
  const headers = {"X-MyComfy-Admin": "traffic"};
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch("/local/v1/traffic" + path, {
    mode: "same-origin", credentials: "omit", redirect: "error", cache: "no-store", headers, ...options,
  });
  if (response.status === 403) throw new Error("请从配置的管理后台地址打开此页。");
  if (response.status === 422) throw new Error("规则未通过校验，请检查限额范围与接口适用范围。");
  if (!response.ok) throw new Error("流量接口返回 HTTP " + response.status + "。");
  return response.json();
}

/** 总览只统计当前时窗，筛选接口表时保持总览和网关同步信息不变。 */
function renderSummary() {
  const {items, gateway, window_minutes, as_of} = snapshot;
  const total = key => items.reduce((count, row) => count + row.metrics[key], 0);
  const observed = total("requests");
  byId("requests-total").textContent = number(observed);
  byId("rpm-total").textContent = rate(observed / window_minutes, observed);
  byId("edge-total").textContent = number(gateway.metrics_available ? total("edge_rejections") : null);
  byId("core-total").textContent = number(total("core_rejections"));
  byId("metrics-note").hidden = gateway.metrics_available;
  byId("metrics-note").textContent = gateway.metrics_available ? ""
    : "网关计数暂不可用，当前请求量、请求速率、错误数和响应耗时仅统计进入 Core 的流量。";
  renderGateway(gateway, as_of);
}

/** 展示期望版本及实际网关回执；写入成功时也不推定网关已应用。 */
function renderGateway(gateway, asOf) {
  const sync = byId("edge-sync");
  const states = {
    applied: ["网关已同步", "good"], pending: ["等待网关应用", "warn"],
    unavailable: ["网关同步状态不可用", "warn"], error: ["网关应用失败", "bad"],
  };
  const [label, tone] = states[gateway.state] ?? states.unavailable;
  if (sync.textContent !== label) sync.textContent = label;
  sync.className = "state " + tone;
  sync.dataset.state = gateway.state;
  const details = [];
  if (gateway.desired_revision != null) details.push("目标版本 " + gateway.desired_revision);
  if (gateway.applied_revision != null) details.push("已应用 " + gateway.applied_revision);
  if (gateway.updated_at) details.push("网关回报 " + new Date(gateway.updated_at).toLocaleString("zh-CN"));
  if (asOf) details.push("统计截至 " + new Date(asOf).toLocaleString("zh-CN"));
  if (snapshot.metrics_source) details.push("统计来源 " + (snapshot.metrics_source === "gateway" ? "网关" : "Core"));
  byId("sync-detail").textContent = details.join(" · ");
}

/** 表格单元格主行显示当前值，小字显示口径或默认值。 */
function cell(row, value, detail = "") {
  const td = row.insertCell();
  td.textContent = value;
  if (detail) {
    const note = document.createElement("small");
    note.textContent = detail;
    td.append(note);
  }
  return td;
}

/** 根据两个层级各自的作用范围显示当前限额。 */
function describe(rule, route, layer) {
  if (layer === "edge") {
    return rule.edge_rpm ? number(rule.edge_rpm) + " 次/分 · 突发 " + number(rule.edge_burst)
      : "关闭 · 突发 " + number(rule.edge_burst);
  }
  const limits = [];
  if (route.user_scoped) limits.push("用户 " + (rule.user_rpm ? number(rule.user_rpm) + " 次/分" : "关闭"));
  if (route.provider_scoped) limits.push("服务商 " + (rule.provider_concurrency ? number(rule.provider_concurrency) + " 并发" : "关闭"));
  return limits.join(" · ") || "不适用";
}

/** 用接口模板、方法和入口渲染目录，每一行的统计和两层规则都来自同一次读取。 */
function renderRows(quiet = false) {
  if (!snapshot) return;
  const listener = byId("listener").value;
  const needle = byId("route-search").value.trim().toLowerCase();
  const fragment = document.createDocumentFragment();
  for (const route of snapshot.items) {
    const name = route.method + " " + route.path;
    if ((listener && listener !== route.listener) || !name.toLowerCase().includes(needle)) continue;
    const tr = document.createElement("tr");
    tr.dataset.key = route.listener + ":" + name;
    const identity = tr.insertCell();
    const method = document.createElement("span");
    method.className = "method";
    method.textContent = route.method;
    const path = document.createElement("span");
    path.className = "route-path";
    path.textContent = route.path;
    const scope = document.createElement("small");
    scope.textContent = (labels[route.listener] || route.listener) + " · " + (route.overridden ? "自定义规则" : "默认规则");
    identity.append(method, path, scope);
    const m = route.metrics;
    cell(tr, number(m.requests), rate(m.requests_per_minute, m.requests) + " 次/分").dataset.metric = "requests";
    cell(tr, number(m.edge_rejections)).dataset.metric = "edge_rejections";
    cell(tr, number(m.core_rejections)).dataset.metric = "core_rejections";
    cell(tr, number(m.errors_4xx) + " / " + number(m.errors_5xx),
      m.gateway_failures ? "网关产生的 5xx 共 " + number(m.gateway_failures) + " 次" : "").className = "number";
    cell(tr, m.avg_latency_ms == null ? "—" : number(m.avg_latency_ms) + " ms",
      m.max_latency_ms == null ? "" : "最大 " + number(m.max_latency_ms) + " ms");
    for (const layer of ["edge", "core"]) {
      const current = describe(route.rule, route, layer);
      const defaults = describe(route.defaults, route, layer);
      cell(tr, current, route.overridden && defaults !== "不适用" ? "默认 " + defaults : "");
    }
    const edit = document.createElement("button");
    edit.type = "button";
    edit.textContent = "编辑规则";
    edit.setAttribute("aria-label", "编辑规则 " + name);
    edit.onclick = () => openEditor(route);
    tr.insertCell().append(edit);
    fragment.append(tr);
  }
  byId("no-routes").hidden = fragment.childElementCount > 0;
  const tbody = byId("traffic-rows");
  const next = Array.from(fragment.children);
  // 定时统计刷新只替换有变化的数据格，保留每个按钮的 DOM、键盘焦点和表格滚动位置。
  if (quiet === true && next.length === tbody.rows.length &&
      next.every((row, i) => row.dataset.key === tbody.rows[i].dataset.key)) {
    next.forEach((row, i) => {
      const live = tbody.rows[i];
      const cells = Array.from(row.cells);
      for (let j = 0; j < cells.length - 1; j++) {
        if (!live.cells[j].isEqualNode(cells[j])) live.cells[j].replaceWith(cells[j]);
      }
      live.querySelector("button").onclick = row.querySelector("button").onclick;
    });
  } else {
    const focused = quiet === true && tbody.contains(document.activeElement)
      ? document.activeElement.closest("tr")?.dataset.key : null;
    tbody.replaceChildren(fragment);
    if (focused) {
      const replacement = next.find(row => row.dataset.key === focused)?.querySelector("button");
      (replacement || byId("route-search")).focus({preventScroll: true});
    }
  }
}

/** 当前页可见且用户允许时，在上一笔请求结束后安排下一次读取。 */
function scheduleRefresh() {
  window.clearTimeout(refreshTimer);
  if (byId("auto-refresh").checked && !document.hidden && !dialog.open && !writing && !reading) {
    refreshTimer = window.setTimeout(() => load("", true), 15_000);
  }
}

/** 离开页面、暂停自动刷新或打开规则草稿时，取消后台读取，防止迟到的数据改动页面。 */
function updatePolling() {
  if (document.hidden || !byId("auto-refresh").checked || dialog.open) polling?.abort();
  scheduleRefresh();
}

/** 切换时段或手动刷新时丢弃旧读取；失败时保留上一次结果和对应时窗。 */
async function load(success = "", quiet = false) {
  if (quiet && (reading || dialog.open || writing || document.hidden || !byId("auto-refresh").checked)) return;
  window.clearTimeout(refreshTimer);
  reading?.abort();
  const controller = new AbortController();
  reading = controller;
  polling = quiet ? controller : null;
  const selector = byId("window-minutes");
  if (!quiet) {
    byId("refresh").disabled = true;
    byId("traffic-rows").inert = true;
    announce("list-status", "正在读取流量…");
  }
  try {
    const data = await request("?window_minutes=" + encodeURIComponent(selector.value), {signal: controller.signal});
    if (reading !== controller || controller.signal.aborted) return;
    snapshot = data;
    renderSummary();
    renderRows(quiet);
    if (!quiet || byId("list-status").dataset.tone === "error") announce("list-status", success, "success");
  } catch (error) {
    if (reading !== controller || controller.signal.aborted) return;
    if (snapshot) selector.value = String(snapshot.window_minutes);
    else if (byId("edge-sync").textContent !== "网关状态未知") byId("edge-sync").textContent = "网关状态未知";
    announce("list-status", (success ? success + " " : "") + "刷新失败。" + error.message, "error");
  } finally {
    if (reading === controller) {
      reading = null;
      polling = null;
      byId("refresh").disabled = false;
      byId("traffic-rows").inert = false;
      scheduleRefresh();
    }
  }
}

/** 编辑时使用服务端给出的当前值和默认值，仅展示该路由适用的 Core 限额。 */
function openEditor(route) {
  editing = route;
  byId("rule-target").textContent = (labels[route.listener] || route.listener) + " · " + route.method + " " + route.path;
  for (const key of fieldKeys) {
    const id = key.replaceAll("_", "-");
    byId(id).value = route.rule[key];
    byId(id + "-help").textContent = "默认 " + number(route.defaults[key]) +
      (key === "edge_burst" ? "。填 0 表示不额外放行突发请求。" : "。填 0 关闭此限制。");
  }
  byId("user-setting").hidden = !route.user_scoped;
  byId("provider-setting").hidden = !route.provider_scoped;
  byId("core-fields").hidden = !route.user_scoped && !route.provider_scoped;
  byId("user-rpm").disabled = !route.user_scoped;
  byId("provider-concurrency").disabled = !route.provider_scoped;
  byId("reset-rule").disabled = !route.overridden;
  announce("form-status", "");
  dialog.showModal();
  updatePolling();
}

/** 保存完整的两层限额，或通过 rule:null 删除该路由覆盖项。 */
async function save(reset) {
  if (writing || !editing) return;
  const {listener, method, path, overridden} = editing;
  const rule = reset ? null : Object.fromEntries(fieldKeys.map(key => {
    const input = byId(key.replaceAll("_", "-"));
    return [key, input.disabled ? 0 : input.valueAsNumber];
  }));
  writing = true;
  const locks = ["edge-fields", "core-fields", "save", "cancel", "reset-rule"];
  for (const id of locks) byId(id).disabled = true;
  announce("form-status", "正在保存…");
  try {
    const saved = await request("/rules", {method: "PUT", body: JSON.stringify({listener, method, path, rule})});
    // 写入回执先更新本行；即使随后的统计刷新失败，也不会让管理员继续编辑过期的限额。
    editing.rule = saved.rule;
    editing.defaults = saved.defaults;
    editing.overridden = saved.overridden;
    renderRows();
    renderGateway(saved.gateway, snapshot.as_of);
  } catch (error) {
    announce("form-status", error.message, "error");
    return;
  } finally {
    writing = false;
    for (const id of locks) byId(id).disabled = false;
    byId("reset-rule").disabled = !overridden;
  }
  dialog.close();
  await load(reset ? "已恢复默认规则。" : "已保存规则。");
}

renderAdminNav(byId("nav"), "traffic");
byId("listener").addEventListener("change", renderRows);
byId("route-search").addEventListener("input", renderRows);
byId("window-minutes").addEventListener("change", () => load());
byId("refresh").addEventListener("click", () => { if (!reading) void load(); });
byId("auto-refresh").addEventListener("change", updatePolling);
document.addEventListener("visibilitychange", updatePolling);
byId("cancel").addEventListener("click", () => dialog.close());
dialog.addEventListener("cancel", event => { if (writing) event.preventDefault(); });
dialog.addEventListener("close", () => { editing = null; scheduleRefresh(); });
byId("rule-form").addEventListener("submit", event => { event.preventDefault(); void save(false); });
byId("reset-rule").addEventListener("click", () => save(true));
void load();
