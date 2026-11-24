"""通过管理页面即时修改整个策略系列的提示词扩写设置。"""

import re

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from mycomfy.database.models import StrategySeries, StrategyVersion
from mycomfy.database.prompt_models import TemplatePromptSettings
from mycomfy.errors import ApiError, not_found
from mycomfy.generation.state import canonical_digest
from mycomfy.generation.strategy import (
    effective_prompt_assist,
    prompt_text_fields,
    validate_prompt_settings,
)


class TemplatePromptUpdate(BaseModel):
    """要求明确传入完整设置或 null，避免空请求误关扩写。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    prompt_assist: dict | None


def is_template_prompt_settings_operation(request):
    """识别模板页面可同源读写的单个版本设置路径。"""
    return request.method in {"GET", "PUT"} and bool(
        re.fullmatch(
            r"/local/v1/strategy-versions/[^/]+/prompt-assist-settings", request.scope["path"]
        )
    )


def _etag(version, latest, override):
    """串入最新版本和未筛选的覆盖状态，使隐藏字段的变化也能令旧页面的标签失效。"""
    state = [
        version.id,
        version.content_digest,
        latest,
        override is not None,
        override.config if override else None,
    ]
    return f'"{canonical_digest(state)}"'


def _check_if_match(request, etag):
    """传入条件时依照强标签或通配符核验，程序化调用可以省略该请求头。"""
    supplied = request.headers.getlist("if-match")
    if supplied:
        candidates = {tag.strip() for header in supplied for tag in header.split(",")}
        if candidates != {"*"} and etag not in candidates:
            raise ApiError(
                412, "PROMPT_SETTINGS_CHANGED", "设置或模板版本已更新，请重新加载后再保存。"
            )


def _check_historical_fields(db, version, latest, override):
    """保护旧页面看不到的设置字段，包括尚未覆盖的最新版内置默认字段。"""
    current = (
        override.config
        if override is not None
        else db.scalar(
            select(StrategyVersion.content["prompt_assist"]).where(
                StrategyVersion.strategy_key == version.strategy_key,
                StrategyVersion.version == latest,
            )
        )
    )
    if current and set(current["fields"]) - prompt_text_fields(version.content):
        raise ApiError(
            409,
            "PROMPT_SETTINGS_VERSION_CONFLICT",
            "此版本未包含当前设置的全部字段，请加载最新模板后再保存。",
        )


def build_template_prompt_settings_router(sessions, audit):
    """为本机管理入口注册模板设置的读取与即时保存操作。"""
    router = APIRouter(prefix="/local/v1/strategy-versions")

    @router.get("/{version_id}/prompt-assist-settings")
    def get_prompt_settings(version_id: str, response: Response):
        """从同一查询快照返回生效设置和标签，供管理员保存时检验是否有人改过。"""
        with sessions() as db:
            state = db.execute(
                select(StrategyVersion, StrategySeries.latest_version, TemplatePromptSettings)
                .join_from(StrategyVersion, StrategySeries)
                .outerjoin(
                    TemplatePromptSettings,
                    TemplatePromptSettings.strategy_key == StrategySeries.key,
                )
                .where(StrategyVersion.id == version_id)
            ).one_or_none()
            if state is None:
                raise not_found()
            version, latest, override = state
            response.headers["ETag"] = _etag(version, latest, override)
            return {
                "prompt_assist": effective_prompt_assist(
                    db, version, {version.strategy_key: override}
                )
            }

    @router.put("/{version_id}/prompt-assist-settings")
    def update_prompt_settings(
        version_id: str, body: TemplatePromptUpdate, request: Request, response: Response
    ):
        """仅更新系列覆盖，保留已发布版本的内容、摘要及版本号。"""
        with sessions.begin() as db:
            version = db.get(StrategyVersion, version_id)
            if version is None:
                raise not_found()
            # 与发布新版本共用系列行锁，避免两个首次保存同时插入相同主键。
            series = db.get(StrategySeries, version.strategy_key, with_for_update=True)
            override = db.get(TemplatePromptSettings, series.key)
            _check_if_match(request, _etag(version, series.latest_version, override))
            config = validate_prompt_settings(version.content, body.prompt_assist)
            # 显式关闭适用于整个系列；非空的历史版本编辑须保护已有字段。
            if config is not None and version.version != series.latest_version:
                _check_historical_fields(db, version, series.latest_version, override)
            if override is None:
                override = TemplatePromptSettings(strategy_key=series.key)
                db.add(override)
            override.config = config
            request.state.target = version.id
            audit(db, request, 200)
            response.headers["ETag"] = _etag(version, series.latest_version, override)
            # 对当前版本已完成校验，可直接返回规范化结果；其他版本读取时按其字段筛选。
            return {"prompt_assist": config}

    return router
