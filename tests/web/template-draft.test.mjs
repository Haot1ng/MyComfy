import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import {buildTemplateDraft, promptTextFields, readApiInputs, stripJsonComments, writePromptAssist} from "../../web/admin/template-draft.js";

const graph = '{"prompt":{"class_type":"Text","inputs":{"text":"","seed":18446744073709551613,"flag":true,"cfg":4.5}},' +
  '"save":{"class_type":"SaveVideo","inputs":{"video":["prompt",0],"path":"video","image":"first.png"}}}';
const output = {node_id: "save", path_input: "path", media_type: "video/mp4"};
const binding = {input: "first_frame", title: "首帧", kind: "asset", node_id: "save", node_input: "image"};
const base = {name: " 新模板 ", key: "custom-h3", workflowText: graph, output};

test("JSONC 仅替换注释和 BOM，保留行列、转义字符串以及数值写法", () => {
  const key = "字段/*注释？*/";
  const prompt = 'https://example.test/路径?x=// "引号" \\ /*提示文字*/ \\\\';
  const literal = JSON.stringify({[key]: prompt}).slice(1, -1);
  const source = '\uFEFF// 首行 \r\n{/* 🐈\r跨行\n说明 */' + literal +
    ',"seed":18446744073709551613, // 留在原列\r"cfg":1.000e+03,"zero":-0}//末行';
  const strict = stripJsonComments(source);
  assert.equal(strict.length, source.length);
  assert.deepEqual(strict.match(/[\r\n]/g), source.match(/[\r\n]/g));
  for (const token of [literal, "18446744073709551613", "1.000e+03", "-0"]) {
    assert.equal(strict.indexOf(token), source.indexOf(token));
  }
  assert.equal(JSON.parse(strict)[key], prompt);
  assert.equal(stripJsonComments(strict), strict);
});

test("未闭合的块注释报告原始行号，注释不能拼接数值或开通其他 JSON 扩展", () => {
  assert.throws(() => stripJsonComments('// line\r\n{\r"n":1 /*未闭合'), /第 3 行.+结束标记/);
  for (const invalid of ['[1/*说明*/2]', '{"n":1,/*说明*/}', '{"s":"未闭合 /*', '{"n":1}/']) {
    assert.throws(() => JSON.parse(stripJsonComments(invalid)), SyntaxError);
  }
  assert.equal(stripJsonComments('{"s":"/* 未闭合的文字"}'), '{"s":"/* 未闭合的文字"}');
});

test("带注释的 API 工作流与完整模板均能生成严格 JSON 并精确保留种子", () => {
  const workflowText = "\uFEFF// 工作流\r\n" + graph.replace('"seed":', '/* 仅作说明 */ "seed":') + "\n// EOF";
  assert.deepEqual(readApiInputs(workflowText), readApiInputs(graph));
  const built = buildTemplateDraft({...base, workflowText, bindings: [binding]});
  const copied = buildTemplateDraft({presetText: "// 模板\n" + built, name: "复制", key: "forked"});
  for (const result of [built, copied]) {
    assert.equal(JSON.parse(result).workflow.save.class_type, "SaveVideo");
    assert.ok(result.includes('"seed":18446744073709551613'));
    assert.doesNotMatch(result, /仅作说明|\/\/ 模板|\/\/ EOF/);
  }
});

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

test("只列出可填写的已绑定字符串，排除素材、枚举、固定值和越界字段名", () => {
  const properties = {
    prompt: {type: "string", title: "镜头"}, asset: {type: "string"}, seed: {type: "integer"},
    choice: {type: "string", enum: ["a", "b"]}, fixed: {type: "string", const: "a"},
    unbound: {type: "string"}, " ": {type: "string"}, ["景".repeat(129)]: {type: "string"},
    ["🎬".repeat(128)]: {type: "string"}, ref: {$ref: "#/$defs/text"},
  };
  const bindings = Object.keys(properties).filter(name => name !== "unbound").map(input => ({kind: "value", input}));
  bindings.push({kind: "asset", input: "asset"});
  const fields = promptTextFields({input_schema: {properties}, bindings});
  assert.deepEqual(fields, [{name: "prompt", title: "镜头"}, {name: "🎬".repeat(128), title: "🎬".repeat(128)}]);
});

test("添加、更新与移除提示词配置都保留工作流和 Schema 的原始数值及同名嵌套字段", () => {
  const nested = '"workflow":{"n":{"inputs":{"seed":18446744073709551613,"prompt_assist":[{"q":"} , [ \\""}]}}}';
  const schema = '"input_schema":{"properties":{"cfg":{"default":1.000000000000001e+03}}}';
  const before = "{" + nested + "," + schema + "}";
  const config = {instructions: '保留 {"猫":"&<"} 的外观', fields: ["prompt"]};
  const added = writePromptAssist(before, config);
  assert.deepEqual(JSON.parse(added).prompt_assist, config);
  assert.ok(added.includes(nested));
  assert.ok(added.includes(schema));
  // 顶层键使用 Unicode 转义时仍然只修改该成员，且可从对象首、中、末三个位置移除。
  const old = '"prompt_\\u0061ssist":{"instructions":"旧指令","fields":["a"]}';
  for (const members of [[old, nested, schema], [nested, old, schema], [nested, schema, old]]) {
    const updated = writePromptAssist("{" + members.join(",") + "}", config);
    assert.deepEqual(JSON.parse(updated).prompt_assist, config);
    assert.equal(writePromptAssist(updated, null), before);
  }
});
