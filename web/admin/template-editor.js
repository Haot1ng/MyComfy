import { buildTemplateDraft, readApiInputs } from "./template-draft.js";

const presetPaths = new Map([
  ["h3-t2v", "./presets/h3-t2v.strategy.json"],
  ["h3-i2v", "./presets/h3-i2v.strategy.json"],
]);

/** 将可选目标按节点分组，选项值用于找回原始节点及其字段名。 */
function fillTargets(select, targets, accepts) {
  select.replaceChildren(new Option("请选择节点输入", ""));
  let group;
  let lastId;
  for (const [index, field] of targets.entries()) {
    if (!accepts(field)) continue;
    if (lastId !== field.id) {
      group = document.createElement("optgroup");
      group.label = "#" + field.id + " · " + field.nodeType;
      select.append(group);
      lastId = field.id;
    }
    group.append(new Option("#" + field.id + " · " + field.name, String(index)));
  }
}

/** 根据同一工作流的可编辑字段创建一行用户输入绑定。 */
function bindingRow(targets, onDirty) {
  const row = document.createElement("div");
  row.className = "editor-binding";
  // 固定表单结构不含导入内容；外部节点及字段名只写入 option 的文本。
  row.innerHTML = `
    <label>参数名<input name="input" required placeholder="例如 prompt"></label>
    <label>显示名称<input name="title" placeholder="例如 提示词（可选）"></label>
    <label>输入类型<select name="kind"><option value="value">普通参数</option><option value="asset">上传素材</option></select></label>
    <label>绑定到节点输入<select name="target" required></select></label>
    <button type="button" data-remove>移除</button>`;
  const target = row.querySelector('[name="target"]');
  const kind = row.querySelector('[name="kind"]');
  fillTargets(target, targets, () => true);
  kind.addEventListener("change", () => {
    const previous = target.value;
    // 上传后的素材路径只能写回字符串输入，其他类型保留原始参数类型。
    fillTargets(target, targets, field => kind.value === "value" || field.type === "string");
    if (Array.from(target.options).some(option => option.value === previous)) target.value = previous;
  });
  row.querySelector("[data-remove]").addEventListener("click", () => {
    row.remove();
    onDirty();
  });
  return row;
}

/** 装配新增模板表单，在维护者确认预览时交给上层统一发布。 */
export function mountTemplateEditor(container, {onDraft, onDirty}) {
  const form = document.createElement("form");
  form.className = "template-editor";
  form.innerHTML = `
    <fieldset class="editor-controls">
      <legend>新增模板</legend>
      <div class="editor-fields">
        <label>模板名称<input id="new-template-name" name="templateName" required maxlength="180" placeholder="例如 商品展示视频"></label>
        <label>模板标识<input id="new-template-key" name="templateKey" required maxlength="64" pattern="[a-z0-9][a-z0-9_\\-]*" placeholder="例如 product-video" aria-describedby="new-template-key-help"></label>
      </div>
      <p id="new-template-key-help" class="template-hint">标识用于区分模板，可用小写字母、数字、下划线和连字符。</p>
      <label>工作流来源<select id="new-template-source" name="source">
        <option value="h3-t2v">内置 H3 文生视频</option>
        <option value="h3-i2v">内置 H3 图生视频</option>
        <option value="api">自定义 ComfyUI API 工作流</option>
      </select></label>
      <fieldset class="editor-custom" hidden disabled>
        <legend>自定义工作流</legend>
        <label>选择 API JSON 文件<input id="new-workflow-file" type="file" accept=".json,application/json"></label>
        <label>工作流 JSON<textarea id="new-template-workflow" rows="5" spellcheck="false" placeholder="粘贴从 ComfyUI 导出的 API 格式 JSON"></textarea></label>
        <button type="button" data-read>读取节点</button>
        <div data-config hidden>
          <div class="editor-binding-head"><h3>工作台输入</h3><button type="button" data-add>添加输入</button></div>
          <p class="template-hint">仅添加用户需要填写的参数。其他参数沿用工作流里的值。</p>
          <div data-bindings></div>
          <h3>视频输出</h3>
          <div class="editor-fields">
            <label>保存节点的路径输入<select name="outputTarget" required></select></label>
            <label>输出格式<select name="outputType"><option value="video/mp4">MP4</option><option value="video/webm">WebM</option><option value="video/quicktime">MOV</option></select></label>
          </div>
        </div>
      </fieldset>
      <p data-status class="template-message" role="status"></p>
      <button type="submit" class="highlight">预览新模板</button>
    </fieldset>`;
  container.replaceChildren(form);
  container.hidden = true;

  const controls = form.querySelector(".editor-controls");
  const custom = form.querySelector(".editor-custom");
  const config = form.querySelector("[data-config]");
  const bindings = form.querySelector("[data-bindings]");
  const file = form.querySelector("#new-workflow-file");
  const workflow = form.querySelector("#new-template-workflow");
  const source = form.elements.source;
  const output = form.elements.outputTarget;
  const status = form.querySelector("[data-status]");
  const readButton = form.querySelector("[data-read]");
  let targets = [];
  let revision = 0;
  let fileRead = 0;
  let busy = false;

  /** 编辑后撤回上一份待发布内容，防止异步结果覆盖新草稿。 */
  function invalidate() {
    revision++;
    status.textContent = "";
    delete status.dataset.tone;
    onDirty();
  }

  /** 清空失效的节点定位和绑定，避免在替换工作流后沿用旧 ID。 */
  function clearTargets() {
    config.hidden = true;
    readButton.disabled = false;
    output.disabled = true;
    targets = [];
    bindings.replaceChildren();
    output.replaceChildren();
  }

  /** 解析 API 工作流并为当前图建立可选的表单输入和视频保存目标。 */
  function readWorkflow() {
    invalidate();
    clearTargets();
    try {
      const nodes = readApiInputs(workflow.value);
      targets = nodes.flatMap(node => node.fields.map(field => ({
        id: node.id, nodeType: node.type, name: field.name, type: field.type,
      })));
      fillTargets(output, targets, field => field.type === "string");
      // 只有唯一且常见的文件名前缀字段可直接预选，多个候选交给维护者核对。
      const candidates = targets.filter(field => field.type === "string" && field.name === "filename_prefix");
      if (candidates.length === 1) output.value = String(targets.indexOf(candidates[0]));
      config.hidden = false;
      readButton.disabled = true;
      output.disabled = false;
      status.textContent = "工作流已读取，请核对输入绑定和视频输出。";
    } catch (error) {
      status.textContent = error.message;
      status.dataset.tone = "error";
    }
  }

  /** 按表单选择生成策略正文并提交预览，异步读取期间发生编辑则丢弃结果。 */
  async function preview(event) {
    event.preventDefault();
    if (busy) return;
    invalidate();
    const snapshot = revision;
    try {
      const draft = {name: form.elements.templateName.value, key: form.elements.templateKey.value};
      if (source.value === "api") {
        if (config.hidden) throw new Error("请先读取工作流节点，再设置视频输出。");
        draft.workflowText = workflow.value;
        draft.bindings = Array.from(bindings.children, row => {
          const field = targets[Number(row.querySelector('[name="target"]').value)];
          return {
            input: row.querySelector('[name="input"]').value, title: row.querySelector('[name="title"]').value,
            kind: row.querySelector('[name="kind"]').value, node_id: field.id, node_input: field.name,
          };
        });
        const field = targets[Number(output.value)];
        draft.output = {node_id: field.id, path_input: field.name, media_type: form.elements.outputType.value};
      } else {
        const response = await fetch(presetPaths.get(source.value), {credentials: "omit", redirect: "error"});
        if (!response.ok) throw new Error("无法读取内置 H3 策略，请重试。");
        draft.presetText = await response.text();
      }
      if (revision !== snapshot) return;
      await onDraft(buildTemplateDraft(draft));
    } catch (error) {
      if (revision !== snapshot) return;
      status.textContent = error.message || "无法生成模板。";
      status.dataset.tone = "error";
    }
  }

  // 输入和选择都会撤回待发布预览；切换工作流时还须丢弃上一份绑定。
  form.addEventListener("input", invalidate);
  source.addEventListener("change", () => {
    fileRead++;
    invalidate();
    custom.hidden = source.value !== "api";
    custom.disabled = custom.hidden;
  });
  workflow.addEventListener("input", () => {
    fileRead++;
    file.value = "";
    clearTargets();
  });
  file.addEventListener("change", async () => {
    const selected = file.files[0];
    if (!selected) return;
    const request = ++fileRead;
    workflow.value = "";
    clearTargets();
    invalidate();
    try {
      const text = await selected.text();
      if (fileRead !== request) return;
      workflow.value = text;
      readWorkflow();
    } catch {
      if (fileRead === request) {
        status.textContent = "无法读取文件，请重新选择或粘贴 JSON。";
        status.dataset.tone = "error";
      }
    } finally {
      if (fileRead === request) file.value = "";
    }
  });
  readButton.addEventListener("click", readWorkflow);
  form.querySelector("[data-add]").addEventListener("click", () => {
    const row = bindingRow(targets, invalidate);
    bindings.append(row);
    invalidate();
    row.querySelector("input").focus();
  });
  form.addEventListener("submit", preview);

  return {
    /** 打开新草稿，可沿用刚预览的 API 工作流原文和其中的精确数值。 */
    open(apiText = null) {
      if (container.hidden || apiText !== null) {
        fileRead++;
        form.reset();
        clearTargets();
        source.value = apiText === null ? "h3-t2v" : "api";
        custom.hidden = custom.disabled = apiText === null;
        container.hidden = false;
        if (apiText === null) {
          invalidate();
        } else {
          workflow.value = apiText;
          readWorkflow();
        }
      }
      form.elements.templateName.focus();
    },
    /** 离开新增界面时使延迟的文件或模板读取失效。 */
    close() {
      fileRead++;
      revision++;
      container.hidden = true;
    },
    /** 发布期间锁定表单，确保发布正文与用户核对的预览一致。 */
    setBusy(value) {
      busy = value;
      controls.disabled = value;
    },
  };
}
