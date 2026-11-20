// 用户的组权限由服务端在 /api/v1/me 合并返回。
export const PAGE_PERMISSIONS = Object.freeze({
  generate: "strategies.view",
  jobs: "jobs.view",
  notifications: "notifications.view",
});

/** 判断当前用户是否拥有指定的页面或操作权限。 */
export function hasPermission(profile, permission) {
  return typeof permission === "string"
    && Array.isArray(profile?.permissions)
    && profile.permissions.includes(permission);
}

/** 检查目标页面，直接进入地址和点击导航使用同一套规则。 */
export function canVisitPage(profile, page) {
  return Object.hasOwn(PAGE_PERMISSIONS, page)
    && hasPermission(profile, PAGE_PERMISSIONS[page]);
}

/** 根据声明的权限显示或隐藏导航、页面容器和按钮。 */
export function applyAccess(root, profile) {
  for (const element of root.querySelectorAll("[data-requires-permission]")) {
    element.hidden = !hasPermission(profile, element.dataset.requiresPermission);
  }
}

/** 从当前 Cookie 对应的会话读取最新分组和权限。 */
export async function loadAccess(request = globalThis.fetch) {
  const response = await request("/api/v1/me", { credentials: "same-origin", cache: "no-store" });
  if (response.status === 401) return null;
  if (!response.ok) throw new Error("无法读取当前用户权限");
  const profile = await response.json();
  if (!Array.isArray(profile.groups) || !Array.isArray(profile.permissions)) {
    throw new Error("用户权限响应格式不正确");
  }
  return profile;
}
