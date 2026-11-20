# MyComfy

本地视频生成平台。用户可在网页工作台注册、上传素材、选择策略版本并提交批次，维护人员可在本机监控页追踪运行状态。Worker 在 MySQL 登记执行意图后向本机 ComfyUI 提交一次工作流。只有文件通过契约校验、发布到正式位置并完成登记，用户才会看到成功状态。

## 目录与阅读顺序

```text
MyComfy/
├── src/mycomfy/                  Python 服务与命令入口
│   ├── api/                      HTTP 应用、认证与 SSE
│   ├── generation/               批次、策略、任务状态与 Worker
│   ├── engine/                   ComfyUI 部署、进程管理与协议适配
│   ├── artifacts/                文件读写、媒体校验与发布
│   ├── database/                 数据库会话、模型与迁移入口
│   └── ops/                      容量、监控与备份
├── web/
│   ├── client/                   C 端网页
│   └── admin/                    本机管理网页
├── migrations/                   Alembic 数据库版本
├── scripts/                      测试与验收命令
├── tests/{unit,integration,e2e}/  单元、集成和端到端测试
└── docs/                         部署、运维与验收
```

两个网页使用原生 ES 模块和本地静态资源，随 Python 服务安装。业务监听器的根路径提供用户工作台，本机监听器的根路径提供只读管理后台。[C 端开发入口](web/client/README.md) · [管理后台开发入口](web/admin/README.md) · [前后台 API 契约](docs/web-api.md)

后端可以跟随一次视频请求读代码。

1. 用户登录并提交 `POST /api/v1/batches`。`app.py` 挂载路由和审计中间件，受理前校验页面会话及归属。[HTTP 入口](src/mycomfy/api/app.py) · [认证](src/mycomfy/api/auth.py)
2. `admit_batch` 核对幂等键、策略版本和输入素材，把批次、任务与初始事件一并写入 MySQL。[批次受理](src/mycomfy/generation/jobs.py) · [业务表](src/mycomfy/database/models.py)
3. Worker 领取空闲槽位，保存提交授权，再由 Adapter 向 ComfyUI 发送工作流。回执丢失时从这里跟踪原执行和实例代次。[调度与核对](src/mycomfy/generation/worker.py) · [引擎协议](src/mycomfy/engine/adapter.py)
4. 引擎完成后，`register_outputs` 登记发布意图，发布器校验和归档文件。当所有必需产物可读时，事务写入任务成功状态。[发布与恢复](src/mycomfy/artifacts/publication.py)
5. 用户通过 `GET /api/v1/assets/{id}/content` 读取完整或指定范围的视频字节。重新连接时，页面从上次游标补取事件，离线期间产生的终态通知可从通知列表读取。[文件读取](src/mycomfy/artifacts/storage.py) · [事件与通知](src/mycomfy/api/events.py)

## 云端开发

使用 Python 3.12、uv、FFmpeg 与 ffprobe。Linux 上的进程管理使用文件锁和进程树信息。完整集成测试还需要 Docker。

```bash
uv sync --locked
uv run --locked pytest -q tests/unit
uv run --locked python scripts/test_mysql.py
```

最后一条命令在回环端口启动临时 MySQL 8.4，为每条数据库集成用例创建独立库，完成后回收容器。所有云端测试可在 CPU 上运行。其中一条跨模块测试会故意丢弃引擎提交回执，重建 Worker，并核对两个可解码 MP4 的完整下载和 Range 内容。可单独运行这条用例。

```bash
uv run --locked python scripts/test_mysql.py -q tests/e2e/test_cloud_delivery.py
```

## 启动服务

准备 MySQL 8.4 业务库和具有迁移权限的账号，并设置以下环境变量。数据库 URL 中的特殊字符须按 URL 规则编码。维护凭据文件存放随机令牌，文件模式设为 `0600`。

| 环境变量 | 用途 |
| --- | --- |
| `MYCOMFY_DATABASE_URL` | SQLAlchemy 连接地址，形式为 `mysql+pymysql://USER:PASSWORD@HOST:3306/DATABASE` |
| `MYCOMFY_DATA_ROOT` | API 与 Worker 共用的正式文件和运行数据目录 |
| `MYCOMFY_PUBLIC_ORIGIN` | 用户网页与用户 API 的同源 HTTPS 地址 |
| `MYCOMFY_MAINTENANCE_TOKEN_FILE` | 本机维护 API 的令牌文件路径 |
| `MYCOMFY_REDIS_URL` | 可选。写请求会话预检使用的 Redis 连接串，支持 ACL 用户名、密码和 TLS |
| `MYCOMFY_SESSION_CACHE_TTL_SECONDS` | 预检缓存的最长保留时间，默认 60 秒，且不超过会话剩余有效期 |
| `MYCOMFY_ENGINE_CONFIG` | 自动部署生成的 `engines.json` 路径 |
| `MYCOMFY_CAPACITY_CONFIG` | 部署方实测后填写的卷水位、队列与用户配额配置 |

先执行数据库迁移，再分别运行 API、引擎管理和 Worker。后三条命令是独立的长驻进程。

```bash
uv run --locked mycomfy migrate
uv run --locked mycomfy api --port 8000 --local-port 8001
uv run --locked mycomfy runtime serve
uv run --locked mycomfy worker
```

用户 API 位于 `/api/v1`。公开 HTTPS 入口将网页、静态资源和用户 API 转发至业务端口 8000，SSE 连接需关闭代理缓冲并将空闲超时设为大于心跳周期。默认在服务所在机器打开 [本机监控](http://127.0.0.1:8001/) 可查看全局运行状态。该回环端口同时提供本机维护接口。维护端点接受 `Authorization: Bearer <token>`。登录后的写请求携带有效 Cookie、`Origin`、`X-Session-Generation` 和 `X-CSRF-Token`。

## 部署和验收

- 对照规格 A01–A07、R01–R09 查看已在云端验证的行为及对应测试。[云端验证记录](docs/cloud-acceptance.md)
- 在 GPU 目标机上按清单安装 ComfyUI、固定版本节点、Python 环境和模型，生成实例配置。[引擎部署](docs/engine-deployment.md)
- 停止接单、核对实例代次、查看运行指标或演练成套备份恢复。[运维与恢复](docs/operations.md)
- 在已部署工作流和目标硬件上验证真实视频、下载摘要、耗时与吞吐，记录人工画质及引擎能力结论。[目标机验收](docs/gpu-acceptance.md)
