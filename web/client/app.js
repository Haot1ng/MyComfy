import {createSession, request} from "./session.js";
import {mountComposer} from "./composer.js";
import {mountHistory} from "./history.js";

const root = document.getElementById("app");
const tabKey = "mycomfy.tab";
const channel = typeof BroadcastChannel === "function" ? new BroadcastChannel("mycomfy.session") : null;
const sections = [
  ["/home", "工作台", "home"], ["/create", "创建批次", "create"], ["/jobs", "任务记录", "jobs"],
  ["/notifications", "站内通知", "bell"], ["/account", "账户设置", "account"],
];
const states = {
  QUEUED: "排队中", EXECUTING: "执行中", RECOVERING: "正在核对", RETRY_WAIT: "等待重试",
  PUBLISHING: "发布中", SUCCEEDED: "已成功", FAILED: "已失败", CANCELLED: "已取消",
};
const logo = '<span class="brandmark" aria-hidden="true"><i></i><i></i><i></i></span><strong>MyComfy</strong>';
let user;
let connection;
let currentView;
let routeReady;
let pageAbort;
let accountPending = false;
let stopGeneration = 0;

function navigate(path) {
  if (location.hash === "#" + path) return;
  location.hash = path;
}

function readPin() {
  try { return JSON.parse(sessionStorage.getItem(tabKey)); } catch { return null; }
}

function pin(value) {
  try { sessionStorage.setItem(tabKey, JSON.stringify(value)); } catch { /* Storage is optional. */ }
}

function identity(value) {
  return {user_id: value.user_id, session_generation: value.session_generation};
}

function sameSession(a, b) {
  return a && b && a.user_id === b.user_id && a.session_generation === b.session_generation;
}

function closeView() {
  pageAbort?.abort();
  currentView?.dispose?.();
  currentView = null;
}

function endSession(message = "") {
  stopGeneration++;
  closeView();
  connection?.dispose();
  connection = null;
  user = null;
  accountPending = false;
  pin({signedOut: true});
  history.replaceState(null, "", location.pathname + location.search);
  showAuth(false, message);
}

if (channel) {
  channel.onmessage = ({data}) => {
    if (!user || !data || accountPending) return;
    if ((data.kind === "opened" && !sameSession(user, data.identity)) ||
        (data.kind === "closed" && sameSession(user, data.identity))) {
      // Another tab's announcement may arrive after a newer login. Verify our own binding.
      void connection.api("/me").catch(() => {});
    }
  };
}

function messageFor(error) {
  const known = {
    AUTH_FAILED: "账号或密码不正确，请重新输入。",
    USERNAME_TAKEN: "这个用户名已被使用，请更换。",
    LOGIN_LIMIT: "登录尝试过于频繁，请稍后再试。",
    INVALID_PASSWORD: "新密码需要 8 至 1024 个字符。",
    INVALID_USERNAME: "请输入有效用户名。",
    SESSION_INVALID: "会话已失效，请重新登录。",
    ORIGIN_REJECTED: "网页来源与服务配置不一致，请从配置的工作台地址访问。",
  };
  const message = known[error.code] || (error instanceof TypeError ? "无法连接服务，请检查网络后重试。" : error.message);
  return message + (error.requestId ? "（请求号 " + error.requestId + "）" : "");
}

function showAuth(register, notice = "", name = "") {
  document.title = (register ? "注册" : "登录") + " · MyComfy";
  root.innerHTML = '<main class="auth-layout"><section class="auth-art" aria-label="视频生成工作台">' +
    '<div class="auth-brand">' + logo + '</div><div class="film-guides" aria-hidden="true"></div>' +
    '<div class="art-caption"><span class="viewfinder" aria-hidden="true"></span><h2>视频生成工作台</h2>' +
    '<p>从工作流与素材，到可预览、可下载的成片。</p></div></section>' +
    '<section class="auth-panel"><div class="auth-box"><div class="auth-switch" aria-label="账户操作">' +
    '<button type="button" data-mode="login">登录</button><button type="button" data-mode="register">注册</button></div>' +
    '<h1></h1><p class="auth-desc"></p><form class="stack"><label class="field">用户名' +
    '<input name="username" autocomplete="username" required maxlength="100"></label>' +
    '<label class="field">密码<span class="password-wrap"><input name="password" type="password" maxlength="1024" required>' +
    '<button type="button" class="show-pass" aria-label="显示密码" aria-pressed="false">显示</button></span></label>' +
    '<p class="form-status" role="status" aria-live="polite"></p><button class="btn primary auth-submit" type="submit"></button></form>' +
    '</div></section></main>';
  const box = root.querySelector(".auth-box");
  const form = box.querySelector("form");
  const status = form.querySelector(".form-status");
  const submit = form.querySelector(".auth-submit");
  const flip = form.querySelector(".show-pass");
  const password = form.elements.password;
  const username = form.elements.username;
  username.value = name;
  username.placeholder = "输入用户名";
  password.autocomplete = register ? "new-password" : "current-password";
  password.minLength = register ? 8 : 1;
  password.placeholder = register ? "设置至少 8 位密码" : "输入密码";
  box.querySelector("h1").textContent = register ? "创建账户" : "登录工作台";
  box.querySelector(".auth-desc").textContent = register ? "注册后，即可使用自己的素材和任务空间。" : "使用你的 MyComfy 账户继续创作。";
  status.textContent = notice;
  submit.textContent = register ? "创建账户" : "登录";
  const modes = box.querySelectorAll("[data-mode]");
  modes.forEach(button => {
    button.setAttribute("aria-pressed", String((button.dataset.mode === "register") === register));
    button.onclick = () => showAuth(button.dataset.mode === "register", "", username.value);
  });
  flip.onclick = () => {
    const showing = password.type === "password";
    password.type = showing ? "text" : "password";
    flip.textContent = showing ? "隐藏" : "显示";
    flip.setAttribute("aria-label", showing ? "隐藏密码" : "显示密码");
    flip.setAttribute("aria-pressed", String(showing));
  };
  form.onsubmit = async event => {
    event.preventDefault();
    if (submit.disabled) return;
    status.textContent = "";
    submit.disabled = true;
    modes.forEach(button => button.disabled = true);
    try {
      const credentials = {username: username.value, password: password.value};
      if (register) {
        await request("/accounts", {method: "POST", body: credentials});
        showAuth(false, "注册成功，请登录。", credentials.username);
        root.querySelector('[name="password"]').focus();
      } else {
        openSession(await request("/sessions", {method: "POST", body: credentials}), true);
      }
    } catch (error) { status.textContent = messageFor(error); }
    finally {
      submit.disabled = false;
      modes.forEach(button => button.disabled = false);
    }
  };
}

function openSession(next, announce) {
  closeView();
  connection?.dispose();
  accountPending = false;
  user = next;
  const generation = ++stopGeneration;
  pin(identity(user));
  if (announce) channel?.postMessage({kind: "opened", identity: identity(user)});
  connection = createSession(user, () => endSession("会话已失效，请重新登录。"));
  root.innerHTML = '<div class="studio-shell"><aside class="rail"><a class="brand" href="#/home" aria-label="MyComfy 工作台">' +
    logo + '</a><nav aria-label="工作台导航"></nav><div class="rail-foot"><span class="live-dot"></span>' +
    '<span id="live-state" role="status"></span></div></aside><div class="workspace"><header class="topbar">' +
    '<div class="breadcrumb">视频工作室<span>/</span><b id="location"></b></div>' +
    '<a class="user-link" href="#/account"><span class="avatar" aria-hidden="true"></span><span class="user-name"></span></a>' +
    '</header><main class="page" id="page"></main></div></div>';
  root.querySelector(".avatar").textContent = Array.from(user.username)[0].toUpperCase();
  root.querySelector(".user-name").textContent = user.username;
  const nav = root.querySelector("nav");
  for (const [path, label, icon] of sections) {
    const link = document.createElement("a");
    link.href = "#" + path;
    link.innerHTML = '<i aria-hidden="true" class="nav-icon icon-' + icon + '"></i><span>' + label + '</span>';
    if (path === "/notifications") {
      const dot = document.createElement("i");
      dot.className = "unread-dot";
      dot.hidden = true;
      dot.setAttribute("aria-label", "有未读通知");
      link.append(dot);
    }
    nav.append(link);
  }
  routeReady = renderRoute();
  startSync(generation);
}

let badgeRequest = 0;
async function refreshBadge() {
  const revision = ++badgeRequest;
  const result = await connection.api("/notifications?unread=true&limit=1");
  const dot = root.querySelector(".unread-dot");
  if (dot && revision === badgeRequest) dot.hidden = !result.items.length;
}

function startSync(generation) {
  connection.startEvents(async () => {
    await routeReady;
    if (generation !== stopGeneration) return;
    await Promise.all([currentView?.refresh?.(), refreshBadge()]);
  }, status => {
    if (generation !== stopGeneration) return;
    root.querySelector("#live-state").textContent = status;
    root.querySelector(".live-dot").classList.toggle("online", status === "实时同步");
  });
}

function accountView(host) {
  host.innerHTML = '<header class="page-title"><h1>账户设置</h1></header><section class="card account-card">' +
    '<h2>修改密码</h2><p class="muted">修改成功后，当前页签将使用新会话。</p><form class="stack">' +
    '<label class="field">当前密码<input type="password" name="old" required maxlength="1024" autocomplete="current-password"></label>' +
    '<label class="field">新密码<input type="password" name="next" required minlength="8" maxlength="1024" placeholder="至少 8 个字符" autocomplete="new-password"></label>' +
    '<p role="status" class="form-status"></p><button class="btn primary" type="submit">更新密码</button></form>' +
    '</section><div class="account-actions"><button type="button" class="btn sign-out">退出登录</button></div>';
  const form = host.querySelector("form");
  const status = host.querySelector('[role="status"]');
  const owner = identity(user);
  async function change(logout) {
    if (accountPending) return;
    accountPending = true;
    const generation = stopGeneration;
    // Abort old reads before the server changes the Cookie. The write survives route changes.
    connection.dispose();
    connection = createSession(user, () => endSession("会话已失效，请重新登录。"));
    host.querySelectorAll("button").forEach(button => button.disabled = true);
    try {
      if (logout) {
        await connection.api("/sessions/current", {method: "DELETE"});
        if (generation !== stopGeneration) return;
        channel?.postMessage({kind: "closed", identity: owner});
        endSession("已退出登录。");
      } else {
        const next = await connection.api("/me/password", {
          method: "POST", body: {current_password: form.elements.old.value, new_password: form.elements.next.value},
        });
        if (generation !== stopGeneration) return;
        openSession(next, true);
        const feedback = root.querySelector(".form-status");
        if (feedback) feedback.textContent = "密码已更新。";
      }
    } catch (error) {
      if (generation === stopGeneration) {
        status.textContent = messageFor(error);
        accountPending = false;
        if (location.hash !== "#/account") routeReady = renderRoute();
        startSync(generation);
        host.querySelectorAll("button").forEach(button => button.disabled = false);
      }
    }
  }
  form.onsubmit = event => { event.preventDefault(); void change(false); };
  host.querySelector(".sign-out").onclick = () => change(true);
}

function homeView(host, ctx) {
  host.innerHTML = '<header class="page-title"><h1>工作台</h1></header><section class="home-cover">' +
    '<div><h2>视频创作空间</h2><p>选择工作流、上传素材，开始一批新的生成任务。</p><a href="#/create" class="cover-action">创建批次 <span aria-hidden="true">↗</span></a></div></section>' +
    '<section class="card recent"><header><h2>最近任务</h2><a href="#/jobs">全部任务 <span aria-hidden="true">→</span></a></header><div class="recent-list">正在读取任务…</div></section>';
  const list = host.querySelector(".recent-list");
  let revision = 0;
  async function refresh() {
    const id = ++revision;
    try {
      const {items} = await ctx.api("/jobs?limit=5");
      if (id !== revision || ctx.signal.aborted) return;
      list.replaceChildren();
      if (!items.length) {
        const empty = document.createElement("p");
        empty.className = "home-empty";
        empty.textContent = "暂无生成任务。提交批次后，可以在这里查看进度。";
        list.append(empty);
      }
      for (const task of items) {
        const link = document.createElement("a");
        link.className = "task-entry";
        link.href = "#/job/" + encodeURIComponent(task.job_id);
        const title = document.createElement("strong");
        title.textContent = task.client_task_key;
        const time = document.createElement("time");
        time.textContent = new Date(task.created_at).toLocaleString("zh-CN", {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = states[task.state] || task.state;
        link.append(title, time, badge);
        list.append(link);
      }
    } catch (error) {
      if (!ctx.signal.aborted && id === revision) { list.textContent = messageFor(error); throw error; }
    }
  }
  return {refresh};
}

async function renderRoute() {
  closeView();
  if (!user) return;
  const path = location.hash.slice(1) || "/home";
  const parent = /^\/(?:job|batch)\//.test(path) ? "/jobs" : path;
  const section = sections.find(([key]) => key === parent);
  if (!section) { location.replace("#/home"); return; }
  const label = section[1];
  document.title = label + " · MyComfy";
  root.querySelector("#location").textContent = label;
  root.querySelectorAll("nav a").forEach(link => {
    if (link.hash === "#" + parent) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  const controller = new AbortController();
  pageAbort = controller;
  const client = connection;
  const ctx = {
    session: user, signal: controller.signal, navigate, mediaUrl: client.mediaUrl,
    onNotificationsChanged: () => refreshBadge().catch(() => {}),
    api: (path, options = {}) => client.api(path, {
      ...options, signal: options.signal ? AbortSignal.any([options.signal, controller.signal]) : controller.signal,
    }),
  };
  const host = document.createElement("div");
  root.querySelector("#page").replaceChildren(host);
  window.scrollTo(0, 0);
  try {
    let view;
    if (path === "/create") view = {dispose: await mountComposer(host, ctx)};
    else if (path === "/account") accountView(host);
    else if (path === "/home") { view = homeView(host, ctx); currentView = view; await view.refresh(); }
    else view = await mountHistory(host, ctx, path);
    if (controller.signal.aborted) view?.dispose?.();
    else currentView = view;
  } catch (error) {
    if (controller.signal.aborted) return;
    const message = document.createElement("p");
    message.setAttribute("role", "alert");
    message.textContent = messageFor(error);
    const button = document.createElement("button");
    button.className = "btn";
    button.textContent = "重新加载";
    button.onclick = () => { routeReady = renderRoute(); };
    host.replaceChildren(message, button);
  }
}

window.addEventListener("hashchange", () => { if (user && !accountPending) routeReady = renderRoute(); });
window.addEventListener("pageshow", event => { if (event.persisted) location.reload(); });
async function boot() {
  const previous = readPin();
  if (previous?.signedOut) { showAuth(false); return; }
  try {
    const headers = previous ? {"X-Page-User": previous.user_id, "X-Session-Generation": previous.session_generation} : {};
    const me = await request("/me", {headers});
    if (previous && !sameSession(previous, me)) { endSession("此页签的会话已替换，请重新登录。"); return; }
    openSession(me, false);
  } catch (error) {
    if (error.code === "SESSION_INVALID") { showAuth(false, previous ? "会话已失效，请重新登录。" : ""); return; }
    root.innerHTML = '<main class="boot"><p role="alert"></p><button class="btn">重新连接</button></main>';
    root.querySelector("p").textContent = messageFor(error);
    root.querySelector("button").onclick = boot;
  }
}
void boot();
