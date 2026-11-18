# MyComfy

本地视频生成平台的 Python 服务。用户可经 HTTP 注册、上传素材、选择策略版本并提交批次。Worker 在 MySQL 登记执行意图后向本机 ComfyUI 提交一次工作流。只有文件通过契约校验、发布到正式位置并完成登记，用户才会看到成功状态。

## 云端开发

使用 Python 3.12、uv、FFmpeg 与 ffprobe。Linux 上的进程管理使用文件锁和进程树信息。完整集成测试还需要 Docker。

```bash
uv sync --locked
uv run --locked pytest -q tests/unit
uv run --locked python scripts/test_mysql.py
```

最后一条命令在回环端口启动临时 MySQL 8.4，为每条数据库集成用例创建独立库，完成后回收容器。所有云端测试可在 CPU 上运行。其中一条跨模块测试会故意丢弃引擎提交回执，重建 Worker，并核对两个可解码 MP4 的完整下载和 Range 内容。可单独运行这条用例。

```bash
uv run --locked python scripts/test_mysql.py -q tests/test_cloud_delivery.py
```

## 启动服务

准备 MySQL 8.4 业务库和具有迁移权限的账号，并设置以下环境变量。数据库 URL 中的特殊字符须按 URL 规则编码。维护凭据文件存放随机令牌，文件模式设为 `0600`。

| 环境变量 | 用途 |
| --- | --- |
| `MYCOMFY_DATABASE_URL` | SQLAlchemy 连接地址，形式为 `mysql+pymysql://USER:PASSWORD@HOST:3306/DATABASE` |
| `MYCOMFY_DATA_ROOT` | API 与 Worker 共用的正式文件和运行数据目录 |
| `MYCOMFY_PUBLIC_ORIGIN` | 用户网页与用户 API 的同源 HTTPS 地址 |
| `MYCOMFY_MAINTENANCE_TOKEN_FILE` | 本机维护 API 的令牌文件路径 |
| `MYCOMFY_ENGINE_CONFIG` | 自动部署生成的 `engines.json` 路径 |
| `MYCOMFY_CAPACITY_CONFIG` | 部署方实测后填写的卷水位、队列与用户配额配置 |

先执行数据库迁移，再分别运行 API、引擎管理和 Worker。后三条命令是独立的长驻进程。

```bash
uv run --locked mycomfy migrate
uv run --locked mycomfy api --port 8000 --local-port 8001
uv run --locked mycomfy runtime serve
uv run --locked mycomfy worker
```

用户 API 位于 `/api/v1`。同源反向代理转发业务端口 8000，SSE 连接需关闭代理缓冲并将空闲超时设为大于心跳周期。回环端口 8001 提供本机只读监控及维护操作。维护端点接受 `Authorization: Bearer <token>`。登录后的写请求携带有效 Cookie、`Origin`、`X-Session-Generation` 和 `X-CSRF-Token`。

## 部署和验收

- 对照规格 A01–A07、R01–R09 查看已在云端验证的行为及对应测试。[云端验证记录](docs/cloud-acceptance.md)
- 在 GPU 目标机上按清单安装 ComfyUI、固定版本节点、Python 环境和模型，生成实例配置。[引擎部署](docs/engine-deployment.md)
- 停止接单、核对实例代次、查看运行指标或演练成套备份恢复。[运维与恢复](docs/operations.md)
- 在已部署工作流和目标硬件上验证真实视频、下载摘要、耗时与吞吐，记录人工画质及引擎能力结论。[目标机验收](docs/gpu-acceptance.md)
