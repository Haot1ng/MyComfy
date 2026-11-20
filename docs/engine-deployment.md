# 在 GPU 机器上部署 ComfyUI

默认通过容器部署。engine 镜像携带 Git、编译工具、Python、venv 和 pip，GPU 设备与驱动库由宿主机注入。运行管理使用 Linux 的 pidfd、子进程和 /proc 信息，与 ComfyUI 运行在同一个容器。启动前准备数据库、持久目录和 GPU 后端的步骤见项目首页。[全栈 Compose](../README.md#启动服务)

## 配置工作流依赖

在宿主机的 `deploy.config.json` 中编辑 `engine`。模板使用 ComfyUI 标签 `v0.3.59` 和 CUDA 12.6 的 PyTorch 2.7.1 组合，目标平台为 Linux x86_64 和 Python 3.12。使用其他显卡、驱动或工作流时，请用适配的轮子索引与版本号。精确版本包括轮子的本地版本后缀，例如 `2.7.1+cu126`。[配置模板](../deploy.config.example.json) · [ComfyUI 版本](https://github.com/Comfy-Org/ComfyUI/releases/tag/v0.3.59) · [PyTorch 版本组合](https://pytorch.org/get-started/previous-versions/)

假设工作流使用一个本地模型和 VideoHelperSuite 节点，可将 `engine.nodes`、`engine.models` 替换为以下字段。请将占位提交号换成你使用的节点版本，并将模型文件放到 `var/deployment/model-import/video.safetensors`。

~~~json
{
  "nodes": {
    "VideoHelperSuite": {
      "url": "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git",
      "version": "<已验证的40位Git提交号>"
    }
  },
  "models": {
    "video-checkpoint": {
      "source": "model-import/video.safetensors",
      "path": "checkpoints/video.safetensors"
    }
  }
}
~~~

本地 `source` 从 `paths.deployment` 指定的目录读取，脚本自动计算 SHA-256。若提供 `sha256`，脚本会核验文件与指定摘要一致。HTTPS 来源必填上游发布的 64 位小写 `sha256`。`path` 是引擎模型目录内的目标路径，首级是模型分类。模型标识（例中的 `video-checkpoint`）对应策略的 `requirements.models`。

数据库就绪后，同机部署运行 `python3 scripts/deploy.py`。分机时，先在业务机运行 `python3 scripts/deploy.py --core-only` 完成迁移，再在 GPU 机运行 `python3 scripts/deploy.py --gpu-only`。安装引擎时，临时容器会检出 ComfyUI 和节点，读取各自的 `requirements.txt`，生成带哈希的依赖锁并安装。脚本打印本次使用的锁和清单路径。两者存放在数据目录的 `engines/generated/` 下，实际运行配置为 `engines/engines.json`。相同输入重复部署时复用已解析的版本和依赖锁。[数据库与分机地址配置](../README.md#已有部署与分机部署)

## 安装清单

以下格式供已有 `.env` 和预制依赖锁的部署使用。旧版入口读取 `var/deployment/deployment.json`，其中包含实际引擎及节点的 40 位提交号、模型和锁文件的 SHA-256、引擎容器可见的 GPU 编号或 GPU UUID。相对文件路径以此清单所在目录为基准。

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

使用统一 JSON 时，运维命令可通过部署入口的 `--compose` 运行，这会用同一份配置连接现有服务。若使用自定义配置路径，把 `--config 文件路径` 放在 `--compose` 之前。安装阶段会在所选 GPU 上验证 `torch.cuda.is_available()`，将实际 CUDA、PyTorch 和 GPU 信息写入实例清单。Worker 在同一个容器网络命名空间内访问实例端口。

~~~sh
python3 scripts/deploy.py --compose exec engine nvidia-smi -L
python3 scripts/deploy.py --compose exec engine python -m mycomfy.cli runtime ready
python3 scripts/deploy.py --compose logs --tail 100 engine worker
~~~

`ready` 在至少一个当前配置的代次有有效心跳、端口归属正确且 ComfyUI 响应时成功。正式验收需要用实际模型、固定工作流和已发布策略提交任务并下载视频。[目标机生成验收](gpu-acceptance.md)

升级前逐实例排空，使用监控页上显示的实例 ID 和当前代次。下面的示例对应 gpu0 的第一代。待在途执行结算、原进程树退出后，更新 JSON 中的版本并执行部署脚本。若控制台正在等待已暂停实例就绪，可在另一终端按当前代次恢复实例。

~~~sh
python3 scripts/deploy.py --compose exec engine python -m mycomfy.cli runtime drain --instance gpu0 --generation 1
# 完成排空、部署和核对后，按当前代次恢复。
python3 scripts/deploy.py --compose exec engine python -m mycomfy.cli runtime resume --instance gpu0 --generation 1
~~~

Compose 正常停止 engine 时会请求各代次退出并等待落盘凭据。强制终止或掉电后，缺失退出凭据的实例可能进入隔离。核对实际执行、进程与文件后，再在原 engine 服务内执行 `runtime resolve`。[运维说明](operations.md)

从已有宿主机引擎切换时，先在原运行管理中排空实例，等候其任务结算和 `ended.json`，再停止原管理进程。保留数据库及原代次目录，按容器内路径重新部署清单。对于已暂停的实例，在确认 MySQL 迁移完毕后，用 `python3 scripts/deploy.py --compose up -d --no-deps engine worker` 启动 GPU 服务，在 engine 容器内核对并 `resume` 当前代次，随后用 `runtime ready` 确认接单。沿用旧版 `.env` 的部署可直接执行相同的 `docker compose` 命令，在 engine 容器中运行 `mycomfy-deploy-engine --check` 校验预制清单和依赖锁。

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
