# deploy.py 部署命令手册

在 Linux 或 WSL 2 的仓库根目录运行 `python3 scripts/deploy.py`。Windows 单机部署先启动 Docker Desktop，在 Windows 终端执行 `wsl -d Ubuntu-24.04`，进入 Ubuntu 后执行 `cd ~/MyComfy`。GPU 端需要容器能访问 NVIDIA 显卡。[部署环境要求](../README.md#启动服务)

## 单机常用命令

| 目的 | 在 WSL 中执行 |
| --- | --- |
| 首次安装或部署更新后的源码 | `python3 scripts/deploy.py --single-host` |
| 正常停机后，用已有镜像和容器恢复服务 | `python3 scripts/deploy.py --compose --profile database up -d --no-recreate --no-build --wait mysql redis core gateway engine worker` |
| 查看所有服务的状态，包括已经退出的迁移容器 | `python3 scripts/deploy.py --compose --profile database ps -a` |
| 关闭电脑或 WSL 前优雅停机 | `python3 scripts/deploy.py --stop` |

日常开机用第二条；它会跳过镜像构建和 GPU 安装器。更新版本时，先用 `git pull --ff-only origin main` 拉取源码，再部署。正在处理任务的机器可先执行 `--stop`，等排空完成。

## 全部命令行参数

下表每一项都接在 `python3 scripts/deploy.py` 后。五种模式 `--single-host`、`--db-only`、`--core-only`、`--gpu-only`、`--compose` 一次只能选一种。[参数和模式实现](../scripts/deploy.py)

| 参数 | 实际行为及使用条件 |
| --- | --- |
| `-h`、`--help` | 打印当前检出版本的帮助并退出。 |
| 无参数 | 按配置中的 `single_host` 决定是否先启动内置数据库。仓库模板为 `false`，此时要求 MySQL、Redis 已就绪，再部署业务端和 GPU 端。 |
| `--config ./deploy.prod.json` | 指定 JSON，可与部署、停机或 Compose 操作组合。若显式指定的文件不存在，脚本会提示并使用仓库模板。 |
| `--single-host` | 本次启用单机连接配置，并在本机部署数据库、业务端和 GPU 端。 |
| `--db-only` | 在数据机启动项目自带的 MySQL、Redis，等待两者健康。 |
| `--core-only` | 在业务机构建 `migrate`、`core`、`gateway`，执行迁移并启动 API、工作台和管理后台。先准备好数据库。 |
| `--gpu-only` | 在 GPU 机构建 `engine`、`worker`，安装或校验引擎后启动两者。要求 MySQL 已完成迁移。 |
| `--stop` 或 `--single-host --stop` | 按 Worker、引擎、业务端、项目自带数据库的顺序停止本机已有服务。 |
| `--gpu-only --stop` | 排空 Worker、停止引擎并核验代次退出记录。 |
| `--core-only --stop` | 先停止 `core` 并结束事件订阅，再排空并停止 `gateway`，工作台、管理后台和 API 一起下线。 |
| `--db-only --stop` | 停止本项目的 MySQL 和 Redis 容器。分机时最后停止数据机。 |
| `--compose <参数…>` | 用同一份部署配置运行 Docker Compose。后续参数全部传给 Compose；将 `--config` 等部署脚本参数写在它前面。 |

分机启动顺序为 **数据机 → 业务机 → GPU 机**。业务机与 GPU 机还需把同一共享盘挂载到相同绝对路径，并让部署用户可读写，以便交接上传素材和结果文件。显式指定角色时只对该角色执行部署；配置里的 `single_host` 仍决定数据库连接地址。[分机配置示例](../README.md#已有部署与分机部署)

### Compose 运维

```bash
# 自定义配置时，把 --config 放在 --compose 前
python3 scripts/deploy.py --config ./deploy.config.json --compose --profile database ps -a

# 配置里声明的服务、镜像，以及已有项目容器实际使用的镜像
python3 scripts/deploy.py --compose --profile database config --services
python3 scripts/deploy.py --compose --profile database config --images
python3 scripts/deploy.py --compose --profile database images

# 读取最近日志，或在运行中的容器里打开 shell；用 exit 返回宿主终端
python3 scripts/deploy.py --compose logs --tail 100 gateway core engine worker
python3 scripts/deploy.py --compose exec core sh
python3 scripts/deploy.py --compose exec engine sh

# 验证容器能枚举 GPU，再验证 MyComfy 管理的引擎实例确实就绪
python3 scripts/deploy.py --compose exec engine nvidia-smi -L
python3 scripts/deploy.py --compose exec engine python -m mycomfy.cli runtime ready

# 查看本机 Compose 支持的所有命令
python3 scripts/deploy.py --compose --help
```

`--profile database` 将可选的数据服务纳入本次 Compose 配置。单纯执行 `restart` 会重启原容器；更新 Python 代码或修改启动参数后，用对应角色的部署命令应用更改。

## 配置文件与落盘位置

默认查找顺序是仓库根目录的 `deploy.config.json`、已有的旧版部署（`.env` 或预制的 `deployment.json`）、仓库的 `deploy.config.example.json`。首次自定义时可用 `cp -n deploy.config.example.json deploy.config.json` 复制模板。自定义 JSON 中显式填写的相对目录，以该 JSON 所在目录为基准。下表使用仓库模板的默认路径。[配置解析实现](../scripts/deployment_config.py)

| 文件或目录 | 谁读取、保存什么 |
| --- | --- |
| `deploy.config.json`、`deploy.config.example.json` | 前者由部署者填写，后者是可直接部署的模板。`deploy.py` 将连接地址、端口等转换为 Compose 进程的环境变量，只将 `engine` 分组传给 GPU 安装器。 |
| `.env`、`.env.example` | 旧版部署的变量文件和模板。使用统一 JSON 时脚本向 Compose 传入 `--env-file /dev/null`，因此仓库的 `.env` 不参与这次变量解析。 |
| `compose.yaml` | Compose 的服务定义，包括镜像或构建目标、启动命令、网络、依赖、挂载、GPU 和健康检查。 |
| `Dockerfile`、`pyproject.toml`、`uv.lock` | 构建 MyComfy 应用镜像和镜像内的 Python 环境，`uv.lock` 固定应用依赖。 |
| `var/gateway/` | 业务机的网关状态目录，可用 `paths.gateway` 指定。`rules.json` 保存 Core 发布的入口规则，`status.json` 是 Nginx 的实际应用回执，`edge.sqlite3` 保存按分钟汇总的流量。规则在管理页修改。[流量与限流说明](api-traffic.md) |
| `var/config/maintenance-token` | 部署业务端时自动生成或复用，`core` 将其用于需要维护凭据的接口。 |
| `var/deployment/` | 在 GPU 容器内只读挂载为 `/deployment`。可放本地模型和节点来源。旧版部署还在这里提供预制的 `deployment.json` 与依赖锁。 |
| `var/data/engines/generated/` | 统一 JSON 安装器在此缓存固定版本源码，生成 `locks/<摘要>/requirements-gpu.lock` 和 `manifests/<摘要>/deployment.json`。这份锁固定 ComfyUI、节点和 PyTorch 的依赖。 |
| `var/data/engines/model-objects/`、`var/data/engines/releases/` | 前者按 SHA-256 缓存模型；后者按版本保存 ComfyUI 源码、自定义节点、独立的 `venv`，并用硬链接引用已校验的权重。 |
| `var/data/engines/engines.json` | 安装器生成的实例配置。常驻引擎管理器据此找到各实例的 Python、ComfyUI 入口、GPU 和端口。 |
| `var/data/engines/instances/gpu0/g*/` | 每个启动代次的运行目录。模型加载或生成失败时看 `engine.log`；进程身份和退出记录分别在 `identity.json`、`ended.json`。 |
| `var/stop/<项目名>.json` | 优雅停机期间记录待核验代次。退出凭据全部匹配后，部署脚本清除此记录。 |

已有 JSON 中的 `public.admin_test_origin` 会按通用管理来源读取，新配置使用 `public.admin_origin`。沿用旧版 `.env` 且需要管理域名时，将 `MYCOMFY_ADMIN_TEST_ORIGIN` 改为 `MYCOMFY_ADMIN_ORIGIN`。本机默认部署无需填写该项。

### 仓库模板的实际值

| 项目 | 默认配置 |
| --- | --- |
| 部署与重试 | `single_host: false`，`manual_retry_limit: 3`。设为 `0` 可关闭手动重试。 |
| MySQL | `127.0.0.1:3306`，库名 `mycomfy`，用户 `mycomfy`，模板密码 `123456`。 |
| Redis | `127.0.0.1:6379`，用户 `mycomfy_cache`，模板密码 `123456`。 |
| 工作台和管理后台 | 分别为 `http://127.0.0.1:8000/` 和 `http://127.0.0.1:8001/`。 |
| ComfyUI 与 PyTorch | ComfyUI `v0.33.0`，Torch `2.11.0+cu130`，torchvision `0.26.0+cu130`，torchaudio `2.11.0+cu130`。 |
| 模型与实例 | 配置 5 份 MiniMax H3 权重，`nodes` 为空。实例 `gpu0` 使用第 `0` 张 GPU，ComfyUI 在容器网络中监听 `8188`。 |
| 实例时间参数 | `observation_seconds: 120`，`reconcile_seconds: 120`，`timeout_seconds: 3600`。 |

实际端口以本机部署为准。例如 `ps -a` 显示 `127.0.0.1:13306->3306/tcp` 时，宿主机客户端使用 `13306`，单机 GPU 容器仍通过 Compose 网络连接 `mysql:3306`。[完整配置模板](../deploy.config.example.json)

## Docker 镜像和容器行为

完整单机部署有七项服务，通常可见六个常驻容器和一个成功退出的迁移容器。下表采用默认项目名 `mycomfy`。[Compose 服务定义](../compose.yaml) · [镜像构建定义](../Dockerfile)

| 服务 | 镜像 | 启动后的行为和观察方法 |
| --- | --- | --- |
| `mysql` | `mysql:8.4` | 官方镜像首次在空数据卷初始化业务库与账号，以后复用命名卷 `mysql-data`，挂载在 `/var/lib/mysql`。健康检查用业务账号执行 `SELECT 1`。 |
| `redis` | `redis:7-alpine` | 启动时配置 ACL 用户，供业务端缓存会话预检、跨进程统计用户请求和模型服务商并发。健康检查执行 `PING`。当前关闭了 RDB 快照和 AOF，重启后重新累计限流计数，按业务访问重建缓存。 |
| `migrate` | `mycomfy-migrate`（Compose 按项目命名） | 与 Worker 使用相同的 Dockerfile 构建目标，执行 `python -m mycomfy.cli migrate`。成功后显示 `Exited (0)`。 |
| `core` | `mycomfy-core:local` | 一个 Python 进程提供工作台、后台和 API，只读挂载 `web/client`、`web/admin`。通过宿主网络连接数据库，为网关提供私有套接字 `public.sock` 和 `admin.sock`。健康检查请求管理套接字。 |
| `gateway` | `mycomfy-gateway:local` | 在宿主网络运行 Nginx，占用原工作台和管理端口，经共享卷中的两个套接字分别转发。控制进程校验并热加载规则、汇总访问统计，健康检查读取实际运行修订。 |
| `engine` | `mycomfy-engine:local` | 申请 `gpus: all`，以 `runtime serve --stop-engines-on-exit` 启动管理器，再启动和监护 ComfyUI 子进程。健康检查执行 `runtime ready`；可在对应代次的 `engine.log` 查看采样和导出日志。 |
| `worker` | `mycomfy-worker:local` | 从 MySQL 领任务并更新状态，与 `engine` 共用网络空间，经 `127.0.0.1:8188` 调用默认实例。镜像根据调度循环的心跳报告健康状态，队列为空也可以是 `healthy`。 |

应用代码和管理进程的 Python 环境 `/opt/venv` 随本地镜像构建。运行模型的 Python 在 `var/data/engines/releases/<摘要>/venv`，权重保存在宿主机数据目录；重建引擎容器时可复用这些文件。

## 一次单机部署的执行顺序

1. **读取配置并准备挂载。** 脚本解析 Compose，确定端口、目录和部署用户的 UID/GID，准备维护令牌。单机模式下，宿主网络中的业务端使用回环地址和发布端口连接数据库；GPU 端使用 Compose 内网地址。
2. **启动数据服务。** Compose 执行 `up -d --wait mysql redis`，等 MySQL、Redis 都健康后才继续。
3. **构建应用镜像。** Compose 执行 `build migrate core gateway engine worker`，从当前 Git 检出目录读取 Dockerfile、源码和依赖锁，可复用已有构建缓存。
4. **安装引擎。** 脚本执行 `compose run --rm --no-deps -T`，用 `engine` 镜像启动一个临时容器，入口为 `mycomfy-deploy-engine`。统一 JSON 部署通过标准输入传入引擎配方。安装器准备源码和依赖锁、校验模型并验证 GPU，完成后删除临时容器，结果保存在宿主机挂载目录。
5. **迁移并启动常驻服务。** Compose 执行 `up -d --wait --no-build core gateway engine worker`。依赖要求 `migrate` 成功退出，然后启动常驻进程。引擎容器启动后 Worker 即可开始核对任务，脚本等服务就绪后打印工作台地址。

优雅停机时，脚本先让 Worker 停止领新任务并排空在途执行，再核对引擎代次的 `ended.json`，随后关闭业务端与本项目的数据服务。若核验失败，脚本会中止，保留后续服务供检查；确认提示“所选服务已停止”后，即可在 Windows 终端执行 `wsl --shutdown`。[引擎部署与恢复](engine-deployment.md)
