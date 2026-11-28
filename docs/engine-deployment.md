# 在 GPU 机器上部署 ComfyUI

默认通过容器部署。engine 镜像携带 Git、编译工具、Python、venv 和 pip，GPU 设备与驱动库由宿主机注入。运行管理使用 Linux 的 pidfd、子进程和 /proc 信息，与 ComfyUI 运行在同一个容器。启动前准备数据库、持久目录和 GPU 后端的步骤见项目首页。[全栈 Compose](../README.md#启动服务)

## 配置工作流依赖

新检出的仓库直接使用 `deploy.config.example.yaml`。模板固定 ComfyUI `v0.33.0`，以及 Linux x86_64、Python 3.12 的 PyTorch `2.11.0+cu130`、torchvision `0.26.0+cu130` 和 torchaudio `2.11.0+cu130`。宿主机需要支持 CUDA 13.0 的 NVIDIA 驱动。要调整模型或运行环境，可将模板复制为 `deploy.config.yaml`，在副本中编辑 `engine`。[配置模板](../deploy.config.example.yaml) · [ComfyUI 版本](https://github.com/Comfy-Org/ComfyUI/tree/v0.33.0#installing) · [PyTorch 版本组合](https://pytorch.org/get-started/previous-versions/)

默认的 `engine.models` 包含 MiniMax H3 文生视频和图生视频共用的扩散模型、文本编码器、视频 VAE、音频 VAE，以及可选的 8 步 Turbo LoRA。首次部署引擎会从 Comfy-Org 的固定快照下载这五个文件，共约 42 GB，并校验各自的 SHA-256。两份工作流使用 ComfyUI 内置节点和普通文本提示词，均可保留 `engine.nodes` 为空。图生视频还需要用户提交首帧图片。[官方模型文件](https://huggingface.co/Comfy-Org/MiniMax-H3/tree/4b53a35191437de05370da548e59aeabe2a5a09a) · [文生视频工作流](https://github.com/Comfy-Org/workflow_templates/blob/ea7faae6999fb31338f43408ff4a575bd3f7d9a0/templates/video_minimax_h3_t2v.json) · [图生视频工作流](https://github.com/Comfy-Org/workflow_templates/blob/ea7faae6999fb31338f43408ff4a575bd3f7d9a0/templates/video_minimax_h3_i2v.json)

**已有模型文件时**，例如已下载音频 VAE，可将文件放到 `var/deployment/model-import/`，只修改 `engine.models.h3-audio-vae` 的 `source`，保留原有 `path`、摘要和其余四项模型。在现有 `engine.models` 下更新这一项。

~~~yaml
engine:
  models:
    h3-audio-vae:
      source: model-import/minimax_h3_audio_vae_fp32.safetensors
      path: vae/minimax_h3_audio_vae_fp32.safetensors
      sha256: "8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48"
~~~

**更换工作流时**，按新工作流加载的文件修改 `engine.models`。远程模型的 `source` 填 HTTPS 地址，`sha256` 填对应的 64 位小写摘要。本地模型的 `source` 填相对于 `paths.deployment` 的路径，脚本会自动计算摘要，提供了 `sha256` 就会校验。`path` 填 ComfyUI 模型目录中的相对路径，分类和文件名要与工作流一致。若使用额外节点，在 `engine.nodes` 中为每个节点包填写仓库 `url` 和固定的 `version`。

**发布到工作台时**，打开业务机管理后台的“工作流模板”，选择内置 H3 策略，预览节点后即可发布。要创建自己的模板，点击“新增模板”，填写名称和新标识，选择 H3 模板或导入 ComfyUI API 工作流。自定义工作流需根据实际节点绑定提示词、素材输入和视频输出。需要固定模型版本时，可导入完整策略，在 `requirements.models` 中声明对应的模型标识和摘要。[打开工作流模板](http://127.0.0.1:8001/templates.html) · [新增模板步骤](../web/admin/README.md#新增模板) · [策略发布接口](web-api.md#本机维护)

在 GPU 主机运行 `python3 scripts/deploy.py --single-host` 即可启动本机 Docker 的 MySQL、Redis 及全部应用服务。仓库模板将数据库端口与网页端口绑定到本机，两个数据库服务的密码均为字符串 `123456`。如果在自定义配置中将顶层 `single_host` 设为 `true`，日常部署直接运行 `python3 scripts/deploy.py` 即可。使用外部数据库时，保留 `single_host` 默认值 `false`，待数据库就绪后运行 `python3 scripts/deploy.py`。分机时，先在业务机运行 `python3 scripts/deploy.py --core-only` 完成迁移，再在 GPU 机运行 `python3 scripts/deploy.py --gpu-only`。[配置读取顺序与分机部署](../README.md#已有部署与分机部署)

安装引擎时，临时容器会检出 ComfyUI 和节点，读取各自的 `requirements.txt`，生成带哈希的依赖锁并安装。脚本打印本次使用的锁和清单路径。两者存放在数据目录的 `engines/generated/` 下，实际运行配置为 `engines/engines.json`。相同输入重复部署时复用已解析的版本和依赖锁。

权重按 SHA-256 存在 `<paths.data>/engines/model-objects/`。普通部署只获取当前配置中本机缺失的权重。缓存校验失败时会报告具体文件，可在 GPU 端运行 `python3 scripts/deploy.py --gpu-only --refresh-weights`，从配置的来源重新获取本次部署的权重。单机使用 `--single-host --refresh-weights`。本地文件来源从磁盘读取，HTTPS 来源会产生相应的下载流量。

## 五图 H3 分段编码模板

需要用五张图生成 20 秒视频时，可导入 [384×384 模板](../examples/strategies/h3-five-image-20s-segmented-384.strategy.json) 或 [640×640 模板](../examples/strategies/h3-five-image-20s-segmented-640.strategy.json)。内存紧张的机器可先使用 384 版。两版均保留全片风格提示和四段可分别编辑的运镜。

1. **更新引擎。** 已部署的 WSL 单机在项目根目录依次执行下面的命令。使用自定义配置文件时沿用原来的 `--config`。分机部署可只在 GPU 机使用 `--gpu-only --stop` 和 `--gpu-only`。

   ```bash
   python3 scripts/deploy.py --stop &&
   git pull --ff-only origin main &&
   python3 scripts/deploy.py --single-host
   ```

2. **导入模板。** 在管理后台的“工作流模板”中选择“打开 JSON 文件”，选上面的完整策略，预览后点击“发布新版本”。在前台创建新任务时选“五图持续运镜（分段生成）”，依次上传五张图片。对于仓库固定的 ComfyUI v0.33.0，部署器会安装随包提供的 `MyComfySegmentedH3Video` 节点，并将其内容摘要登记到引擎清单；调度会按模板所需的节点版本检查引擎。
3. **查看输出。** 四对相邻图片依次生成，每次只解码当前一段，将 120 帧逐帧交给 FFmpeg 编码成 H.264 临时文件，再处理下一对。最后通过 concat 和 `-c:v copy` 将四段封装成一个无声 MP4，24 fps、480 帧。五张参考图在第 0、120、240、360、479 帧（从 0 计）。任务详情可下载最终视频，当前代次的 `engine.log` 会记录分段序号。

每次执行仍是一个 ComfyUI 请求。中途失败或取消时，已经编码的片段只作临时文件，重新执行将从第一段开始。分段处理节省的是四段解码画面和整片拼接数组同时驻留的内存；模型权重、单段采样和 VAE 解码仍各有内存需求。

## 安装清单

一键部署从业务 YAML 的 `engine` 分组生成安装清单。部署脚本把该分组通过标准输入交给临时引擎容器中的 `python -m mycomfy.cli deploy`。使用仓库模板的默认数据目录时，可在宿主机查看以下文件。

| 文件 | 记录的内容 |
| --- | --- |
| `var/data/engines/generated/locks/<摘要>/requirements-gpu.lock` | 按选定的 Python、PyTorch、ComfyUI 和节点依赖生成的 pip 依赖锁，包含精确版本与文件哈希。 |
| `var/data/engines/generated/manifests/<摘要>/deployment.json` | 本次安装实际采用的 ComfyUI 和节点提交、模型路径和 SHA-256、依赖锁路径与摘要、GPU 实例参数。 |
| `var/data/engines/engines.json` | 安装成功后供引擎管理器读取的实例配置，包括可执行文件、模型搜索路径和实际探测的 GPU 环境。 |

需要给某个策略单独设定时限时，在 YAML 的 `engine.instances` 对应实例中设置 `strategy_timeouts`。按策略系列键或版本 ID 配置 `observation_seconds`、`timeout_seconds`、`reconcile_seconds`，单位为秒。重新部署 GPU 端后，安装器会将其写入生成的实例配置。[配方解析](../src/mycomfy/engine/recipe.py) · [安装执行](../src/mycomfy/engine/deploy.py)

## 容器运行与验收

在 GPU 主机用 `python3 scripts/deploy.py --compose` 执行运维命令，脚本使用部署时的 YAML 连接现有服务。若使用自定义配置路径，把 `--config 文件路径` 放在 `--compose` 之前。安装阶段会在所选 GPU 上验证 `torch.cuda.is_available()`，将实际 CUDA、PyTorch 和 GPU 信息写入实例清单。Worker 在同一个容器网络命名空间内访问实例端口。

~~~sh
python3 scripts/deploy.py --compose exec engine nvidia-smi -L
python3 scripts/deploy.py --compose exec engine python -m mycomfy.cli runtime ready
python3 scripts/deploy.py --compose logs --tail 100 engine worker
~~~

`ready` 在至少一个当前配置的代次可接单、心跳有效、端口归属正确且 ComfyUI 队列可读时成功。运行管理器和 Docker 就绪检查都用 1 秒超时请求 `GET /prompt`，验证 `exec_info.queue_remaining` 是非负整数。队列空闲和忙碌时都能通过探测。Worker 通过 `/queue` 和 `/history/{prompt_id}` 跟踪具体任务。要验证 H3 在目标显卡上能生成视频，分别用已发布的文生、图生策略提交一个短样本，图生样本上传首帧。核验两个视频均可下载、播放，并记录实际显存峰值与耗时。[目标机生成验收](gpu-acceptance.md)

升级前逐实例排空，使用监控页上显示的实例 ID 和当前代次。下面的示例对应 gpu0 的第一代。待在途执行结算、原进程树退出后，更新部署配置中 `engine` 的相应版本并执行部署脚本。若控制台正在等待已暂停实例就绪，可在另一终端按当前代次恢复实例。

~~~sh
python3 scripts/deploy.py --compose exec engine python -m mycomfy.cli runtime drain --instance gpu0 --generation 1
# 完成排空、部署和核对后，按当前代次恢复。
python3 scripts/deploy.py --compose exec engine python -m mycomfy.cli runtime resume --instance gpu0 --generation 1
~~~

Compose 正常停止 engine 时会请求各代次退出并等待监护程序写入 `ended.json`。

### 掉电后的代次恢复

原 GPU 主机掉电或 WSL 内核重启后，管理器会核对本机实例目录、`launch.json` 中的代次和 `identity.json` 中的内核启动标识。若有 `child.json`，子进程也须属于同一旧内核。确认内核已经更换后，管理器补写原因是 `HOST_REBOOT_CONFIRMED` 的 `ended.json`，记录新旧启动标识和确认时间，并解除该代次因进程退出无法确认而产生的隔离。升级前已进入这种隔离的实例同样适用。

若旧代次仍占用任务槽位，Worker 先按原执行记录核对产物。已获准提交的执行若留下完整输出，会进入校验与发布。缺少必需输出时，该次执行记为 `LOST`。释放槽位后，已配置且未暂停的实例会启动下一代。同一内核中仅重建容器、身份记录冲突或从备份恢复到其他机器，都不能据此确认原进程树已退出。遇到这类隔离，请按运维流程核实后再执行 `runtime resolve`。[运维说明](operations.md)

## 隔离和验收

运行管理重启时通过持久身份重新接管已启动的代次。每代工作目录保存启动、进程身份、子进程退出凭据和轮转日志。隔离时核对该代次的进程树、所有在途发送及产物；在它们都已结清后，记录核对依据并恢复。

~~~sh
python3 scripts/deploy.py --compose exec engine python -m mycomfy.cli runtime resolve --instance gpu0 --generation 1 --reason '实际处置记录编号及核实依据'
~~~

目标机验收记录应包含驱动、CUDA、PyTorch、引擎提交、节点提交和模型摘要，以及真实生成的必需视频、断线核对和取消行为。用同一输入和种子测定等待、生成、后处理、发布耗时及显存峰值，再填写部署时限。
