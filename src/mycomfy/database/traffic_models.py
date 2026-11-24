"""保存本机管理员覆盖的接口流量规则。"""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class TrafficRule(Base):
    """按监听器、HTTP 方法和路由模板寻址，仅管理员保存的覆盖值占用记录。"""

    __tablename__ = "traffic_rules"

    listener: Mapped[str] = mapped_column(String(8), primary_key=True)
    method: Mapped[str] = mapped_column(String(10), primary_key=True)
    path: Mapped[str] = mapped_column(String(255), primary_key=True)
    edge_rpm: Mapped[int]
    edge_burst: Mapped[int]
    user_rpm: Mapped[int]
    provider_concurrency: Mapped[int]
