/** 两份随程序提供的模板在数据库尚未发布任何版本时也能预览和发布。 */
export const builtins = [
  {key: "minimax-h3-t2v", name: "MiniMax H3 文生视频", url: "./presets/h3-t2v.strategy.json"},
  {key: "minimax-h3-i2v", name: "MiniMax H3 图生视频", url: "./presets/h3-i2v.strategy.json"},
];

/** 遍历所有元数据页，按标识保留最新已发布版本，再补上尚未发布的内置模板。 */
export async function readTemplateCatalog(signal) {
  const byKey = new Map();
  let cursor = null;
  do {
    const query = new URLSearchParams({limit: "100"});
    if (cursor) query.set("cursor", cursor);
    const response = await fetch("/local/v1/strategies?" + query, {
      mode: "same-origin", credentials: "omit", cache: "no-store", redirect: "error", signal,
    });
    if (!response.ok) throw new Error("HTTP " + response.status);
    const page = await response.json();
    for (const item of page.items) {
      const key = item.strategy_key;
      // 发布可能与分页同时发生。保留该系列见过的最高版本，并使用不可变版本 ID 读取详情。
      if (!byKey.has(key) || item.version > byKey.get(key).version) {
        byKey.set(key, {
          key, name: item.name, version: item.version,
          url: "/local/v1/strategy-versions/" + encodeURIComponent(item.strategy_version_id),
        });
      }
    }
    cursor = page.next_cursor;
  } while (cursor);
  return [...byKey.values(), ...builtins.filter(template => !byKey.has(template.key))];
}
