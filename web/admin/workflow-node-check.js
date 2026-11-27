/** 按每个引擎列出缺少的节点，保留同一类型在工作流中的所有出现位置。 */
export function describeNodeCheck({status, nodes, instances}) {
  if (!["ok", "missing", "unknown"].includes(status) || !Array.isArray(nodes) || !Array.isArray(instances)) {
    throw new Error("节点检查响应无效。");
  }
  const byType = new Map(nodes.map(node => [node.class_type, node]));
  const available = instances.filter(instance => instance.status === "ok").map(instance => instance.id);
  const lines = [];

  /** 用工作流节点编号和可用的包名定位节点；服务端未识别来源时保留原类型名。 */
  function describeType(type) {
    const node = byType.get(type);
    const ids = node?.node_ids?.map(id => "#" + id).join("、");
    return [type, ids, node?.package].filter(Boolean).join(" · ");
  }

  if (status === "ok") {
    lines.push("节点检查通过" + (available.length ? "（" + available.join("、") + "）" : "") + "。");
  } else if (status === "missing") {
    lines.push("节点检查失败。当前没有一台匹配的引擎具备工作流需要的全部节点。");
  } else {
    lines.push(instances.length
      ? "节点待核实。匹配引擎的节点清单不完整，仍可发布完整模板。"
      : "节点待核实。当前没有与此工作流匹配的已配置引擎，仍可预先发布完整模板。");
  }
  // 即使一台引擎已通过，也标明其他引擎的缺项。包名只作定位提示，判断来自实际节点清单。
  const missingTypes = new Set();
  for (const instance of instances.filter(instance => instance.status === "missing")) {
    lines.push(instance.id + " 缺少");
    for (const type of instance.missing) {
      missingTypes.add(type);
      lines.push(describeType(type) + (byType.get(type)?.source === "custom" ? "（自定义节点）" : ""));
    }
  }
  const custom = nodes.filter(node => node.source === "custom" && !missingTypes.has(node.class_type));
  if (custom.length) lines.push("工作流引用的自定义节点", ...custom.map(node => describeType(node.class_type)));
  if (status === "missing") {
    lines.push("请核对 ComfyUI 版本和自定义节点包。补齐节点并重新部署引擎后，点击「重新检查节点」。");
  }
  return lines.join("\n");
}

/** 检查当前显示的工作流。切换文件时取消旧请求，引擎更新后可原地重试。 */
export function mountNodeCheck(container, onChange = () => {}) {
  const status = document.createElement("p");
  status.className = "template-message";
  status.setAttribute("role", "status");
  const retry = document.createElement("button");
  retry.type = "button";
  retry.textContent = "重新检查节点";
  container.replaceChildren(status, retry);
  container.hidden = true;
  let source = null;
  let request = null;
  let state = "idle";

  /** 同步校验结果、重试入口与外层发布控件。图形预览和输入绑定一直可用。 */
  function show(next, text) {
    state = next;
    container.hidden = state === "idle";
    retry.hidden = !["missing", "unknown", "error"].includes(state);
    status.dataset.tone = {ok: "success", missing: "error", error: "error", unknown: "warning"}[state] || "";
    status.textContent = text;
    onChange();
  }

  /** 直接发送严格 JSON 原文，保留大整数种子，画布和子图由服务端统一识别。 */
  async function check(text) {
    request?.abort();
    source = text;
    const current = new AbortController();
    request = current;
    show("checking", "正在核对引擎已注册的节点…");
    try {
      const response = await fetch("/local/v1/workflow-node-check", {
        method: "POST", mode: "same-origin", credentials: "omit", cache: "no-store", redirect: "error",
        headers: {"Content-Type": "application/json", "X-MyComfy-Admin": "templates"},
        signal: current.signal, body: text,
      });
      const data = await response.json().catch(() => null);
      if (request !== current) return;
      if (!response.ok) throw new Error(data?.error?.message || "HTTP " + response.status);
      if (!data) throw new Error("服务未返回节点清单。");
      show(data.status, describeNodeCheck(data));
    } catch (error) {
      if (request === current) show("error", "无法完成节点检查。 " + error.message);
    }
  }
  retry.addEventListener("click", () => { if (source !== null) void check(source); });

  return {
    check,
    /** 发布过程中固定已预览的检查结果；结束后恢复重试按钮。 */
    setBusy(value) {
      retry.disabled = value;
    },
    /** 清除待校验内容，并让迟到的同源请求失效。 */
    clear() {
      request?.abort();
      request = null;
      source = null;
      show("idle", "");
    },
    /** 无可用清单时保留原有离线发布能力，明确缺失或检查失败时等待修复和重试。 */
    allowsPublish() {
      return state === "ok" || state === "unknown";
    },
  };
}
