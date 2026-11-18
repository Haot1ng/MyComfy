# C 端网页

这个目录用于开发用户侧页面。页面覆盖注册与登录、素材上传、策略选择、提交批次、任务进度和视频预览与下载。

网页通过同源的 `/api/v1` 访问 Python 服务。登录响应提供 Cookie、会话代次和 CSRF 令牌，后续需要登录的写请求携带 `X-Session-Generation` 和 `X-CSRF-Token`。[用户 API](../../src/mycomfy/api/app.py)

任务变化、断线补发和站内通知使用事件接口。[事件与通知](../../src/mycomfy/api/events.py)
