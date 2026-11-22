const root = "/api/v1";
const jobEvents = [
  "job.queued", "job.executing", "job.recovering", "job.retry_wait", "job.publishing",
  "job.succeeded", "job.failed", "job.cancelled", "job.blocked_changed",
  "asset.available", "asset.unavailable",
];

// 任务参数中的整数种子可能超过 JavaScript 的安全整数范围。
export function parseInputJson(text) {
  return JSON.parse(text, (name, value, context) => {
    if (typeof value !== "number") return value;
    if (!Number.isFinite(value)) throw new Error("数值超出范围");
    if (!Number.isInteger(value) || Number.isSafeInteger(value)) return value;
    if (context?.source && typeof JSON.rawJSON === "function") return JSON.rawJSON(context.source);
    throw new Error("当前浏览器无法精确表示此整数");
  });
}

function parseDisplayJson(text) {
  if (typeof JSON.rawJSON === "function") return parseInputJson(text);
  // Older browsers can still display large seeds as exact decimal text.
  // Skip quoted strings so numbers inside prompts and asset IDs stay untouched.
  const tokens = /"(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;
  return JSON.parse(text.replace(tokens, token => {
    if (token.startsWith('"')) return token;
    const value = Number(token);
    return Number.isInteger(value) && !Number.isSafeInteger(value) ? JSON.stringify(token) : token;
  }));
}

export async function request(path, {preserveNumbers = false, ...options} = {}) {
  const headers = new Headers(options.headers);
  let body = options.body;
  if (body != null && !(body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }
  const response = await fetch(root + path, {
    ...options, headers, body, credentials: "same-origin", cache: "no-store",
  });
  if (response.status === 204) return null;
  const data = preserveNumbers && response.ok
    ? parseDisplayJson(await response.text())
    : await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(data?.error?.message || "服务暂时不可用，请稍后重试");
    Object.assign(error, {
      status: response.status, code: data?.error?.code, details: data?.error?.details,
      requestId: response.headers.get("X-Request-ID"), retryAfter: response.headers.get("Retry-After"),
    });
    throw error;
  }
  if (!data) throw new Error("服务器响应格式有误");
  return data;
}

/** 管理绑定到单个页签会话的请求、媒体和事件连接。 */
export function createSession(session, onInvalid, onPermissionsChanged = () => {}) {
  const lifetime = new AbortController();
  let stopStream = () => {};
  function dispose() {
    lifetime.abort();
    stopStream();
  }
  function invalidate() {
    if (lifetime.signal.aborted) return;
    dispose();
    onInvalid();
  }
  async function api(path, options = {}) {
    const headers = new Headers(options.headers);
    headers.set("X-Page-User", session.user_id);
    headers.set("X-Session-Generation", session.session_generation);
    if (options.method && !["GET", "HEAD"].includes(options.method.toUpperCase())) {
      headers.set("X-CSRF-Token", session.csrf_token);
    }
    const signal = options.signal
      ? AbortSignal.any([options.signal, lifetime.signal]) : lifetime.signal;
    try {
      return await request(path, {...options, headers, signal});
    } catch (error) {
      if (!signal.aborted && error.code === "SESSION_INVALID") invalidate();
      if (!signal.aborted && error.code === "PERMISSION_DENIED") onPermissionsChanged();
      throw error;
    }
  }

  function mediaUrl(path) {
    const url = new URL(path, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith(root + "/assets/")) {
      throw new Error("无效的文件地址");
    }
    url.searchParams.set("page_user", session.user_id);
    url.searchParams.set("session_generation", session.session_generation);
    return url.href;
  }

  function startEvents(refresh, onStatus) {
    stopStream();
    let stopped = false;
    let stream;
    let timer;
    let refreshTimer;
    let attempt = 0;
    let cursor;
    let delay = 1000;

    function reconnect(fullSync, immediate = false) {
      if (stopped) return;
      const nextAttempt = ++attempt;
      if (fullSync) cursor = null;
      stream?.close();
      stream = null;
      clearTimeout(timer);
      clearTimeout(refreshTimer);
      refreshTimer = null;
      if (globalThis.navigator?.onLine === false) {
        onStatus("离线，等待网络");
        return;
      }
      onStatus("重新连接中");
      timer = setTimeout(() => connect(fullSync, nextAttempt), immediate ? 0 : delay);
      delay = Math.min(delay * 2, 15000);
    }

    function changed(event) {
      if (stopped || event.currentTarget !== stream) return;
      if (!/^\d+$/.test(event.lastEventId)) { reconnect(true); return; }
      if (BigInt(event.lastEventId) <= BigInt(cursor)) return;
      cursor = event.lastEventId;
      if (!refreshTimer) {
        const scheduled = attempt;
        refreshTimer = setTimeout(async () => {
          refreshTimer = null;
          try { if (!stopped) await refresh(); }
          catch { if (!stopped && scheduled === attempt) reconnect(true); }
        }, 160);
      }
    }

    async function connect(fullSync, activeAttempt) {
      const current = () => !stopped && activeAttempt === attempt;
      try {
        await api("/me");
        if (!current()) return;
        if (fullSync || cursor == null) {
          const water = await api("/events/cursor");
          if (!current()) return;
          cursor = water.user_seq;
        }
        // A dropped stream may have delivered the last event before its queued refresh ran.
        await refresh();
        if (!current()) return;
        const query = new URLSearchParams({
          after: cursor, session_generation: session.session_generation, page_user: session.user_id,
        });
        const source = new EventSource(root + "/events?" + query);
        stream = source;
        source.onopen = () => { if (current()) { delay = 1000; onStatus("实时同步"); } };
        source.addEventListener("session.revoked", () => { if (current()) invalidate(); });
        source.addEventListener("permissions.changed", () => {
          if (!current()) return;
          onPermissionsChanged();
          reconnect(true);
        });
        for (const type of jobEvents) source.addEventListener(type, changed);
        source.onerror = () => {
          if (stream === source) reconnect(source.readyState === EventSource.CLOSED);
        };
      } catch {
        if (current()) reconnect(fullSync);
      }
    }
    function networkChanged() {
      delay = 1000;
      reconnect(cursor == null, true);
    }
    globalThis.addEventListener?.("offline", networkChanged);
    globalThis.addEventListener?.("online", networkChanged);
    stopStream = () => {
      stopped = true;
      globalThis.removeEventListener?.("offline", networkChanged);
      globalThis.removeEventListener?.("online", networkChanged);
      clearTimeout(timer);
      clearTimeout(refreshTimer);
      stream?.close();
    };
    onStatus("连接中");
    if (globalThis.navigator?.onLine === false) networkChanged();
    else void connect(true, attempt);
    return stopStream;
  }
  return {api, mediaUrl, startEvents, dispose};
}
