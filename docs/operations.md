# 单机运行、容量与数据恢复

一键部署由 Nginx 接收工作台和管理后台的流量，按各接口的 IP 规则放行后，经两个私有 Unix 套接字分别转交 Core。在管理后台“API 流量”可查看入口状态和两层限流。[流量口径与配置说明](api-traffic.md)

直接运行 `mycomfy api` 时，同一业务 API 进程同时监听用户端口与本机回环维护端口，两套生命周期分别启动事件分发和独立于监控页面的告警轮询。维护调用使用权限受限的令牌文件。通过进程托管器分别管理双监听 API、引擎管理和 Worker。仅诊断本机监听时可使用 `mycomfy local-api`。数据库迁移在启动服务前执行。

## 会话预检缓存

为 API 进程设置 `MYCOMFY_REDIS_URL` 即可启用。连接串包含地址、端口、数据库编号和可选的 ACL 用户名、密码。TLS 示例为 `rediss://USER:PASSWORD@REDIS_HOST:6379/0`。用户名和密码中的特殊字符需要 URL 编码，真实凭据通过部署环境变量提供。

Redis 用于批次提交、任务取消和用户改密的中间件预检。缓存未命中或 Redis 访问失败时，预检回查 MySQL。只有通过数据库校验的会话才会写入 Redis，缓存最多保存 `MYCOMFY_SESSION_CACHE_TTL_SECONDS` 秒，默认 60 秒，且不超过会话剩余有效期。

写事务再次以 MySQL 为准。登录换代、退出、改密和维护重置密码在提交事务后尽力清理旧缓存。如果删除失败，后续命中旧缓存的写请求会在 MySQL 确认会话失效后重试删除。页签代次或 CSRF 请求头不匹配时保留对应的会话缓存。对私有数据的读取仍在 MySQL 校验会话。

## 用户分组

迁移 `0004` 创建 `standard` 标准组，并把现有用户加入该组，保留升级前的功能访问。注册的用户也默认加入标准组。一个用户可属于多个组，权限按并集合并。分组列表为空时，用户仍可登录、查看自己的会话及改密，业务接口按权限拒绝。

在后台“用户管理”页可查看全部账号、设置所属分组。直连和公网 Admin 链接适用相同的规则。维护程序也可通过任一已启用的 Admin 入口使用 Bearer 凭据调用下表接口，其中创建和修改分组定义需要该凭据。[用户管理页](../web/admin/README.md#用户管理)

| 接口 | 用途 |
| --- | --- |
| `GET /local/v1/groups` | 读取分组、各组权限和可选权限清单 |
| `POST /local/v1/groups` | 创建分组，传 `key`、`name`、`permissions` |
| `PUT /local/v1/groups/{group_key}` | 更新组名和权限，传 `name`、`permissions` |
| `GET /local/v1/users` | 分页查看账户 ID、用户名、所属分组与生效权限 |
| `GET /local/v1/users/{user_id}/groups` | 读取用户当前的组和生效权限 |
| `PUT /local/v1/users/{user_id}/groups` | 传入 `group_keys`，替换该用户所属分组 |

例如，创建 `viewer` 分组并授予 `jobs.view`，然后把用户的 `group_keys` 设置为 `["viewer"]`。该用户通过 `GET /api/v1/me` 获取新权限后能看到任务页面，提交或取消会收到 `403 PERMISSION_DENIED`。组权限保存在 MySQL，修改后即可影响现有会话的业务请求。C 端分组权限与本机维护凭据分别校验。[前端权限接入](../web/client/README.md)

## 监控与停止接单

用 `docker ps --filter name=mycomfy-worker` 可以查看 Worker 容器的健康状态。启动阶段为 `starting`，调度循环正常推进后为 `healthy`。探针每 10 秒读取本容器协调器的心跳，持续数据库错误或超过当前处理阶段的期限会触发失败。连续三次检查失败后，Docker 显示 `unhealthy`。恢复轮询后会重新显示 `healthy`。GPU 是否就绪由 `engine` 的探针另外检查。

启动本机 API 后，访问 `GET /local/v1/monitor/overview` 查看排队数、最早等待时间、重试、隔离、发布积压、卷水位和告警。集合 `requests`、`batches`、`jobs`、`executions`、`instances`、`metrics` 和 `files` 支持列表及按 ID 查看详情。按执行筛选 `/local/v1/monitor/logs?execution_id=...` 可翻页读取引擎日志。执行详情保留实例代次、节点、预算和历史日志位置。采样过期时查看实例心跳与进程存活、HTTP 可达、可接单和实际占用各项。

本机服务每 10 秒检查隔离、待核对执行、发布故障、受损文件、低磁盘余量及事件积压。告警激活与解除写入 `mycomfy.alerts` 日志，进程日志通过系统日志收集器路由到部署指定的接收人。事件积压告警秒数由 `MYCOMFY_EVENT_ALERT_SECONDS` 设置。引擎采样和日志轮转由运行管理处理，关闭监控页面后仍持续运行。

计划维护前，从监控读取实例 ID 和当前代次，执行：

```bash
uv run mycomfy runtime drain --instance "$INSTANCE_ID" --generation "$GENERATION"
```

等待执行提交回执、任务发布及进程树状态达到可核对边界。发现超时或无法确认旧进程退出时，保留工作目录、占用、代次、进程身份和日志。查明提交路径、确认旧进程及其子进程已退出并核对输出后，使用 `runtime resolve --instance ... --generation ... --reason ...` 记录解除隔离依据。计划恢复领取使用 `runtime resume` 并传入当前实例及代次。正常取消由用户任务接口提交。

## 容量设置

`MYCOMFY_CAPACITY_CONFIG` 指向部署的 JSON 文件。各项容量单位为字节，卷路径须指向已挂载的实际文件系统。下例数值仅展示配置结构，目标机需按实测峰值填写。数据库卷路径选择在业务主机上可见、与数据库文件位于同一文件系统的目录。实例工作卷使用实际工作路径测量。API 进程的临时目录应与 spool 对应。

```json
{
  "user_upload_bytes": 10737418240,
  "user_jobs": 100,
  "global_jobs": 1000,
  "execution_bytes": 2147483648,
  "publish_bytes": 536870912,
  "strategies": {"video": {"execution_bytes": 3221225472, "publish_bytes": 1073741824}},
  "volumes": {
    "storage": {"path": "/srv/mycomfy/data/objects", "stop_bytes": 1073741824, "resume_bytes": 2147483648, "reserve_bytes": 268435456},
    "spool": {"path": "/tmp", "stop_bytes": 536870912, "resume_bytes": 1073741824, "reserve_bytes": 134217728},
    "database": {"path": "/srv/mysql", "stop_bytes": 1073741824, "resume_bytes": 2147483648, "reserve_bytes": 268435456},
    "engine": {"path": "/srv/mycomfy/engine", "stop_bytes": 2147483648, "resume_bytes": 3221225472, "reserve_bytes": 268435456}
  }
}
```

同设备的预留相加，停止与恢复阈值均额外保留 `reserve_bytes`。领取时按素材大小、工作峰值和归档峰值预留空间。表单解包、素材登记、引擎输出封存及单件归档分别核销对应预留。输出封存后，按已知源文件大小重新估算目标盘待写入量。水位低于停止线时暂停受影响操作，升至恢复线后再接受新任务。超出用户配额返回 429，全局容量不足返回 503。策略及模型一致且实例暂时离线的批次仍可受理。历史任务、已保存产物和独立上传素材长期保留。

## 停写备份

1. 暂停对外写入口和本机维护写入，停止实例接单并等候在途操作到达持久边界。停止 API、Worker、引擎管理和相关文件写入者，使 MySQL 业务行与文件位于同一静止恢复点。
2. 准备可容纳本组数据的独立备份介质。选择正式对象、暂存、受保护引擎源、外置工作流和部署清单所在的真实目录。参数使用互不嵌套的根，目录下的符号链接需替换为真实源目录的选择。受限凭据恢复步骤保存在同一组的受限部署说明中。服务凭据、登录凭据摘要和会话等敏感信息随备份权限管理。
3. 在应用环境中设置源库的 `MYCOMFY_DATABASE_URL` 后，创建并核验本组备份。示例使用本地路径占位。

```bash
uv run python -m mycomfy.ops.backup create /backup/2026-11-18 \
  --root data=/srv/mycomfy/data --root work=/srv/mycomfy/engine \
  --root manifests=/srv/mycomfy/config
uv run python -m mycomfy.ops.backup verify /backup/2026-11-18
```

备份独立保存迁移版本、每表行数、恢复点、各文件的路径、字节数和 SHA-256。备份成功及完整核验后再恢复对外服务。本地文件系统上先同步文件和目录项，再原子发布清单并同步备份父目录。恢复时在提交数据库前先同步复制后的文件树。断电验收还取决于备份介质和磁盘的写缓存行为、MySQL 持久化设置和停写点是否成立，需在部署上演练。备份频率、保留代数、备份介质访问权和恢复目标随实际部署登记。

## 隔离恢复

1. 在隔离目录和空 MySQL 库恢复。切换 `MYCOMFY_DATABASE_URL` 到目标库，使用与备份匹配的程序版本执行迁移。
2. 执行恢复命令，它先核对全部摘要，再恢复目录、表关系、作业原有状态和发布意图。恢复后的实例暂停接单。历史进程身份、启动清单和日志仅供核对。隔离副本丢弃运行中 PID，禁用历史引擎地址，中断能力置为关闭，为有占用的实例创建独立控制目录并隔离。运维核实原代次后才解除隔离。

```bash
uv run mycomfy migrate
uv run python -m mycomfy.ops.backup restore /backup/2026-11-18 /srv/restore/trial
uv run python -m mycomfy.ops.backup reconcile-files /srv/restore/trial/data
```

3. 在恢复目录验证策略工作流、环境清单、文件归属、取消后可见性及恢复的发布记录。核对旧进程树和槽位后再启动对应代次。将应用的文件根和引擎清单重定位到恢复环境，并重新设置容量配置及凭据权限。
4. 报告记录恢复点、迁移版本、备份和恢复耗时、受检文件、不可用对象、未结束发布和遗留问题。R09 云端集成测试在两个独立 MySQL 库间执行，并实际从备份的源视频恢复一次发布。
