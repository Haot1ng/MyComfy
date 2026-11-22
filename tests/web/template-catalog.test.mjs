import assert from "node:assert/strict";
import test from "node:test";
import {builtins, readTemplateCatalog} from "../../web/admin/template-catalog.js";

test("模板选择器读取后续页，以已发布版本替代同标识内置模板", async t => {
  const pages = [
    {items: [{strategy_key: builtins[0].key, name: "文生 v2", version: 2, strategy_version_id: "sv2"}],
      next_cursor: "next/page"},
    {items: [
      {strategy_key: "imported", name: "导入的视频", version: 1, strategy_version_id: "sv3"},
      {strategy_key: builtins[0].key, name: "旧版本", version: 1, strategy_version_id: "sv1"},
    ], next_cursor: null},
  ];
  const cursors = [];
  t.mock.method(globalThis, "fetch", async url => {
    cursors.push(new URL(url, "http://localhost").searchParams.get("cursor"));
    return Response.json(pages.shift());
  });
  const templates = await readTemplateCatalog(new AbortController().signal);
  assert.deepEqual(cursors, [null, "next/page"]);
  assert.deepEqual(templates.map(item => item.key), [builtins[0].key, "imported", builtins[1].key]);
  assert.equal(templates[0].url, "/local/v1/strategy-versions/sv2");
  assert.equal(templates[1].url, "/local/v1/strategy-versions/sv3");
  assert.equal(templates[2].url, builtins[1].url);
});
