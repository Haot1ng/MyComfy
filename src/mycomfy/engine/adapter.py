"""ComfyUI 单次提交协议及每次执行的独立工作区。"""

from __future__ import annotations

import copy
import hashlib
import mimetypes
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from mycomfy.artifacts.storage import materialized_input_name


class Rejected(Exception):
    """The engine explicitly rejected the workflow before queueing it."""


class NotSent(Exception):
    """A new connection could not be established, so the POST was never sent."""


class Unconfirmed(Exception):
    """The sender must reconcile this attempt before deciding its outcome."""


@dataclass
class Observation:
    state: str
    outputs: dict = field(default_factory=dict)
    stage: str | None = None


class ComfyAdapter:
    def __init__(self, runtime: dict, *, transport=None):
        """绑定已配置的回环引擎，禁用代理及自动跳转。"""
        url = urlsplit(runtime["base_url"])
        if url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("ComfyUI must use a loopback HTTP endpoint")
        self.client = httpx.Client(
            base_url=runtime["base_url"],
            transport=transport,
            trust_env=False,
            timeout=10,
            follow_redirects=False,
        )

    def close(self):
        """释放本次轮询持有的 HTTP 连接。"""
        self.client.close()

    def submit(self, prompt_id, workflow, execution_id):
        """提交一次带持久关联标识的工作流，并区分明确拒收与结果未知。"""
        # 即使没有收到响应字节，读写异常也可能发生在引擎已接收之后。
        try:
            response = self.client.post(
                "/prompt",
                json={
                    "prompt_id": prompt_id,
                    "client_id": execution_id,
                    "prompt": workflow,
                    "extra_data": {"mycomfy_execution_id": execution_id},
                },
            )
        except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
            raise NotSent("CONNECTION_NOT_ESTABLISHED") from exc
        except httpx.TransportError as exc:
            raise Unconfirmed("SUBMISSION_TRANSPORT_ERROR") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise Unconfirmed("INVALID_RECEIPT") from exc
        if not isinstance(body, dict):
            raise Unconfirmed("INVALID_RECEIPT")
        if (
            response.status_code == 400
            and isinstance(body.get("error"), dict)
            and body["error"].get("type")
            in {"prompt_outputs_failed_validation", "invalid_prompt", "prompt_no_outputs"}
        ):
            raise Rejected("WORKFLOW_REJECTED")
        if not response.is_success or body.get("prompt_id") != prompt_id:
            raise Unconfirmed("PROMPT_ID_UNCONFIRMED")

    def observe(self, prompt_id):
        """按原提交标识查询历史和队列，只有明确的完成记录才产生终态。"""
        try:
            reply = self.client.get(f"/history/{prompt_id}")
            reply.raise_for_status()
            history = reply.json().get(prompt_id)
            if isinstance(history, dict):
                status = history.get("status", {})
                messages = status.get("messages", [])
                if any(item and item[0] == "execution_interrupted" for item in messages):
                    return Observation("INTERRUPTED", history.get("outputs", {}))
                if status.get("status_str") == "error":
                    return Observation("FAILED", history.get("outputs", {}))
                if status.get("completed") is True and status.get("status_str") == "success":
                    return Observation("SUCCEEDED", history.get("outputs", {}))
            reply = self.client.get("/queue")
            reply.raise_for_status()
            queue = reply.json()
            for field, stage in (("queue_running", "running"), ("queue_pending", "engine_queue")):
                if any(
                    isinstance(row, list) and len(row) > 1 and row[1] == prompt_id
                    for row in queue.get(field, [])
                ):
                    return Observation("RUNNING", stage=stage)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise Unconfirmed("ENGINE_LOOKUP_UNAVAILABLE") from exc
        return Observation("UNKNOWN")

    def interrupt(self, prompt_id):
        """按提交标识请求定向中断，由后续核对确认实际执行结束。"""
        # 调用方只在目标部署已验证定向中断能力时使用这个端点。
        self.client.post("/interrupt", json={"prompt_id": prompt_id}).raise_for_status()


def confined(root, *parts):
    """解析真实路径并将文件限定在指定工作目录内。"""
    base = Path(root).resolve()
    target = base.joinpath(*parts).resolve()
    if not target.is_relative_to(base):
        raise ValueError("Path is outside its managed directory")
    return target


def prepare_workflow(execution, content, inputs, assets, store):
    """克隆固定版本工作流，绑定受理参数和独占输入输出路径。"""
    workflow = copy.deepcopy(content["workflow"])
    workspace = execution.workspace
    input_dir = confined(workspace["input_dir"], execution.id)
    output_dir = confined(workspace["output_dir"], execution.id)
    input_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise ValueError("Unsubmitted execution already has output files")

    def materialize(asset_id):
        """通过已授权的素材记录将输入复制到引擎可读目录。"""
        asset = assets[asset_id]
        source = store.path(asset.object_key)
        target = confined(input_dir, materialized_input_name(asset))
        stage = target.with_suffix(".part")
        shutil.copyfile(source, stage)
        os.replace(stage, target)
        return f"{execution.id}/{target.name}"

    for binding in content["bindings"]:
        if binding["input"] not in inputs:
            continue
        value = inputs[binding["input"]]
        if binding["kind"] == "asset":
            value = (
                [materialize(v) for v in value] if isinstance(value, list) else materialize(value)
            )
        workflow[binding["node_id"]]["inputs"][binding["node_input"]] = value
    # 多个角色允许读取同一保存节点的不同历史键，共用节点输出目录。
    for contract in content["output_contract"]:
        workflow[contract["node_id"]]["inputs"][contract["path_input"]] = (
            f"{execution.id}/{node_directory(contract['node_id'])}/output"
        )
    return workflow


def node_directory(node_id):
    """将节点标识映射为路径安全且稳定的目录名。"""
    return hashlib.sha256(node_id.encode()).hexdigest()


def output_paths(execution, contracts, history):
    """按声明的节点与历史键解析产物，并验证每项都归属本次执行。"""
    root = Path(execution.workspace["output_dir"]).resolve()
    expected = confined(root, execution.id)
    roles = {}
    for contract in contracts:
        node = history.get(contract["node_id"], {})
        groups = (
            [node.get(contract["history_key"], [])]
            if contract.get("history_key")
            else node.values()
        )
        paths = []
        for group in groups:
            if not isinstance(group, list):
                continue
            for item in group:
                if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                    continue
                if item.get("type", "output") != "output":
                    continue
                path = confined(root, item.get("subfolder", ""), item["filename"])
                if not path.is_relative_to(expected):
                    raise ValueError("Output does not belong to this execution")
                if path not in paths:
                    paths.append(path)
        roles[contract["role"]] = paths
    return roles


def completed_outputs(execution, contracts, history):
    """核对已停止写入的输出，将可信节点目录中的部分文件也交给发布器。"""
    reported = output_paths(execution, contracts, history)
    found = stopped_outputs(execution, contracts)
    # 执行目录在准备阶段已建好。整目录暂不可用时等待运维修复，不消耗生成预算。
    root = confined(execution.workspace["output_dir"], execution.id)
    root.stat()
    for contract in contracts:
        role = contract["role"]
        # 历史键精确指向角色时优先采用历史定位，避免把同节点的预览或索引文件混入视频。
        candidates = reported[role] or found.get(role, [])
        existing = []
        for path in candidates:
            try:
                path.stat()
            except FileNotFoundError:
                # 引擎已结束、所属工作区可访问且目标缺失，按角色数量判定源输出失败。
                continue
            existing.append(path)
        reported[role] = existing
    return reported


def stopped_outputs(execution, contracts):
    """仅在写进程树已确认退出时从执行独占目录恢复可证明归属的源文件。"""
    root = confined(execution.workspace["output_dir"], execution.id)
    if not root.is_dir():
        return {}
    result = {}
    for contract in contracts:
        # 同节点多角色丢失历史后不能凭文件名重建角色。
        if sum(c["node_id"] == contract["node_id"] for c in contracts) > 1:
            continue
        directory = confined(root, node_directory(contract["node_id"]))
        if directory.is_dir():
            result[contract["role"]] = sorted(
                path
                for path in directory.iterdir()
                if path.name.startswith("output")
                and mimetypes.guess_type(path.name)[0] in contract["media_types"]
                and not path.is_symlink()
                and path.is_file()
            )
    return result
