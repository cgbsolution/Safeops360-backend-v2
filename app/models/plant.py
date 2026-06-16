from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin

if TYPE_CHECKING:
    from app.models.user import User


# Plant + Area only have createdAt in the Prisma schema (no updatedAt) —
# don't use TimestampMixin which would also add an updatedAt column.
# Mixing one in here would cause queries to reference a non-existent column.
class Plant(Base, IdMixin):
    __tablename__ = "Plant"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    location: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    unitType: Mapped[str] = mapped_column(String, nullable=False)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    users: Mapped[list[User]] = relationship(back_populates="plant")
    areas: Mapped[list[Area]] = relationship(back_populates="plant", cascade="all, delete-orphan")


class Area(Base, IdMixin):
    __tablename__ = "Area"

    name: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    plant: Mapped[Plant] = relationship(back_populates="areas")
