from datetime import datetime

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from .models import ID, TIME, Base


class CapacityGate(Base):
    __tablename__ = "capacity_gates"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    paused: Mapped[bool] = mapped_column(default=False)


class DiskReservation(Base):
    __tablename__ = "disk_reservations"
    owner: Mapped[str] = mapped_column(ID, primary_key=True)
    device: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    user_id: Mapped[str] = mapped_column(ID, index=True)
    size: Mapped[int] = mapped_column(BigInteger)
    target_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    upload_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(TIME)
