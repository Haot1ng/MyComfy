import {buildTemplateDraft, writePromptAssist} from "./template-draft.js";

const dialog = document.getElementById("template-example");
const source = document.getElementById("example-json");
const status = document.getElementById("example-status");
let loading = false;

/** 打开可直接导入的完整示例，按需读取随程序发布的文生视频工作流。 */
async function openExample() {
  dialog.showModal();
  if (source.value || loading) return;
  loading = true;
  status.textContent = "正在读取示例…";
  status.dataset.tone = "";
  try {
    const response = await fetch("./presets/h3-t2v.strategy.json", {
      mode: "same-origin", credentials: "omit", redirect: "error",
    });
    if (!response.ok) throw new Error();
    // 仅替换示例系列的名称和标识，移除可选的 AI 规则。节点、模型和数值沿用同版本预设。
    const draft = buildTemplateDraft({
      key: "example-h3-text-to-video", name: "示例 · H3 文生视频", presetText: await response.text(),
    });
    source.value = writePromptAssist(draft, null) + "\n";
    // 同一份只读原文用于预览、下载和复制；本页仅创建一个下载 URL。
    const file = new Blob([source.value], {type: "application/json;charset=utf-8"});
    document.getElementById("example-download").href = URL.createObjectURL(file);
    source.hidden = false;
    document.getElementById("example-actions").hidden = false;
    status.textContent = "";
  } catch {
    status.dataset.tone = "error";
    status.textContent = "示例读取失败，请关闭后重新打开。";
  } finally {
    loading = false;
  }
}

/** 复制完整 JSON，浏览器未授权剪贴板时选中原文供用户手动复制。 */
async function copyExample() {
  try {
    await navigator.clipboard.writeText(source.value);
    status.textContent = "示例 JSON 已复制。";
  } catch {
    source.focus();
    source.select();
    status.textContent = "已选中全部 JSON，请使用系统的复制操作。";
  }
}

document.getElementById("open-template-example").addEventListener("click", openExample);
document.getElementById("example-copy").addEventListener("click", copyExample);
