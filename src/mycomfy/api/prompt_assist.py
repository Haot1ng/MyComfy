"""按已发布策略指定的字段，向启用的语言模型请求一份可编辑的提示词候选。"""

import asyncio
import json

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from mycomfy.database.models import StrategyVersion
from mycomfy.errors import ApiError, not_found
from mycomfy.generation.strategy import effective_prompt_assist

from .permissions import Permission
from .prompt_providers import active_prompt_provider

MAX_TEXT = 8000
MAX_BODY_BYTES = 128 * 1024
PROVIDER_DEADLINE = 45


class AssistDraft(BaseModel):
    """只接收页面选定的一个字段和用户已输入的原文。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    field: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=MAX_TEXT)


async def _read_draft(request):
    """即使请求分块传输也限制 JSON 体积，并拒绝缺失文本或额外的用户数据。"""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise ApiError(413, "REQUEST_TOO_LARGE", "Prompt request is too large")
        body.extend(chunk)
    try:
        draft = AssistDraft.model_validate_json(body)
    except ValidationError as exc:
        # 校验异常可能包含原文；仅给出错误类别，避免将输入带入错误或审计。
        if any(issue["type"] == "json_invalid" for issue in exc.errors(include_input=False)):
            raise ApiError(400, "INVALID_JSON", "Malformed JSON") from None
        raise ApiError(422, "VALIDATION_FAILED", "Check the selected field and text") from None
    if not draft.text.strip():
        raise ApiError(422, "VALIDATION_FAILED", "Enter some text first")
    return draft


def _upstream_error():
    """统一处理服务商拒绝、无效回执和传输错误，不向用户暴露上游内容。"""
    return ApiError(502, "PROMPT_PROVIDER_ERROR", "The prompt provider returned no usable text")


async def generate_text(provider, instructions, field, text, *, transport=None):
    """调用一次 OpenAI 兼容的 Chat Completions 接口，并从有界响应中提取纯文本。"""
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    payload = {
        "model": provider.model,
        "messages": [
            {"role": "system", "content": instructions},
            {
                "role": "user",
                "content": json.dumps({"field": field, "text": text}, ensure_ascii=False),
            },
        ],
        "stream": False,
    }

    try:
        # 限制连通等待及整次调用时长；每次操作只访问配置的端点，禁用代理继承和重定向。
        async with (
            asyncio.timeout(PROVIDER_DEADLINE),
            httpx.AsyncClient(
                timeout=httpx.Timeout(30, connect=5),
                trust_env=False,
                follow_redirects=False,
                transport=transport,
            ) as client,
            client.stream(
                "POST", provider.base_url + "/chat/completions", headers=headers, json=payload
            ) as response,
        ):
            if (
                response.status_code != 200
                or response.headers.get("Content-Encoding", "identity").lower() != "identity"
            ):
                raise _upstream_error()
            # 拒绝压缩体以避免解压膨胀；读取前逐块核验上限，不依赖远端声明的长度。
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                if len(raw) + len(chunk) > MAX_BODY_BYTES:
                    raise _upstream_error()
                raw.extend(chunk)
        choice = json.loads(raw)["choices"][0]
        candidate = choice["message"]["content"]
        if (
            not isinstance(candidate, str)
            or not candidate.strip()
            or len(candidate) > MAX_TEXT
            or choice.get("finish_reason") in ("length", "content_filter", "tool_calls")
        ):
            raise _upstream_error()
        # 明确要求有效 Unicode；损坏的转义字符会在 API 编码阶段造成不透明的 500。
        candidate.encode("utf-8")
        return candidate.strip()
    except (TimeoutError, httpx.TimeoutException):
        raise ApiError(504, "PROMPT_PROVIDER_TIMEOUT", "The prompt provider timed out") from None
    except (httpx.HTTPError, ValueError, TypeError, IndexError, KeyError, RecursionError):
        raise _upstream_error() from None


def build_prompt_assist_router(settings, sessions, authenticate):
    """注册公开端的提示词辅助接口，调用服务商期间不占用数据库连接或用户锁。"""
    router = APIRouter(prefix="/api/v1")

    def select_provider(request, version_id, field):
        """在短事务内核验身份与权限，将系列的当前扩写配置限定到本次版本可用的字段。"""
        with sessions() as db:
            authenticate(db, request, write=True, permission=Permission.SUBMIT_JOBS)
            version = db.get(StrategyVersion, version_id)
            if version is None:
                raise not_found()
            assist = effective_prompt_assist(db, version)
            if not assist or field not in assist["fields"]:
                raise ApiError(
                    422,
                    "PROMPT_ASSIST_FIELD_UNAVAILABLE",
                    "Prompt assistance is unavailable for this field",
                )
            provider = active_prompt_provider(db, settings)
            if provider is None:
                raise ApiError(503, "PROMPT_PROVIDER_UNAVAILABLE", "No prompt provider is enabled")
            return provider, assist["instructions"]

    @router.post("/strategy-versions/{version_id}/prompt-assist")
    async def suggest(version_id: str, request: Request):
        """读取用户主动提交的草稿，只返回候选内容，由当前页面决定是否采用。"""
        draft = await _read_draft(request)
        provider, instructions = await run_in_threadpool(
            select_provider, request, version_id, draft.field
        )
        # 服务商配置快照已离开数据库事务，租约只覆盖实际调用及其超时清理。
        async with request.app.state.traffic_limiter.provider_slot(
            request, provider.id, PROVIDER_DEADLINE
        ):
            text = await generate_text(provider, instructions, draft.field, draft.text)
        return {"text": text}

    return router
