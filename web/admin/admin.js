import { pages, renderAdminNav } from "./navigation.js";
import { renderOverview } from "./overview.js";

const labels = {
  id:"ID", action:"请求", result:"结果", duration_ms:"耗时", target:"目标资源", actor:"调用方",
  created_at:"创建时间", updated_at:"更新时间", authorized_at:"授权提交时间", retry_at:"重试时间",
  heartbeat_at:"心跳时间", sampled_at:"采样时间", user_id:"用户 ID", request_id:"请求 ID",
  state:"任务状态", job_id:"任务 ID", batch_id:"批次 ID", strategy_version_id:"策略版本",
  version:"任务版本", blocked_reason:"阻塞原因", failure:"失败原因", current_execution_id:"当前执行",
  execution_id:"执行 ID", instance_id:"实例 ID", phase:"执行状态", stage:"当前阶段",
  attempt_no:"尝试序号", remaining_attempts:"剩余尝试次数", prompt_id:"引擎请求 ID",
  workflow_digest:"工作流摘要", budget_charged:"已计入预算", budget_refunded:"已返还预算",
  generation:"启动代次", nodes:"工作流节点", environment:"执行环境", log_url:"执行日志",
  configured:"已配置", accepting:"可接单", process_alive:"进程存活", api_reachable:"API 可达",
  occupied:"槽位占用", quarantine_reason:"隔离原因", stale:"心跳时效", metrics_stale:"GPU 采样时效",
  manifest:"环境清单", active_execution_id:"占用中的执行", metrics:"GPU 采样",
  gpu_name:"GPU 型号", gpu_index:"GPU 编号", gpu_memory_used_mib:"已用显存 (MiB)",
  gpu_memory_total_mib:"总显存 (MiB)", gpu_memory_peak_mib:"峰值显存 (MiB)",
  gpu_utilization_percent:"GPU 使用率 (%)", role:"用途", output_index:"产物序号",
  provider:"存储", size:"文件大小", media_type:"媒体类型", digest:"SHA-256",
  media:"媒体规格", complete:"完整", available:"可用", offset:"日志偏移", message:"日志内容",
  since:"起始时间", until:"截止时间", limit:"每页数量", level:"日志级别",
};
const stages = {
  QUEUED:"排队中", EXECUTING:"执行中", RECOVERING:"核对中", PUBLISHING:"发布中",
  RETRY_WAIT:"等待重试", SUCCEEDED:"成功", FAILED:"失败", CANCELLED:"已取消",
  PREPARING:"准备中", PREPARED:"准备就绪", SUBMITTING:"提交中", ACCEPTED:"已受理",
  RUNNING:"运行中", UNCONFIRMED:"待确认", QUARANTINED:"隔离中", INTERRUPTED:"已中断",
  LOST:"执行丢失", NOT_SUBMITTED:"未提交",
};
const references = {
  job_id:"jobs", batch_id:"batches", execution_id:"executions", instance_id:"instances",
  current_execution_id:"executions", active_execution_id:"executions",
};

const byId = id => document.getElementById(id);
const main = byId("data");
const reload = byId("reload");
const auto = byId("auto");
const formatter = new Intl.DateTimeFormat("zh-CN", {
  year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false,
});
let route;
let running;
let refreshTimer;
let loaded = false;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function url(view, id, query) {
  const path = "#/" + view + (id ? "/" + encodeURIComponent(id) : "");
  const search = new URLSearchParams(query).toString();
  return path + (search ? "?" + search : "");
}

function link(title, view, id, query, className) {
  const anchor = element("a", title, className);
  anchor.href = url(view, id, query);
  return anchor;
}

function readRoute() {
  const [path, query = ""] = location.hash.slice(1).split("?");
  const [view = "overview", rawId, ...extra] = path.split("/").filter(Boolean);
  if (!Object.hasOwn(pages, view) || extra.length || (rawId && view === "overview")) return null;
  try {
    const id = rawId ? decodeURIComponent(rawId) : "";
    if (id === "." || id === ".." || id.includes("/") || id.includes("\\")) return null;
    const params = new URLSearchParams(query);
    const valid = new Set([...(pages[view].filters || []), "since", "until", "limit", "cursor"]);
    for (const key of [...params.keys()]) {
      if (!valid.has(key) || !params.get(key)) params.delete(key);
    }
    return { view, id, params };
  } catch {
    return null;
  }
}

function timestamp(value) {
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? String(value) : formatter.format(time);
}

function bytes(value) {
  if (value < 1024) return value + " B";
  const unit = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)));
  return (value / 1024 ** unit).toLocaleString("zh-CN", {maximumFractionDigits:2}) + " " + ["B", "KiB", "MiB", "GiB", "TiB"][unit];
}

function localTime(value) {
  const d = new Date(value);
  if (!value || Number.isNaN(d.getTime())) return "";
  return new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 19);
}

function inputField(form, grid, name, value) {
  const field = element("label", labels[name]);
  const isTime = name === "since" || name === "until";
  const control = element("input");
  control.name = name;
  control.autocomplete = "off";
  control.type = isTime ? "datetime-local" : (name === "limit" ? "number" : "text");
  if (isTime) control.step = "1";
  if (name === "limit") {
    control.min = "1"; control.max = "200"; control.placeholder = "50";
  }
  control.value = isTime ? localTime(value) : value || "";
  control.addEventListener("input", () => form.elements.until.setCustomValidity(""));
  field.append(control);
  grid.append(field);
}

function renderFilters() {
  const holder = byId("filters");
  holder.replaceChildren();
  if (route.view === "overview" || (route.id && route.view !== "logs")) return;
  const details = element("details", undefined, "panel query");
  const keys = route.id ? ["level"] : pages[route.view].filters;
  const active = [...route.params.keys()].filter(key => key !== "cursor");
  details.open = active.length > 0;
  details.append(element("summary", active.length ? "筛选条件 · " + active.length : "筛选条件"));
  const form = element("form");
  const grid = element("div", undefined, "query-grid");
  for (const name of [...keys, "since", "until", "limit"]) inputField(form, grid, name, route.params.get(name));
  const buttons = element("div", undefined, "toolbar");
  const submit = element("button", "应用筛选", "highlight");
  submit.type = "submit";
  buttons.append(submit, link("重置", route.view, route.id, null, "action"));
  form.append(grid, buttons);
  form.addEventListener("submit", event => {
    event.preventDefault();
    const query = new URLSearchParams();
    for (const [key, raw] of new FormData(form)) {
      const value = raw.trim();
      if (value) query.set(key, ["since", "until"].includes(key) ? new Date(value).toISOString() : value);
    }
    if (query.has("since") && query.has("until") && query.get("since") > query.get("until")) {
      form.elements.until.setCustomValidity("截止时间须晚于起始时间");
      form.elements.until.reportValidity();
      return;
    }
    const next = url(route.view, route.id, query);
    if (next === location.hash) void fetchView(); else location.hash = next;
  });
  details.append(form);
  holder.append(details);
}

function stateChip(key, value) {
  const tone = ["SUCCEEDED", "ACCEPTED"].includes(value) ? "good"
    : ["FAILED", "LOST", "QUARANTINED"].includes(value) ? "bad"
    : ["UNCONFIRMED", "RETRY_WAIT", "RECOVERING"].includes(value) ? "warn" : "";
  const chip = element("span", stages[value] || value, "state " + tone);
  chip.title = key + " = " + value;
  return chip;
}

function valueNode(view, row, key, detail = false) {
  const value = row[key];
  if (value === null || value === undefined) return element("span", "—", "faint");
  if (key === "stale" || key === "metrics_stale")
    return element("span", value ? "过期" : "有效", "state " + (value ? "warn" : "good"));
  if (typeof value === "boolean") return element("span", value ? "是" : "否");
  if (key === "state" || key === "phase") return stateChip(key, value);
  if (key.endsWith("_at")) {
    const stamp = element("time", timestamp(value));
    stamp.dateTime = value; stamp.title = value;
    return stamp;
  }
  if (key === "log_url") return link("查看执行日志 →", "logs", row.id);
  if (key === "duration_ms") return element("span", value + " ms", "code");
  if (key === "size") return element("span", bytes(value), "code");
  if (key === "message") return element("div", value, "message code");
  if (key === "result" && view === "requests") {
    return element("span", value, "state code " + (value >= 500 ? "bad" : value >= 400 ? "warn" : "good"));
  }
  let target = references[key];
  if (key === "id" && !detail) target = view;
  if (key === "target" && view === "requests") {
    const byPrefix = {b:"batches", j:"jobs", ex:"executions", a:"files"};
    const prefix = String(value).split("_")[0];
    target = Object.hasOwn(byPrefix, prefix) ? byPrefix[prefix] : null;
  }
  if (target) {
    const anchor = link(value, target, value, null, "code id-value");
    anchor.title = value;
    return anchor;
  }
  return element("span", value, key === "id" || key.endsWith("_id") ? "code" : "");
}

function pageLinks(data) {
  const controls = element("div", undefined, "pagination");
  controls.append(element("span", "本页 " + data.items.length + (route.view === "logs" ? " 行" : " 条记录")));
  const query = new URLSearchParams(route.params);
  if (query.has("cursor")) {
    query.delete("cursor");
    controls.append(link("返回首页", route.view, route.id, query, "action"));
  }
  if (data.next_cursor) {
    query.set("cursor", data.next_cursor);
    controls.append(link("下一页 →", route.view, route.id, query, "action"));
  }
  return controls;
}

function listView(data) {
  const view = route.view;
  const panel = element("div", undefined, "panel");
  if (view === "logs" && route.id)
    panel.append(link("返回执行详情", "executions", route.id, null, "action"));
  if (!data.items.length) {
    panel.append(element("div", data.next_cursor ? "本页没有匹配记录，可继续翻页。" : "暂无匹配记录", "empty"));
  } else {
    const wrap = element("div", undefined, "tablebox");
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "region");
    wrap.setAttribute("aria-label", pages[view].title + "列表，可横向滚动");
    const table = element("table");
    const header = table.createTHead().insertRow();
    for (const key of pages[view].columns) {
      const th = element("th", labels[key]);
      th.scope = "col"; header.append(th);
    }
    const body = table.createTBody();
    for (const row of data.items) {
      const tr = body.insertRow();
      for (const key of pages[view].columns) tr.insertCell().append(valueNode(view, row, key));
    }
    wrap.append(table); panel.append(wrap);
  }
  panel.append(pageLinks(data));
  return panel;
}

function relatedLinks(view, row) {
  const box = element("div", undefined, "related");
  box.append(link("← " + pages[view].title, view, null, null, "action"));
  const links = {
    batches:[["批内任务","jobs","batch_id"],["请求审计","requests","batch_id"]],
    jobs:[["执行历史","executions","job_id"],["任务文件","files","job_id"],["关联日志","logs","job_id"]],
    executions:[["执行产物","files","execution_id"],["执行日志","logs"]],
    instances:[["执行记录","executions","instance_id"],["实例日志","logs","instance_id"]],
  };
  for (const [label, target, filter] of links[view] || []) {
    box.append(filter ? link(label,target,null,{[filter]:row.id},"action") : link(label,target,row.id,null,"action"));
  }
  return box;
}

function detailView(row) {
  const view = route.view;
  const container = element("div");
  const panel = element("div", undefined, "panel detail");
  const props = element("dl");
  for (const [key, value] of Object.entries(row)) {
    if (value !== null && typeof value === "object") continue;
    const group = element("div", undefined, "property");
    const dd = element("dd");
    dd.append(valueNode(view, row, key, true));
    group.append(element("dt", labels[key] || key), dd); props.append(group);
  }
  panel.append(props);
  for (const [key,value] of Object.entries(row)) {
    if (!value || typeof value !== "object") continue;
    const section = element("details", undefined, "structured");
    section.dataset.field = key;
    section.open = key === "metrics" || key === "result";
    section.append(element("summary", labels[key] || key));
    if (key === "metrics" && Object.keys(value).length) {
      const list = element("dl");
      for (const [field,reading] of Object.entries(value)) {
        const item = element("div", undefined, "property");
        item.append(element("dt", labels[field] || field), element("dd", field === "sampled_at" && reading ? timestamp(reading) : (reading ?? "—"), "code"));
        list.append(item);
      }
      section.append(list);
    } else {
      section.append(element("pre", JSON.stringify(value, null, 2)));
    }
    panel.append(section);
  }
  container.append(relatedLinks(view, row), panel);
  return container;
}

function liveView() {
  return route && (route.view === "overview" || route.view === "instances");
}

function schedule() {
  clearTimeout(refreshTimer);
  if (liveView() && auto.checked && !document.hidden) {
    refreshTimer = setTimeout(fetchView, 10000);
  }
}

function replaceSnapshot(content) {
  // 同一视图自动刷新时保留展开的采样详情和横向表格位置。
  const expanded = new Map(Array.from(main.querySelectorAll("details[data-field]"), node => [node.dataset.field, node.open]));
  for (const node of content.querySelectorAll("details[data-field]")) {
    if (expanded.has(node.dataset.field)) node.open = expanded.get(node.dataset.field);
  }
  const scroll = main.querySelector('[role="region"]')?.scrollLeft || 0;
  const focused = document.activeElement;
  const focusedLink = main.contains(focused) ? focused.closest("a")?.href : null;
  const focusedField = main.contains(focused) ? focused.closest("details")?.dataset.field : null;
  main.replaceChildren(content);
  const region = main.querySelector('[role="region"]');
  if (region) region.scrollLeft = scroll;
  if (focusedLink) {
    Array.from(main.querySelectorAll("a")).find(anchor => anchor.href === focusedLink)?.focus({preventScroll:true});
  } else if (focusedField) {
    Array.from(main.querySelectorAll("details")).find(node => node.dataset.field === focusedField)?.querySelector("summary")?.focus({preventScroll:true});
  }
}

async function fetchView() {
  clearTimeout(refreshTimer);
  if (!route) return;
  running?.abort();
  const request = new AbortController();
  running = request;
  reload.disabled = true;
  main.setAttribute("aria-busy", "true");
  const notice = byId("notice");
  notice.replaceChildren();
  const params = !route.id || route.view === "logs" ? route.params.toString() : "";
  const endpoint = "/local/v1/monitor/" + route.view + (route.id ? "/" + encodeURIComponent(route.id) : "") + (params ? "?" + params : "");
  try {
    const response = await fetch(endpoint, {credentials:"omit", cache:"no-store", signal:request.signal});
    let data;
    try { data = await response.json(); } catch { throw new Error("监控服务响应格式异常（HTTP " + response.status + "）"); }
    if (!response.ok) {
      const err = new Error([data.error?.message || response.statusText, data.request_id].filter(Boolean).join(" · "));
      err.code = data.error?.code;
      throw err;
    }
    if (running !== request) return;
    const content = route.view === "overview" ? renderOverview(data)
      : route.id && route.view !== "logs" ? detailView(data) : listView(data);
    replaceSnapshot(content);
    byId("updated").textContent = "更新于 " + timestamp(new Date());
    loaded = true;
  } catch (error) {
    if (running !== request || request.signal.aborted) return;
    notice.textContent = (loaded ? "刷新失败，保留上次结果。" : "暂时无法读取数据。") + " " + error.message;
    if (route.params.has("cursor")) {
      const params = new URLSearchParams(route.params);
      params.delete("cursor");
      notice.append(link("从首页重新查询", route.view, route.id, params));
    }
  } finally {
    if (running === request) {
      running = null;
      reload.disabled = false;
      main.setAttribute("aria-busy", "false");
      schedule();
    }
  }
}

function navigate() {
  clearTimeout(refreshTimer);
  running?.abort();
  running = null;
  route = readRoute();
  if (!route) { location.replace("#/overview"); return; }
  loaded = false;
  const title = pages[route.view].title + (route.id && route.view !== "logs" ? "详情" : "");
  byId("title").textContent = title;
  byId("location").textContent = pages[route.view].title;
  document.title = title + " · MyComfy";
  for (const anchor of byId("nav").children) {
    if (anchor.dataset.view === route.view) anchor.setAttribute("aria-current","page");
    else anchor.removeAttribute("aria-current");
  }
  byId("auto-label").hidden = !liveView();
  byId("updated").textContent = "";
  main.replaceChildren();
  renderFilters();
  void fetchView();
}

renderAdminNav(byId("nav"));
reload.addEventListener("click", fetchView);
auto.addEventListener("change", schedule);
document.querySelector(".skip").addEventListener("click", event => {
  event.preventDefault();
  byId("content").focus();
});
window.addEventListener("hashchange", navigate);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && auto.checked && liveView()) void fetchView(); else schedule();
});
navigate();
