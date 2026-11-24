"""保存管理员配置的提示词服务商、当前选择及模板级提示词设置。"""

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import ID, Base


class PromptProvider(Base):
    __tablename__ = "prompt_providers"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    base_url: Mapped[str] = mapped_column(String(2048))
    model: Mapped[str] = mapped_column(String(200))
    encrypted_api_key: Mapped[str | None] = mapped_column(Text)


class PromptProviderSelection(Base):
    __tablename__ = "prompt_provider_selection"
    __table_args__ = (CheckConstraint("id = 1", name="ck_prompt_provider_singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    provider_id: Mapped[str | None] = mapped_column(ForeignKey("prompt_providers.id"))


class TemplatePromptSettings(Base):
    """一条记录覆盖该系列全部版本；已有记录的空值表示管理员明确关闭扩写。"""

    __tablename__ = "template_prompt_settings"

    strategy_key: Mapped[str] = mapped_column(ForeignKey("strategy_series.key"), primary_key=True)
    config: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
