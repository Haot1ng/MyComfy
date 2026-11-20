import assert from "node:assert/strict";
import test from "node:test";

import { applyAccess, canVisitPage, loadAccess } from "./access.mjs";

test("浏览组可进入任务页，提交与取消按钮保持隐藏", () => {
  const profile = { groups: [{ key: "viewer" }], permissions: ["jobs.view"] };
  const elements = ["jobs.view", "jobs.submit", "jobs.cancel"].map((permission) => ({
    hidden: true, dataset: { requiresPermission: permission },
  }));
  applyAccess({ querySelectorAll: () => elements }, profile);
  assert.equal(canVisitPage(profile, "jobs"), true);
  assert.equal(canVisitPage(profile, "generate"), false);
  assert.equal(canVisitPage(profile, "unknown"), false);
  assert.deepEqual(elements.map((element) => element.hidden), [false, true, true]);
  applyAccess({ querySelectorAll: () => elements }, null);
  assert.ok(elements.every((element) => element.hidden));
});

test("从当前会话读取有效权限，退出后关闭受保护控件", async () => {
  const profile = { groups: [{ key: "standard" }], permissions: ["jobs.view", "jobs.submit"] };
  const result = await loadAccess(async (url, options) => {
    assert.equal(url, "/api/v1/me");
    assert.equal(options.credentials, "same-origin");
    return new Response(JSON.stringify(profile));
  });
  assert.deepEqual(result, profile);
  assert.equal(await loadAccess(async () => new Response(null, { status: 401 })), null);
});
