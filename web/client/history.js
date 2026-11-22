const states = {
  QUEUED: "排队中", EXECUTING: "执行中", RECOVERING: "核对中", RETRY_WAIT: "等待重试",
  PUBLISHING: "发布中", SUCCEEDED: "已成功", FAILED: "已失败", CANCELLED: "已取消",
};
const terminal = new Set(["SUCCEEDED", "FAILED", "CANCELLED"]);
const settledExecutions = new Set(["SUCCEEDED", "FAILED", "INTERRUPTED", "NOT_SUBMITTED", "LOST"]);
const phases = {
  PREPARING: "准备素材", PREPARED: "准备就绪", SUBMITTING: "正在提交", ACCEPTED: "引擎已接收",
  RUNNING: "引擎运行中", UNCONFIRMED: "等待核对", QUARANTINED: "实例已隔离",
  NOT_SUBMITTED: "提交前结束", SUCCEEDED: "引擎已完成", FAILED: "引擎执行失败",
  INTERRUPTED: "执行已中断", LOST: "执行已丢失",
};
const reasons = {
  NO_COMPATIBLE_DEPLOYMENT: "等待兼容的运行环境", OUTPUT_COUNT_MISMATCH: "输出文件数量未满足要求",
  INVALID_OUTPUT: "输出文件未通过校验", VOLUME_LOW: "存储空间不足",
};

const dateFormat = new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" });

function dateText(value) {
  const date = value && new Date(value);
  return date && Number.isFinite(+date) ? dateFormat.format(date) : "—";
}

function element(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.append(...children.filter(child => child != null));
  return node;
}

function button(label, action, className = "btn") {
  const control = element("button", className, label);
  control.type = "button";
  control.addEventListener("click", action);
  return control;
}

function link(label, path, ctx, className = "hist-link") {
  const node = element("a", className, label);
  node.href = "#" + path;
  node.addEventListener("click", event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    ctx.navigate(path);
  });
  return node;
}

function badge(state) {
  const node = element("span", "hist-state", states[state] || state);
  node.dataset.state = state;
  return node;
}

function selectField(title, choices) {
  const control = element("select");
  for (const [value, label] of choices) {
    const option = element("option", null, label);
    option.value = value;
    control.append(option);
  }
  return { control, label: element("label", "hist-field", title, control) };
}

function empty(title, description = "") {
  return element("div", "hist-empty", element("strong", null, title),
    description ? element("p", "muted", description) : null);
}

function facts(entries) {
  const list = element("dl", "hist-facts");
  for (const [title, value] of entries) {
    if (value == null || value === "") continue;
    list.append(element("div", null, element("dt", null, title), element("dd", null, value)));
  }
  return list;
}

function prettySize(size) {
  if (size < 1024) return size + " B";
  if (size < 1048576) return (size / 1024).toFixed(1) + " KiB";
  return (size / 1048576).toFixed(1) + " MiB";
}

function mediaDescription(asset) {
  const metadata = asset.media || {};
  const info = [asset.media_type, prettySize(asset.size)];
  if (metadata.width && metadata.height) info.push(metadata.width + " × " + metadata.height);
  if (metadata.duration != null) info.push(metadata.duration.toFixed(1) + " 秒");
  if (metadata.fps != null) info.push(Number(metadata.fps.toFixed(2)) + " fps");
  return info.join(" · ");
}

function downloadLink(url, complete) {
  const download = element("a", "btn", complete ? "下载" : "下载部分文件");
  const downloadUrl = new URL(url);
  downloadUrl.searchParams.set("download", "1");
  download.href = downloadUrl.href;
  download.download = "";
  return download;
}

function assetCard(asset, ctx) {
  const header = element("div", "hist-asset-head",
    element("div", null, element("strong", null, asset.role), element("p", "muted", mediaDescription(asset))));
  const card = element("article", "hist-asset", header);
  if (asset.available && asset.content_url && ctx.can("assets.download")) {
    const url = new URL(ctx.mediaUrl(asset.content_url), location.origin);
    header.append(downloadLink(url, asset.complete));
    if (asset.complete && asset.media_type.startsWith("video/")) {
      const video = element("video");
      if (video.canPlayType(asset.media_type)) {
        video.controls = true;
        video.playsInline = true;
        video.preload = "metadata";
        video.setAttribute("aria-label", asset.role + " 视频预览");
        video.src = url.href;
        video.addEventListener("error", () => {
          video.replaceWith(element("p", "hist-note", "暂时无法预览。可刷新任务或尝试下载。"));
        }, { once: true });
        card.append(video);
      } else {
        card.append(element("p", "hist-note", "当前浏览器无法预览此格式，可下载后播放。"));
      }
    }
  }
  if (!asset.available) card.append(element("p", "hist-note", "文件暂不可读取"));
  if (!asset.complete) card.append(element("p", "hist-note", "文件不完整"));
  card.append(element("details", "hist-file-detail", element("summary", null, "文件信息"),
    facts([["文件 ID", asset.asset_id], ["SHA-256", asset.digest]])));
  return card;
}

function makeJobTable(rows, ctx, showBatch) {
  const table = element("table", "hist-table");
  const headings = ["任务", "状态", ...(showBatch ? ["批次"] : []), "提交时间"];
  const header = element("tr");
  for (const text of headings) header.append(element("th", null, text));
  table.append(element("thead", null, header));
  const body = element("tbody");
  for (const job of rows) {
    const title = link(job.client_task_key, "/job/" + encodeURIComponent(job.job_id), ctx);
    const jobCell = element("td", null, title, element("small", "hist-id", job.job_id));
    const stateCell = element("td", null, badge(job.state));
    if (job.blocked_reason) stateCell.append(element("small", "hist-secondary", reasons[job.blocked_reason] || job.blocked_reason));
    if (job.state === "RETRY_WAIT" && job.retry_at) stateCell.append(element("small", "hist-secondary", dateText(job.retry_at)));
    const row = element("tr", null, jobCell, stateCell);
    if (showBatch) row.append(element("td", null, link(job.batch_id, "/batch/" + encodeURIComponent(job.batch_id), ctx, "hist-id hist-link")));
    row.append(element("td", "hist-date", dateText(job.created_at)));
    body.append(row);
  }
  table.append(body);
  return element("div", "hist-scroll", table);
}

function batchCounts(counts) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const legend = element("div", "hist-counts");
  const bar = element("div", "hist-count-bar");
  for (const state of Object.keys(states)) {
    if (!counts[state]) continue;
    const item = badge(state);
    item.append(" " + counts[state]);
    legend.append(item);
    const segment = element("span");
    segment.dataset.state = state;
    segment.style.flexGrow = counts[state];
    bar.append(segment);
  }
  bar.setAttribute("aria-hidden", "true");
  return element("div", "card hist-summary", element("h2", null, "共 " + total + " 个任务"), bar, legend);
}

function releaseMedia(node) {
  for (const media of node.querySelectorAll("img, video, audio")) {
    media.pause?.();
    media.removeAttribute("src");
    media.load?.();
  }
}

function originalAsset(id, asset, label, ctx) {
  const card = element("li", "hist-original-asset", element("code", "hist-id", id));
  if (!ctx.can("assets.download")) {
    card.append(element("small", "muted", "当前权限仅可查看素材引用"));
    return card;
  }
  if (asset) card.append(element("small", "muted", mediaDescription(asset)));
  if (!asset?.available || !asset.content_url) {
    card.append(element("span", "hist-note", "素材暂不可读取"));
    return card;
  }
  const url = new URL(ctx.mediaUrl(asset.content_url), location.origin);
  const actions = element("div", "hist-original-actions", downloadLink(url, asset.complete));
  card.append(actions);
  const kind = asset.media_type.split("/")[0];
  if (!asset.complete || !["image", "audio", "video"].includes(kind)) return card;

  const display = element("div", "hist-original-preview");
  const toggle = button("预览", () => {
    if (display.firstChild) {
      releaseMedia(display);
      display.replaceChildren();
      toggle.textContent = "预览";
      toggle.setAttribute("aria-expanded", "false");
      return;
    }
    if (!ctx.can("assets.download") || ctx.signal.aborted) return;
    const media = element(kind === "image" ? "img" : kind);
    if (kind === "image") {
      media.alt = label;
    } else {
      media.controls = true;
      media.preload = "metadata";
      media.setAttribute("aria-label", label);
      if (kind === "video") media.playsInline = true;
    }
    media.addEventListener("error", () => {
      if (media.parentNode === display) {
        media.replaceWith(element("p", "hist-note", "预览失败，可下载后查看。"));
      }
    }, { once: true });
    media.src = url.href;
    display.append(media);
    toggle.textContent = "收起预览";
    toggle.setAttribute("aria-expanded", "true");
  });
  toggle.setAttribute("aria-expanded", "false");
  actions.prepend(toggle);
  card.append(display);
  return card;
}

function renderOriginal(data, ctx) {
  const about = element("section", "card hist-panel", element("h2", null, data.client_task_key),
    facts([
      ["提交时间", dateText(data.created_at)],
      ["所属批次", link(data.batch_id, "/batch/" + encodeURIComponent(data.batch_id), ctx)],
      ["策略", data.strategy_name || data.strategy_key], ["版本", data.strategy_version],
      ["策略版本 ID", data.strategy_version_id],
    ]));
  const parameters = element("dl", "hist-original-fields");
  const materials = element("dl", "hist-original-fields");
  const assetFields = new Set(data.asset_fields);
  for (const [name, value] of Object.entries(data.inputs)) {
    const label = Object.hasOwn(data.field_labels, name) ? data.field_labels[name] : name;
    const title = element("dt", null, label, label !== name ? element("code", "hist-id", name) : null);
    if (assetFields.has(name)) {
      const ids = Array.isArray(value) ? value : [value];
      const content = element("dd");
      if (ids.length) {
        const assets = element("ol", "hist-original-assets");
        for (const id of ids) {
          const asset = Object.hasOwn(data.input_assets, id) ? data.input_assets[id] : null;
          assets.append(originalAsset(id, asset, label, ctx));
        }
        content.append(assets);
      } else {
        content.append(element("span", "muted", "空列表 []"));
      }
      materials.append(element("div", null, title, content));
    } else {
      const text = typeof value === "string" && value !== "" ? value : JSON.stringify(value, null, 2);
      parameters.append(element("div", null, title, element("dd", null, element("pre", null, text))));
    }
  }
  const sections = [about];
  if (parameters.childElementCount || !materials.childElementCount) {
    sections.push(element("section", "card hist-panel", element("h2", null, "输入参数"),
      parameters.childElementCount ? parameters : empty("没有输入参数")));
  }
  if (materials.childElementCount) {
    sections.push(element("section", "card hist-panel", element("h2", null, "输入素材"), materials));
  }
  return sections;
}

export function mountOriginal(host, ctx, jobId) {
  const path = "/job/" + encodeURIComponent(jobId);
  const body = element("div", "hist-results");
  const header = element("header", "hist-header", element("div", null,
    link("返回任务详情", path, ctx, "hist-back"), element("h1", null, "原始任务信息"),
    element("p", "hist-page-id", jobId)));
  const root = element("section", "history-view", header, body);
  host.replaceChildren(root);
  const controller = new AbortController();

  function dispose() {
    controller.abort();
    releaseMedia(root);
    ctx.signal.removeEventListener("abort", dispose);
    root.remove();
  }
  async function load() {
    if (controller.signal.aborted) return;
    const loading = element("p", "hist-loading", "正在读取原始任务信息…");
    loading.setAttribute("role", "status");
    body.replaceChildren(loading);
    try {
      const data = await ctx.api("/jobs/" + encodeURIComponent(jobId) + "/original", {
        signal: controller.signal, preserveNumbers: true,
      });
      if (!controller.signal.aborted) body.replaceChildren(...renderOriginal(data, ctx));
    } catch (error) {
      if (controller.signal.aborted) return;
      const title = error.status === 404 ? "任务不存在或无权查看" : "无法读取原始任务信息";
      const reason = [error.message, error.requestId && "请求 ID " + error.requestId].filter(Boolean).join(" · ");
      const notice = element("div", "hist-error", element("strong", null, title),
        element("span", null, reason), button("重试", load));
      notice.setAttribute("role", "alert");
      body.replaceChildren(notice);
    }
  }
  ctx.signal.addEventListener("abort", dispose, { once: true });
  if (ctx.signal.aborted) dispose();
  else void load();
  return { dispose };
}

export async function mountHistory(host, ctx, path) {
  const parts = path.split("/");
  const view = parts[1];
  const id = parts[2] ? decodeURIComponent(parts[2]) : null;
  const isJob = view === "job";
  const isBatch = view === "batch";
  const isInbox = view === "notifications";
  const titles = { jobs: "我的任务", job: "任务详情", batch: "批次详情", notifications: "站内通知" };
  const heading = element("h1", null, titles[view]);
  const header = element("header", "hist-header", element("div", null, heading));
  if (isBatch || isJob) {
    header.firstChild.prepend(link("‹  我的任务", "/jobs", ctx, "hist-back"));
    header.firstChild.append(element("p", "hist-page-id", id));
  } else if (view === "jobs") {
    header.append(link("新建批次", "/create", ctx, "btn primary"));
  }

  const controls = element("div", "hist-controls");
  const summary = element("div", "hist-summary-slot");
  const errorBox = element("div", "hist-error");
  const actionError = element("div", "hist-error");
  for (const box of [errorBox, actionError]) box.setAttribute("role", "alert");
  const results = element("div", "hist-results", element("p", "hist-loading", "读取中…"));
  const pagination = element("nav", "hist-pagination");
  pagination.setAttribute("aria-label", "分页");
  const root = element("section", "history-view", header, controls, summary, errorBox, actionError, results, pagination);
  host.replaceChildren(root);

  let disposed = false;
  let readController;
  let pendingRead;
  let pendingPath;
  let refreshAgain = false;
  let retryDelay = 2000;
  const writes = new AbortController();
  let nextCursor = null;
  const cursors = [null];
  const filters = new URLSearchParams();
  let currentJob = null;
  let cancelPending = false;
  let confirmCancel = false;
  let pollTimer;
  const unreadPending = new Set();
  const appliedJobs = new Map();
  const fileNodes = new Map();
  const fileGrid = element("div", "hist-assets");
  const fileArea = element("section", "card hist-panel", element("h2", null, "生成文件"), fileGrid);
  const detailArea = element("section", "card hist-panel");

  function dispose() {
    disposed = true;
    clearTimeout(pollTimer);
    readController?.abort();
    writes.abort();
    for (const cached of fileNodes.values()) releaseMedia(cached.node);
    fileNodes.clear();
    ctx.signal?.removeEventListener("abort", dispose);
    root.remove();
  }
  ctx.signal?.addEventListener("abort", dispose, { once: true });
  if (ctx.signal?.aborted) dispose();

  function showError(box, error, retry) {
    const title = error.status === 404 ? "记录不存在或无权查看" : "请求未完成";
    const message = [error.message, error.code, error.requestId && "请求 ID " + error.requestId].filter(Boolean).join(" · ");
    box.replaceChildren(element("strong", null, title), element("span", null, message));
    if (retry) box.append(button("重试", retry));
  }

  function filter(key, value) {
    if (value) filters.set(key, value);
    else filters.delete(key);
    cursors.splice(1);
    nextCursor = null;
    pagination.replaceChildren();
    results.replaceChildren(element("p", "hist-loading", "读取中…"));
    refresh();
  }

  if (view === "jobs") {
    const state = selectField("任务状态", [["", "全部状态"], ...Object.entries(states)]);
    state.control.onchange = () => filter("state", state.control.value);
    const batch = element("input");
    batch.type = "search";
    batch.placeholder = "输入批次 ID";
    const search = element("form", "hist-filter", element("label", "hist-field", "批次筛选", batch));
    const submit = element("button", "btn", "查询");
    search.append(submit);
    search.onsubmit = event => {
      event.preventDefault();
      filter("batch_id", batch.value.trim());
    };
    controls.append(state.label, search);
  }
  if (isInbox) {
    const unread = selectField("阅读状态", [["", "全部通知"], ["true", "未读"], ["false", "已读"]]);
    unread.control.onchange = () => filter("unread", unread.control.value);
    controls.append(unread.label);
  }

  function renderPagination() {
    pagination.replaceChildren();
    if (!nextCursor && cursors.length === 1) return;
    const previous = button("上一页", () => { cursors.pop(); refresh(); });
    previous.disabled = cursors.length === 1;
    const next = button("下一页", () => { cursors.push(nextCursor); refresh(); });
    next.disabled = !nextCursor;
    for (const node of [previous, next]) node.addEventListener("click", () => {
      previous.disabled = true;
      next.disabled = true;
    });
    pagination.append(previous, element("span", "muted", "第 " + cursors.length + " 页"), next);
  }

  async function markRead(note, control) {
    const key = note.notification_id;
    if (unreadPending.has(key)) return;
    unreadPending.add(key);
    control.disabled = true;
    actionError.replaceChildren();
    try {
      await ctx.api("/notifications/" + encodeURIComponent(key) + "/read", { method: "PUT", signal: writes.signal });
      if (disposed) return;
      await refresh();
      if (!disposed) ctx.onNotificationsChanged?.();
    } catch (error) {
      if (!disposed) showError(actionError, error);
    } finally {
      unreadPending.delete(key);
      control.disabled = false;
    }
  }

  function renderInbox(items) {
    if (!items.length) return empty(filters.get("unread") === "true" ? "没有未读通知" : "暂无通知");
    const list = element("ul", "hist-inbox");
    for (const note of items) {
      const state = note.data?.state;
      const title = "任务" + (states[state] || "有新动态");
      const item = element("li", note.read_at ? "hist-notice" : "hist-notice unread");
      const content = element("div", null, element("strong", null, title),
        element("p", "hist-secondary", dateText(note.created_at) + (note.read_at ? " · 已读" : " · 未读")));
      if (note.data?.job_id && ctx.can("jobs.view")) content.append(link(note.data.job_id, "/job/" + encodeURIComponent(note.data.job_id), ctx, "hist-id hist-link"));
      item.append(content);
      if (!note.read_at && ctx.can("notifications.mark_read")) {
        const read = button("标为已读", () => markRead(note, read));
        read.disabled = unreadPending.has(note.notification_id);
        item.append(read);
      }
      list.append(item);
    }
    return list;
  }

  function renderFiles(assets) {
    const visible = new Set(assets.map(asset => asset.asset_id));
    for (const [key, cached] of fileNodes) {
      if (!visible.has(key)) {
        releaseMedia(cached.node);
        cached.node.remove();
        fileNodes.delete(key);
      }
    }
    if (!assets.length) {
      fileGrid.replaceChildren(empty("暂无可见文件"));
      return;
    }
    fileGrid.querySelector(".hist-empty")?.remove();
    for (const [index, asset] of assets.entries()) {
      const key = asset.asset_id;
      const signature = JSON.stringify(asset);
      let cached = fileNodes.get(key);
      if (cached?.signature !== signature) {
        const node = assetCard(asset, ctx);
        if (cached) releaseMedia(cached.node);
        cached?.node.replaceWith(node);
        cached = { signature, node };
        fileNodes.set(key, cached);
      }
      if (fileGrid.children[index] !== cached.node) {
        fileGrid.insertBefore(cached.node, fileGrid.children[index] || null);
      }
    }
  }

  function windingDown(job) {
    return job?.state === "CANCELLED" && job.current_execution_id && !settledExecutions.has(job.execution_phase);
  }

  async function cancelJob() {
    if (!ctx.can("jobs.cancel") || cancelPending || !currentJob || terminal.has(currentJob.state)) return;
    cancelPending = true;
    confirmCancel = false;
    renderDetail(currentJob);
    actionError.replaceChildren();
    try {
      const result = await ctx.api("/jobs/" + encodeURIComponent(id) + "/cancel", { method: "POST", signal: writes.signal });
      if (disposed) return;
      if (result.job_version >= currentJob.job_version) {
        const next = { ...currentJob, ...result };
        // A cancellation can immediately revoke access to previously listed results.
        if (result.state === "CANCELLED") next.assets = [];
        renderDetail(next);
      }
      await refresh();
    } catch (error) {
      if (!disposed) showError(actionError, error);
    } finally {
      cancelPending = false;
      if (!disposed && currentJob) renderDetail(currentJob);
    }
  }

  function renderDetail(job) {
    if (currentJob && job.job_version < currentJob.job_version) return;
    currentJob = job;
    if (terminal.has(job.state)) confirmCancel = false;
    if (detailArea.parentNode !== results) results.replaceChildren(detailArea, fileArea);
    const top = element("div", "hist-section-heading", element("h2", null, job.client_task_key),
      element("div", "hist-original-actions",
        link("原始任务信息", "/job/" + encodeURIComponent(job.job_id) + "/original", ctx), badge(job.state)));
    detailArea.replaceChildren(top, facts([
      ["提交时间", dateText(job.created_at)],
      ["所属批次", link(job.batch_id, "/batch/" + encodeURIComponent(job.batch_id), ctx)],
      ["策略版本", job.strategy_version_id],
      ["尝试序号", job.attempt_no],
      ["执行状态", phases[job.execution_phase] || job.execution_phase],
      ["当前阶段", job.stage], ["重试时间", job.retry_at && dateText(job.retry_at)],
      ["阻塞原因", reasons[job.blocked_reason] || job.blocked_reason],
      [job.state === "CANCELLED" ? "原因记录" : "失败摘要", reasons[job.failure] || job.failure],
    ]));
    if (Number.isFinite(job.progress) && job.state === "EXECUTING") {
      const meter = element("progress");
      meter.max = 1;
      meter.value = job.progress;
      detailArea.append(element("label", "hist-progress", "执行进度 " + Math.round(meter.value * 100) + "%", meter));
    }
    if (windingDown(job)) detailArea.append(element("p", "hist-note", "取消已生效，原执行仍在收尾。"));
    if (!terminal.has(job.state) && ctx.can("jobs.cancel")) {
      if (confirmCancel) {
        const confirm = element("div", "hist-confirm", element("span", null, "确认取消此任务？"),
          button("继续生成", () => { confirmCancel = false; renderDetail(job); }),
          button("确认取消", cancelJob, "btn hist-danger"));
        detailArea.append(confirm);
      } else {
        const cancel = button(cancelPending ? "正在取消…" : "取消任务", () => { confirmCancel = true; renderDetail(job); });
        cancel.disabled = cancelPending;
        detailArea.append(cancel);
      }
    }
    renderFiles(job.assets || []);
  }

  function listPath() {
    const query = new URLSearchParams(filters);
    query.set("limit", "20");
    const cursor = cursors.at(-1);
    if (cursor) query.set("cursor", cursor);
    const base = isBatch ? "/batches/" + encodeURIComponent(id) : isInbox ? "/notifications" : "/jobs";
    return base + "?" + query;
  }

  function refresh() {
    if (disposed) return;
    clearTimeout(pollTimer);
    const endpoint = isJob ? "/jobs/" + encodeURIComponent(id) : listPath();
    // Bursts of events for the same page need one follow-up snapshot, even on a slow link.
    if (pendingRead && pendingPath === endpoint) {
      refreshAgain = true;
      return pendingRead;
    }
    readController?.abort();
    const request = new AbortController();
    readController = request;
    pendingPath = endpoint;
    refreshAgain = false;
    pendingRead = readPage(endpoint, request).finally(() => {
      if (disposed || request.signal.aborted) return;
      pendingRead = null;
      if (refreshAgain) refresh();
    });
    return pendingRead;
  }

  async function readPage(endpoint, request) {
    let retry = false;
    try {
      const data = await ctx.api(endpoint, { signal: request.signal });
      if (disposed || request.signal.aborted) return;
      retryDelay = 2000;
      errorBox.replaceChildren();
      if (isJob) {
        if (JSON.stringify(currentJob) !== JSON.stringify(data)) renderDetail(data);
      } else {
        if (isInbox && data.items.length === 0 && cursors.length > 1) {
          cursors.pop();
          return refresh();
        }
        nextCursor = data.next_cursor;
        if (isBatch) summary.replaceChildren(batchCounts(data.counts));
        if (isInbox) results.replaceChildren(renderInbox(data.items));
        else {
          const jobs = data.items.map(job => {
            const last = appliedJobs.get(job.job_id);
            if (last && last.job_version > job.job_version) return last;
            appliedJobs.set(job.job_id, job);
            return job;
          });
          results.replaceChildren(jobs.length ? makeJobTable(jobs, ctx, !isBatch) :
            empty("暂无任务", filters.size ? "可调整筛选条件后再试。" : "提交批次后可在这里查看生成进度。"));
        }
        renderPagination();
      }
    } catch (error) {
      if (disposed || request.signal.aborted) return;
      retry = !error.status || error.status >= 500;
      results.querySelector(".hist-loading")?.remove();
      if (error.status === 404 || error.status === 401) {
        if (isJob) renderFiles([]);
        results.replaceChildren();
        summary.replaceChildren();
        pagination.replaceChildren();
        currentJob = null;
      }
      showError(errorBox, error, () => {
        if (error.code === "INVALID_CURSOR") cursors.splice(1);
        refresh();
      });
    } finally {
      if (!disposed && !request.signal.aborted) {
        if (retry) {
          pollTimer = setTimeout(refresh, retryDelay);
          retryDelay = Math.min(retryDelay * 2, 30000);
        } else if (isJob && currentJob && (!terminal.has(currentJob.state) || windingDown(currentJob))) {
          pollTimer = setTimeout(refresh, 5000);
        }
      }
    }
  }

  await refresh();
  return { refresh, dispose };
}
