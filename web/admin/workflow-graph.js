/** 创建仅以文本展示导入内容的 DOM 元素。 */
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

/** 判断 JSON 值能否用作键值对象。 */
function record(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** 校验并收集节点及连线；Map 也能安全保存名为 __proto__ 的节点。 */
function graphOf(raw, canvas, strategy) {
  const nodes = new Map();
  const edges = [];

  /** 统一不同工作流格式的节点编号，并保留原始字段供详情查看。 */
  function add(id, type, title, fields) {
    if ((typeof id !== "string" && !Number.isSafeInteger(id)) || id === "" || !type || typeof type !== "string")
      throw new Error("工作流节点需要有效的编号和节点类型。");
    id = String(id);
    if (nodes.has(id)) throw new Error("节点编号重复：" + id);
    nodes.set(id, {id, type, title: typeof title === "string" && title ? title : type,
      fields, incoming: [], outgoing: [], bindings: [], outputs: []});
  }

  /** 解析有向边，同时记录节点两侧的连接，供布局和节点详情共用。 */
  function connect(from, slot, to, input) {
    const source = nodes.get(String(from));
    const target = nodes.get(String(to));
    if (!source || !target || !Number.isSafeInteger(slot) || slot < 0)
      throw new Error("无效连线：" + from + " → " + to + " / " + input);
    const edge = {source, slot, target, input};
    edges.push(edge);
    source.outgoing.push(edge);
    target.incoming.push(edge);
  }

  if (canvas) {
    for (const node of raw.nodes) {
      if (!record(node)) throw new Error("画布节点必须是对象。");
      const widgets = node.widgets_values_named ?? node.widgets_values ?? {};
      add(node.id, node.type, node.title, Object.entries(widgets));
    }
    // 子图边界在 LiteGraph 文档中独立保存，也可能是连线的端点。
    if (raw.inputNode && !nodes.has(String(raw.inputNode.id))) add(raw.inputNode.id, "子图输入", "", []);
    if (raw.outputNode && !nodes.has(String(raw.outputNode.id))) add(raw.outputNode.id, "子图输出", "", []);
    const inputPorts = new Map(raw.nodes.map(node => [String(node.id), node.inputs]));
    if (raw.outputNode) inputPorts.set(String(raw.outputNode.id), raw.outputs);
    if (raw.links != null && !Array.isArray(raw.links)) throw new Error("画布 links 需要是数组。");
    for (const link of raw.links ?? []) {
      // 旧版画布将连线写为元组，新版序列化为命名字段。
      if (!Array.isArray(link) && !record(link)) throw new Error("画布连线格式无效。");
      const [, from, slot, to, index] = Array.isArray(link) ? link
        : [link.id, link.origin_id, link.origin_slot, link.target_id, link.target_slot];
      if (!Number.isSafeInteger(index) || index < 0) throw new Error("画布连线的目标插槽无效。");
      connect(from, slot, to, inputPorts.get(String(to))?.[index]?.name ?? String(index));
    }
  } else {
    for (const [id, node] of Object.entries(raw)) {
      if (!record(node) || !record(node.inputs)) throw new Error("节点 " + id + " 缺少有效的 inputs。");
      add(id, node.class_type, node._meta?.title, Object.entries(node.inputs));
    }
    for (const node of nodes.values()) {
      // API 工作流用 [来源节点编号, 输出序号] 表示边，其余值是普通输入。
      const literals = [];
      for (const [name, value] of node.fields) {
        if (Array.isArray(value) && value.length === 2 && typeof value[0] === "string" && Number.isInteger(value[1]))
          connect(value[0], value[1], node.id, name);
        else literals.push([name, value]);
      }
      node.fields = literals;
    }
    for (const binding of Array.isArray(strategy?.bindings) ? strategy.bindings : []) {
      if (record(binding)) nodes.get(binding.node_id)?.bindings.push(binding);
    }
    for (const output of Array.isArray(strategy?.output_contract) ? strategy.output_contract : []) {
      if (record(output)) nodes.get(output.node_id)?.outputs.push(output);
    }
  }
  if (!nodes.size && !canvas) throw new Error("工作流中没有节点。");
  return {nodes, edges};
}

/** 按依赖排布节点，反馈环在拓扑排序剩余时仍保留其节点及连线。 */
function positions(nodes, edges) {
  const levels = new Map(nodes.map(node => [node, 0]));
  const pending = new Map(nodes.map(node => [node, 0]));
  const successors = new Map(nodes.map(node => [node, []]));
  for (const {source, target} of edges) {
    successors.get(source).push(target);
    pending.set(target, pending.get(target) + 1);
  }
  const queue = nodes.filter(node => pending.get(node) === 0);
  for (let i = 0; i < queue.length; i++) {
    const source = queue[i];
    for (const target of successors.get(source)) {
      levels.set(target, Math.max(levels.get(target), levels.get(source) + 1));
      pending.set(target, pending.get(target) - 1);
      if (!pending.get(target)) queue.push(target);
    }
  }
  const rows = new Map();
  return new Map(nodes.map(node => {
    const column = levels.get(node);
    const row = rows.get(column) ?? 0;
    rows.set(column, row + 1);
    return [node, {x: 24 + column * 224, y: 24 + row * 82}];
  }));
}

/** 将选中节点的参数、连接、表单绑定及产物契约展示为可读的字段表。 */
function showDetails(panel, node, select) {
  panel.replaceChildren(el("h3", "节点详情"), el("p", "#" + node.id + " · " + node.type, "wg-node-type"));
  const fields = el("dl");
  /** 增加一个字段，可跳转到与当前节点相连的另一节点。 */
  function row(name, value, target) {
    const dd = el("dd");
    if (target) {
      const button = el("button", value, "wg-link");
      button.type = "button";
      button.addEventListener("click", () => select(target));
      dd.append(button);
    } else {
      dd.textContent = typeof value === "string" ? value : JSON.stringify(value);
    }
    fields.append(el("dt", name), dd);
  }
  for (const binding of node.bindings)
    row(binding.kind === "asset" ? "素材绑定" : "输入绑定", binding.input + " → " + binding.node_input);
  for (const output of node.outputs) {
    const types = Array.isArray(output.media_types) ? output.media_types.filter(type => typeof type === "string") : [];
    row("产物", [output.role, ...types].filter(value => typeof value === "string").join(" · "));
  }
  for (const [name, value] of node.fields) row(name, value);
  for (const edge of node.incoming)
    row(edge.input, "#" + edge.source.id + " · " + edge.source.title + " [" + edge.slot + "]", edge.source);
  for (const edge of node.outgoing)
    row("输出 [" + edge.slot + "]", "#" + edge.target.id + " · " + edge.target.title + " / " + edge.input, edge.target);
  panel.append(fields.childElementCount ? fields : el("p", "此节点没有输入参数或连接。"));
}

/** 渲染可滚动的有向节点图以及可通过键盘操作的节点选择器。 */
function draw(container, graph) {
  const all = [...graph.nodes.values()];
  const bar = el("div", undefined, "wg-bar");
  bar.append(el("strong", all.length + " 个节点 · " + graph.edges.length + " 条连线"));
  container.replaceChildren(bar);
  if (!all.length) return;
  const chooser = el("select");
  const label = el("label", "定位节点 ");
  for (const node of all) chooser.add(new Option("#" + node.id + " · " + node.title, node.id));
  label.append(chooser);
  const zoom = el("button", "适应画布", "wg-zoom");
  zoom.type = "button";
  zoom.setAttribute("aria-pressed", "false");
  bar.append(label, zoom);
  const viewport = el("div", undefined, "wg-viewport");
  viewport.tabIndex = 0;
  viewport.setAttribute("role", "region");
  viewport.setAttribute("aria-label", "工作流节点连线图，可横向和纵向滚动");
  const paper = el("div", undefined, "wg-paper");
  const stage = el("div", undefined, "wg-stage");
  viewport.append(paper);
  paper.append(stage);
  const details = el("section", undefined, "wg-details");
  container.append(viewport, details);

  // 特大工作流只绘制选中节点邻域。节点选择器和详情仍然能访问整张图。
  const neighborhood = all.length > 350 || graph.edges.length > 1200;
  if (neighborhood) bar.append(el("small", "大图显示选中节点及邻接节点"));
  let buttons = new Map();
  let paths = new Map();
  let previous;
  let fit = false, scale = 1, observer;
  let diagramWidth, diagramHeight;

  /** 适配模式缩放整张图，外层纸张只占缩放后的尺寸，原始尺寸模式保留滚动。 */
  function rescale() {
    scale = fit ? Math.min(1, Math.max(1, viewport.clientWidth - 2) / diagramWidth, 360 / diagramHeight) : 1;
    stage.style.transform = "scale(" + scale + ")";
    paper.style.width = Math.ceil(diagramWidth * scale) + "px";
    paper.style.height = Math.ceil(diagramHeight * scale) + "px";
  }

  zoom.addEventListener("click", () => {
    fit = !fit;
    zoom.textContent = fit ? "原始大小" : "适应画布";
    zoom.setAttribute("aria-pressed", String(fit));
    rescale();
    observer?.disconnect();
    if (fit) {
      observer = new ResizeObserver(() => {
        if (viewport.isConnected) rescale();
        else observer.disconnect();
      });
      observer.observe(viewport);
    }
  });

  /** 使用固定卡片几何尺寸绘制 SVG 连线，所有节点标签通过 textContent 写入。 */
  function paint(selected) {
    let visible = all, edges = graph.edges;
    if (neighborhood) {
      edges = [...new Set([...selected.incoming, ...selected.outgoing])].slice(0, 250);
      visible = [...new Set([selected, ...edges.flatMap(edge => [edge.source, edge.target])])];
    }
    const coords = positions(visible, edges);
    const width = Math.max(...Array.from(coords.values(), p => p.x)) + 204;
    const height = Math.max(...Array.from(coords.values(), p => p.y)) + 80;
    stage.style.width = width + "px";
    stage.style.height = height + "px";
    diagramWidth = width;
    diagramHeight = height;
    rescale();
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(height));
    svg.setAttribute("aria-hidden", "true");
    paths = new Map();
    for (const edge of edges) {
      const from = coords.get(edge.source), to = coords.get(edge.target);
      const x = from.x + 180, y = from.y + 27, endY = to.y + 27;
      const path = document.createElementNS(svg.namespaceURI, "path");
      const bend = Math.min(90, Math.max(22, Math.abs(to.x - x) / 2));
      // 后向边和自环绕过卡片上方，垂直同列时仍可辨认路径。
      const top = Math.min(from.y, to.y) - 12;
      // 跨列长边沿行间空隙绕行，避免被误读为连接中途经过的节点。
      const adjacentColumn = to.x > x && to.x - from.x <= 224;
      const curve = adjacentColumn
        ? `M ${x} ${y} C ${x+bend} ${y} ${to.x-bend} ${endY} ${to.x} ${endY}`
        : `M ${x} ${y} Q ${x+18} ${y} ${x+18} ${top} H ${to.x-16} Q ${to.x-20} ${endY} ${to.x} ${endY}`;
      path.setAttribute("d", curve + ` M ${to.x-10} ${endY-4} l 6 4 -6 4`);
      svg.append(path);
      paths.set(edge, path);
    }
    const fragment = document.createDocumentFragment();
    fragment.append(svg);
    buttons = new Map();
    for (const node of visible) {
      const point = coords.get(node);
      const button = el("button", undefined, "wg-card");
      button.type = "button";
      button.style.left = point.x + "px";
      button.style.top = point.y + "px";
      button.title = node.title + " · " + node.type;
      button.setAttribute("aria-pressed", "false");
      button.append(el("small", "#" + node.id), el("strong", node.title));
      button.addEventListener("click", () => choose(node, false));
      fragment.append(button);
      buttons.set(node, button);
    }
    stage.replaceChildren(fragment);
  }

  /** 同步节点选择、相关连线和字段面板，定位时只滚动图内视口。 */
  function choose(node, center) {
    const refocus = neighborhood && stage.contains(document.activeElement);
    if (!previous || neighborhood) paint(node);
    buttons.get(previous)?.setAttribute("aria-pressed", "false");
    const button = buttons.get(node);
    button.setAttribute("aria-pressed", "true");
    const adjacent = new Set([...node.incoming.map(edge => edge.source), ...node.outgoing.map(edge => edge.target)]);
    for (const [neighbor, card] of buttons) card.classList.toggle("wg-peer", adjacent.has(neighbor));
    for (const [edge, path] of paths)
      path.classList.toggle("wg-active", edge.source === node || edge.target === node);
    chooser.value = node.id;
    previous = node;
    showDetails(details, node, target => choose(target, true));
    if (center) {
      viewport.scrollLeft = (button.offsetLeft + button.offsetWidth / 2) * scale - viewport.clientWidth / 2;
      viewport.scrollTop = (button.offsetTop + button.offsetHeight / 2) * scale - viewport.clientHeight / 2;
    }
    if (center || refocus) button.focus({preventScroll: true});
  }

  chooser.addEventListener("change", () => choose(graph.nodes.get(chooser.value), true));
  const first = all.find(node => !node.incoming.length) ?? all[0];
  choose(first, false);
}

/** 识别策略、ComfyUI API 或画布 JSON，解析主工作流的节点和连线。 */
export function parseWorkflow(rawJson) {
  if (!record(rawJson)) throw new Error("模板需要是一个 JSON 对象。");
  const canvas = Array.isArray(rawJson.nodes);
  // 裸节点图也可能含名为 workflow 的普通节点；策略内部同样允许节点名 inputs。
  const wrapped = record(rawJson.workflow)
    && !(typeof rawJson.workflow.class_type === "string" && record(rawJson.workflow.inputs));
  const kind = canvas ? "canvas" : wrapped ? "strategy" : "api";
  return {kind, graph: graphOf(kind === "strategy" ? rawJson.workflow : rawJson, canvas, wrapped ? rawJson : null)};
}

/** 在导入页面展示交互式节点图，并返回文件类型供页面选择发布操作。 */
export function renderWorkflowPreview(container, rawJson) {
  const {kind, graph} = parseWorkflow(rawJson);
  const frame = el("div", undefined, "wg");
  const body = el("div");
  if (kind === "canvas" && Array.isArray(rawJson.definitions?.subgraphs) && rawJson.definitions.subgraphs.length) {
    const scopes = [rawJson, ...rawJson.definitions.subgraphs];
    const label = el("label", "查看画布 ", "wg-scope");
    const select = el("select");
    select.add(new Option("主画布", "0"));
    scopes.slice(1).forEach((scope, i) => select.add(new Option(scope?.name || "子图 " + (i + 1), String(i + 1))));
    label.append(select);
    frame.append(label);
    select.addEventListener("change", () => {
      const scope = scopes[Number(select.value)];
      try {
        if (!Array.isArray(scope?.nodes)) throw new Error("子图缺少 nodes 数组。");
        draw(body, scope === rawJson ? graph : graphOf(scope, true));
      } catch (error) {
        body.replaceChildren(el("p", error.message, "wg-error"));
      }
    });
  }
  draw(body, graph);
  frame.append(body);
  container.replaceChildren(frame);
  return kind;
}
