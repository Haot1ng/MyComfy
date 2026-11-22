# MyComfy V1 前台与管理后台 API

用户前台通过同源 `/api/v1` 完成账号、批次生成和视频交付。本机管理后台通过 `/local/v1/monitor` 查询全局运行状态。维护人员通过本机凭据调用策略发布和密码重置接口。本文列出的请求和响应以当前实现为准，页面依赖的接口限制见末节。[V1 实现与验收规格](https://chatgpt.com/space/page_d83476ac4c1c81918dc24a4ed1be8e51)

## 入口与权限

| 调用方 | 接口前缀 | 访问条件 |
| --- | --- | --- |
| 用户前台 | `/api/v1` | 网页和 API 使用同源 HTTPS。公开写请求校验 `Origin`。注册和登录以外的接口需要有效会话，任务、素材、产物和通知均按当前用户授权。 |
| 本机只读监控 | `/local/v1/monitor` | 只读 GET，免账号登录。API 要求连接对端与 `Host` 为回环地址，携带的 `Origin` 必须匹配本机入口。 |
| 模板导入页、本机维护程序 | `/local/v1` | 满足同样的本机入口限制，写请求另带 `Authorization: Bearer <token>`。凭据来自操作系统权限受限的维护令牌文件。 |

API 默认业务端口为 `8000`，本机端口为 `127.0.0.1:8001`。两个监听器分别在各自的根路径 `/` 提供用户网页和管理网页。用户网页的同源反向代理将页面、前台静态资源和业务 API 转到业务端口。管理网页和监控 API 仅使用本机回环入口。监控视图仅调用监控接口，独立的模板导入页在维护人员确认发布时调用策略版本接口。公开代理转发 SSE 时关闭缓冲，空闲超时设得长于心跳周期。[双端口监听](../src/mycomfy/api/server.py) · [部署与运维](operations.md)

## 通用约定

- 资源 ID、会话代次和 64 位事件序号使用字符串。事件序号按十进制整数精确比较，JavaScript 可使用 `BigInt`。时间使用带时区的 RFC 3339，响应中的 UTC 时间可按用户所在时区展示。
- 普通请求和响应使用 JSON。素材上传、媒体字节和 SSE 使用各自的媒体类型。成功的 `204` 响应没有响应体。
- 每次 API 请求返回 `X-Request-ID` 并登记业务审计。网页静态资源使用 ETag 和条件读取。业务失败还在响应体提供 `request_id`，可通过本机请求监控查询。重放请求有新的请求 ID，业务幂等性由对应接口单独定义。
- 分页响应为 `{items, next_cursor}`，其中 `next_cursor` 为字符串或 `null`。用户列表 `limit` 默认 50、最大 100，本机列表默认 50、最大 200。使用原筛选条件和返回的游标续页，变更筛选时从首页重新加载。任务列表按创建时间和 ID 倒序，批次详情按提交顺序列任务，通知按事件序号倒序。

错误响应示例。批量校验错误的 `index` 从零开始，`path` 为 JSON Pointer。界面根据 `error.code` 和 `details` 定位错误，并提供请求 ID 供排查。部分错误的 `details` 为 `null`。

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "Check the task parameters",
    "details": [
      {"index": 1, "client_task_key": "clip-2", "path": "/tasks/1/inputs/prompt", "code": "REQUIRED"}
    ]
  },
  "request_id": "r_example"
}
```

| HTTP 状态 | 常见错误码及处理 |
| --- | --- |
| 400 | `INVALID_JSON`、`INVALID_CURSOR`、`INVALID_FILTER` 或 `RESYNC_REQUIRED`。重新构造请求或重做相应同步。 |
| 401 | `SESSION_INVALID` 表示会话失效。`AUTH_FAILED` 表示登录凭据或改密时提交的当前密码校验失败。 |
| 403 | `ORIGIN_REJECTED`、`CSRF_REJECTED`、`LOCAL_ONLY` 或 `MAINTENANCE_FORBIDDEN`。分别检查来源、会话令牌、监听入口或维护凭据。 |
| 404 | `NOT_FOUND`。资源不存在或不在当前调用方的可访问范围内。 |
| 409 | `USERNAME_TAKEN` 或 `IDEMPOTENCY_CONFLICT`。分别处理用户名冲突和同一批次键绑定了不同内容。 |
| 413、415 | `UPLOAD_TOO_LARGE`、`INVALID_MEDIA` 或 `UNSUPPORTED_MEDIA_TYPE`。检查大小、实际媒体内容或请求编码。 |
| 416 | `INVALID_RANGE`。媒体请求的单段字节范围不可满足。 |
| 422 | `VALIDATION_FAILED` 等参数或业务校验错误。按 `details` 映射到表单和任务项。 |
| 429 | `LOGIN_LIMIT`、`USER_JOB_QUOTA` 或 `USER_STORAGE_QUOTA`。提示对应限制，响应有 `Retry-After` 时按其指示等待。 |
| 503 | `GLOBAL_QUEUE_FULL`、`VOLUME_LOW`、`STORAGE_UNAVAILABLE`、`OBJECT_UNAVAILABLE`、`DATABASE_UNAVAILABLE` 等。提示受影响操作暂不可用。批次提交结果不明时按原请求重放。 |

[HTTP 路由与错误处理](../src/mycomfy/api/app.py) · [分页实现](../src/mycomfy/api/pagination.py) · [容量限制](../src/mycomfy/ops/capacity.py)

## 用户前台

以下路径均相对于 `/api/v1`。

### 账号与会话

| 方法与路径 | 输入 | 成功响应 |
| --- | --- | --- |
| `POST /accounts` | `{username, password}` | `201 {user_id}`，随后调用登录。 |
| `POST /sessions` | `{username, password}` | `200` 会话对象，设置会话 Cookie 并撤销该账号的旧会话。 |
| `GET /me` | 会话 Cookie | `200` 当前会话对象，额外返回用户所属的 `groups` 及授权并集 `permissions`。 |
| `DELETE /sessions/current` | 会话 Cookie、页签代次和 CSRF | `204`，撤销本次匹配的会话。 |
| `POST /me/password` | `{current_password, new_password}` | `200` 新会话对象，设置轮换后的 Cookie。 |

会话对象包含 `user_id`、`username`、`session_generation`、`csrf_token`。会话 Cookie 名为 `mycomfy_session`，生产环境使用 `HttpOnly`、`Secure`、`SameSite=Lax`。浏览器自动携带 Cookie。页签针对已知会话的 fetch 请求携带 `X-Page-User`（打开页签时的用户 ID）和 `X-Session-Generation`，写请求另需 `X-CSRF-Token`。服务端将页签身份与 Cookie 比对。已有 API 调用方可省略页签用户头，读取接口也可省略代次。

前端使用 `permissions` 决定页面和按钮的可见性。业务接口重新校验当前授权，缺少对应权限时返回 `403 PERMISSION_DENIED`。[页面与操作的权限表](../web/client/README.md#按分组显示页面与按钮)

注册用户名最多 100 字符，服务端按 NFKC 规范化、去除首尾空白并折叠大小写。注册、改密与维护重置的新密码要求 8 至 1024 字符。

页面收到 `SESSION_INVALID` 或 SSE 的 `session.revoked` 时停止订阅并清除私有视图。SSE 连接报错时用 `GET /me` 核对用户和页签代次，确认失效时停止重连。旧页签保留原代次用于识别失效，由用户重新登录进入新会话。退出响应依靠服务端撤销对应会话，浏览器 Cookie 保持原样直到到期或轮换。已受理的任务照常运行。[会话实现](../src/mycomfy/api/auth.py) · [用户路由](../src/mycomfy/api/app.py)

### 策略与素材

| 方法与路径 | 输入 | 成功响应 |
| --- | --- | --- |
| `GET /strategies` | 分页 | `200`，每个系列最高已发布版本。 |
| `GET /strategies/{key}/versions` | 系列键、分页 | `200`，同系列历史版本，按版本号倒序。 |
| `GET /strategy-versions/{version_id}` | 版本 ID | `200`，单个策略版本对象。 |
| `POST /assets` | `multipart/form-data`，唯一文件字段 `file` | `201`，已登记的素材对象，角色为 `input`。 |
| `GET /assets/{asset_id}` | 文件 ID | `200`，授权后的素材或产物元数据。 |

策略版本对象含 `strategy_version_id`、`strategy_key`、`version`、`name`、`content_digest`、`input_schema`、`asset_fields`、`accepting`、`reason`、`expose_after_cancel`。按各任务选定版本的 JSON Schema 生成表单，使用该版本的参数类型、必填项和默认值。已有兼容部署但实例忙、维护或暂时离线时仍可受理。缺少兼容部署时返回 `accepting: false` 和 `NO_COMPATIBLE_DEPLOYMENT`。

版本的 `asset_fields` 是去重后的素材输入字段名数组。按该数组和对应字段的 Schema 区分上传控件、普通参数、单值及数组。每次上传一个文件，浏览器负责生成 multipart boundary。服务端校验实际类型、媒体内容、大小和配额，持久化成功后才返回 `asset_id`。任务的素材参数提交此 ID，数组字段提交 ID 数组。单文件上限由 `MYCOMFY_MAX_UPLOAD_BYTES` 配置，默认 100 MiB。上传素材与批次分别登记，批次校验失败后仍可用已登记的素材 ID 重新提交。[策略对象](../src/mycomfy/generation/strategy.py#L274) · [上传路由](../src/mycomfy/api/app.py#L588) · [部署默认值](../src/mycomfy/config.py)

### 批次、任务与取消

| 方法与路径 | 输入 | 成功响应 |
| --- | --- | --- |
| `POST /batches` | `{client_batch_key, tasks}` | `202 {batch_id, tasks: [{client_task_key, job_id}]}`，任务保持请求顺序。 |
| `GET /batches/{batch_id}` | 批次 ID、分页 | `200 {batch_id, counts, items, next_cursor}`，`counts` 是整个批次按已有任务状态分组的计数，缺少的状态按零处理。 |
| `GET /jobs` | 可选 `state`、`batch_id` 及分页 | `200`，当前用户的任务摘要列表。 |
| `GET /jobs/{job_id}` | 任务 ID | `200`，任务详情及其可见产物。 |
| `POST /jobs/{job_id}/cancel` | 可省略请求体，或传 `{reason}`，最长 255 字符 | `200 {job_id, state, job_version}`。非终态原子取消，终态请求返回已提交的结论。 |

```json
{
  "client_batch_key": "order-01",
  "tasks": [
    {
      "client_task_key": "clip-1",
      "strategy_version_id": "sv_example",
      "inputs": {"prompt": "日出中的港口", "reference_asset_ids": ["a_example"]}
    }
  ]
}
```

- 每批至少一项，默认最多 20 项，由 `MYCOMFY_MAX_BATCH_TASKS` 配置。每项独立选择策略版本。`client_task_key` 在批内唯一，两种客户端键均为 1 至 200 字符。
- 整批校验通过后才创建记录。对逐项 `422`，按任务键或索引和字段路径展示错误。未受理的批次修改后可沿用原键。已受理的批次，同一用户以同键同内容重放可取回原 ID，键相同但内容变化返回 `409 IDEMPOTENCY_CONFLICT`。响应丢失时保留原键和请求快照，按原任务和数组顺序重放。
- 任务摘要含 `job_id`、`batch_id`、`strategy_version_id`、`client_task_key`、`state`、`job_version`、`created_at`、`blocked_reason`、`retry_at`。详情增加 `current_execution_id`、`stage`、`execution_phase`、`progress`、`attempt_no`、`failure`、`assets`。未知值按 `null` 处理。

| `state` | 页面含义 |
| --- | --- |
| `QUEUED` | 排队中。有 `blocked_reason` 时同时显示等待原因。 |
| `EXECUTING` | 执行中。按可获得的 `stage` 和尝试序号展示。 |
| `RECOVERING` | 正在核对原执行，后续状态从服务端同步。 |
| `RETRY_WAIT` | 等待自动重试，参考 `retry_at`。 |
| `PUBLISHING` | 正在校验和发布文件。 |
| `SUCCEEDED` | 必需文件可读且已登记。文件后来不可用时，单独显示文件状态。 |
| `FAILED` | 最终失败，详情可显示 `failure`。 |
| `CANCELLED` | 业务取消已生效。原执行可能仍在核对或收尾，按详情中的执行阶段提示。 |

取消与成功以事务先后裁决。页面用取消响应的 `state` 更新结论，随后刷新详情。每项任务独立变化，同批其他任务可继续执行。用户历史从 `GET /jobs` 进入，以 `batch_id` 打开批次汇总。[批次及任务数据](../src/mycomfy/generation/jobs.py) · [状态转换](../src/mycomfy/generation/state.py)

### 产物预览与下载

| 方法与路径 | 输入 | 成功响应 |
| --- | --- | --- |
| `GET /jobs/{job_id}/assets` | 任务 ID、分页 | `200`，仅返回在固定策略版本下可见的产物。 |
| `GET /assets/{asset_id}/content` | 可选 `download=1`、单段 `Range: bytes=...` | 整文件 `200`，有效单段范围 `206` 并附 `Content-Range`，下载附 `Content-Disposition: attachment`。 |

公开文件对象含 `asset_id`、`role`、`media_type`、`size`、`digest`、`media`、`complete`、`available`、`content_url`。`size` 为字节，`digest` 为 SHA-256，`media` 可含 `width`、`height`、`duration`（秒）、`fps`，字段可能缺失或为 `null`。文件不可用时 `content_url` 为 `null`。

播放器使用完整、可用且浏览器支持的视频的内容地址。可见且可用的部分文件通过 `download=1` 下载并标明不完整。浏览器在预览和下载 URL 中附 `page_user` 与 `session_generation`，使自动发起的字节请求也绑定页签身份。每次读取字节，包括播放器发起的 Range，均校验会话、归属和取消后开放规则。文件故障期间可能返回 `503 OBJECT_UNAVAILABLE`。已取消任务的产物可见性取决于所绑定版本的 `expose_after_cancel`。[文件授权与下载](../src/mycomfy/api/app.py#L571) · [Range 与媒体校验](../src/mycomfy/artifacts/storage.py)

### 事件与站内通知

| 方法与路径 | 输入 | 成功响应 |
| --- | --- | --- |
| `GET /events/cursor` | 当前会话 | `200 {user_seq}`，当前已提交事件水位，无事件时为字符串 `"0"`。 |
| `GET /events` | `session_generation`、`after`、可选 `page_user`，重连可用 `Last-Event-ID` | `200 text/event-stream`，按当前用户事件序号补发和推送。 |
| `GET /notifications` | 可选 `unread=true/false`、分页 | `200`，通知对象含 `notification_id, user_seq, type, data, created_at, read_at`，未读的 `read_at` 为 `null`。 |
| `PUT /notifications/{notification_id}/read` | 通知 ID、认证写请求头 | `204`，重复提交保留首次标记时间。 |

1. 初始化时先读取事件水位 W，再加载所需任务和通知快照，然后建立一条当前页用户聚合流 `/events?after=W&session_generation=G&page_user=U`。
2. SSE 持久帧的 `id` 是事件序号，`event` 是业务类型，`data` 包含 `event_id`、`job_id`、`job_version`、`state`、`occurred_at` 及该事件的附加字段。实际类型包括状态转换产生的 `job.queued`、`job.executing`、`job.recovering`、`job.retry_wait`、`job.publishing`、`job.succeeded`、`job.failed`、`job.cancelled`，以及 `job.blocked_changed`、`asset.available`、`asset.unavailable`。
3. 以用户和序号去重。应用事件或快照时按 `job_version` 防止状态回退。事件中缺少的详情字段从查询接口刷新。终态事件可刷新通知，即使该事件对应的任务快照已更新。成功、最终失败和取消会生成持久站内通知，已读由显式请求标记。
4. 重连优先使用 `Last-Event-ID`，否则使用 `after`。游标非法、超出水位或丢失时重新执行水位和快照流程。原生 EventSource 的错误事件无法读取 HTTP 错误体，遇到连接关闭时先核对会话，确认会话有效后可重新同步。心跳是注释，`session.revoked` 是控制帧，两者均无需推进持久游标。

[SSE 和通知接口](../src/mycomfy/api/events.py) · [事件字段](../src/mycomfy/generation/state.py#L26)

## 本机只读管理后台

以下路径均相对于 `/local/v1/monitor`。常规集合支持 `GET /{collection}` 分页和 `GET /{collection}/{id}` 详情。集合列表按 ID 续页，详情 ID 与对应列表记录的 `id` 一致。总览为单对象，日志为按行翻页的特殊集合。

| 视图 | 集合或路径 | 主要字段与关联 |
| --- | --- | --- |
| 总览 | `GET /overview` | `sampled_at`、`states`、`queue_length`、`oldest_wait_seconds`、`retries`、`isolated`、`publication_backlog`、`volumes`、`alerts`。卷包含 `roles, free_bytes, reserved_bytes, stop_bytes, resume_bytes, paused`。告警按对象键列原因。 |
| 请求 | `requests` | `id` 是 `X-Request-ID`，并有 `actor, action, result, duration_ms, target, created_at`。据 `action` 判断 `target` 所指资源，`result` 是 HTTP 状态。 |
| 批次 | `batches` | `id, user_id, created_at`。通过 `GET /jobs?batch_id=...` 查看该批任务。 |
| 任务 | `jobs`，同一视图也可用 `tasks` | `id, batch_id, user_id, strategy_version_id, state, version, current_execution_id, created_at, retry_at, blocked_reason, failure`。执行历史通过 `GET /executions?job_id=...` 获取。 |
| 执行 | `executions` | `id, job_id, batch_id, strategy_version_id, instance_id, generation, attempt_no, phase, prompt_id, environment, workflow_digest, stage, budget_charged, budget_refunded, remaining_attempts, created_at, authorized_at, updated_at, result, nodes, log_url`。通过 `result` 中存在的阶段迁移证据和最近节点采样追踪执行。 |
| 实例与 GPU | `instances`、`metrics` | `id, generation, configured, accepting, manifest, heartbeat_at, active_execution_id, quarantine_reason, process_alive, api_reachable, occupied, metrics, metrics_stale, stale`。`metrics` 为最新采样，含采样时间、显存用量与峰值和 GPU 使用率。过期标记与采样值一起展示。 |
| 文件 | `files` | `id, job_id, execution_id, user_id, role, output_index, provider, size, media_type, digest, media, complete, available, created_at`。用任务、执行 ID 串联文件诊断。 |
| 日志 | `GET /logs`、`GET /logs/{execution_id}` | 行含 `instance_id, generation, execution_id, offset, sampled_at, message`。执行详情提供 `log_url`。按实例查看当前代次日志时，`execution_id` 可为 `null`。 |

列表按资源支持 ID 和时间筛选。请求可按 `request_id`、`user_id` 或作为审计目标的资源 ID 查询。常用条件如下。

| 集合 | 适用的 ID 筛选 |
| --- | --- |
| `batches` | `batch_id, user_id` |
| `jobs`、`tasks` | `job_id, batch_id, user_id` |
| `executions` | `execution_id, job_id, instance_id, batch_id, user_id` |
| `instances`、`metrics` | `instance_id` |
| `files` | `job_id, execution_id, instance_id, user_id` |
| `logs` | `execution_id, job_id, instance_id, batch_id, user_id`。可用 `level` 筛选日志文本。 |

`since` 和 `until` 接受带时区的 RFC 3339。常规列表按创建时间筛选，实例与指标按心跳时间，日志按日志行时间。日志 API 限制每页读取量并脱敏，轮转后旧游标可能失效。总览和采样可定时刷新，后端采集和告警独立于网页运行。[监控字段、筛选与日志](../src/mycomfy/ops/monitor.py) · [GPU 采样](../src/mycomfy/engine/runtime.py#L124)

## 本机维护

本机管理后台的 `/templates.html` 可预览内置 H3 策略及本地 JSON 文件。选择维护令牌文件并确认发布后，页面将完整策略以 `application/json` 提交到策略版本接口，凭据通过 `Authorization: Bearer <token>` 传递。同一 `strategy_key` 的每次成功发布都会分配下一个版本号。[模板导入操作](../web/admin/README.md#导入模板)

| 方法与路径 | 输入 | 成功响应 |
| --- | --- | --- |
| `POST /local/v1/strategy-versions` | 完整策略对象。包含 `strategy_key, name, workflow, input_schema, bindings, requirements, progress_mapping, output_contract, scheduling, retry, artifacts`。 | `201 {strategy_version_id, strategy_key, version, content_digest}`。版本号按系列分配，发布内容不可变。 |
| `POST /local/v1/users/{user_id}/password-reset` | `{new_password}` | `204`。同事务更新密码、撤销当前会话和记录审计。 |

停止接单、恢复接单和解除隔离由运维命令处理。运行管理按实例 ID 和代次核对目标，解除隔离时记录处置依据。[维护路由](../src/mycomfy/api/app.py#L327) · [策略校验](../src/mycomfy/generation/strategy.py#L108) · [运维命令](operations.md)

## 页面设计涉及的接口限制

| 设计项 | 当前接口与联调要求 |
| --- | --- |
| 执行进度 | 任务详情的 `progress` 当前固定为 `null`，SSE 提供持久业务事件。页面可展示状态和阶段，活跃任务的详情会轮询刷新。节点百分比需要公开的可靠采样契约。 |
| 前台历史入口 | 当前提供任务列表、按 ID 查询批次和按 ID 查询素材。独立批次历史、素材库或全局作品页需要相应列表契约。现有接口支持从任务进入所属批次和可见产物。 |

[策略响应](../src/mycomfy/generation/strategy.py#L274) · [任务详情](../src/mycomfy/generation/jobs.py#L246) · [用户可用路由](../src/mycomfy/api/app.py#L364) · [后端验收记录](cloud-acceptance.md)
