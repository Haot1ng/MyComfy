# Linux GPU 机器上的 ComfyUI 部署

部署脚本使用系统已有的 Git、含 venv 和 pip 的 Python、NVIDIA 驱动与 nvidia-smi。运行管理需要 Linux 5.3 或更高版本的 pidfd、可读取的同用户 /proc 信息，以及引擎本机的 MySQL 和文件目录。业务 API、Worker、运行管理按 V1 单机拓扑部署。云端测试使用引擎替身；目标机的实际模型、节点、GPU 资源和策略观察时限按目标机验收填写。

## 安装清单

把下例保存为 deployment.json，替换引擎及节点的 40 位 Git 提交号、模型和依赖锁的 SHA-256、机器上实际的路径与 GPU。模型来源支持 HTTPS 和本机文件，相对路径以清单目录为起点。models 的键为策略 requirements.models 所用标识，path 的首级目录为 ComfyUI 模型分类。nodes 的键会成为 custom_nodes 下的目录名。以下秒数是初始配置示例。

~~~json
{
  "python": "/usr/bin/python3.12",
  "comfyui": {"url": "https://github.com/Comfy-Org/ComfyUI.git", "commit": "<固定提交>"},
  "nodes": {
    "VideoHelperSuite": {"url": "<节点仓库地址>", "commit": "<固定提交>"}
  },
  "models": {
    "video-checkpoint": {
      "path": "checkpoints/video.safetensors",
      "source": "/srv/model-import/video.safetensors",
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
