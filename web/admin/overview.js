const jobStates = new Map([
  ["QUEUED", ["排队中", "#96b8d9"]],
  ["EXECUTING", ["执行中", "#61d6c1"]],
  ["RECOVERING", ["核对中", "#b6a1ef"]],
  ["RETRY_WAIT", ["等待重试", "#f0b971"]],
  ["PUBLISHING", ["发布中", "#7f9cf1"]],
  ["SUCCEEDED", ["已成功", "#9bc98d"]],
  ["FAILED", ["已失败", "#f38f88"]],
  ["CANCELLED", ["已取消", "#89969d"]],
]);

const volumeRoles = new Map([
  ["storage", "正式存储"],
  ["spool", "上传暂存"],
  ["database", "数据库"],
  ["engine", "引擎工作区"],
]);

const identifiers = new Map([
  ["asset", "files"],
  ["execution", "executions"],
  ["instance", "instances"],
]);

const alertLabels = new Map([
  ["OBJECT_UNAVAILABLE", "文件暂不可用"],
  ["UNCONFIRMED", "执行等待核对"],
  ["QUARANTINED", "执行已隔离"],
  ["VOLUME_LOW", "存储卷可用空间不足"],
  ["STORAGE_UNAVAILABLE", "存储卷暂不可用"],
  ["EVENT_DISPATCH_DELAY", "用户事件分发延迟"],
]);

const numberFormat = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 1});

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function bytes(value) {
  if (!Number.isFinite(value)) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  const magnitude = Math.abs(value);
  const unit = magnitude === 0 ? 0 : Math.min(units.length - 1, Math.max(0, Math.floor(Math.log2(magnitude) / 10)));
  return numberFormat.format(value / (1024 ** unit)) + " " + units[unit];
}

function waitTime(seconds) {
  if (seconds < 60) return [Math.floor(seconds), "秒"];
  if (seconds < 3600) return [Math.floor(seconds / 60), "分钟"];
  return [numberFormat.format(seconds / 3600), "小时"];
}

function heading(title, side) {
  const header = element("div", "ov-heading");
  header.append(element("h2", "", title));
  if (side) header.append(side);
  return header;
}

function stats(data) {
  const cards = element("div", "ov-stats");
  const [wait, unit] = waitTime(data.oldest_wait_seconds);
  const values = [
    ["排队任务", numberFormat.format(data.queue_length), "项"],
    ["最久排队", wait, unit],
    ["等待重试", numberFormat.format(data.retries), "项"],
    ["隔离实例", numberFormat.format(data.isolated), "个"],
    ["待发布文件", numberFormat.format(data.publication_backlog), "份"],
  ];
  for (const [title, value, unit] of values) {
    const card = element("div", "ov-stat");
    const line = element("div", "ov-stat-count");
    line.append(element("strong", "", value), element("small", "", unit));
    card.append(element("div", "ov-stat-label", title), line);
    cards.append(card);
  }
  return cards;
}

function distribution(counts) {
  const section = element("section", "panel ov-panel");
  const table = element("dl", "ov-state-list");
  const bar = element("div", "ov-bar");
  bar.setAttribute("aria-hidden", "true");
  const states = new Map(jobStates);
  for (const state of Object.keys(counts)) {
    if (!states.has(state)) states.set(state, [state, "#a1b2b8"]);
  }
  let total = 0;
  for (const [state, [label, color]] of states) {
    const value = counts[state] ?? 0;
    total += value;
    const group = element("div", "ov-state");
    const name = element("dt", "", label);
    name.style.setProperty("--state-color", color);
    group.append(name, element("dd", "", numberFormat.format(value)));
    table.append(group);
    if (value > 0) {
      const segment = element("span");
      segment.style.backgroundColor = color;
      segment.style.flexGrow = value;
      bar.append(segment);
    }
  }
  section.append(heading("任务状态", element("span", "ov-counter", numberFormat.format(total) + " 项")), bar, table);
  return section;
}

function alertsPanel(alerts) {
  const entries = Object.entries(alerts);
  const section = element("section", "panel ov-panel");
  section.append(heading("当前告警", element("span", entries.length ? "ov-counter ov-warning" : "ov-counter", entries.length + " 条")));
  if (!entries.length) {
    section.append(element("div", "ov-clear", "当前无活动告警"));
    return section;
  }
  const list = element("ul", "ov-alerts");
  for (const [key, reason] of entries) {
    const row = element("li");
    const knownLabel = alertLabels.get(reason);
    row.append(element("strong", "", knownLabel || reason));
    if (knownLabel) row.append(element("code", "ov-reason", reason));
    const slash = key.indexOf("/");
    const collection = identifiers.get(key.slice(0, slash));
    if (slash > 0 && collection && key.length > slash + 1) {
      const link = element("a", "ov-object", key);
      link.href = "#/" + collection + "/" + encodeURIComponent(key.slice(slash + 1));
      row.append(link);
    } else {
      row.append(element("code", "ov-object", key));
    }
    list.append(row);
  }
  section.append(list);
  return section;
}

function volumePanel(volumes, alerts) {
  const section = element("section", "panel ov-panel ov-capacity");
  const entries = Object.entries(volumes);
  section.append(heading("存储卷水位", element("span", "ov-counter", entries.length + " 个卷")));
  if (!entries.length) {
    section.append(element("p", "ov-empty", "暂无存储卷采样"));
    return section;
  }
  const frame = element("div", "ov-scroll");
  frame.tabIndex = 0;
  frame.setAttribute("role", "region");
  frame.setAttribute("aria-label", "存储卷水位表");
  const table = element("table", "ov-volumes");
  const head = table.createTHead().insertRow();
  for (const text of ["设备 / 用途", "磁盘剩余", "已预留", "扣除预留后", "停止水位", "恢复水位", "接单状态"]) {
    const th = element("th", "", text);
    th.scope = "col";
    head.append(th);
  }
  const body = table.createTBody();
  for (const [device, value] of entries) {
    const row = body.insertRow();
    const name = element("th");
    name.scope = "row";
    name.append(element("strong", "", device), element("small", "", value.roles.map(r => volumeRoles.get(r) || r).join(" / ")));
    row.append(name);
    for (const n of [value.free_bytes, value.reserved_bytes, value.free_bytes - value.reserved_bytes, value.stop_bytes, value.resume_bytes]) {
      row.insertCell().textContent = bytes(n);
    }
    const isLow = alerts["volume/" + device] === "VOLUME_LOW";
    const state = value.paused ? "已暂停" : isLow ? "低于停止水位" : "正常";
    row.insertCell().append(element("span", value.paused || isLow ? "ov-warning" : "ov-good", state));
  }
  frame.append(table);
  section.append(frame);
  return section;
}

export function renderOverview(data) {
  const dashboard = element("div", "ov-dashboard");
  const mid = element("div", "ov-mid");
  mid.append(distribution(data.states), alertsPanel(data.alerts));
  dashboard.append(stats(data), mid, volumePanel(data.volumes, data.alerts));
  const time = new Date(data.sampled_at);
  if (Number.isFinite(time.getTime())) {
    const footer = element("p", "ov-sampled", "采样时间 ");
    const sampled = element("time", "", time.toLocaleString("zh-CN", {hour12: false}));
    sampled.dateTime = time.toISOString();
    footer.append(sampled);
    dashboard.append(footer);
  }
  return dashboard;
}
