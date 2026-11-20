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
├── scripts/                      一键部署、测试与验收命令
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

### 全栈 Docker Compose（Linux / Windows WSL 2）

**部署只需维护一份 `deploy.config.json`，入口是 `python3 scripts/deploy.py`。** 在 Linux 或 WSL 终端运行，需要 Python 3 和 Docker Compose 2.30.0 或更新版本。Windows 下建议在 WSL 的 Linux 文件系统（如 `~/MyComfy`）中检出项目，使用 Docker Desktop 4.34 或更新版本的 WSL 2 后端，并启用 **Settings → Resources → Network → Enable host networking**。全栈部署还需要 Docker 能访问 NVIDIA GPU。Linux 使用 NVIDIA 驱动和 NVIDIA Container Toolkit，Windows 使用支持 WSL 的 NVIDIA 驱动。[Docker GPU 支持](https://docs.docker.com/desktop/features/gpu/) · [宿主网络](https://docs.docker.com/engine/network/drivers/host/#docker-desktop)

**首次填写配置**。在项目根目录复制模板，并用编辑器打开生成的 `deploy.config.json`。该文件保存本机密码，默认由 Git 忽略。

```bash
umask 077
cp -n deploy.config.example.json deploy.config.json
```

| JSON 字段 | 填写内容 |
| --- | --- |
| `mysql.user`、`mysql.password` | MySQL 应用账号和密码。空数据卷会创建 `mycomfy` 库并初始化所填账号。首次初始化请使用单行凭据，避开单引号与反斜杠。复用已有数据卷时填写已有凭据，轮换密码需同步修改库内账号。 |
| `redis.user`、`redis.password` | Redis 缓存账号和密码。两个服务的密码均填原值，由脚本编码连接串。 |
| `engine.comfyui` | ComfyUI 仓库地址与发布标签或 40 位 Git 提交号。 |
| `engine.pytorch` | CUDA 轮子索引，以及目标环境需要的 torch、torchvision、torchaudio 精确版本。模板示例为 Linux x86_64、Python 3.12、CUDA 12.6，请按显卡驱动和工作流选取匹配组合。 |
| `engine.nodes`、`engine.models` | 填写工作流使用的自定义节点及模型。模板中的两项为空。生成实际视频前需补齐工作流依赖，并发布相应策略。 |
| `engine.instances`、`paths`、`ports` | 模板使用一张 GPU、本地数据目录和默认端口。按需调整 GPU 编号、目录或端口。`paths` 中填写的相对路径以本配置文件所在目录为基准。 |

**启动**。在 GPU 机运行下面的一条命令。脚本读取 JSON，在引擎容器内检出源码，解析并锁定指定版本的依赖，准备模型并检测 CUDA，随后运行数据库迁移并启动服务。锁文件和安装清单由脚本生成。用 `--config /路径/部署配置.json` 可以指定另一份配置。[配置模板](deploy.config.example.json) · [部署脚本](scripts/deploy.py) · [节点与模型示例](docs/engine-deployment.md#配置工作流依赖)

```bash
python3 scripts/deploy.py
```

**先体验网页**时，只填写 MySQL 和 Redis 的账号密码，运行 `python3 scripts/deploy.py --core-only`。默认工作台为 `http://127.0.0.1:8000/`，本机监控为 `http://127.0.0.1:8001/`。准备好 GPU 与工作流依赖后，用同一份配置执行全栈命令。

| 服务 | 容器职责 |
| --- | --- |
| `mysql`、`redis`、`migrate` | 保存业务状态、缓存会话预检、在服务启动前迁移数据库。 |
| `core` | 托管 API、工作台与只读管理网页。公网 HTTPS 代理可通过 JSON 的 `public.origin` 设置外部来源，并设置 `public.cookie_secure` 为 `true`。 |
| `engine` | 唯一请求 GPU 的服务，执行 `runtime serve` 并启动、监护 ComfyUI 子进程。实例端口仅监听该容器网络命名空间内的回环地址。 |
| `worker` | 通过 MySQL 领任务，使用 `network_mode: service:engine` 连接回环引擎。与 engine、core 以相同绝对路径挂载数据目录，交接输入与产物。 |

日常部署可重复使用同一份配置。运行 `python3 scripts/deploy.py --compose ps` 查看容器状态，运行 `python3 scripts/deploy.py --compose logs --tail 100 core` 查看业务服务日志。脚本把 JSON 中的连接参数传给 Compose，UID 和 GID 取当前用户。在计划停机或更换引擎配置前，先排空在途任务。[Dockerfile](Dockerfile) · [compose.yaml](compose.yaml) · [引擎运维](docs/engine-deployment.md#容器运行与验收)

### 已有部署与分机部署

已有 `.env`、`deployment.json` 和 GPU 依赖锁的部署可沿用原流程。在仓库根目录没有 `deploy.config.json` 时，脚本读取旧配置。切换到 JSON 时，请填入原账号、密码、端口和持久目录，再运行同一部署命令。[旧版环境变量模板](.env.example) · [旧版安装清单](docs/engine-deployment.md#安装清单)

若业务端与 GPU 分机，engine 和 worker 仍需在同一台 GPU 机。业务机先启动 core 和数据库并完成迁移。两端以相同绝对路径挂载支持文件锁和原子重命名的同一份数据，并核对共享卷的设备编号及容量预留。GPU 机使用自己的本地 `.env`，将 `MYCOMFY_ENGINE_DATABASE_URL` 指向容器网络可达的同一业务库，运行 `docker compose run --rm --no-deps --build --entrypoint mycomfy-deploy-engine engine` 部署引擎，再运行 `docker compose up -d --no-deps --build --wait engine worker`。默认 MySQL 发布在业务机的回环地址，跨机数据库连接需配置私网通道。[容量核算](src/mycomfy/ops/capacity.py#L91)

### 从 Python 环境启动

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
