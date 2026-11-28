# MyComfy

本地视频生成平台。用户可在网页工作台注册、上传素材、选择策略版本并提交批次，维护人员可在本机监控页追踪运行状态。Worker 在 MySQL 登记执行意图后向本机 ComfyUI 提交一次工作流。只有文件通过契约校验、发布到正式位置并完成登记，用户才会看到成功状态。

## 目录与阅读顺序

```text
MyComfy/
├── src/mycomfy/                  Python 服务与命令入口
│   ├── cli.py                    mycomfy 命令入口，分派 api、worker、runtime 等子命令
│   ├── gateway/                  Nginx 入口规则、热加载与流量计量
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
│   └── deploy.py                 宿主机部署入口，读取 YAML 并按角色运行 Compose
├── tests/{unit,integration,e2e}/  单元、集成和端到端测试
└── docs/                         部署、运维与验收
```

<details>
<summary>展开完整文件树（190 个文件，含用途注释）</summary>

```text
MyComfy/
├── README.md                                                  # 项目入口与快速上手
├── scripts/                                                   # 部署、测试和验收命令
│   ├── accept_gpu.py                                          # 通过 API 验收视频生成与下载
│   ├── config_document.py                                     # 安全读取、校验 YAML 文档
│   ├── deploy.py                                              # 宿主机一键部署、停机及测试入口
│   ├── deploy_tunnel.py                                       # 切换测试入口，退出时恢复
│   ├── deployment_config.py                                   # 将 YAML 转为容器变量和引擎配方
│   ├── deployment_weights.py                                  # 为镜像构建准备已校验的 Ref2VA 权重
│   ├── funnel_client.py                                       # 启动并回收 Tailscale Funnel 转发
│   ├── test_mysql.py                                          # 启动临时 MySQL 并运行测试
│   ├── tunnel_settings.py                                     # 校验并保存隧道设备名和端口
│   └── vendor/                                                # 部署端内置依赖
│       ├── README.md                                          # 内置 YAML 解析器来源与许可
│       └── pyyaml-6.0.3.zip                                   # 部署脚本随附的 YAML 解析器
├── deploy.config.example.yaml                                 # 部署参数与默认 H3 引擎配置
├── deploy.test.example.yaml                                   # 临时隧道设备名与端口示例
├── Dockerfile                                                 # 构建 Core、网关、Worker 和引擎镜像
├── compose.yaml                                               # 编排应用、MySQL 与 Redis 容器
├── src/                                                       # Python 源码
│   └── mycomfy/                                               # 服务端主包
│       ├── __init__.py                                        # MyComfy 包说明
│       ├── cli.py                                             # 分派服务、迁移和引擎子命令
│       ├── config.py                                          # 从环境变量加载运行配置
│       ├── errors.py                                          # 定义 API 与会话错误
│       ├── origins.py                                         # 校验并规范化工作台和后台来源地址
│       ├── api/                                               # HTTP 接口、鉴权、事件和限流
│       │   ├── __init__.py                                    # 导出 HTTP 应用创建入口
│       │   ├── app.py                                         # 注册工作台和管理 API、鉴权及审计
│       │   ├── auth.py                                        # 注册登录、会话校验与撤销
│       │   ├── events.py                                      # 分发用户事件并推送 SSE
│       │   ├── group_admin.py                                 # 管理权限组和用户归组
│       │   ├── pagination.py                                  # 编解码分页游标
│       │   ├── permissions.py                                 # 查询分组权限并校验操作权限
│       │   ├── prompt_assist.py                               # 调用模型扩写用户提示词
│       │   ├── prompt_provider_keys.py                        # 加解密模型服务商密钥
│       │   ├── prompt_providers.py                            # 管理并切换提示词服务商
│       │   ├── server.py                                      # 启动用户与管理 API 监听器
│       │   ├── session_cache.py                               # 缓存 Redis 会话预检结果
│       │   ├── template_prompt_settings.py                    # 更新模板扩写指令与可扩写字段
│       │   ├── traffic_admin.py                               # 查询 API 流量并修改限流规则
│       │   ├── traffic_catalog.py                             # 枚举路由并向网关发布规则
│       │   ├── traffic_limiter.py                             # 限制用户请求频率和模型调用并发
│       │   └── web.py                                         # 挂载工作台和后台静态页面
│       ├── generation/                                        # 任务受理、调度与状态
│       │   ├── __init__.py                                    # 任务生成包说明
│       │   ├── jobs.py                                        # 受理批次、查询任务、取消与重试
│       │   ├── node_check.py                                  # 检查模板的节点依赖并报告缺项
│       │   ├── state.py                                       # 管理任务状态迁移、预算和重试结算
│       │   ├── strategy.py                                    # 校验、发布策略版本并匹配引擎
│       │   ├── worker.py                                      # 调度任务、核对引擎执行并恢复中断
│       │   ├── worker_health.py                               # 用协调器心跳判断 Worker 健康
│       │   └── workflow_nodes.py                              # 提取 API 工作流、画布与子图中的节点类型
│       ├── engine/                                            # ComfyUI 安装、通信与进程管理
│       │   ├── __init__.py                                    # ComfyUI 引擎包说明
│       │   ├── adapter.py                                     # 提交工作流、核对队列及提取输出
│       │   ├── bundled.py                                     # 校验并安装内置 H3 节点
│       │   ├── deploy.py                                      # 按固定清单安装引擎、节点和模型
│       │   ├── gpu_probe.py                                   # 在引擎虚拟环境验证 CUDA 和可选的 SageAttention
│       │   ├── lockfile.py                                    # 生成 GPU 依赖及其哈希锁
│       │   ├── model_objects.py                               # 校验、缓存和刷新模型权重
│       │   ├── node_catalog.py                                # 采集引擎当前代次注册的节点
│       │   ├── progress.py                                    # 通过 WebSocket 采集节点进度
│       │   ├── recipe.py                                      # 将引擎配方解析为固定安装清单
│       │   ├── runtime.py                                     # 管理引擎实例、运行代次及优雅停机
│       │   ├── supervisor.py                                  # 监护引擎进程树并记录退出凭据
│       │   └── comfy_nodes/                                   # 随引擎安装的自定义节点
│       │       ├── __init__.py                                # 注册五图生成四段视频的 H3 节点
│       │       ├── segment.py                                 # 根据两个关键帧采样一段 H3 视频
│       │       └── video.py                                   # 用 FFmpeg 分段编码并拼接 MP4
│       ├── artifacts/                                         # 素材存储与结果发布
│       │   ├── __init__.py                                    # 文件存储与产物发布包说明
│       │   ├── publication.py                                 # 登记、发布产物并恢复中断的发布
│       │   └── storage.py                                     # 校验媒体、存储文件并支持分段下载
│       ├── database/                                          # 数据库模型与会话
│       │   ├── __init__.py                                    # 数据库包说明
│       │   ├── capacity_models.py                             # 定义容量控制和磁盘预留表
│       │   ├── models.py                                      # 定义用户、任务、素材等核心表
│       │   ├── prompt_models.py                               # 定义提示词服务商及模板设置表
│       │   ├── schema.py                                      # 执行迁移并检查数据库版本
│       │   ├── session.py                                     # 创建数据库会话、ID 与时间戳
│       │   └── traffic_models.py                              # 定义 API 限流规则表
│       ├── gateway/                                           # Nginx 网关
│       │   ├── __init__.py                                    # Nginx 网关包说明
│       │   ├── __main__.py                                    # 启停 Nginx、热加载规则并报告健康状态
│       │   ├── metrics.py                                     # 按分钟汇总 Nginx 请求日志
│       │   └── nginx.py                                       # 将路由和限流规则编译为 Nginx 配置
│       └── ops/                                               # 监控、容量与备份
│           ├── __init__.py                                    # 运维工具包说明
│           ├── backup.py                                      # 备份、校验并恢复数据库与文件
│           ├── capacity.py                                    # 检查配额、磁盘水位并预留空间
│           ├── monitor.py                                     # 提供运行监控、日志查询和告警
│           ├── traffic_metrics.py                             # 按路由和时间窗口查询流量
│           └── traffic_store.py                               # 读写限流配置并计算生效规则
├── web/                                                       # 浏览器页面
│   ├── client/                                                # 用户工作台
│   │   ├── README.md                                          # 工作台使用与权限接入说明
│   │   ├── access.mjs                                         # 按权限控制页面与按钮
│   │   ├── access.test.mjs                                    # 测试前端页面与按钮权限
│   │   ├── app.js                                             # 工作台路由、登录注册与账户设置
│   │   ├── client.css                                         # 工作台布局与通用控件样式
│   │   ├── composer.css                                       # 任务创建表单样式
│   │   ├── composer.js                                        # 按模板生成表单、上传素材并提交任务
│   │   ├── history.css                                        # 任务历史与详情样式
│   │   ├── history.js                                         # 查看任务、预览产物与重试失败任务
│   │   ├── index.html                                         # 工作台网页入口
│   │   ├── session.js                                         # 管理会话请求、媒体与事件订阅
│   │   └── studio.webp                                        # 登录页与工作台首页封面图
│   └── admin/                                                 # 管理后台
│       ├── README.md                                          # 后台使用与模板维护指南
│       ├── admin.css                                          # 后台布局与通用控件样式
│       ├── admin.js                                           # 监控列表、详情与自动刷新
│       ├── graph.css                                          # 工作流节点图样式
│       ├── index.html                                         # 运行监控首页
│       ├── navigation.js                                      # 后台侧栏与页面导航
│       ├── overview.css                                       # 监控总览样式
│       ├── overview.js                                        # 渲染任务分布、告警与磁盘用量
│       ├── providers.css                                      # 提示词服务商页面样式
│       ├── providers.html                                     # 提示词服务商配置页
│       ├── providers.js                                       # 管理并切换扩写服务商
│       ├── template-catalog.js                                # 合并已发布与内置模板目录
│       ├── template-draft.js                                  # 解析 JSONC、绑定输入并生成策略草稿
│       ├── template-editor.css                                # 模板编辑器样式
│       ├── template-editor.js                                 # 编辑输入绑定与提示词扩写规则
│       ├── template-example.js                                # 展示、复制和下载模板示例
│       ├── templates.css                                      # 模板管理页面样式
│       ├── templates.html                                     # 工作流模板管理页
│       ├── templates.js                                       # 导入、预览和发布工作流模板
│       ├── traffic.css                                        # 接口流量与限流表格样式
│       ├── traffic.html                                       # API 流量管理页
│       ├── traffic.js                                         # 展示接口流量并编辑两层限流规则
│       ├── users.css                                          # 用户列表与分组编辑样式
│       ├── users.html                                         # 用户与分组管理页
│       ├── users.js                                           # 分页查看用户并修改分组
│       ├── workflow-graph.js                                  # 解析工作流并绘制交互式节点图
│       ├── workflow-node-check.js                             # 展示引擎缺失节点与工作流位置
│       └── presets/                                           # 内置工作流模板
│           ├── example-h3-text-to-video.strategy.jsonc        # 逐字段注释的 H3 文生视频示例
│           ├── example-video-copy.canvas.json                 # 视频读写示例的 ComfyUI 画布格式
│           ├── example-video-copy.api.json                    # 同一工作流的 ComfyUI API 格式
│           ├── h3-i2v.strategy.json                           # H3 图生视频预设
│           └── h3-t2v.strategy.json                           # H3 文生视频预设
├── migrations/                                                # 数据库结构迁移
│   ├── env.py                                                 # 加载模型并执行数据库迁移
│   ├── script.py.mako                                         # 生成新迁移脚本的模板
│   └── versions/                                              # 按版本执行的结构变更
│       ├── 0001_initial_schema.py                             # 建立初始业务表
│       ├── 0002_capacity.py                                   # 新增磁盘水位和空间预留表
│       ├── 0003_capacity_target.py                            # 记录目标卷的预留字节数
│       ├── 0004_user_groups.py                                # 新增用户组、成员及权限
│       ├── 0005_manual_retry_count.py                         # 记录任务手动重试次数
│       ├── 0006_prompt_providers.py                           # 新增扩写服务商和模板配置表
│       ├── 0007_traffic_rules.py                              # 新增限流规则表及审计字段
│       └── 0008_engine_node_catalogs.py                       # 保存引擎注册的节点清单
├── examples/                                                  # 可导入示例
│   ├── deployment/
│   │   └── h3-ref2va.models.yaml                              # 角色参考主模型的锁定来源和校验值
│   └── strategies/                                            # 视频生成策略
│       ├── h3-character-reference.strategy.jsonc              # 一张主图、两张可选补图，支持扩写动作提示词
│       ├── h3-five-image-20s-segmented-384.strategy.json      # 五图分段视频模板，20 秒、384×384
│       └── h3-five-image-20s-segmented-640.strategy.json      # 五图分段视频模板，20 秒、640×640
├── tests/                                                     # 自动化测试
│   ├── conftest.py                                            # 为测试创建独立 MySQL 库
│   ├── e2e/                                                   # 端到端测试
│   │   ├── test_cloud_delivery.py                             # 验证提交批次到发布 MP4 的完整流程
│   │   ├── test_job_original_web.py                           # 验证任务原始参数与素材页面
│   │   ├── test_job_retry_web.py                              # 验证前台重试按钮、权限与次数
│   │   ├── test_prompt_assist_web.py                          # 验证前台扩写、预览与草稿保护
│   │   ├── test_prompt_providers_web.py                       # 验证后台切换模型服务商与更新密钥
│   │   ├── test_template_prompt_assist_web.py                 # 验证模板扩写配置与示例导入
│   │   ├── test_traffic_web.py                                # 验证后台流量图表与两层限流设置
│   │   └── test_user_admin_web.py                             # 验证后台改组与用户权限生效
│   ├── integration/                                           # 集成测试
│   │   ├── test_accept_gpu.py                                 # 验证 GPU 验收脚本的取证与失败报告
│   │   ├── test_admin_origin.py                               # 验证管理入口的来源与监听边界
│   │   ├── test_api.py                                        # 验证会话、分组、批次提交与任务接口
│   │   ├── test_compose_roles.py                              # 验证 Compose 角色依赖与连接参数
│   │   ├── test_deploy.py                                     # 验证引擎重复安装与模型完整性
│   │   ├── test_deploy_cli.py                                 # 验证部署命令、角色选择与启停顺序
│   │   ├── test_deploy_tunnel.py                              # 验证临时隧道启停与业务入口恢复
│   │   ├── test_engine_lockfile.py                            # 验证依赖来源、锁定与哈希安装
│   │   ├── test_events.py                                     # 验证事件续传、会话撤销与通知已读
│   │   ├── test_gateway.py                                    # 验证 Nginx 路由、热加载与流量计数
│   │   ├── test_migrations.py                                 # 验证数据库升降级、版本检查与数据回填
│   │   ├── test_ops.py                                        # 验证容量预留、监控、日志与备份恢复
│   │   ├── test_prompt_assist.py                              # 验证提示词扩写鉴权、限流与调用范围
│   │   ├── test_prompt_providers.py                           # 验证服务商密钥保护与并发切换
│   │   ├── test_publication.py                                # 验证产物发布中断后的恢复
│   │   ├── test_runtime.py                                    # 验证引擎进程接管、停机与退出记录
│   │   ├── test_segmented_node.py                             # 验证分段节点输出和失败清理
│   │   ├── test_serve.py                                      # 验证 API 端口与 Unix 套接字隔离
│   │   ├── test_storage.py                                    # 验证上传回滚、文件完整性与范围读取
│   │   ├── test_template_prompt_settings.py                   # 验证模板扩写设置的保存、恢复与并发
│   │   ├── test_traffic_admin.py                              # 验证流量管理接口与网关状态同步
│   │   ├── test_traffic_catalog.py                            # 核对路由目录与网关配置快照
│   │   ├── test_traffic_limiter.py                            # 验证限流窗口、并发配额与异常释放
│   │   └── test_worker_protocol.py                            # 验证领任务、提交、重试与执行对账
│   ├── unit/                                                  # 单元测试
│   │   ├── test_contracts.py                                  # 核对批次摘要、策略约束与函数注释
│   │   ├── test_deployment_config.py                          # 验证部署 YAML 解析与参数传递
│   │   ├── test_deployment_weights.py                         # 验证 Ref2VA 本地复用及显式刷新
│   │   ├── test_engine_observation.py                         # 验证历史与队列查询、错误和任务匹配
│   │   ├── test_engine_node_catalog.py                        # 验证节点清单采集、重试和换代
│   │   ├── test_funnel_client.py                              # 验证双端口 Funnel 转发与清理
│   │   ├── test_optional_image_bindings.py                    # 验证可选图片缺省时的节点与连线裁剪
│   │   ├── test_segment_video.py                              # 验证 FFmpeg 分段编码、拼接与清理
│   │   ├── test_segmented_templates.py                        # 核对 H3 分段模板与已部署节点
│   │   ├── test_strategy_prompt_assist.py                     # 验证策略发布时的扩写字段约束
│   │   ├── test_tunnel_settings.py                            # 验证测试隧道配置、设备名与端口
│   │   ├── test_worker_health.py                              # 验证 Worker 心跳与健康探针
│   │   └── test_workflow_nodes.py                             # 验证画布及 API 工作流的节点提取
│   └── web/                                                   # 前端 JavaScript 测试
│       ├── session.test.mjs                                   # 验证前台会话、事件重连与原始参数显示
│       ├── template-catalog.test.mjs                          # 验证模板分页与已发布版本优先显示
│       ├── template-draft.test.mjs                            # 验证模板草稿、JSONC 解析与输入绑定
│       └── workflow-graph.test.mjs                            # 验证工作流预览的节点与连线
├── docs/                                                      # 部署、运维与接口文档
│   ├── api-traffic.md                                         # API 流量统计与两层限流说明
│   ├── cloud-acceptance.md                                    # 云端集成验收记录
│   ├── deploy-cli.md                                          # 部署命令和配置项手册
│   ├── engine-deployment.md                                   # ComfyUI、节点与模型部署指南
│   ├── gpu-acceptance.md                                      # 真实 GPU 生成与交付验收步骤
│   ├── https-tunnel-test.md                                   # 临时 HTTPS 隧道测试指南
│   ├── operations.md                                          # 权限、监控、容量与备份恢复手册
│   └── web-api.md                                             # 工作台与管理后台 API 契约
├── pyproject.toml                                             # Python 依赖、命令入口及工具配置
├── uv.lock                                                    # 锁定 Python 依赖版本
├── alembic.ini                                                # Alembic 数据库迁移配置
├── .gitattributes                                             # 固定内置节点源码的换行符
├── .gitignore                                                 # 排除本地配置、数据与缓存
└── .dockerignore                                              # 筛选镜像构建所需文件
```

</details>

在宿主机用 `python3 scripts/deploy.py` 部署。引擎镜像以 `python -m mycomfy.cli` 为入口，由 `deploy` 子命令安装引擎，`runtime` 子命令管理实例。备份与恢复工具使用 `python -m mycomfy.ops.backup`。部署脚本和镜像内部命令只维护当前版本。变更入口或参数时，同步更新调用方、测试和部署手册。[部署入口](scripts/deploy.py) · [引擎配方](src/mycomfy/engine/recipe.py) · [实例管理](src/mycomfy/engine/runtime.py) · [备份工具](src/mycomfy/ops/backup.py)

两个网页使用原生 ES 模块和本地静态资源，随 Python 服务安装。业务监听器的根路径提供用户工作台，本机监听器的根路径提供只读监控，管理后台的“工作流模板”可新增模板、预览节点并发布策略，“用户管理”可查看全部账号并调整所属权限组。“API 流量”列出所有业务和管理 API，支持查看请求量、错误和耗时，以及在线调整每个接口的限流规则。[流量与限流说明](docs/api-traffic.md) · [C 端开发入口](web/client/README.md) · [管理后台开发入口](web/admin/README.md) · [前后台 API 契约](docs/web-api.md)

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

按操作查命令、配置文件和容器行为，见 [deploy.py 部署命令手册](docs/deploy-cli.md)。

### 全栈 Docker Compose（Linux / Windows WSL 2）

**在同一台 GPU 机器上部署**，需要 Python 3、Docker Compose 2.30.0 或更新版本，以及可在容器中使用的 NVIDIA GPU。Linux 使用 NVIDIA 驱动和 NVIDIA Container Toolkit。Windows 下建议在 WSL 的 Linux 文件系统（如 `~/MyComfy`）中检出项目，使用支持 WSL 的 NVIDIA 驱动和 Docker Desktop 4.34 或更新版本的 WSL 2 后端，并启用 **Settings → Resources → Network → Enable host networking**，供 Core 和数据库迁移进程连接数据服务。网页入口通过 Docker 端口映射发布到 Windows。[Docker GPU 支持](https://docs.docker.com/desktop/features/gpu/) · [宿主网络](https://docs.docker.com/engine/network/drivers/host/#docker-desktop)

**首次部署**可在新检出的仓库根目录直接运行以下命令。脚本读取仓库自带的中文注释模板 `deploy.config.example.yaml`，MySQL 和 Redis 的默认密码都是字符串 `123456`，数据库和网页端口仅绑定本机 `127.0.0.1`。模板预置 ComfyUI v0.33.0 和 MiniMax H3 文生视频、图生视频共用的五个权重文件。GPU 端首次下载这五个文件约需 42 GB，镜像、Python 环境和生成的视频还需额外磁盘空间。后续完整部署按 SHA-256 复用已有权重，只获取本地缺失的模型。要从配置的来源重新获取全部权重，单机可运行 `python3 scripts/deploy.py --single-host --refresh-weights`。

```bash
python3 scripts/deploy.py --single-host
```

这条命令先启动 MySQL 和 Redis 并等待健康检查通过，再构建应用镜像、在容器里安装 ComfyUI、执行数据库迁移，最后启动 Core、Nginx 网关、引擎和 Worker。业务端通过回环地址和配置的端口连接数据服务，GPU 容器在 Compose 网络内连接 `mysql:3306`。部署完成后，在本机管理后台 `http://127.0.0.1:8001/` 进入“工作流模板”，直接发布内置的 H3 策略，或从自己的 ComfyUI API 工作流新增模板，然后在工作台 `http://127.0.0.1:8000/` 创建批次。[新增与导入步骤](web/admin/README.md#新增模板) · [配置模板](deploy.config.example.yaml) · [节点与模型示例](docs/engine-deployment.md#配置工作流依赖)

**临时从外网测试已有部署**时，在运行 Core 和网关的业务机上登录 Tailscale 并开通 Funnel 后，在 WSL 或 Linux 的项目目录执行下面的命令。

```bash
python3 scripts/deploy.py --test
```

终端会打印工作台和管理后台的 HTTPS 链接。管理链接提供免登录管理权限。保持终端打开，结束时按 **Ctrl+C**，等待两条临时转发关闭并恢复原业务来源。首次使用默认值成功开通两条转发后，脚本将本机的完整设备名及外部端口保存到 `deploy.test.yaml`。也可提前复制 `deploy.test.example.yaml` 并编辑。原业务配置由 `--config` 或默认查找顺序选定。[首次准备与配置示例](docs/https-tunnel-test.md)

**启用 AI 提示词扩写**时，在管理后台的“提示词服务商”添加兼容 OpenAI Chat Completions 的 API 基址、模型标识及可选 API 密钥，再点击“启用”。基址由业务端容器访问，例如填 `https://api.example.com/v1`，服务端会追加 `/chat/completions`。可以保存多套配置，切换后从下一次扩写开始生效。

在管理后台「工作流模板」的导入入口可下载画布 JSON 和 API 格式 JSON 示例，两种格式展示同一组视频读写节点。「查看 JSON 示例」还提供带逐字段注释的 H3 完整策略（JSONC）及其标准 JSON。上传文件时可以省略 `prompt_assist`，先发布并正常生成视频。选中已发布模板后，在“AI 提示词辅助”里填写指令、勾选文本字段并单独保存。新设置从下一次扩写生效，无需重新上传文件、发布版本或重启服务。内置 H3 文生视频、图生视频预设和仓库的五图分段示例均附有各自的指令。工作台用户可以直接提交原文，也可以点击“扩写提示词”，预览并编辑建议后再采用。一次扩写仅将模板指令、当前字段及其草稿发给生效的服务。[完整注释示例](web/admin/presets/example-h3-text-to-video.strategy.jsonc) · [API 与字段说明](docs/web-api.md#策略与素材)

**需要自定义密码、模型或端口时**，在首次部署前复制模板，再编辑仓库根目录的 `deploy.config.yaml`。文件中的 `#` 注释说明了各项用途和填写方式。部署脚本优先读取这份配置，Git 默认忽略该文件。

```bash
umask 077
cp -n deploy.config.example.yaml deploy.config.yaml
```

在副本中保存修改后，单机运行 `python3 scripts/deploy.py --single-host`。密码保留引号，例如 `password: "123456"`，端口用整数，例如 `api: 8000`。表中的点号表示 YAML 的嵌套分组。

| 部署配置字段 | 填写内容 |
| --- | --- |
| `single_host` | 顶层布尔值。设置为 `true` 后，直接运行 `python3 scripts/deploy.py` 即可在这台 GPU 主机的 Docker 中部署全部服务。省略或设为 `false` 时，无参部署连接已就绪的数据服务。 |
| `mysql.host`、`redis.host` | 业务端连接的主机名或 IP。模板都填 `127.0.0.1`，端口分别取 `ports.mysql`（默认 3306）和 `ports.redis`（默认 6379）。 |
| `public.host`、`public.origin` | `public.host` 是 Docker 发布工作台的宿主地址。`public.origin` 为 `null` 或省略时按该地址和 `ports.api` 生成 HTTP 来源。经域名或 HTTPS 代理访问时，显式填写浏览器使用的来源，供登录和写请求校验。 |
| `public.admin_origin` | 可选的管理后台 HTTPS 来源。本机地址为 `http://127.0.0.1:<ports.admin>`。通过自管反向代理使用管理域名时，填写独立于工作台的 HTTPS 来源，并让上游保留浏览器的 Host。两个管理入口提供相同权限。 |
| `public.trusted_proxy_cidrs` | 可提供客户端地址的上游代理 IP 或 CIDR 数组，默认为空。Nginx 按此配置处理转发头，供 IP 限流使用。 |
| `mysql.bind_host`、`redis.bind_host` | 可选。运行随附数据库容器时，端口发布到数据库服务器的哪个本机 IP，默认都是 `127.0.0.1`。分机示例见下文。 |
| `mysql.user`、`mysql.password` | MySQL 应用账号和密码。随附容器在空数据卷上创建 `mycomfy` 库并初始化所填账号，初始化凭据请避开单引号和反斜杠。复用已有数据卷时填写已有凭据。 |
| `redis.user`、`redis.password` | Redis 缓存账号和密码。两个服务的密码均填原值，由脚本编码连接串。 |
| `engine.comfyui` | ComfyUI 仓库地址与发布标签或 40 位 Git 提交号。 |
| `engine.pytorch` | 模板在 Linux x86_64、Python 3.12 下使用 PyTorch `2.11.0+cu130`，目标机需要支持 CUDA 13.0 的 NVIDIA 驱动。 |
| `engine.sageattention` | 为使用 SageAttention 的工作流指定 Python 包版本或本地 wheel，随引擎安装并在目标 GPU 上验证。默认关闭。[部署示例](docs/engine-deployment.md#为节点启用-sageattention) |
| `engine.nodes`、`engine.models` | 默认 H3 工作流使用 ComfyUI 内置节点。五个模型文件均固定下载地址和 SHA-256。要让用户在工作台选择文生或图生视频，还需发布相应的业务策略。 |
| `engine.instances`、`paths`、`ports` | 模板使用一张 GPU、本地数据目录和默认端口。按需调整 GPU 编号、目录或端口。`paths` 中填写的相对路径以本配置文件所在目录为基准。 |
| `engine.ref2va_in_image` | 设为 `true` 时，部署脚本准备 H3 Ref2VA 权重，再将本地文件打入引擎镜像，供「角色参考与动作」模板使用。默认关闭。[配置及导入步骤](docs/deploy-cli.md#部署角色参考视频模型) |

**已有独立 MySQL 和 Redis** 时，保留模板的 `single_host: false`，在配置中填写已有服务的地址与凭据，并运行 `python3 scripts/deploy.py`。先在 MySQL 准备好 `mycomfy` 库并授予应用账号迁移和读写权限。[部署脚本](scripts/deploy.py)

**先体验网页**时，可直接使用模板的本机数据库参数。先运行 `python3 scripts/deploy.py --db-only`。数据库就绪后运行 `python3 scripts/deploy.py --core-only`，脚本会执行迁移并启动 `core` 和 `gateway`。显式指定 `--db-only`、`--core-only`、`--gpu-only` 或 `--compose` 时，只执行所选操作。配置中的 `single_host: true` 仍决定这些服务连接本机数据库。

| 服务 | 容器职责 |
| --- | --- |
| `mysql`、`redis`、`migrate` | 保存业务状态和限流规则、缓存会话及共享限流计数、在服务启动前迁移数据库。 |
| `gateway` | Nginx 在独立的桥接网络中监听工作台与后台，经共享卷的 Unix 套接字转发给 Core，并按来源 IP 限流。Docker 将容器端口 8000 发布到业务机的 `public.host:ports.api`，将 8001 发布到 `127.0.0.1:ports.admin`。 |
| `core` | 托管 API、工作台和本机管理后台，并在鉴权后执行用户限额。使用 HTTPS 反向代理时，在部署配置的 `public.origin` 填写工作台外部来源，默认随 HTTPS 来源启用 Secure Cookie。 |
| `engine` | 唯一请求 GPU 的服务，执行 `runtime serve` 并启动、监护 ComfyUI 子进程。实例端口仅监听该容器网络命名空间内的回环地址。 |
| `worker` | 通过 MySQL 领任务，使用 `network_mode: service:engine` 连接回环引擎。与 engine、core 以相同绝对路径挂载数据目录，交接输入与产物。镜像自带健康检查，在 `docker ps` 的 STATUS 列显示协调循环的状态。队列为空时，能正常完成轮询的 Worker 同样显示 `healthy`。 |

每个失败任务默认可由用户手动重试 3 次。在 `deploy.config.yaml` 顶层设置 `manual_retry_limit: 3` 可调整次数，设为 `0` 可关闭。重新部署业务端后生效。已有任务继续沿用已消耗的次数，模板内的自动重试单独计数。

### 优雅停机

单机部署在 WSL 的项目目录运行以下命令。

```bash
python3 scripts/deploy.py --stop
```

Worker 先停止领新任务，等待在途执行到达可恢复的边界。脚本随后停止引擎并核对当前代次的退出记录，最后停止业务端、MySQL 和 Redis。看到“所选服务已停止”后，就可以在 Windows 终端执行 `wsl --shutdown`。长视频可能需要数分钟以上。若提示退出凭据缺失，请保留现场，先检查该代次的 `ended.json` 和引擎日志。

分机部署可分别执行 `--gpu-only --stop`、`--core-only --stop`、`--db-only --stop`，数据库机最后停。容器、数据卷和未领取的任务都会保留。

**日常单机启动**时，运行以下命令复用本机已有镜像和已安装的 ComfyUI、模型。

```bash
python3 scripts/deploy.py --single-host --no-build
```

脚本先等 MySQL 和 Redis 健康，再启动 Core、网关、引擎和 Worker，完成后打印配置中的工作台和管理后台地址。Compose 直接使用本地标签指向的镜像。如果已单独构建新版 Worker，Compose 会用新版镜像更新对应容器。自定义配置沿用 `--config /路径/部署配置.yaml`。查看容器状态可执行 `python3 scripts/deploy.py --compose --profile database ps -a`。[Compose 配置](compose.yaml) · [引擎运维](docs/engine-deployment.md#容器运行与验收)

### 已有部署与分机部署

假设数据库机、业务机、GPU 机位于同一私网。MySQL 与 Redis 在 `10.20.0.10`，业务工作台在 `10.20.0.20:8000`，GPU 机是 `10.20.0.30`。三台主机可以使用相同的地址配置。在各机 `deploy.config.yaml` 对应分组中更新下列字段，账号、密码和引擎配置沿用现场填写的值。

```yaml
mysql:
  host: "10.20.0.10"
  bind_host: "10.20.0.10"
redis:
  host: "10.20.0.10"
  bind_host: "10.20.0.10"
public:
  host: "10.20.0.20"
  origin: null
ports:
  mysql: 3306
  redis: 6379
  api: 8000
```

Docker 在业务机的 `public.host` 上发布 `ports.api`，将工作台流量转发给 `gateway`。示例自动生成工作台来源 `http://10.20.0.20:8000`，GPU 机部署完成时也会显示这个地址。若用 HTTPS 反向代理，`public.origin` 填用户实际访问的 HTTPS 域名和对外端口，`ports.api` 仍填写代理转发到的本地 API 端口。

| 顺序 | 在哪里运行 | 配置和命令 |
| --- | --- | --- |
| 1 | 数据库机 `10.20.0.10` | 如使用随附容器，将 `mysql.bind_host` 和 `redis.bind_host` 都设为该机私网 IP `10.20.0.10`，运行 `python3 scripts/deploy.py --db-only`。已有独立数据库服务可跳过此步。 |
| 2 | 业务机 `10.20.0.20` | 将 `mysql.host` 和 `redis.host` 都设为 `10.20.0.10`，填写对应凭据，运行 `python3 scripts/deploy.py --core-only`。等待迁移和业务端启动成功。 |
| 3 | GPU 机 `10.20.0.30` | 填好业务工作台地址、数据库地址及本机 `engine` 参数，运行 `python3 scripts/deploy.py --gpu-only`。它安装引擎，仅启动 `engine` 和 `worker`。 |

业务机需要能访问 MySQL 和 Redis。GPU 机可以省略整个 `redis` 配置。Worker 直接连接 MySQL 领取任务和回写状态，任务执行无需访问业务机的 HTTP 接口。示例可以在私网防火墙允许业务机、GPU 机连接 `10.20.0.10:3306`，允许业务机连接 `10.20.0.10:6379`。MySQL 与 Redis 在不同主机时，分别填写各自的地址和端口。

业务机与 GPU 机还要把同一共享盘挂载到相同绝对路径，例如两边的 `paths.data` 都填写 `/srv/mycomfy/data`。两端容器使用部署用户的 UID/GID，需有共享读写权限，文件系统需支持文件锁和原子重命名。部署时核对两端共享卷的设备编号与容量预留。[容量核算](src/mycomfy/ops/capacity.py#L91)

默认情况下，`mysql.host` 填写私网地址时，GPU 容器也连接该地址及 `ports.mysql`。填写回环地址时，默认按项目自带的单机数据库处理，GPU 容器通过同一 Compose 网络连接 `mysql:3306`。如果 GPU 容器需要走另一条路（例如宿主机的独立数据库地址），填写从容器可达的 `mysql.engine_host` 和 `mysql.engine_port`。

#### 配置读取顺序

不传 `--config` 时，脚本按以下顺序选择一份完整配置。

1. 仓库根目录的 `deploy.config.yaml`。
2. 仓库根目录的 `deploy.config.yml`。
3. 仓库自带的 [deploy.config.example.yaml](deploy.config.example.yaml)。

使用自定义配置时，运行 `python3 scripts/deploy.py --config ./deploy.prod.yaml --single-host`，并在后续启停和测试时沿用该文件。部署配置使用 `.yaml` 或 `.yml`。常规部署遇到指定的 YAML 文件缺失时，脚本提示后使用仓库模板。使用 `--test` 分享运行中的部署时，显式路径必须存在。复用 MySQL 数据卷时，填写库内已有账号的凭据。[部署命令手册](docs/deploy-cli.md#配置文件与落盘位置)

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

用户 API 位于 `/api/v1`。公开 HTTPS 入口将网页、静态资源和用户 API 转发至业务端口 8000，SSE 连接需关闭代理缓冲并将空闲超时设为大于心跳周期。管理后台提供全局监控、API 流量和限流配置、工作流模板和用户分组管理。后台页面免登录，命令行维护调用可以使用 `Authorization: Bearer <token>`。用户工作台登录后的写请求携带有效 Cookie、`Origin`、`X-Session-Generation` 和 `X-CSRF-Token`。[本机管理后台](http://127.0.0.1:8001/)

## 部署和验收

- 对照规格 A01–A07、R01–R09 查看已在云端验证的行为及对应测试。[云端验证记录](docs/cloud-acceptance.md)
- 在 GPU 目标机上按清单安装 ComfyUI、固定版本节点、Python 环境和模型，生成实例配置。[引擎部署](docs/engine-deployment.md)
- 停止接单、核对实例代次、查看运行指标或演练成套备份恢复。[运维与恢复](docs/operations.md)
- 在已部署工作流和目标硬件上验证真实视频、下载摘要、耗时与吞吐，记录人工画质及引擎能力结论。[目标机验收](docs/gpu-acceptance.md)
