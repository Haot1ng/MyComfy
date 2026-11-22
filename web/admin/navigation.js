export const pages = {
  overview: { title: "运行总览", symbol: "▦" },
  requests: {
    title: "请求审计", symbol: "⇄", filters: ["request_id", "batch_id", "job_id", "execution_id", "instance_id", "user_id"],
    columns: ["id", "action", "result", "duration_ms", "target", "created_at"],
  },
  batches: {
    title: "批次", symbol: "▤", filters: ["batch_id", "user_id"], columns: ["id", "user_id", "created_at"],
  },
  jobs: {
    title: "任务", symbol: "≡", filters: ["job_id", "batch_id", "user_id"],
    columns: ["id", "state", "batch_id", "blocked_reason", "created_at"],
  },
  executions: {
    title: "执行记录", symbol: "▷", filters: ["execution_id", "job_id", "batch_id", "instance_id", "user_id"],
    columns: ["id", "phase", "stage", "attempt_no", "job_id", "instance_id"],
  },
  instances: {
    title: "实例与 GPU", symbol: "▣", filters: ["instance_id"],
    columns: ["id", "generation", "process_alive", "api_reachable", "accepting", "occupied", "stale", "metrics_stale"],
  },
  files: {
    title: "文件", symbol: "▱", filters: ["job_id", "execution_id", "instance_id", "user_id"],
    columns: ["id", "role", "media_type", "size", "complete", "available", "job_id"],
  },
  logs: {
    title: "引擎日志", symbol: "≔", filters: ["execution_id", "instance_id", "job_id", "batch_id", "user_id", "level"],
    columns: ["sampled_at", "instance_id", "generation", "execution_id", "message"],
  },
};

/** 构建本机管理侧栏，监控视图共用哈希路由，模板维护使用独立页面。 */
export function renderAdminNav(nav, currentView) {
  const links = document.createDocumentFragment();
  const monitorPath = currentView === "templates" ? "./#/" : "#/";
  const views = {...pages, templates: {title: "工作流模板", symbol: "◇"}};
  for (const [view, page] of Object.entries(views)) {
    const anchor = document.createElement("a");
    anchor.href = view === "templates" ? "./templates.html" : monitorPath + view;
    anchor.dataset.view = view;
    if (view === currentView) anchor.setAttribute("aria-current", "page");
    const symbol = document.createElement("span");
    symbol.className = "navsymbol";
    symbol.setAttribute("aria-hidden", "true");
    symbol.textContent = page.symbol;
    anchor.append(symbol, document.createTextNode(page.title));
    links.append(anchor);
  }
  nav.replaceChildren(links);
}
