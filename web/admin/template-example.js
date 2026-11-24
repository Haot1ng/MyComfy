import {stripJsonComments} from "./template-draft.js";

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
    const response = await fetch("./presets/example-h3-text-to-video.strategy.jsonc", {
      mode: "same-origin", credentials: "omit", redirect: "error",
    });
    if (!response.ok) throw new Error();
    const annotated = await response.text();
    // 去掉注释占用的空行和行尾空白。直接保留 JSON 数字原文，避免 64 位种子被舍入。
    const strict = stripJsonComments(annotated).split(/\r?\n/).map(line => line.trimEnd())
      .filter(Boolean).join("\n") + "\n";
    JSON.parse(strict);
    const jsonFile = new Blob([strict], {type: "application/json;charset=utf-8"});
    const annotatedFile = new Blob([annotated], {type: "text/plain;charset=utf-8"});
    document.getElementById("example-download-json").href = URL.createObjectURL(jsonFile);
    document.getElementById("example-download").href = URL.createObjectURL(annotatedFile);
    source.value = annotated;
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

/** 复制含注释的示例原文，浏览器未授权剪贴板时选中原文供用户手动复制。 */
async function copyExample() {
  try {
    await navigator.clipboard.writeText(source.value);
    status.textContent = "带注释的示例已复制。";
  } catch {
    source.focus();
    source.select();
    status.textContent = "已选中全部 JSON，请使用系统的复制操作。";
  }
}

document.getElementById("open-template-example").addEventListener("click", openExample);
document.getElementById("example-copy").addEventListener("click", copyExample);
