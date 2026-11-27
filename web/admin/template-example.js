import {stripJsonComments} from "./template-draft.js";

const dialog = document.getElementById("template-example");
const format = document.getElementById("example-format");
const source = document.getElementById("example-json");
const status = document.getElementById("example-status");
const actions = document.getElementById("example-actions");
const standard = document.getElementById("example-download-json");
const annotated = document.getElementById("example-download");
const copy = document.getElementById("example-copy");
const descriptions = {
  canvas: "读取一段视频并另存为 MP4。nodes 记录节点和控件值，links 连接读取节点与保存节点。在 ComfyUI 中运行时，请先给 LoadVideo 选择自己的视频。",
  api: "同一份视频读写工作流。class_type 指定节点类型，SaveVideo.inputs.video 引用读取节点的输出。新增模板时，把上传素材绑定到 #1 的 file，把视频输出绑定到 #2 的 filename_prefix。",
  strategy: "这份 H3 文生视频完整策略带有逐字段注释，可直接导入并发布。注释中的 prompt_assist 演示如何为提示词启用扩写。",
};
let request = null;
let generatedUrl = null;

/** 切换示例或关闭窗口时，取消上一次读取并释放生成的标准 JSON 下载文件。 */
function clearExample() {
  request?.abort();
  request = null;
  if (generatedUrl) URL.revokeObjectURL(generatedUrl);
  generatedUrl = null;
  actions.hidden = true;
  source.hidden = true;
  source.value = "";
}

/** 加载所选格式，保证预览、复制内容和下载文件始终来自同一份示例。 */
async function loadExample() {
  clearExample();
  const current = new AbortController();
  request = current;
  const file = format.selectedOptions[0].dataset.file;
  const url = "./presets/" + file;
  const isAnnotated = format.value === "strategy";
  document.getElementById("example-description").textContent = descriptions[format.value];
  status.textContent = "正在读取示例…";
  status.dataset.tone = "";
  try {
    const response = await fetch(url, {
      mode: "same-origin", credentials: "omit", redirect: "error", signal: current.signal,
    });
    if (!response.ok) throw new Error();
    const text = await response.text();
    if (current !== request) return;

    // 标准示例直接提供原文件。完整策略另生成去注释版本，保留大整数种子的原始写法。
    if (isAnnotated) {
      const strict = stripJsonComments(text).split(/\r?\n/).map(line => line.trimEnd())
        .filter(Boolean).join("\n") + "\n";
      generatedUrl = URL.createObjectURL(new Blob([strict], {type: "application/json;charset=utf-8"}));
      annotated.href = url;
      annotated.download = file;
    }
    annotated.hidden = !isAnnotated;
    standard.href = generatedUrl ?? url;
    standard.download = file.replace(/\.jsonc$/, ".json");
    copy.textContent = isAnnotated ? "复制带注释的示例" : "复制示例";
    source.value = text;
    source.hidden = false;
    actions.hidden = false;
    status.textContent = "";
  } catch {
    if (current !== request) return;
    status.dataset.tone = "error";
    status.textContent = "示例读取失败，请关闭后重新打开。";
  }
}

/** 复制当前示例原文，浏览器未授权剪贴板时选中原文供用户手动复制。 */
async function copyExample() {
  const current = request;
  try {
    await navigator.clipboard.writeText(source.value);
    if (current === request) status.textContent = "示例已复制。";
  } catch {
    if (current !== request) return;
    source.focus();
    source.select();
    status.textContent = "已选中全部 JSON，请使用系统的复制操作。";
  }
}

document.getElementById("open-template-example").addEventListener("click", () => {
  dialog.showModal();
  void loadExample();
});
format.addEventListener("change", loadExample);
copy.addEventListener("click", copyExample);
dialog.addEventListener("close", clearExample);
