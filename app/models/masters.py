"""Master / lookup tables — Department, MasterItem.
ContractorCompany is defined in app.models.epc (canonical definition with all
EPC columns). Import it from there; do NOT redefine it here.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, func  # noqa: F401
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class Department(Base, IdMixin):
    __tablename__ = "Department"
    __table_args__ = (UniqueConstraint("plantId", "name", name="Department_plantId_name_key"),)

    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    code: Mapped[str | None] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MasterItem(Base, IdMixin):
    """Generic key-value lookup. `type` discriminates between SHIFT,
    ACTIVITY_TYPE, HAZARD_CATEGORY, ENERGY_SOURCE, ROOT_CAUSE_CATEGORY."""

    __tablename__ = "MasterItem"
    __table_args__ = (UniqueConstraint("type", "code", name="MasterItem_type_code_key"),)

    type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    code: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
