# deploy.py 部署命令手册

在 Linux 或 WSL 2 的仓库根目录运行 `python3 scripts/deploy.py`。Windows 单机部署先启动 Docker Desktop，在 Windows 终端执行 `wsl -d Ubuntu-24.04`，进入 Ubuntu 后执行 `cd ~/MyComfy`。GPU 端需要容器能访问 NVIDIA 显卡。[部署环境要求](../README.md#启动服务)

## 单机常用命令

| 目的 | 在 WSL 中执行 |
| --- | --- |
| 首次安装或部署更新后的源码 | `python3 scripts/deploy.py --single-host` |
| 从配置的来源重新获取全部引擎权重并部署 | `python3 scripts/deploy.py --single-host --refresh-weights` |
| 使用本机已有镜像和已安装的引擎启动全套服务 | `python3 scripts/deploy.py --single-host --no-build` |
| 查看所有服务的状态，包括已经退出的迁移容器 | `python3 scripts/deploy.py --compose --profile database ps -a` |
| 临时分享已运行业务的工作台和后台 | `python3 scripts/deploy.py --test`，前台保留，按 Ctrl+C 恢复 |
| 关闭电脑或 WSL 前优雅停机 | `python3 scripts/deploy.py --stop` |

日常启动使用 `--single-host --no-build`。脚本让 Compose 跳过镜像构建和拉取，等 MySQL、Redis 健康，再按迁移依赖启动 Core、网关、引擎和 Worker。引擎直接读取上次安装生成的 `<数据目录>/engines/engines.json`，复用持久目录中的 ComfyUI 和模型。若本地镜像标签已更新，Compose 可重建对应容器。服务就绪后，脚本打印工作台和管理后台的访问地址。自定义配置示例为 `python3 scripts/deploy.py --config ./deploy.prod.yaml --single-host --no-build`。

完整部署默认复用 `<paths.data>/engines/model-objects/` 中摘要匹配的文件，首次缺失才下载。换用新摘要只需获取缺少的那份。已有文件校验失败会报错，如需从来源重新获取本次配置的权重，可使用 `--refresh-weights`。

更新运行中的单机部署时，先用当前检出版本运行 `python3 scripts/deploy.py --stop`，等在途任务排空并完成停机，再用 `git pull --ff-only origin main` 拉取源码，执行 `python3 scripts/deploy.py --single-host` 部署新版本。这样停机指令与现场镜像对应。首次用 YAML 接管已有部署时，先将现用数据库地址、账号、端口、目录和模型配置填入 `deploy.config.yaml`，再执行新版部署命令。

## 全部命令行参数

下表每一项都接在 `python3 scripts/deploy.py` 后。`--single-host`、`--db-only`、`--core-only`、`--gpu-only`、`--compose` 和 `--test` 一次只选一种模式。[参数和模式实现](../scripts/deploy.py)

| 参数 | 实际行为及使用条件 |
| --- | --- |
| `-h`、`--help` | 打印当前检出版本的帮助并退出。 |
| 无参数 | 按配置中的 `single_host` 决定是否先启动内置数据库。仓库模板为 `false`，此时要求 MySQL、Redis 已就绪，再部署业务端和 GPU 端。 |
| `--config ./deploy.prod.yaml` | 指定 `.yaml` 或 `.yml` 业务配置，可用于部署、停机、Compose 操作和临时测试。显式指定的 YAML 缺失时，常规模式提示后使用仓库模板，`--test` 模式报错并退出。 |
| `--test` | 临时为已运行的 Core 和网关切换浏览器来源，开启两条 Funnel，打印工作台与后台链接。命令在前台运行，Ctrl+C 后关闭本次转发并恢复原业务入口。 |
| `--test-config ./share.qa.yaml` | 与 `--test` 一起指定设备和外部 HTTPS 端口。默认读取仓库的 `deploy.test.yaml`。首次无此文件时采用示例，两条转发注册成功后将实际设备名和端口保存到本机 `deploy.test.yaml`。 |
| `--single-host` | 本次启用单机连接配置，并在本机部署数据库、业务端和 GPU 端。 |
| `--no-build` | 使用所选配置对应的本地镜像启动，可搭配 `--single-host` 或按角色启动。启动 GPU 端时需要已安装的引擎。单机首次安装或更新引擎配方请运行 `--single-host`。 |
| `--refresh-weights` | 在本次引擎部署中从配置的来源重新获取全部权重，下载完成后校验 SHA-256。本地来源从磁盘读取。可用于完整部署或 `--gpu-only`，与 `--no-build`、停机、测试和纯业务或数据库部署互斥。 |
| `--db-only` | 在数据机启动项目自带的 MySQL、Redis，等待两者健康。 |
| `--core-only` | 在业务机构建 `migrate`、`core`、`gateway`，执行迁移并启动 API、工作台和管理后台。先准备好数据库。 |
| `--gpu-only` | 在 GPU 机构建 `engine`、`worker`，安装或校验引擎后启动两者。要求 MySQL 已完成迁移。 |
| `--stop` 或 `--single-host --stop` | 按 Worker、引擎、业务端、项目自带数据库的顺序停止本机已有服务。 |
| `--gpu-only --stop` | 排空 Worker、停止引擎并核验代次退出记录。 |
| `--core-only --stop` | 先停止 `core` 并结束事件订阅，再排空并停止 `gateway`，工作台、管理后台和 API 一起下线。 |
| `--db-only --stop` | 停止本项目的 MySQL 和 Redis 容器。分机时最后停止数据机。 |
| `--compose <参数…>` | 用同一份部署配置运行 Docker Compose。后续参数全部传给 Compose；将 `--config` 等部署脚本参数写在它前面。 |

分机启动顺序为 **数据机 → 业务机 → GPU 机**。业务机与 GPU 机还需把同一共享盘挂载到相同绝对路径，并让部署用户可读写，以便交接上传素材和结果文件。显式指定角色时只对该角色执行部署；配置里的 `single_host` 仍决定数据库连接地址。[分机配置示例](../README.md#已有部署与分机部署)

仅显式使用 `--test` 会调用 Tailscale。测试使用当前部署的业务数据，管理链接具有免登录管理权限。两类配置文件的用途、首次准备和同一终端的启停步骤见[临时外网测试](https-tunnel-test.md)。

### Compose 运维

```bash
# 自定义配置时，把 --config 放在 --compose 前
python3 scripts/deploy.py --config ./deploy.config.yaml --compose --profile database ps -a

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

省略 `--config` 时，脚本从仓库根目录依次查找 `deploy.config.yaml`、`deploy.config.yml`，找到第一份即采用。两者都缺失时使用 `deploy.config.example.yaml`。[配置选择实现](../scripts/deploy.py)

首次自定义可运行 `cp -n deploy.config.example.yaml deploy.config.yaml`，在副本里按中文注释改值。用空格缩进，把字段写在所属分组下。同一分组只写一次。密码和 GPU 编号用字符串，例如 `password: "123456"`、`gpu: "0"`。端口和重试次数写整数，例如 `api: 8000`。开关用 `true`、`false`，未配置的管理来源可写 `admin_origin: null`。注册模型和节点时，沿用模板的映射结构。解析 YAML 所需的库已随仓库提供。[带注释的完整模板](../deploy.config.example.yaml)

改完配置后，单机重新运行 `python3 scripts/deploy.py --single-host`。分机在受影响的主机运行 `--db-only`、`--core-only` 或 `--gpu-only`。自定义路径用 `python3 scripts/deploy.py --config ./deploy.prod.yaml --single-host`，以后启停和查询沿用该路径。配置中显式填写的相对目录以该配置文件所在目录为基准。下表列出仓库模板的默认位置。[配置解析实现](../scripts/deployment_config.py)

| 文件或目录 | 谁读取、保存什么 |
| --- | --- |
| `deploy.config.yaml`、`deploy.config.example.yaml` | 前者存放本机配置，后者提供带中文注释的默认值。`deploy.py` 将连接地址、端口等转换为 Compose 进程的环境变量，只将 `engine` 分组传给 GPU 安装器。 |
| `deploy.test.yaml`、`deploy.test.example.yaml` | 前者保存本机测试的设备名和外部端口，后者提供首次运行的默认值。测试时通过 `--config` 或常规查找顺序另行选取原业务配置。 |
| `compose.yaml` | Compose 的服务定义，包括镜像或构建目标、启动命令、网络、依赖、挂载、GPU 和健康检查。 |
| `Dockerfile`、`pyproject.toml`、`uv.lock` | 构建 MyComfy 应用镜像和镜像内的 Python 环境，`uv.lock` 固定应用依赖。 |
| `var/gateway/` | 业务机的网关状态目录，可用 `paths.gateway` 指定。`rules.json` 保存 Core 发布的入口规则，`status.json` 是 Nginx 的实际应用回执，`edge.sqlite3` 保存按分钟汇总的流量。规则在管理页修改。[流量与限流说明](api-traffic.md) |
| `var/config/maintenance-token` | 部署业务端时自动生成或复用，`core` 将其用于需要维护凭据的接口。 |
| `var/deployment/` | 在 GPU 容器内只读挂载为 `/deployment`，供安装器解析 `engine` 分组引用的本地模型和 Git 仓库。可用 `paths.deployment` 指定宿主目录。 |
| `var/data/engines/generated/` | 引擎安装器在此缓存固定版本源码，生成 `locks/<摘要>/requirements-gpu.lock` 和 `manifests/<摘要>/deployment.json`。这份锁固定 ComfyUI、节点和 PyTorch 的依赖。 |
| `var/data/engines/model-objects/`、`var/data/engines/releases/` | 前者按 SHA-256 缓存模型；后者按版本保存 ComfyUI 源码、自定义节点、独立的 `venv`，并用硬链接引用已校验的权重。 |
| `var/data/engines/build-contexts/ref2va/` | 为 Ref2VA 镜像构建单独提供已校验的权重文件。 |
| `var/data/engines/engines.json` | 安装器生成的实例配置。常驻引擎管理器据此找到各实例的 Python、ComfyUI 入口、GPU 和端口。 |
| `var/data/engines/instances/gpu0/g*/` | 每个启动代次的运行目录。模型加载或生成失败时看 `engine.log`；进程身份和退出记录分别在 `identity.json`、`ended.json`。 |
| `var/stop/<项目名>.json` | 优雅停机期间记录待核验代次。退出凭据全部匹配后，部署脚本清除此记录。 |

本机直连时，`public.origin: null` 或省略此项会按 `public.host` 和 `ports.api` 生成工作台的 HTTP 来源。以默认主机地址为例，设置 `ports.api: 18000` 后，工作台来源为 `http://127.0.0.1:18000`。通配绑定 `0.0.0.0` 和 `::` 对应的本机来源分别使用 `127.0.0.1` 和 `[::1]`。本机后台地址始终由 `127.0.0.1` 和 `ports.admin` 决定。

已有 YAML 若写了 `origin: "http://127.0.0.1:8000"`，此值仍优先使用。要让地址随发布端口自动调整，将这行改为 `origin: null` 或删除该字段，再用原配置重新部署业务端。使用 HTTPS 代理时，保留浏览器实际访问的 `public.origin`。同时对外提供后台时，将 `public.admin_origin` 设为后台的独立 HTTPS 来源。

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

实际端口以本机部署为准。例如 `ps -a` 显示 `127.0.0.1:13306->3306/tcp` 时，宿主机客户端使用 `13306`，单机 GPU 容器仍通过 Compose 网络连接 `mysql:3306`。[完整配置模板](../deploy.config.example.yaml)

### 部署角色参考视频模型

内置首帧模板使用 FL2VA，「角色参考与动作」模板使用 Ref2VA。两份主模型可以装在同一个引擎中。每个工作流的 `UNETLoader` 指定要加载的文件，调度器按模板要求的模型标识和 SHA-256 匹配引擎。

1. 在已有 `deploy.config.yaml` 的 `engine:` 下添加 `ref2va_in_image: true`，与 `comfyui:` 同级。保留现有的模型、目录和实例配置。脚本自动登记 `h3-ref2va`。此模板还使用默认 H3 部署中的文本编码器、视频 VAE 和音频 VAE。
2. 如果 GPU 机已在运行，先等 Worker 排空并关闭旧引擎，再部署。沿用原先的 `--config`，以便脚本找到同一数据目录。分机时在 GPU 端运行以下命令。

   ```bash
   python3 scripts/deploy.py --gpu-only --stop
   python3 scripts/deploy.py --gpu-only
   ```

   单机上的部署命令为 `python3 scripts/deploy.py --single-host`。如需提前只构建引擎镜像，可运行 `python3 scripts/deploy.py --compose build engine`。这两条入口都优先使用持久缓存。缓存缺失时，脚本可以从本机已有的 Ref2VA 镜像恢复权重。本地没有可用来源时才从锁定来源首次下载约 **20.97 GB**。Docker 从本地文件构建 `mycomfy-engine-ref2va:local`。即使清空 BuildKit 缓存，只要保留已校验的持久权重，重新构建时也可使用该文件。重新创建镜像层仍可能需要读取和写入约 21 GB 的本地数据。镜像和持久权重各需约 21 GB，另需为构建缓存与引擎虚拟环境留出空间。[固定权重来源](../examples/deployment/h3-ref2va.models.yaml)
3. 更新 Core 和 Worker 后，在管理后台打开「工作流模板 → 打开 JSON 文件」，选择角色参考模板，点击「发布新版本」。工作台选择这个版本后，上传主图，在「场景与动作」中填写希望角色做什么。图 2 和图 3 均可留空，也可以只补其中一张。转头动作可补同一角色的侧面照，全身动作可补能看清服装和体态的照片。模板生成一个连续镜头，默认输出 640×384、24 fps、约 5.17 秒的 MP4。[角色参考模板](../examples/strategies/h3-character-reference.strategy.jsonc) · [ComfyUI 原生参考视频工作流](https://docs.comfy.org/tutorials/video/minimax/minimax-h3-native)
4. 如需 AI 辅助，在后台选定生效的提示词服务商。工作台点击「扩写提示词」后，可预览并采用结果，也可直接提交原文。仅「场景与动作」字段参与扩写，参考图直接用于视频生成。完整编写要求位于模板的 `prompt_assist.instructions`。若这个模板系列已单独保存过扩写设置，后台设置优先。要更新指令，在「提示词扩写设置」勾选「为此模板启用提示词辅助」，将新指令粘贴到「给 AI 的编写要求」，选中「场景与动作」并保存。

要确认 GPU 已能接单，可在业务机上运行以下只读查询。默认实例为 `gpu0`；如果改过实例名，替换 URL 的最后一段。预期 `accepting` 为 `True`，模型摘要为 `9255f52b6677845ad238f20dfaafa94727053694127ab7f255c048f0f9365779`。

```bash
admin=$(docker port mycomfy-gateway-1 8001/tcp)
curl --noproxy '*' -fsS "http://$admin/local/v1/monitor/instances/gpu0" |
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["accepting"], d["manifest"]["models"].get("h3-ref2va"))'
```

## Docker 镜像和容器行为

完整单机部署有七项服务，通常可见六个常驻容器和一个成功退出的迁移容器。下表采用默认项目名 `mycomfy`。[Compose 服务定义](../compose.yaml) · [镜像构建定义](../Dockerfile)

| 服务 | 镜像 | 启动后的行为和观察方法 |
| --- | --- | --- |
| `mysql` | `mysql:8.4` | 官方镜像首次在空数据卷初始化业务库与账号，以后复用命名卷 `mysql-data`，挂载在 `/var/lib/mysql`。健康检查用业务账号执行 `SELECT 1`。 |
| `redis` | `redis:7-alpine` | 启动时配置 ACL 用户，供业务端缓存会话预检、跨进程统计用户请求和模型服务商并发。健康检查执行 `PING`。当前关闭了 RDB 快照和 AOF，重启后重新累计限流计数，按业务访问重建缓存。 |
| `migrate` | `mycomfy-migrate`（Compose 按项目命名） | 与 Worker 使用相同的 Dockerfile 构建目标，通过宿主网络连接数据库，执行 `python -m mycomfy.cli migrate`。成功后显示 `Exited (0)`。 |
| `core` | `mycomfy-core:local` | 一个 Python 进程提供工作台、后台和 API，只读挂载 `web/client`、`web/admin`。通过宿主网络连接数据库，为网关提供私有套接字 `public.sock` 和 `admin.sock`。健康检查请求管理套接字。 |
| `gateway` | `mycomfy-gateway:local` | 镜像内的 Nginx 使用独立桥接网络，经共享卷中的 Unix 套接字把请求交给 Core。Docker 将宿主 `public.host:ports.api` 映射到容器端口 8000，将宿主 `127.0.0.1:ports.admin` 映射到容器端口 8001。健康检查通过私有套接字核对生效规则，宿主端口可用性可从 Windows 或 Linux 发起 HTTP 请求验证。 |
| `engine` | `mycomfy-engine:local` | 申请 `gpus: all`，以 `runtime serve --stop-engines-on-exit` 启动管理器，再启动和监护 ComfyUI 子进程。健康检查执行 `runtime ready`；可在对应代次的 `engine.log` 查看采样和导出日志。 |
| `worker` | `mycomfy-worker:local` | 从 MySQL 领任务并更新状态，与 `engine` 共用网络空间，经 `127.0.0.1:8188` 调用默认实例。镜像根据调度循环的心跳报告健康状态，队列为空也可以是 `healthy`。 |

Linux 业务机在共享局域网中部署时，建议使用 Docker Engine 28.0 或更新版本。Docker 记录了早期版本的一个限制：同一二层网络中的其他主机在特定条件下也能访问发布到回环地址的端口。[Docker 端口发布说明](https://docs.docker.com/engine/network/port-publishing/#publishing-ports)

应用代码和管理进程的 Python 环境 `/opt/venv` 随本地镜像构建。运行模型的 Python 在 `var/data/engines/releases/<摘要>/venv`，权重保存在宿主机数据目录；重建引擎容器时可复用这些文件。

## 一次单机部署的执行顺序

1. **读取配置并准备挂载。** 脚本解析 Compose，确定端口、目录和部署用户的 UID/GID，准备维护令牌。单机模式下，宿主网络中的业务端使用回环地址和发布端口连接数据库；GPU 端使用 Compose 内网地址。
2. **启动数据服务。** Compose 执行 `up -d --wait mysql redis`，等 MySQL、Redis 都健康后才继续。
3. **构建应用镜像。** Compose 执行 `build migrate core gateway engine worker`，从当前 Git 检出目录读取 Dockerfile、源码和依赖锁，可复用已有构建缓存。启用 `ref2va_in_image` 时，同时使用本机准备好的 Ref2VA 文件。
4. **安装引擎。** 脚本用 `compose run --rm --no-deps -T` 启动临时 `engine` 容器。镜像入口 `python -m mycomfy.cli` 接收参数 `deploy - --base /deployment --root <数据目录>/engines`，其中 `-` 表示从标准输入读取引擎配方。宿主脚本从 YAML 提取 `engine` 分组，序列化为 JSON 后写入安装器的标准输入。安装器固定源码版本、生成依赖锁和清单、校验模型并验证 GPU。安装完成后临时容器退出，结果保存在宿主机挂载目录。
5. **迁移并启动常驻服务。** Compose 执行 `up -d --wait --no-build core gateway engine worker`。依赖要求 `migrate` 成功退出，然后启动常驻进程。引擎容器启动后 Worker 即可开始核对任务。部署业务端时，脚本等服务就绪后打印工作台和管理后台地址。`--gpu-only` 只打印业务机的工作台地址。

优雅停机时，脚本先让 Worker 停止领新任务并排空在途执行，再在引擎容器中执行 `python -m mycomfy.cli runtime stop-snapshot` 记录需要核验的代次，停止引擎后逐份核对其 `ended.json`，随后关闭业务端与本项目的数据服务。若核验失败，脚本会中止，保留后续服务供检查；确认提示“所选服务已停止”后，即可在 Windows 终端执行 `wsl --shutdown`。[引擎部署与恢复](engine-deployment.md)
