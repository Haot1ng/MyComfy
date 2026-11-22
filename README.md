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

两个网页使用原生 ES 模块和本地静态资源，随 Python 服务安装。业务监听器的根路径提供用户工作台，本机监听器的根路径提供只读监控，管理后台的“工作流模板”可新增模板、预览节点并发布策略。[C 端开发入口](web/client/README.md) · [管理后台开发入口](web/admin/README.md) · [前后台 API 契约](docs/web-api.md)

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

**在同一台 GPU 机器上部署**，需要 Python 3、Docker Compose 2.30.0 或更新版本，以及可在容器中使用的 NVIDIA GPU。Linux 使用 NVIDIA 驱动和 NVIDIA Container Toolkit。Windows 下建议在 WSL 的 Linux 文件系统（如 `~/MyComfy`）中检出项目，使用支持 WSL 的 NVIDIA 驱动和 Docker Desktop 4.34 或更新版本的 WSL 2 后端，并启用 **Settings → Resources → Network → Enable host networking**。[Docker GPU 支持](https://docs.docker.com/desktop/features/gpu/) · [宿主网络](https://docs.docker.com/engine/network/drivers/host/#docker-desktop)

**首次部署**可在新检出的仓库根目录直接运行以下命令。脚本读取仓库自带的 `deploy.config.example.json`，MySQL 和 Redis 的默认密码都是字符串 `123456`，数据库和网页端口仅绑定本机 `127.0.0.1`。模板预置 ComfyUI v0.33.0 和 MiniMax H3 文生视频、图生视频共用的五个权重文件。首次部署 GPU 端会下载约 42 GB 模型，镜像、Python 环境和生成的视频还需额外磁盘空间。

```bash
python3 scripts/deploy.py --single-host
```

这条命令先启动 MySQL 和 Redis 并等待健康检查通过，再构建应用镜像、在容器里安装 ComfyUI、执行数据库迁移，最后启动 API、引擎和 Worker。业务端通过回环地址和配置的端口连接数据服务，GPU 容器在 Compose 网络内连接 `mysql:3306`。部署完成后，在本机管理后台 `http://127.0.0.1:8001/` 进入“工作流模板”，直接发布内置的 H3 策略，或从自己的 ComfyUI API 工作流新增模板，然后在工作台 `http://127.0.0.1:8000/` 创建批次。[新增与导入步骤](web/admin/README.md#新增模板) · [配置模板](deploy.config.example.json) · [节点与模型示例](docs/engine-deployment.md#配置工作流依赖)

要把这两个页面临时分享给外网测试人员，在一个 WSL 窗口启动 MyComfy，再在一个 Windows PowerShell 窗口运行 `scripts/share-test.ps1`。脚本使用 Tailscale Funnel 同时打开两个 HTTPS 链接，按 Ctrl+C 可一起关闭。工作台使用原有账号，后台链接允许任何访客查看全局监控并发布模板。[两个窗口的启动命令](docs/https-tunnel-test.md)

**需要自定义密码、模型或端口时**，在首次部署前复制模板，再编辑仓库根目录的 `deploy.config.json`。部署脚本优先使用已有的这份配置，Git 默认忽略该文件。

```bash
umask 077
cp -n deploy.config.example.json deploy.config.json
```

| JSON 字段 | 填写内容 |
| --- | --- |
| `single_host` | 顶层布尔值。设置为 `true` 后，直接运行 `python3 scripts/deploy.py` 即可在这台 GPU 主机的 Docker 中部署全部服务。省略或设为 `false` 时，无参部署连接已就绪的数据服务。 |
| `mysql.host`、`redis.host` | 业务端连接的主机名或 IP。模板都填 `127.0.0.1`，端口分别取 `ports.mysql`（默认 3306）和 `ports.redis`（默认 6379）。 |
| `public.host`、`public.origin` | `public.host` 是业务机监听 API 的本机地址，端口取 `ports.api`。`public.origin` 填用户浏览器实际访问工作台的来源（协议、主机、端口），供登录和写请求校验。 |
| `public.admin_test_origin` | 可选，默认关闭。设置后，指定 HTTPS 来源可经保留原始 Host 的本机代理免登录查看监控、创建和发布模板。工作台和管理后台需使用两个不同的 HTTPS 来源。临时测试可用 `--test-tunnel` 自动配置。 |
| `mysql.bind_host`、`redis.bind_host` | 可选。运行随附数据库容器时，端口发布到数据库服务器的哪个本机 IP，默认都是 `127.0.0.1`。分机示例见下文。 |
| `mysql.user`、`mysql.password` | MySQL 应用账号和密码。随附容器在空数据卷上创建 `mycomfy` 库并初始化所填账号，初始化凭据请避开单引号和反斜杠。复用已有数据卷时填写已有凭据。 |
| `redis.user`、`redis.password` | Redis 缓存账号和密码。两个服务的密码均填原值，由脚本编码连接串。 |
| `engine.comfyui` | ComfyUI 仓库地址与发布标签或 40 位 Git 提交号。 |
| `engine.pytorch` | 模板在 Linux x86_64、Python 3.12 下使用 PyTorch `2.11.0+cu130`，目标机需要支持 CUDA 13.0 的 NVIDIA 驱动。 |
| `engine.nodes`、`engine.models` | 默认 H3 工作流使用 ComfyUI 内置节点。五个模型文件均固定下载地址和 SHA-256。要让用户在工作台选择文生或图生视频，还需发布相应的业务策略。 |
| `engine.instances`、`paths`、`ports` | 模板使用一张 GPU、本地数据目录和默认端口。按需调整 GPU 编号、目录或端口。`paths` 中填写的相对路径以本配置文件所在目录为基准。 |

**已有独立 MySQL 和 Redis** 时，保留模板的 `"single_host": false`，在 JSON 中填写已有服务的地址与凭据，并运行 `python3 scripts/deploy.py`。先在 MySQL 准备好 `mycomfy` 库并授予应用账号迁移和读写权限。[部署脚本](scripts/deploy.py)

**先体验网页**时，可直接使用模板的本机数据库参数。先运行 `python3 scripts/deploy.py --db-only`。数据库就绪后运行 `python3 scripts/deploy.py --core-only`，脚本会执行迁移并启动 `core`。显式指定 `--db-only`、`--core-only`、`--gpu-only` 或 `--compose` 时，只执行所选操作。配置中的 `"single_host": true` 仍决定这些服务连接本机数据库。

| 服务 | 容器职责 |
| --- | --- |
| `mysql`、`redis`、`migrate` | 保存业务状态、缓存会话预检、在服务启动前迁移数据库。 |
| `core` | 托管 API、工作台和本机管理后台。公网 HTTPS 代理可通过 JSON 的 `public.origin` 设置外部来源，并设置 `public.cookie_secure` 为 `true`。 |
| `engine` | 唯一请求 GPU 的服务，执行 `runtime serve` 并启动、监护 ComfyUI 子进程。实例端口仅监听该容器网络命名空间内的回环地址。 |
| `worker` | 通过 MySQL 领任务，使用 `network_mode: service:engine` 连接回环引擎。与 engine、core 以相同绝对路径挂载数据目录，交接输入与产物。 |

用 `--config /路径/部署配置.json` 可指定配置文件。运行 `python3 scripts/deploy.py --compose --profile database ps` 查看同一项目的数据库和应用状态。计划停机前先排空在途任务。只停业务端用 `python3 scripts/deploy.py --compose stop core`，GPU 端用 `python3 scripts/deploy.py --compose stop worker engine`，数据库可以继续服务。[Compose 配置](compose.yaml) · [引擎运维](docs/engine-deployment.md#容器运行与验收)

### 已有部署与分机部署

假设数据库机、业务机、GPU 机位于同一私网。MySQL 与 Redis 在 `10.20.0.10`，业务工作台在 `10.20.0.20:8000`，GPU 机是 `10.20.0.30`。三台主机可以使用相同的地址配置。下列字段合并到各自的 `deploy.config.json`，账号、密码和引擎配置沿用现场填写的值。

```json
{
  "mysql": {"host": "10.20.0.10", "bind_host": "10.20.0.10"},
  "redis": {"host": "10.20.0.10", "bind_host": "10.20.0.10"},
  "public": {"host": "10.20.0.20", "origin": "http://10.20.0.20:8000"},
  "ports": {"mysql": 3306, "redis": 6379, "api": 8000}
}
```

只有业务机的 `core` 会绑定 `public.host` 和 `ports.api`。GPU 机读取同一个 `public.origin`，部署完成时会显示打开工作台的地址。若用 HTTPS 反向代理，`public.origin` 填用户实际访问的 HTTPS 域名和对外端口，`ports.api` 仍填写代理转发到的本地 API 端口。

| 顺序 | 在哪里运行 | 配置和命令 |
| --- | --- | --- |
| 1 | 数据库机 `10.20.0.10` | 如使用随附容器，将 `mysql.bind_host` 和 `redis.bind_host` 都设为该机私网 IP `10.20.0.10`，运行 `python3 scripts/deploy.py --db-only`。已有独立数据库服务可跳过此步。 |
| 2 | 业务机 `10.20.0.20` | 将 `mysql.host` 和 `redis.host` 都设为 `10.20.0.10`，填写对应凭据，运行 `python3 scripts/deploy.py --core-only`。等待迁移和业务端启动成功。 |
| 3 | GPU 机 `10.20.0.30` | 填好业务工作台地址、数据库地址及本机 `engine` 参数，运行 `python3 scripts/deploy.py --gpu-only`。它安装引擎，仅启动 `engine` 和 `worker`。 |

业务机需要能访问 MySQL 和 Redis。GPU 机可以省略整个 `redis` 配置。Worker 直接连接 MySQL 领取任务和回写状态，任务执行无需访问业务机的 HTTP 接口。示例可以在私网防火墙允许业务机、GPU 机连接 `10.20.0.10:3306`，允许业务机连接 `10.20.0.10:6379`。MySQL 与 Redis 在不同主机时，分别填写各自的地址和端口。

业务机与 GPU 机还要把同一共享盘挂载到相同绝对路径，例如两边的 `paths.data` 都填写 `/srv/mycomfy/data`。两端容器使用部署用户的 UID/GID，需有共享读写权限，文件系统需支持文件锁和原子重命名。部署时核对两端共享卷的设备编号与容量预留。[容量核算](src/mycomfy/ops/capacity.py#L91)

默认情况下，`mysql.host` 填写私网地址时，GPU 容器也连接该地址及 `ports.mysql`。填写回环地址时，默认按项目自带的单机数据库处理，GPU 容器通过同一 Compose 网络连接 `mysql:3306`。如果 GPU 容器需要走另一条路（例如宿主机的独立数据库或私网隧道），填写从容器可达的 `mysql.engine_host` 和 `mysql.engine_port`。

#### 配置读取顺序

1. 显式传入 `--config 文件路径` 时，使用指定文件。如果文件不存在，脚本直接读取仓库的 `deploy.config.example.json`，其中的相对路径均以仓库根目录为基准。
2. 省略 `--config` 时，优先读取仓库根目录已有的 `deploy.config.json`。如果没有该文件，但存在旧版 `.env` 或部署目录已有 `deployment.json`，则沿用旧版部署流程。
3. 仓库中只有模板时，直接使用模板。已有旧版配置的机器也可显式运行 `python3 scripts/deploy.py --config deploy.config.example.json --single-host` 选用模板。沿用现有 MySQL 数据卷时，配置中的凭据须与库内账号匹配。[旧版环境变量模板](.env.example) · [旧版安装清单](docs/engine-deployment.md#安装清单)

### 从 Python 环境启动

准备 MySQL 8.4 业务库和具有迁移权限的账号，并设置以下环境变量。数据库 URL 中的特殊字符须按 URL 规则编码。使用命令行维护接口时，维护凭据文件存放随机令牌，文件模式设为 `0600`。

| 环境变量 | 用途 |
| --- | --- |
| `MYCOMFY_DATABASE_URL` | SQLAlchemy 连接地址，形式为 `mysql+pymysql://USER:PASSWORD@HOST:3306/DATABASE` |
| `MYCOMFY_DATA_ROOT` | API 与 Worker 共用的正式文件和运行数据目录 |
| `MYCOMFY_PUBLIC_ORIGIN` | 用户网页与用户 API 的同源 HTTPS 地址 |
| `MYCOMFY_MAINTENANCE_TOKEN_FILE` | 命令行策略发布、分组管理和密码重置等维护操作的令牌文件路径 |
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

用户 API 位于 `/api/v1`。公开 HTTPS 入口将网页、静态资源和用户 API 转发至业务端口 8000，SSE 连接需关闭代理缓冲并将空闲超时设为大于心跳周期。本机管理入口提供全局监控和模板发布。模板页在业务机上直接发布，命令行维护调用可以使用 `Authorization: Bearer <token>`。用户工作台登录后的写请求携带有效 Cookie、`Origin`、`X-Session-Generation` 和 `X-CSRF-Token`。[本机管理后台](http://127.0.0.1:8001/)

## 部署和验收

- 对照规格 A01–A07、R01–R09 查看已在云端验证的行为及对应测试。[云端验证记录](docs/cloud-acceptance.md)
- 在 GPU 目标机上按清单安装 ComfyUI、固定版本节点、Python 环境和模型，生成实例配置。[引擎部署](docs/engine-deployment.md)
- 停止接单、核对实例代次、查看运行指标或演练成套备份恢复。[运维与恢复](docs/operations.md)
- 在已部署工作流和目标硬件上验证真实视频、下载摘要、耗时与吞吐，记录人工画质及引擎能力结论。[目标机验收](docs/gpu-acceptance.md)
