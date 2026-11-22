import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import {buildTemplateDraft, readApiInputs} from "../../web/admin/template-draft.js";

const graph = '{"prompt":{"class_type":"Text","inputs":{"text":"","seed":18446744073709551613,"flag":true,"cfg":4.5}},' +
  '"save":{"class_type":"SaveVideo","inputs":{"video":["prompt",0],"path":"video","image":"first.png"}}}';
const output = {node_id: "save", path_input: "path", media_type: "video/mp4"};
const binding = {input: "first_frame", title: "首帧", kind: "asset", node_id: "save", node_input: "image"};
const base = {name: " 新模板 ", key: "custom-h3", workflowText: graph, output};

test("裸 API 工作流生成可发布策略，标量、连线和原始随机种子处理正确", () => {
  const nodes = readApiInputs(graph);
  assert.deepEqual(nodes[0].fields.map(field => field.type), ["string", "integer", "boolean", "number"]);
  assert.deepEqual(nodes[1].fields.map(field => field.name), ["path", "image"]);
  const result = buildTemplateDraft({...base, bindings: [binding]});
  assert.ok(result.includes('"seed":18446744073709551613'));
  const strategy = JSON.parse(result);
  assert.equal(strategy.name, "新模板");
  assert.deepEqual(strategy.input_schema.properties.first_frame, {title: "首帧", type: "string"});
  assert.deepEqual(strategy.input_schema.required, ["first_frame"]);
  assert.equal(strategy.bindings[0].kind, "asset");
  assert.equal(strategy.output_contract[0].path_input, "path");
});

test("同一字段能驱动两个输入，重复的目标或不兼容类型被拒绝", () => {
  const same = {...binding, node_id: "prompt", node_input: "text"};
  const shared = JSON.parse(buildTemplateDraft({...base, bindings: [binding, same]}));
  assert.deepEqual(shared.input_schema.required, ["first_frame"]);
  assert.equal(shared.bindings.length, 2);
  for (const other of [
    binding, {...same, kind: "value"}, {...binding, node_input: "path"},
    {...binding, node_input: "video"}, {...same, node_input: "flag"},
  ]) assert.throws(() => buildTemplateDraft({...base, bindings: [binding, other]}));
  assert.throws(() => buildTemplateDraft({...base, key: "Not a key"}));
});

test("两个内置模板复制时仅替换系列标识及展示名称", () => {
  for (const type of ["t2v", "i2v"]) {
    const path = new URL("../../web/admin/presets/h3-" + type + ".strategy.json", import.meta.url);
    const preset = readFileSync(path, "utf8").replace("757358688076805", "18446744073709551613");
    const expected = JSON.parse(preset);
    const name = '模板 {"name": "内层, []"}';
    const fork = buildTemplateDraft({presetText: preset, name, key: "new-series"});
    assert.deepEqual(JSON.parse(fork), {...expected, name, strategy_key: "new-series"});
    assert.ok(fork.includes("18446744073709551613"));
    assert.throws(() => buildTemplateDraft({presetText: preset, name, key: expected.strategy_key}));
  }
});

test("在选择输入前识别画布文件和失效的 API 连线", () => {
  assert.throws(() => readApiInputs('{"nodes":[],"links":[]}'), /API/);
  assert.throws(() => readApiInputs(graph.replace('["prompt",0]', '["missing",0]')), /连线/);
  assert.throws(() => readApiInputs('{'), /JSON/);
});
