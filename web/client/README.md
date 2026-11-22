# MyComfy 视频生成工作台

在配置的公开 HTTPS 来源打开根路径即可使用工作台。页面由业务监听器提供，默认后端端口为 `8000`。网页使用原生 ES 模块和本地 CSS，登录页和工作台封面使用随包分发的静态图片。安装 Python 包后即可提供页面和资源。[启动与部署](../../README.md) · [两端 API 契约](../../docs/web-api.md)

| 入口 | 用户操作 |
| --- | --- |
| 账号入口 | 注册、登录。会话替换后，旧页签退出私有视图。 |
| 工作台 | 创建批次或打开最近的任务。 |
| 创建批次 | 每项选择策略和版本，按 Schema 填写参数或上传素材。服务端逐项校验，超时后可用原批次键和原始内容核对结果。 |
| 任务记录 | 按状态、批次筛选，打开批次汇总或任务详情，取消未结束任务。详情中可预览或下载产物，点“原始任务信息”可查看当时的提示词、其他参数和输入素材。 |
| 站内通知 | 查看成功、最终失败、已取消通知，筛选并标记已读，可跳转对应任务。 |
| 账户设置 | 修改密码、退出当前会话。修改密码成功后为当前页签轮换凭据。 |

页面路由使用 URL 片段，可直接收藏任务或批次详情。任务详情以当前执行阶段报告进度，接口提供可靠比例时才显示百分比。关键变化通过每页一条 SSE 同步，重连时按用户游标补发。用户、会话代次及资源访问权由服务端校验。[会话与事件客户端](session.js) · [批次表单](composer.js) · [任务与通知](history.js)

仅在单机回环开发时，可设置 `MYCOMFY_PUBLIC_ORIGIN=http://127.0.0.1:8000` 与 `MYCOMFY_COOKIE_SECURE=false` 后运行 API，并从同一地址打开网页。生产部署使用同源 HTTPS 和 Secure Cookie。实际生成还需已发布且有兼容部署的策略、运行中的 Worker 与 ComfyUI。[引擎部署](../../docs/engine-deployment.md)

## 按分组显示页面与按钮

登录或密码轮换成功后，工作台用 `GET /api/v1/me` 读取页签对应的会话与权限。响应中的 `groups` 是用户所属分组，`permissions` 是这些分组授权的并集。路由和导航用同一规则检查页面，操作按钮分别检查对应权限。[工作台入口](app.js) · [权限模块](access.mjs)

| 页面或操作 | 权限 |
| --- | --- |
| 生成页面 `generate` | `strategies.view` |
| 任务页面 `jobs` | `jobs.view` |
| 通知页面 `notifications` | `notifications.view` |
| 提交任务 | `jobs.submit` |
| 取消任务 | `jobs.cancel` |
| 上传素材 | `assets.upload` |
| 预览或下载文件 | `assets.download` |
| 标记通知已读 | `notifications.mark_read` |

页签重新获得焦点、收到 `403 PERMISSION_DENIED` 或收到任务事件流的 `permissions.changed` 时，工作台重新读取权限并更新页面。退出登录或会话失效时清除私有页面。所有业务请求由后端再次核验当前权限。

```bash
node --test tests/web/session.test.mjs web/client/access.test.mjs
```
