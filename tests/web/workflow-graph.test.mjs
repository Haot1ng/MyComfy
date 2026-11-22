import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import {parseWorkflow} from "../../web/admin/workflow-graph.js";

test("H3 图生模板预览保留首帧绑定、输入连线和必需的视频产物", () => {
  const file = new URL("../../web/admin/presets/h3-i2v.strategy.json", import.meta.url);
  const template = JSON.parse(readFileSync(file, "utf8"));
  const {kind, graph} = parseWorkflow(template);
  assert.equal(kind, "strategy");
  const image = graph.nodes.get("114");
  assert.equal(image.bindings[0].input, "first_frame");
  assert.equal(image.outgoing[0].input, "first_frame");
  assert.equal(image.outgoing[0].target.type, "MiniMaxH3ImageToVideo");
  assert.equal(graph.nodes.get("92").outputs[0].role, "video");
});

test("API 节点的编号可与策略字段同名", () => {
  const workflow = {
    workflow: {class_type: "Source", inputs: {}},
    inputs: {class_type: "Target", inputs: {image: ["workflow", 0]}},
  };
  assert.equal(parseWorkflow(workflow).kind, "api");
  const {kind, graph} = parseWorkflow({strategy_key: "demo", workflow});
  assert.equal(kind, "strategy");
  assert.equal(graph.nodes.get("inputs").incoming[0].source.id, "workflow");
});

test("画布新旧连线格式均保留输出序号和目标输入名", () => {
  const nodes = [
    {id: 10, type: "Loader"},
    {id: 20, type: "Decode", inputs: [{name: "samples"}]},
  ];
  const tuple = [7, 10, 1, 20, 0, "LATENT"];
  const object = {id: 7, origin_id: 10, origin_slot: 1, target_id: 20, target_slot: 0};
  for (const link of [tuple, object]) {
    const {kind, graph} = parseWorkflow({nodes, links: [link]});
    assert.equal(kind, "canvas");
    assert.equal(graph.edges.length, 1);
    const [edge] = graph.edges;
    assert.deepEqual([edge.source.id, edge.slot, edge.target.id, edge.input], ["10", 1, "20", "samples"]);
  }
});
