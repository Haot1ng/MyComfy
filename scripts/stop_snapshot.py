"""由部署脚本在正在运行的 engine 容器内执行，取得要核验的本机代次。"""

import json
import os
import sys

from sqlalchemy import select

from mycomfy.database.models import EngineInstance, Execution
from mycomfy.database.session import make_sessions
from mycomfy.engine.runtime import ended, load_config


def snapshot():
    """在 Worker 排空后读取本机实例，拒绝停止仍可能执行已提交任务的引擎。"""
    instances = load_config(os.environ["MYCOMFY_ENGINE_CONFIG"])
    sessions = make_sessions(os.environ["MYCOMFY_DATABASE_URL"])
    targets = []
    with sessions() as db:
        for row in db.scalars(select(EngineInstance).where(EngineInstance.id.in_(instances))):
            if not row.runtime.get("run_dir"):
                continue
            if row.active_execution_id and not ended(row.runtime, row.generation):
                # 旧版 Worker 可能在已提交任务失联时退出，先保留现场供其继续对账。
                execution = db.get(Execution, row.active_execution_id)
                if execution is None or execution.authorized_at is not None:
                    sys.exit(
                        f"实例 {row.id} 仍有未核实的执行 {row.active_execution_id}，"
                        "请启动 Worker 并等待对账后重试停机。"
                    )
            targets.append({"generation": row.generation, "run_dir": row.runtime["run_dir"]})
    return targets


if __name__ == "__main__":
    print(json.dumps(snapshot()))
