from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class Manhours(Base, IdMixin):
    __tablename__ = "Manhours"
    __table_args__ = (UniqueConstraint("plantId", "year", "month", name="uq_manhours_period"),)

    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False, index=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)

    headcount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manhoursWorked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contractorManhours: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ltiCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mtcCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fatalCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lostDays: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    ltifr: Mapped[float | None] = mapped_column(Numeric(10, 4))
    trir: Mapped[float | None] = mapped_column(Numeric(10, 4))
    severityRate: Mapped[float | None] = mapped_column(Numeric(10, 4))

    submittedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    submittedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
