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

### 全栈 Docker Compose（Linux / Windows WSL 2）

在有 NVIDIA 显卡的 Docker 主机运行这一套 Compose。Linux 使用 NVIDIA 驱动和 NVIDIA Container Toolkit。Windows 使用支持 WSL 的 NVIDIA 驱动、WSL 2 后端与 Docker Desktop 4.34 或更新版本，在 **Settings → Resources → Network** 启用 **Enable host networking**，用于访问 core 的本机管理端口。使用 Docker Compose 2.30.0 或更新版本。Windows 下请在 WSL 发行版的 Linux 文件系统（如 `~/MyComfy`）中检出项目、保存数据并执行以下 Bash 命令。[Docker GPU 支持](https://docs.docker.com/desktop/features/gpu/) · [宿主网络](https://docs.docker.com/engine/network/drivers/host/#docker-desktop) · [Linux Toolkit 安装](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)

```bash
docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L

umask 077
mkdir -p var/config var/data var/deployment
test -s var/config/maintenance-token || openssl rand -hex 32 > var/config/maintenance-token
# 新项目使用以下配置；已有 .env 时将所缺变量补入原文件并保留原密码。
test -e .env || cat > .env <<EOF
MYSQL_PASSWORD=$(openssl rand -hex 32)
REDIS_PASSWORD=$(openssl rand -hex 32)
MYCOMFY_UID=$(id -u)
MYCOMFY_GID=$(id -g)
MYCOMFY_DATA_ROOT="$PWD/var/data"
EOF
```

把目标工作流的 `deployment.json`、带哈希的 GPU 依赖锁及清单引用的本地模型放入 `var/deployment/`。首次启动前在 engine 容器内安装 ComfyUI、节点及 CUDA 版 PyTorch。安装成功会在数据目录的 `engines/` 下生成 `engines.json`，并保留模型和 Python 环境，供后续重启复用。[部署清单示例](docs/engine-deployment.md#安装清单)

```bash
# 在容器中按配置的绝对路径写入部署结果。
docker compose run --rm --no-deps --build --entrypoint sh engine -ec \
  'exec python -m mycomfy.cli deploy /deployment/deployment.json --root "$MYCOMFY_DATA_ROOT/engines"'
docker compose up -d --build --wait
docker compose ps
```

| 服务 | 容器职责 |
| --- | --- |
| `mysql`、`redis`、`migrate` | 保存业务状态、缓存会话预检、在服务启动前迁移数据库。 |
| `core` | 托管 API 和只读挂载的 front、manage 网页。默认工作台为 `http://127.0.0.1:8000/`，管理入口为 `http://127.0.0.1:8001/`。公网 HTTPS 代理需配套设置 `MYCOMFY_PUBLIC_ORIGIN` 和 `MYCOMFY_COOKIE_SECURE=true`。 |
| `engine` | 唯一请求 GPU 的服务，执行 `runtime serve` 并启动、监护 ComfyUI 子进程。实例端口仅监听该容器网络命名空间内的回环地址。 |
| `worker` | 通过 MySQL 领任务，使用 `network_mode: service:engine` 连接回环引擎。与 engine、core 以相同绝对路径挂载数据目录，交接输入与产物。 |

日常启动用 `docker compose up -d --wait`。在计划停机或更换锁文件前，先排空任务，再停止服务。单独启动业务端可用 `docker compose up -d --build --wait core`。[Dockerfile](Dockerfile) · [compose.yaml](compose.yaml) · [引擎运维](docs/engine-deployment.md#容器运行与验收)

若业务端与 GPU 分机，engine 和 worker 仍需在同一台 GPU 机。业务机先启动 core 和数据库并完成迁移。两端以相同绝对路径挂载支持文件锁和原子重命名的同一份数据，并核对共享卷的设备编号及容量预留。GPU 机使用自己的本地 `.env`，将 `MYCOMFY_ENGINE_DATABASE_URL` 指向容器网络可达的同一业务库，按上述步骤部署引擎，再运行 `docker compose up -d --no-deps --build --wait engine worker`。默认 MySQL 发布在业务机的回环地址，跨机数据库连接需配置私网通道。[容量核算](src/mycomfy/ops/capacity.py#L91)

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
