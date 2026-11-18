"""在临时 MySQL 8.4 容器内运行项目集成测试，并在退出时回收容器。"""

import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError


def run_tests(args):
    """启动独占回环端口的数据库，等待可执行 SQL 后运行 pytest。"""
    repo = Path(__file__).resolve().parents[1]
    password = secrets.token_hex(24)
    env = {**os.environ, "MYSQL_ROOT_PASSWORD": password}
    container = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "-p",
            "127.0.0.1::3306",
            "-e",
            "MYSQL_ROOT_PASSWORD",
            "-e",
            "MYSQL_ROOT_HOST=%",
            "mysql:8.4",
        ],
        text=True,
        env=env,
    ).strip()
    try:
        address = subprocess.check_output(["docker", "port", container, "3306/tcp"], text=True)
        port = int(address.strip().rsplit(":", 1)[1])
        url = f"mysql+pymysql://root:{password}@127.0.0.1:{port}/mysql"
        engine = create_engine(url, connect_args={"connect_timeout": 2})
        try:
            deadline = time.monotonic() + 90
            while True:
                try:
                    with engine.connect() as connection:
                        connection.execute(text("SELECT 1"))
                    break
                except OperationalError:
                    if time.monotonic() > deadline:
                        raise RuntimeError("测试 MySQL 未在 90 秒内就绪") from None
                    time.sleep(1)
        finally:
            engine.dispose()
        # 只把临时测试连接串交给子进程。pytest fixture 自行创建每条用例的数据库。
        return subprocess.run(
            [sys.executable, "-m", "pytest", *(args or ["-q"])],
            cwd=repo,
            env={**os.environ, "MYCOMFY_TEST_DATABASE_URL": url},
            check=False,
        ).returncode
    finally:
        subprocess.run(["docker", "stop", container], stdout=subprocess.DEVNULL, check=True)


if __name__ == "__main__":
    sys.exit(run_tests(sys.argv[1:]))
