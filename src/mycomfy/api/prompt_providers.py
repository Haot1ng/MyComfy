"""管理本机提示词服务商，并为公开优化接口读取一次完整的服务端配置。"""

import re
from dataclasses import dataclass, field
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy import select

from mycomfy.database.prompt_models import PromptProvider, PromptProviderSelection
from mycomfy.database.session import new_id
from mycomfy.errors import ApiError, not_found

from .prompt_provider_keys import decrypt_key, encrypt_key

PREFIX = "/local/v1/prompt-providers"


class ProviderDefinition(BaseModel):
    """管理员写入的 OpenAI 兼容聊天服务配置；密钥字段不参与错误信息的格式化。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    base_url: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    api_key: SecretStr | None = None
    clear_api_key: bool = False

    @field_validator("name", "model")
    @classmethod
    def single_line(cls, value):
        """名称和模型标识需能完整显示为一行文本。"""
        if not value.isprintable():
            raise ValueError("Expected printable text")
        return value

    @field_validator("base_url")
    @classmethod
    def normalize_url(cls, value):
        """校验自建或托管 HTTP 服务的 API 基址，拒绝 URL 内嵌凭据和参数。"""
        if (
            not value.isprintable()
            or any(c.isspace() for c in value)
            or any(c in value for c in ("?", "#", "\\"))
        ):
            raise ValueError("Invalid API base URL")
        # 与实际 HTTP 客户端采用相同的地址规范化，保留用户配置的版本路径。
        try:
            parts = urlsplit(value)
            url = httpx.URL(value)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or "@" in parts.netloc
                or not url.host
                or parts.port == 0
            ):
                raise ValueError
        except (httpx.InvalidURL, ValueError):
            raise ValueError("Invalid API base URL") from None
        normalized = str(url).rstrip("/")
        if len(normalized) > 2048:
            raise ValueError("API base URL is too long")
        return normalized

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value):
        """只允许可直接写入 Bearer 请求头的可见 ASCII 字符。"""
        if value is not None:
            key = value.get_secret_value()
            if len(key) > 2048 or any(not 33 <= ord(char) <= 126 for char in key):
                raise ValueError("Invalid provider credential")
        return value

    @model_validator(mode="after")
    def explicit_key_change(self):
        """拒绝同一次写入既清除又更换密钥。"""
        if self.clear_api_key and self.api_key and self.api_key.get_secret_value():
            raise ValueError("Choose either replacing or clearing the key")
        return self


@dataclass(frozen=True)
class ActivePromptProvider:
    """单个公开请求使用的服务端配置快照，打印对象不会显示实际密钥。"""

    id: str
    name: str
    base_url: str
    model: str
    api_key: str | None = field(repr=False)


def active_prompt_provider(db, settings):
    """用一次联表读取当前选择及全部配置，返回可在事务外使用的独立快照。"""
    provider = db.scalar(
        select(PromptProvider)
        .join(PromptProviderSelection, PromptProviderSelection.provider_id == PromptProvider.id)
        .where(PromptProviderSelection.id == 1)
    )
    if provider is None:
        return None
    key = (
        decrypt_key(settings.data_root, provider.encrypted_api_key)
        if provider.encrypted_api_key
        else None
    )
    return ActivePromptProvider(provider.id, provider.name, provider.base_url, provider.model, key)


def is_prompt_provider_operation(request):
    """识别可经同源服务商管理页面授权的确切资源和方法。"""
    path = request.scope["path"]
    if path == PREFIX:
        return request.method in {"GET", "POST"}
    if request.method == "PUT":
        return bool(re.fullmatch(PREFIX + r"/[^/]+", path))
    return request.method == "POST" and bool(
        re.fullmatch(PREFIX + r"/[^/]+/(activate|disable)", path)
    )


def _payload(provider, active_id):
    """只向维护页面提供可显示的字段和密钥存在标记。"""
    return {
        "id": provider.id,
        "name": provider.name,
        "base_url": provider.base_url,
        "model": provider.model,
        "has_api_key": provider.encrypted_api_key is not None,
        "active": provider.id == active_id,
    }


def _selection(db):
    """锁定迁移创建的唯一选择行，使服务商修改和激活操作依次提交。"""
    selection = db.get(PromptProviderSelection, 1, with_for_update=True)
    if selection is None:
        raise ApiError(503, "PROMPT_PROVIDER_UNAVAILABLE", "Provider selection is unavailable")
    return selection


def _provider(db, provider_id):
    """在持有选择行锁的事务中读取目标服务商，缺失时按本机 API 约定返回 404。"""
    provider = db.get(PromptProvider, provider_id)
    if provider is None:
        raise not_found()
    return provider


def build_prompt_provider_router(settings, sessions, audit):
    """注册服务商目录、凭据编辑和当前服务切换接口，沿用本机鉴权和事务审计。"""
    router = APIRouter(prefix=PREFIX)

    @router.get("")
    def list_providers():
        """在同一个 SQL 快照里列出服务商和当前选择，禁止返回存储的密钥。"""
        with sessions() as db:
            rows = db.execute(
                select(PromptProvider, PromptProviderSelection.provider_id)
                .outerjoin(
                    PromptProviderSelection,
                    PromptProviderSelection.provider_id == PromptProvider.id,
                )
                .order_by(PromptProvider.name, PromptProvider.id)
            )
            return {"items": [_payload(provider, active_id) for provider, active_id in rows]}

    @router.post("", status_code=201)
    def create_provider(body: ProviderDefinition, request: Request):
        """保存新服务商及可选凭据，待管理员显式启用后用于新请求。"""
        with sessions.begin() as db:
            _selection(db)
            provider = PromptProvider(
                id=new_id("provider"), name=body.name, base_url=body.base_url, model=body.model
            )
            if body.api_key and body.api_key.get_secret_value():
                provider.encrypted_api_key = encrypt_key(
                    db, settings.data_root, body.api_key.get_secret_value()
                )
            db.add(provider)
            request.state.target = provider.id
            audit(db, request, 201)
            return _payload(provider, None)

    @router.put("/{provider_id}")
    def update_provider(provider_id: str, body: ProviderDefinition, request: Request):
        """保留省略的密钥；更换服务地址时要求明确替换或清除原凭据。"""
        with sessions.begin() as db:
            selection = _selection(db)
            provider = _provider(db, provider_id)
            request.state.target = provider.id
            key = body.api_key.get_secret_value() if body.api_key else None
            if key:
                provider.encrypted_api_key = encrypt_key(db, settings.data_root, key)
            elif body.clear_api_key:
                provider.encrypted_api_key = None
            elif provider.encrypted_api_key and provider.base_url != body.base_url:
                raise ApiError(
                    422,
                    "PROVIDER_KEY_REQUIRED",
                    "Replace or clear the key when changing the API base URL",
                )
            provider.name, provider.base_url, provider.model = body.name, body.base_url, body.model
            audit(db, request, 200)
            return _payload(provider, selection.provider_id)

    @router.post("/{provider_id}/activate")
    def activate_provider(provider_id: str, request: Request):
        """在同一事务中切换唯一选择指针，并立即对随后开始的优化请求生效。"""
        with sessions.begin() as db:
            selection = _selection(db)
            provider = _provider(db, provider_id)
            selection.provider_id = provider.id
            request.state.target = provider.id
            audit(db, request, 200)
            return _payload(provider, selection.provider_id)

    @router.post("/{provider_id}/disable")
    def disable_provider(provider_id: str, request: Request):
        """仅停用仍被选中的目标服务商，避免过期页面停用刚切换的新服务。"""
        with sessions.begin() as db:
            selection = _selection(db)
            provider = _provider(db, provider_id)
            if selection.provider_id == provider.id:
                selection.provider_id = None
            request.state.target = provider.id
            audit(db, request, 200)
            return _payload(provider, selection.provider_id)

    return router
