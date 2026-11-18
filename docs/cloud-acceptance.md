# Python 服务云端验证记录

在 Linux 云环境中以 Python 3.12.14、MySQL 8.4.13 和 FFmpeg 7.1.5 执行完整测试。命令为 `uv run --locked python scripts/test_mysql.py -q --tb=short`。本次 64 项通过，耗时 32.61 秒。Starlette 的测试客户端对当前 httpx 适配发出一条弃用提示。

| 规格用例 | 云端观察到的结果 | 可复跑的测试 |
| --- | --- | --- |
| A01–A03 | 两个不可变策略版本分别产生任务，MP4 经解码、摘要和范围读取校验。并发重放返回相同 ID，审计号各异。非法批次、跨用户素材和历史版本受理条件经过验证。 | [跨模块交付](../tests/e2e/test_cloud_delivery.py)、[用户与维护 API](../tests/integration/test_api.py) |
| A04–A07 | 同分排序和满周期加分符合规则。并发领取保留唯一占用，拒收只退款一次，重试遵守预算。取消与提交授权、执行结束及产物开放规则按事务时序裁决。 | [Worker 时序](../tests/integration/test_worker_protocol.py)、[发布恢复](../tests/integration/test_publication.py) |
| R01–R03 | 引擎接收后丢弃回执，重建 Worker 仍按原提交找回结果。旧代次消息无法释放新占用。进程树退出证据不明时保留隔离，记录核实依据后可从同一执行恢复。 | [Worker 协议](../tests/integration/test_worker_protocol.py)、[运行管理](../tests/integration/test_runtime.py) |
| R04 | 暂存中断、正式文件已存在但尚未登记、登记后缺失确认均可恢复。损坏源、取消后产物及未知归属文件按各自的持久状态处理。 | [文件发布](../tests/integration/test_publication.py)、[本地存储](../tests/integration/test_storage.py) |
| R05–R07 | 验证了事件分发事务回滚、真实 HTTP SSE 补发和旧会话断开。双端口运行时，公开端口的请求即使伪造 Host 或转发头，也无法访问本机监控和维护路由。 | [事件](../tests/integration/test_events.py)、[双监听](../tests/integration/test_serve.py)、[会话与授权](../tests/integration/test_api.py) |
| R08–R09 | 并发预留、双水位、归档空间重估和上传中断均有覆盖。隔离恢复使用两个独立 MySQL 库，恢复器从备份中的 MP4 源继续发布，能发现缺失或篡改的正式文件。 | [容量与备份](../tests/integration/test_ops.py)、[文件恢复](../tests/integration/test_publication.py) |
| T23 | 以本地 Git 源、带哈希的测试依赖和模型文件连续执行两次安装，并验证未完整写入的模型对外不可见。 | [部署测试](../tests/integration/test_deploy.py) |

数据库迁移已在独立库从空库升级到 `0003`，回退后再升级。Alembic 比对未发现 ORM 与数据库结构差异，构建的 wheel 包含运行所需的三个迁移版本。Ruff 静态检查、格式检查及 47 个 Python 文件中 389 个函数和方法的中文功能说明检查通过。

云端交付覆盖 Python API、Worker、引擎部署与运维。浏览器端用户和监控页面另行实施。目标机指派后，需用实际节点、模型和业务输入补充真实生成、断线及取消能力验证、画质评审和性能报告，并据此设置超时、容量和恢复水位。[目标机验收流程](gpu-acceptance.md)
