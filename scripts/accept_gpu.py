"""通过用户 API 测量目标机视频交付，核对完整下载内容并留存验收证据。"""

import argparse
import getpass
import hashlib
import json
import math
import os
import re
import tempfile
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator


class Environment(BaseModel):
    """由目标机操作者记录的硬件、版本及测量条件。"""

    model_config = ConfigDict(extra="forbid", str_min_length=1)
    gpu_model: str
    vram_mib: PositiveInt
    driver: str
    comfyui_commit: str
    node_versions: dict[str, str]
    model_sha256: dict[str, str] = Field(min_length=1)
    disk_type: str
    instance_count: PositiveInt
    condition: Literal["cold", "warm"]
    preparation: str


class Task(BaseModel):
    """描述一个固定策略版本及能复现该次生成的输入。"""

    model_config = ConfigDict(extra="forbid", str_min_length=1)
    strategy_version_id: str
    inputs: dict[str, Any]
    uploads: dict[str, str | list[str]] = Field(default_factory=dict)
    input_dimensions: str
    frames: PositiveInt
    encoding: str
    seed: int


class Case(BaseModel):
    """保存一组共享测试环境、输入组合与重复次数。"""

    model_config = ConfigDict(extra="forbid")
    environment: Environment
    tasks: list[Task] = Field(min_length=1)
    repetitions: PositiveInt = 1

    @model_validator(mode="after")
    def check_case(self):
        """验证摘要格式并保证每个冷启动样本来自一次独立准备的环境。"""
        if self.environment.condition == "cold" and self.repetitions != 1:
            raise ValueError("每轮冷启动需单独准备环境，repetitions 请设为 1")
        if not all(
            re.fullmatch(r"[0-9a-fA-F]{64}", s) for s in self.environment.model_sha256.values()
        ):
            raise ValueError("model_sha256 应提供每个模型的完整 SHA-256 摘要")
        for task in self.tasks:
            if task.uploads.keys() & task.inputs.keys():
                raise ValueError("同一输入字段只能在 uploads 或 inputs 中指定一次")
        return self


def utc_now():
    """返回含 UTC 时区的采样时间。"""
    return datetime.now(UTC).isoformat()


def save_report(path, report):
    """同盘原子替换报告，避免采样中断损坏上一个完整记录。"""
    with tempfile.NamedTemporaryFile(
        dir=path.parent, mode="w", encoding="utf-8", delete=False
    ) as file:
        temp = Path(file.name)
        try:
            json.dump(report, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def route(resource, key, suffix=""):
    """编码资源 ID，并将所有带会话的请求固定到当前 API 入口。"""
    return f"/api/v1/{resource}/{quote(key, safe='')}{suffix}"


def stage_inputs(client, case, directory, report):
    """读取每个不可变版本的摘要，上传素材并记录实际提交的映射。"""
    versions, uploads = {}, {}
    tasks = []
    for index, item in enumerate(case.tasks):
        version_id = item.strategy_version_id
        if version_id not in versions:
            response = client.get(route("strategy-versions", version_id)).raise_for_status()
            version = response.json()
            if version["strategy_version_id"] != version_id or not version["accepting"]:
                raise ValueError("固定版本当前不可受理")
            versions[version_id] = {
                "content_digest": version["content_digest"],
                "version": version["version"],
                "request_id": response.headers.get("x-request-id"),
            }
        inputs = dict(item.inputs)
        for field, paths in item.uploads.items():
            names = paths if isinstance(paths, list) else [paths]
            ids = []
            for name in names:
                if name not in uploads:
                    path = directory / name
                    with path.open("rb") as file:
                        digest = hashlib.file_digest(file, "sha256").hexdigest()
                        file.seek(0)
                        response = client.post(
                            "/api/v1/assets", files={"file": (path.name, file)}
                        ).raise_for_status()
                    asset = response.json()
                    if digest != asset["digest"]:
                        raise ValueError(f"上传素材摘要不符 {name}")
                    uploads[name] = {
                        "asset_id": asset["asset_id"],
                        "sha256": digest,
                        "request_id": response.headers.get("x-request-id"),
                    }
                ids.append(uploads[name]["asset_id"])
            inputs[field] = ids if isinstance(paths, list) else ids[0]
        tasks.append(
            {
                "client_task_key": f"task-{index + 1}",
                "strategy_version_id": version_id,
                "inputs": inputs,
            }
        )
    report.update(versions=versions, uploads=uploads, submitted_tasks=tasks)
    return tasks


def probe_video(client, asset):
    """读取一个授权字节并核对范围响应，以此确定文件首次可读取的观测时间。"""
    url = route("assets", asset["asset_id"], "/content")
    with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
        response.raise_for_status()
        if (
            response.status_code != 206
            or response.headers.get("content-range") != f"bytes 0-0/{asset['size']}"
            or len(response.read()) != 1
        ):
            raise ValueError(f"文件首字节 Range 校验失败 {asset['asset_id']}")
        return response.headers.get("x-request-id")


def check_download(client, asset, media_dir=None):
    """流式核对完整下载的大小和 SHA-256，按需保留可供人工评审的样本。"""
    url = route("assets", asset["asset_id"], "/content")
    hasher, total = hashlib.sha256(), 0
    # 使用独立临时样本名，只有校验完成的完整内容才保留在报告目录。
    saved = None
    with (
        tempfile.NamedTemporaryFile(dir=media_dir, prefix="sample-", suffix=".part", delete=False)
        if media_dir
        else nullcontext(None)
    ) as output:
        tmp = Path(output.name) if output else None
        try:
            with client.stream("GET", url, params={"download": "1"}) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    hasher.update(chunk)
                    if output:
                        output.write(chunk)
            digest = hasher.hexdigest()
            verified = total == asset["size"] and digest == asset["digest"]
            if tmp and verified:
                suffix = {"video/mp4": ".mp4", "video/webm": ".webm"}.get(
                    asset["media_type"], ".video"
                )
                final = tmp.with_suffix(suffix)
                tmp.rename(final)
                saved = final.name
        finally:
            if tmp:
                tmp.unlink(missing_ok=True)
    return {
        "sha256": digest,
        "size_bytes": total,
        "verified": verified,
        "local_sample": saved,
        "request_id": response.headers.get("x-request-id"),
    }


def observe_batch(client, tasks, batch, persist, timeout, poll, media_dir):
    """按单调时钟测量批次，逐项记录状态、可读时间、执行标识和下载结果。"""
    payload = {"client_batch_key": batch["client_batch_key"], "tasks": tasks}
    start = time.monotonic()
    response = client.post("/api/v1/batches", json=payload)
    batch["request_id"] = response.headers.get("x-request-id")
    response.raise_for_status()
    submitted = response.json()
    batch["batch_id"] = submitted["batch_id"]
    by_key = {task["client_task_key"]: task["strategy_version_id"] for task in tasks}
    bindings = submitted["tasks"]
    if (
        len(bindings) != len(tasks)
        or {v["client_task_key"] for v in bindings} != set(by_key)
        or len({v["job_id"] for v in bindings}) != len(bindings)
    ):
        raise ValueError("批次返回的任务映射不完整")
    jobs = [{**item, "timeline": [], "files": []} for item in bindings]
    batch.update(jobs=jobs, acceptance_seconds=time.monotonic() - start)
    persist()
    pending = {j["job_id"]: j for j in jobs}
    while pending:
        for job_id, sample in list(pending.items()):
            snap = client.get(route("jobs", job_id)).raise_for_status()
            job = snap.json()
            if job["strategy_version_id"] != by_key[sample["client_task_key"]]:
                raise ValueError("执行绑定的策略版本与受理不一致")
            point = {
                "state": job["state"],
                "stage": job.get("stage"),
                "execution_id": job.get("current_execution_id"),
                "attempt_no": job.get("attempt_no"),
            }
            if not sample["timeline"] or any(
                sample["timeline"][-1][k] != v for k, v in point.items()
            ):
                sample["timeline"].append(
                    {
                        **point,
                        "observed_seconds": time.monotonic() - start,
                        "request_id": snap.headers.get("x-request-id"),
                    }
                )
                persist()
            if job["state"] not in {"SUCCEEDED", "CANCELLED", "FAILED"}:
                continue
            sample["state"] = job["state"]
            sample["terminal_seconds"] = time.monotonic() - start
            sample["failure"] = job.get("failure")
            if job["state"] == "SUCCEEDED":
                videos = [
                    a
                    for a in job["assets"]
                    if a["complete"] and a["available"] and a["media_type"].startswith("video/")
                ]
                if not videos:
                    raise ValueError(f"成功任务缺少可读的完整视频 {job_id}")
                for asset in videos:
                    sample["files"].append(
                        {**asset, "range_request_id": probe_video(client, asset)}
                    )
                sample["delivery_seconds"] = time.monotonic() - start
            del pending[job_id]
            persist()
        if pending:
            if time.monotonic() - start > timeout:
                raise TimeoutError("等待任务超过本轮期限，已记录受理及执行标识")
            time.sleep(min(poll, max(0, timeout - (time.monotonic() - start))))
    # 完整下载放在所有任务的可读探测之后，避免视频传输时间污染其它任务的交付延迟。
    batch["terminal_seconds"] = max(j["terminal_seconds"] for j in jobs)
    for sample in jobs:
        for asset in sample["files"]:
            asset["download"] = check_download(client, asset, media_dir)
            persist()
            if not asset["download"]["verified"]:
                raise ValueError(f"下载字节数或摘要不符 {asset['asset_id']}")
    batch["finished_at"] = utc_now()


def summarize(batches):
    """计算已校验视频的端到端分位数，以及全部观察窗口内的成功吞吐。"""
    jobs = [job for batch in batches for job in batch.get("jobs", [])]
    durations = sorted(
        job["delivery_seconds"]
        for job in jobs
        if job.get("files")
        and all(asset.get("download", {}).get("verified") for asset in job["files"])
    )
    # 采用最近秩，报告总样本数，使少量样本的 P95 有明确含义。
    sample_count = len(durations)
    window = sum(b.get("terminal_seconds", 0) for b in batches)
    complete = all("terminal_seconds" in b for b in batches)
    return {
        "total_jobs_observed": len(jobs),
        "verified_successes": sample_count,
        "failed_jobs": sum(j.get("state") == "FAILED" for j in jobs),
        "cancelled_jobs": sum(j.get("state") == "CANCELLED" for j in jobs),
        "delivery_p50_seconds": durations[math.ceil(sample_count * 0.50) - 1]
        if durations
        else None,
        "delivery_p95_seconds": durations[math.ceil(sample_count * 0.95) - 1]
        if durations
        else None,
        "verified_jobs_per_hour": sample_count * 3600 / window if complete and window else None,
    }


def main(argv=None):
    """从命令行读取部署参数、登录目标 API、执行验收并保存成功或失败证据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="用户网页的 HTTPS 源")
    parser.add_argument("--username", required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="写入一个新的 JSON 报告")
    parser.add_argument("--media-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=3600, help="单个批次最长等待秒数")
    parser.add_argument("--poll", type=float, default=1, help="任务快照采样间隔秒数")
    args = parser.parse_args(argv)
    url = urlsplit(args.url)
    if (
        not url.hostname
        or url.username
        or url.password
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
        or (
            url.scheme != "https"
            and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"})
        )
    ):
        parser.error("--url 需要用户入口的 HTTPS 源；回环测试可用 HTTP")
    if not all(math.isfinite(v) and v > 0 for v in (args.poll, args.timeout)):
        parser.error("采样间隔和等待时限必须是有限正数")
    case = Case.model_validate_json(args.case.read_bytes())
    password = os.environ.get("MYCOMFY_ACCEPT_PASSWORD") or getpass.getpass("验收账号密码 ")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # 排他创建，防止误覆盖更早的一次目标机实测。
    with args.output.open("x", encoding="utf-8"):
        pass
    origin = args.url.rstrip("/")
    report = {
        "run_id": uuid4().hex,
        "started_at": utc_now(),
        "api_origin": origin,
        "operator_supplied_case": case.model_dump(),
        "poll_seconds": args.poll,
        "status": "running",
        "batches": [],
        "requires_target_evidence": [
            "运行中与已完成时的断线核对及引擎接收次数",
            "定向中断与槽位释放证据",
            "画面质量、衔接及音画时长评审",
            "实际种子、引擎代次、GPU 显存与各卷峰值、引擎内部阶段计时",
        ],
    }

    def persist():
        """将当前批次和已观察到的事实记录到指定报告。"""
        save_report(args.output, report)

    persist()
    try:
        if args.media_dir:
            args.media_dir.mkdir(parents=True, exist_ok=True)
        with httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=30) as client:
            session = (
                client.post(
                    "/api/v1/sessions",
                    json={
                        "username": args.username,
                        "password": password,
                    },
                )
                .raise_for_status()
                .json()
            )
            client.headers.update(
                {
                    "X-CSRF-Token": session["csrf_token"],
                    "X-Session-Generation": session["session_generation"],
                }
            )
            try:
                tasks = stage_inputs(client, case, args.case.parent, report)
                for _ in range(case.repetitions):
                    batch = {"client_batch_key": str(uuid4()), "started_at": utc_now()}
                    report["batches"].append(batch)
                    persist()
                    observe_batch(
                        client, tasks, batch, persist, args.timeout, args.poll, args.media_dir
                    )
            finally:
                try:
                    client.delete("/api/v1/sessions/current").raise_for_status()
                except httpx.HTTPError:
                    report["logout"] = "unconfirmed"
        report["summary"] = summarize(report["batches"])
        expected = case.repetitions * len(case.tasks)
        report["status"] = (
            "delivery_verified" if report["summary"]["verified_successes"] == expected else "failed"
        )
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        return 130
    except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            report["error_request_id"] = exc.response.headers.get("x-request-id")
    finally:
        report["finished_at"] = utc_now()
        report["summary"] = summarize(report["batches"])
        persist()
    return 0 if report["status"] == "delivery_verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
