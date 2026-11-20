// The pending request is kept per account and tab until its batch ID is known.
const pendingRequests = new Map();

function node(tag, className = '', text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function button(text, action, primary = false) {
  const el = node('button', primary ? 'btn primary' : 'btn', text);
  el.type = 'button';
  el.addEventListener('click', action);
  return el;
}

function option(value, label) {
  return new Option(label, value);
}

function errorText(error) {
  const messages = {
    NO_COMPATIBLE_DEPLOYMENT: '没有可用的兼容部署',
    ASSET_UNAVAILABLE: '素材不存在、不可读取或无权使用',
    USER_JOB_QUOTA: '已达到当前账号的任务配额',
    USER_STORAGE_QUOTA: '已达到当前账号的素材配额',
    GLOBAL_QUEUE_FULL: '队列已满，请稍后重试',
    VOLUME_LOW: '存储空间不足，请稍后重试',
    INVALID_MEDIA: '素材内容无效或格式不受支持',
    UPLOAD_TOO_LARGE: '文件超过上传大小限制',
    IDEMPOTENCY_CONFLICT: '该批次键已绑定其他内容，请先在任务列表核对记录',
    VALIDATION_FAILED: '请检查批次参数',
    SCHEMA_INVALID: '不符合策略要求',
    REQUIRED: '需要填写此参数',
    UNKNOWN_VERSION: '找不到此策略版本',
    DUPLICATE: '任务键重复',
    UNKNOWN_INPUT: '策略未声明此参数',
  };
  let message = messages[error.code] || error.message || error.code || '请求失败';
  if (error.code === 'VALIDATION_FAILED' && !error.details?.length && error.message) {
    message += ' · ' + error.message;
  }
  if (error.retryAfter && /^\d+$/.test(error.retryAfter)) message += ' · ' + error.retryAfter + ' 秒后重试';
  return error.requestId ? message + '（请求 ' + error.requestId + '）' : message;
}

// Preserve integer seeds larger than 2^53 when replaying a JSON snapshot.
function parseInputJson(text) {
  return JSON.parse(text, (name, value, context) => {
    if (typeof value !== 'number') return value;
    if (!Number.isFinite(value)) throw new Error('数值超出范围');
    if (!Number.isInteger(value) || Number.isSafeInteger(value)) return value;
    if (context?.source && typeof JSON.rawJSON === 'function') return JSON.rawJSON(context.source);
    throw new Error('当前浏览器无法精确表示此整数');
  });
}

async function allPages(api, path) {
  const items = [];
  let cursor = null;
  do {
    const params = new URLSearchParams({ limit: '100' });
    if (cursor) params.set('cursor', cursor);
    const page = await api(path + '?' + params);
    items.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor);
  return items;
}

function getPending(key) {
  if (pendingRequests.has(key)) return pendingRequests.get(key);
  try {
    const saved = parseInputJson(sessionStorage.getItem(key));
    if (saved?.client_batch_key && Array.isArray(saved.tasks) && saved.tasks.length) return saved;
  } catch { /* sessionStorage can be disabled by browser policy. */ }
  return null;
}

function setPending(key, payload) {
  if (payload) pendingRequests.set(key, payload);
  else pendingRequests.delete(key);
  try {
    if (payload) sessionStorage.setItem(key, JSON.stringify(payload));
    else sessionStorage.removeItem(key);
  } catch { /* The in-memory snapshot still supports retry in this page. */ }
}

function key() {
  return Array.from(crypto.getRandomValues(new Uint32Array(4)), n => n.toString(16).padStart(8, '0')).join('');
}

function fieldMode(schema, asset) {
  if (asset && schema.type === 'array') return 'asset-list';
  if (asset && schema.type === 'string') return 'asset';
  if (asset) return 'asset-json';
  if (Array.isArray(schema.enum) || schema.type === 'boolean') return 'choice';
  if (schema.type === 'string') return 'text';
  if (schema.type === 'integer' || schema.type === 'number') return 'number';
  return 'json';
}

function fieldSchema(root, rule) {
  if (!rule || typeof rule !== 'object') return {};
  // Resolve local JSON pointers for controls. Complex schemas remain editable as JSON.
  let resolved = rule;
  const seen = new Set();
  while (resolved.$ref?.startsWith('#/') && !seen.has(resolved.$ref)) {
    seen.add(resolved.$ref);
    const parts = resolved.$ref.slice(2).split('/').map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'));
    let target = root;
    for (const part of parts) target = target && Object.hasOwn(target, part) ? target[part] : null;
    if (!target || typeof target !== 'object') break;
    const { $ref, ...annotations } = resolved;
    resolved = { ...target, ...annotations };
  }
  return resolved;
}

function fields(version) {
  const root = version.input_schema;
  return Object.entries(root.properties || {}).map(([name, rule]) => {
    const schema = fieldSchema(root, rule);
    const asset = (version.asset_fields || []).includes(name);
    return { name, schema, mode: fieldMode(schema, asset), required: (root.required || []).includes(name) };
  });
}

function displayValue(mode, value) {
  if (mode === 'asset-list') return value.join('\n');
  if (['json', 'asset-json', 'choice'].includes(mode)) return JSON.stringify(value, null, mode === 'choice' ? 0 : 2);
  if (JSON.isRawJSON?.(value)) return value.rawJSON;
  return String(value);
}

function makeTask(version, id, savedInputs) {
  const values = new Map();
  for (const f of fields(version)) {
    if (savedInputs && Object.hasOwn(savedInputs, f.name)) {
      values.set(f.name, displayValue(f.mode, savedInputs[f.name]));
    } else if (Object.hasOwn(f.schema, 'default')) {
      values.set(f.name, displayValue(f.mode, f.schema.default));
    }
  }
  return { id, version, values, errors: [] };
}

function taskInputs(task) {
  const result = Object.create(null);
  for (const f of fields(task.version)) {
    if (!task.values.has(f.name)) continue;
    const raw = task.values.get(f.name);
    if (!raw && f.mode !== 'text' && f.mode !== 'asset-list') continue;
    try {
      switch (f.mode) {
        case 'asset-list':
          result[f.name] = raw.split(/\r?\n/).map(id => id.trim()).filter(Boolean);
          break;
        case 'choice':
        case 'json':
        case 'asset-json':
          result[f.name] = parseInputJson(raw);
          break;
        case 'number': {
          const num = Number(raw);
          if (!Number.isFinite(num) || (f.schema.type === 'integer' && !Number.isInteger(num))) {
            throw new Error('请输入有效数值');
          }
          result[f.name] = Number.isInteger(num) && !Number.isSafeInteger(num) ? parseInputJson(raw) : num;
          break;
        }
        default:
          result[f.name] = raw;
      }
    } catch (error) {
      task.errors.push({ field: f.name, message: error instanceof SyntaxError ? '请输入有效的 JSON' : error.message });
    }
  }
  return result;
}

function makeControl(f, raw, update) {
  const { schema, mode } = f;
  let control;
  if (mode === 'choice') {
    control = node('select');
    control.add(option('', '请选择'));
    for (const value of schema.enum || [true, false]) {
      const label = typeof value === 'string' ? value : JSON.stringify(value);
      control.add(option(JSON.stringify(value), label));
    }
  } else if (['json', 'asset-json', 'asset-list'].includes(mode) || mode === 'text') {
    control = node('textarea');
    control.rows = mode === 'asset-list' ? 2 : 3;
    if (mode === 'json' || mode === 'asset-json') control.placeholder = 'JSON 值';
    if (mode === 'asset-list') control.placeholder = '每行一个素材 ID';
  } else {
    control = node('input');
    control.type = mode === 'number' ? 'number' : 'text';
    if (mode === 'number') {
      control.step = schema.multipleOf || (schema.type === 'integer' ? '1' : 'any');
      if (schema.minimum !== undefined) control.min = schema.minimum;
      if (schema.maximum !== undefined) control.max = schema.maximum;
    }
    if (mode === 'asset') control.placeholder = '素材 ID';
  }
  control.value = raw ?? '';
  control.addEventListener('input', () => update(control.value));
  return control;
}

export function mountComposer(host, ctx) {
  const storageKey = 'mycomfy.pendingBatch.' + ctx.session.user_id;
  const history = new Map();
  const historyErrors = new Map();
  let pending = getPending(storageKey);
  let batchKey = pending?.client_batch_key || key();
  let active = true;
  let loading = false;
  let busy = false;
  let message = '';
  let catalogError = '';
  let uploading = null;
  let previous = null;
  let series = [];
  let tasks = [];
  let nextTaskNumber = 1;

  const root = node('section', 'composer');
  host.replaceChildren(root);

  function alertBox(text) {
    const box = node('div', 'cmp-alert', text);
    box.setAttribute('role', 'alert');
    return box;
  }

  function newTask(version) {
    let id;
    do {
      id = '任务 ' + String(nextTaskNumber++).padStart(2, '0');
    } while (tasks.some(t => t.id === id));
    return makeTask(version, id);
  }

  function loadHistory(version) {
    const seriesKey = version.strategy_key;
    if (history.has(seriesKey)) return;
    history.set(seriesKey, [version]);
    historyErrors.delete(seriesKey);
    allPages(ctx.api, '/strategies/' + encodeURIComponent(seriesKey) + '/versions').then(items => {
      if (!active) return;
      history.set(seriesKey, items);
      render();
    }).catch(error => {
      if (!active) return;
      historyErrors.set(seriesKey, errorText(error));
      render();
    });
  }

  async function loadEditor() {
    loading = true;
    catalogError = '';
    render();
    try {
      series = await allPages(ctx.api, '/strategies');
      if (!active) return;
      if (previous) {
        const byId = new Map(series.map(v => [v.strategy_version_id, v]));
        const versions = new Set(previous.tasks.map(t => t.strategy_version_id));
        for (const id of versions) {
          if (!byId.has(id)) byId.set(id, await ctx.api('/strategy-versions/' + encodeURIComponent(id)));
        }
        if (!active) return;
        tasks = previous.tasks.map(t => makeTask(byId.get(t.strategy_version_id), t.client_task_key, t.inputs));
        for (const { id } of tasks) {
          const match = /^任务 (\d+)$/.exec(id);
          if (match && Number.isSafeInteger(Number(match[1]) + 1)) {
            nextTaskNumber = Math.max(nextTaskNumber, Number(match[1]) + 1);
          }
        }
        previous = null;
      } else if (series.length && !tasks.length) {
        tasks = [newTask(series.find(item => item.accepting) || series[0])];
      }
      tasks.forEach(t => loadHistory(t.version));
    } catch (error) {
      catalogError = errorText(error);
    } finally {
      loading = false;
      render();
    }
  }

  async function upload(task, f, files) {
    if (!files.length || busy || uploading || pending) return;
    uploading = { task, field: f.name };
    message = '';
    task.errors = task.errors.filter(e => e.field !== f.name);
    render();
    try {
      const multiple = f.mode === 'asset-list' || (f.mode === 'asset-json' && files.length > 1);
      const ids = f.mode === 'asset-list' ? (task.values.get(f.name) || '').split('\n').filter(Boolean) : [];
      for (const file of files) {
        const body = new FormData();
        body.append('file', file);
        const asset = await ctx.api('/assets', { method: 'POST', body });
        if (!active) return;
        ids.push(asset.asset_id);
        const value = multiple ? ids : asset.asset_id;
        task.values.set(f.name, displayValue(f.mode, value));
      }
    } catch (error) {
      task.errors.push({ field: f.name, message: errorText(error) });
    } finally {
      uploading = null;
      render();
    }
  }

  function fieldView(task, f) {
    const wrapper = node('div', 'cmp-field');
    const control = makeControl(f, task.values.get(f.name), raw => task.values.set(f.name, raw));
    control.id = 'cmp-' + encodeURIComponent(task.id) + '-' + encodeURIComponent(f.name);
    const label = node('label', '', f.schema.title || f.name);
    label.htmlFor = control.id;
    if (f.required) {
      const marker = node('span', 'cmp-required', ' *');
      marker.title = '必填';
      label.append(marker);
      control.setAttribute('aria-required', 'true');
    }
    wrapper.append(label);
    if (f.schema.description) wrapper.append(node('small', 'muted', f.schema.description));
    if (f.mode.startsWith('asset')) {
      const picker = node('input');
      picker.type = 'file';
      picker.multiple = f.mode !== 'asset';
      picker.setAttribute('aria-label', '上传素材 · ' + (f.schema.title || f.name));
      picker.addEventListener('change', () => upload(task, f, Array.from(picker.files)));
      wrapper.append(picker);
      if (uploading?.task === task && uploading.field === f.name) wrapper.append(node('small', 'muted', '正在上传素材…'));
    }
    wrapper.append(control);
    const problems = task.errors.filter(issue => issue.field === f.name);
    if (problems.length) {
      control.setAttribute('aria-invalid', 'true');
      const hint = node('small', 'cmp-issue', problems.map(e => e.message).join(' · '));
      hint.id = control.id + '-errors';
      control.setAttribute('aria-describedby', hint.id);
      wrapper.append(hint);
    }
    return wrapper;
  }

  function labeledSelect(label, select) {
    const el = node('label', 'cmp-field', label);
    el.append(select);
    return el;
  }

  function taskView(task, index) {
    const card = node('article', 'card cmp-task');
    const header = node('div', 'cmp-task-header');
    header.append(node('h2', '', /^任务 \d+$/.test(task.id) ? task.id : '任务 ' + (index + 1)));
    if (tasks.length > 1) {
      header.append(button('移除任务', () => {
        tasks.splice(index, 1);
        render();
      }));
    }
    card.append(header);
    const selectors = node('div', 'cmp-selectors');
    const seriesSelect = node('select');
    for (const version of series) seriesSelect.add(option(version.strategy_key, version.name + ' · ' + version.strategy_key));
    seriesSelect.value = task.version.strategy_key;
    function changeVersion(version) {
      tasks[index] = makeTask(version, task.id);
      loadHistory(version);
      render();
    }
    seriesSelect.addEventListener('change', () => changeVersion(series.find(v => v.strategy_key === seriesSelect.value)));
    const versionSelect = node('select');
    const versions = [...(history.get(task.version.strategy_key) || [])];
    if (!versions.some(v => v.strategy_version_id === task.version.strategy_version_id)) versions.push(task.version);
    versions.sort((a, b) => b.version - a.version);
    for (const version of versions) versionSelect.add(option(version.strategy_version_id, 'v' + version.version));
    versionSelect.value = task.version.strategy_version_id;
    versionSelect.addEventListener('change', () => changeVersion(versions.find(v => v.strategy_version_id === versionSelect.value)));
    selectors.append(labeledSelect('生成策略', seriesSelect), labeledSelect('策略版本', versionSelect));
    card.append(selectors);
    const historyError = historyErrors.get(task.version.strategy_key);
    if (historyError) {
      const row = alertBox('历史版本加载失败。' + historyError);
      row.append(button('重新加载版本', () => {
        history.delete(task.version.strategy_key);
        loadHistory(task.version);
      }));
      card.append(row);
    }
    if (!task.version.accepting) card.append(alertBox('暂不可受理 · ' + errorText({ code: task.version.reason })));
    const details = node('details', 'cmp-version');
    details.append(node('summary', '', '版本详情'), node('p', '', '取消后产物' + (task.version.expose_after_cancel ? '可见' : '隐藏')));
    details.append(node('p', '', '版本摘要 ' + task.version.content_digest));
    const schema = node('pre', '', JSON.stringify(task.version.input_schema, null, 2));
    details.append(schema);
    card.append(details);
    for (const f of fields(task.version)) card.append(fieldView(task, f));
    for (const e of task.errors.filter(e => e.field === null)) card.append(alertBox(e.message));
    return card;
  }

  function showErrors(error) {
    tasks.forEach(t => { t.errors = []; });
    const general = [];
    for (const issue of error.details || []) {
      const task = tasks.find(t => t.id === issue.client_task_key) || tasks[issue.index];
      const parts = (issue.path || '').split('/').slice(1).map(s => s.replace(/~1/g, '/').replace(/~0/g, '~'));
      const field = parts[2] === 'inputs' ? parts[3] : null;
      const problem = { field: null, message: [issue.path, errorText(issue)].filter(Boolean).join(' · ') };
      if (task) {
        if (field && fields(task.version).some(f => f.name === field)) problem.field = field;
        task.errors.push(problem);
      } else general.push(problem.message);
    }
    message = [errorText(error), ...general].join(' · ');
  }

  async function send() {
    if (busy || uploading) return;
    if (!pending) {
      tasks.forEach(t => { t.errors = []; });
      const items = tasks.map(t => ({ client_task_key: t.id, strategy_version_id: t.version.strategy_version_id, inputs: taskInputs(t) }));
      if (!tasks.length || tasks.some(t => t.errors.length || !t.version.accepting)) {
        message = '请检查标出的任务参数及策略版本。';
        render();
        root.querySelector('[aria-invalid=true]')?.focus();
        return;
      }
      pending = { client_batch_key: batchKey, tasks: items };
      setPending(storageKey, pending);
    }
    busy = true;
    message = '';
    render();
    try {
      const result = await ctx.api('/batches', { method: 'POST', body: pending });
      if (!active) return;
      if (!result?.batch_id) throw new Error('未收到批次 ID');
      setPending(storageKey, null);
      pending = null;
      ctx.navigate('/batch/' + encodeURIComponent(result.batch_id));
    } catch (error) {
      if (!active) return;
      if ([400, 409, 413, 415, 422, 429].includes(error.status) || ['GLOBAL_QUEUE_FULL', 'VOLUME_LOW'].includes(error.code)) {
        const rejected = pending;
        setPending(storageKey, null);
        pending = null;
        if (error.status === 409) batchKey = key();
        if (!tasks.length) {
          previous = rejected;
          await loadEditor();
          if (!active) return;
        }
        showErrors(error);
      } else {
        message = '尚未确认批次结果。再次提交将使用原请求核对。' + errorText(error);
      }
    } finally {
      busy = false;
      render();
    }
  }

  function render() {
    if (!active) return;
    const heading = node('header', 'cmp-heading');
    heading.append(node('h1', '', '创建批次'));
    root.replaceChildren(heading);
    if (message) root.append(alertBox(message));
    if (pending) {
      const panel = node('div', 'card cmp-pending');
      panel.setAttribute('role', 'status');
      panel.append(node('h2', '', busy ? '正在提交批次…' : '待确认的批次'));
      panel.append(node('p', 'muted', pending.tasks.length + ' 项任务 · ' + pending.client_batch_key));
      if (!busy) panel.append(button('核对提交结果', send, true));
      root.append(panel);
      return;
    }
    if (loading) {
      root.append(node('p', 'muted', '正在加载策略…'));
      return;
    }
    if (catalogError || !series.length) {
      const empty = node('div', 'card cmp-pending');
      empty.append(node('h2', '', catalogError ? '策略加载失败' : '暂无已发布策略'));
      empty.append(node('p', 'muted', catalogError || '发布策略版本后，即可在这里配置生成任务。'));
      empty.append(button('重新加载', loadEditor));
      root.append(empty);
      return;
    }
    const form = node('fieldset', 'cmp-layout');
    form.disabled = Boolean(busy || uploading);
    const list = node('div', 'cmp-task-list');
    for (const [index, task] of tasks.entries()) list.append(taskView(task, index));
    const add = button('＋ 添加任务', () => {
      const version = tasks[tasks.length - 1]?.version || series[0];
      tasks.push(newTask(version));
      render();
      root.querySelector('.cmp-task:last-of-type select')?.focus();
    });
    add.classList.add('cmp-add');
    list.append(add);
    const summary = node('aside', 'card cmp-summary');
    summary.append(node('h2', '', '批次摘要'));
    const total = node('div', 'cmp-total');
    total.append(node('span', 'muted', '任务数量'), node('strong', '', String(tasks.length)));
    summary.append(total);
    const items = node('ol');
    for (const task of tasks) items.append(node('li', '', task.version.name + ' · v' + task.version.version));
    summary.append(items);
    const submit = button('提交批次', send, true);
    submit.disabled = !tasks.length || tasks.some(t => !t.version.accepting);
    summary.append(submit);
    form.append(list, summary);
    root.append(form);
  }

  if (pending) render();
  else loadEditor();
  return () => { active = false; };
}
