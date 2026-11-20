# 在 GPU 机器上部署 ComfyUI

默认通过容器部署。engine 镜像携带 Git、编译工具、Python、venv 和 pip，GPU 设备与驱动库由宿主机注入。运行管理使用 Linux 的 pidfd、子进程和 /proc 信息，与 ComfyUI 运行在同一个容器。启动前准备数据库、持久目录和 GPU 后端的步骤见项目首页。[全栈 Compose](../README.md#启动服务)

## 安装清单

将下例保存为 `var/deployment/deployment.json`，填入实际引擎及节点的 40 位 Git 提交号、模型和依赖锁的 SHA-256、引擎容器可见的 GPU 编号或 GPU UUID。模型来源支持 HTTPS 和相对于清单的文件。例如，`video.safetensors` 放在该目录的 `model-import/` 下，容器通过 `/deployment/model-import/` 读取。models 的键为策略 requirements.models 所用标识，path 的首级目录为 ComfyUI 模型分类。nodes 的键会成为 custom_nodes 下的目录名。以下秒数是初始配置示例。

~~~json
{
  "comfyui": {"url": "https://github.com/Comfy-Org/ComfyUI.git", "commit": "<固定提交>"},
  "nodes": {
    "VideoHelperSuite": {"url": "<节点仓库地址>", "commit": "<固定提交>"}
  },
  "models": {
    "video-checkpoint": {
      "path": "checkpoints/video.safetensors",
      "source": "model-import/video.safetensors",
      "sha256": "<模型 SHA-256>"
    }
  },
  "requirements_lock": "requirements-gpu.lock",
  "requirements_sha256": "<依赖锁 SHA-256>",
  "instances": [{
    "id": "gpu0", "port": 8188, "gpu": "0", "directed_interrupt": false,
    "observation_seconds": 120, "timeout_seconds": 3600, "reconcile_seconds": 120
  }]
}
~~~

用目标引擎和节点的 requirements.txt、CUDA 对应的 PyTorch 轮子及其传递依赖生成带哈希的 pip 锁文件。将清单和锁文件随部署备份保存。首次试运行使用单实例，定向中断在实际引擎版本验证后启用。实例可在 strategy_timeouts 中按策略系列键或版本 ID 填写独立的 observation_seconds、timeout_seconds 和 reconcile_seconds，三个字段均以秒计。

## 容器运行与验收

宿主机运行 `python3 scripts/deploy.py` 即可部署全栈。脚本会在临时 `engine` 容器里执行 `mycomfy-deploy-engine`。如果已进入配好挂载的 `engine` 容器 Shell，也可以直接运行这条命令安装引擎，或用 `mycomfy-deploy-engine --check` 单独校验清单和依赖锁。安装时会在所选 GPU 上验证 `torch.cuda.is_available()`，将实际 CUDA、PyTorch 和 GPU 信息写入生成的清单。每个实例的 `gpu` 字段决定 ComfyUI 子进程使用哪张卡。实例端口由 Worker 在同一个容器网络命名空间内访问。

~~~sh
docker compose exec engine nvidia-smi -L
docker compose exec engine python -m mycomfy.cli runtime ready
docker compose logs --tail 100 engine worker
~~~

`ready` 在至少一个当前配置的代次有有效心跳、端口归属正确且 ComfyUI 响应时成功。正式验收需要用实际模型、固定工作流和已发布策略提交任务并下载视频。[目标机生成验收](gpu-acceptance.md)

升级前逐实例排空，使用监控页上显示的实例 ID 和当前代次。下面的示例对应 gpu0 的第一代。待在途执行结算、原进程树退出后，运行 `docker compose exec engine mycomfy-deploy-engine` 更新清单，再恢复实例。管理命令在运行中的 engine 容器内执行，以便读取真实进程身份和退出凭据。

~~~sh
docker compose exec engine python -m mycomfy.cli runtime drain --instance gpu0 --generation 1
# 完成排空、部署和核对后，按当前代次恢复。
docker compose exec engine python -m mycomfy.cli runtime resume --instance gpu0 --generation 1
~~~

Compose 正常停止 engine 时会请求各代次退出并等待落盘凭据。强制终止或掉电后，缺失退出凭据的实例可能进入隔离。核对实际执行、进程与文件后，再在原 engine 服务内执行 `runtime resolve`。[运维说明](operations.md)

从已有宿主机引擎切换时，先在原运行管理中排空实例，等候其任务结算和 `ended.json`，再停止原管理进程。保留数据库及原代次目录，按容器内路径重新部署清单。对于已暂停的实例，先用 `docker compose up -d` 启动服务，在 engine 容器内核对并 `resume` 当前代次，随后用 `runtime ready` 确认接单。

## 在宿主机直接运行

~~~sh
uv run --locked mycomfy deploy deployment.json --check
uv run --locked mycomfy deploy deployment.json --root /srv/mycomfy/engines
export MYCOMFY_ENGINE_CONFIG=/srv/mycomfy/engines/engines.json
uv run --locked mycomfy runtime apply
uv run --locked mycomfy runtime serve
~~~

apply 和 serve 使用平台的 MYCOMFY_DATABASE_URL 与 MYCOMFY_DATA_ROOT。先执行平台数据库迁移。deploy 对已有模型核验摘要，将新组件部署到版本独立的目录，最后原子写出实例配置。节点升级或驱动调整前，从本机监控获取实例代次，先排空正在使用它的任务。

~~~sh
uv run --locked mycomfy runtime drain --instance gpu0 --generation 1
~~~

排空后，核对监控中槽位已结清、旧进程树已退出。更新部署清单后重新执行 deploy。serve 自动读取新的 engines.json。在当前代次恢复接单，管理程序按配置启动下一代。

~~~sh
uv run --locked mycomfy runtime resume --instance gpu0 --generation 1
~~~

## 隔离和验收

运行管理重启时通过持久身份重新接管已启动的代次。每代工作目录保存启动、进程身份、子进程退出凭据和轮转日志。隔离时核对该代次的进程树、所有在途发送及产物；在它们都已结清后，记录核对依据并恢复。

~~~sh
uv run --locked mycomfy runtime resolve --instance gpu0 --generation 1 --reason '实际处置记录编号及核实依据'
~~~

目标机验收记录应包含驱动、CUDA、PyTorch、引擎提交、节点提交和模型摘要，以及真实生成的必需视频、断线核对和取消行为。用同一输入和种子测定等待、生成、后处理、发布耗时及显存峰值，再填写部署时限。
