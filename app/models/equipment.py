from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship


from app.models._base import Base, IdMixin


# Values match prisma/schema.prisma `enum InspectionFrequency`. Previous values
# (HALFYEARLY / YEARLY) didn't match the Prisma-managed enum on Postgres, so
# any INSERT/SELECT involving frequency raised "invalid input value".
class InspectionFrequency(str, enum.Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    HALF_YEARLY = "HALF_YEARLY"
    ANNUAL = "ANNUAL"


class InspectionStatus(str, enum.Enum):
    SCHEDULED = "SCHEDULED"
    DUE = "DUE"
    OVERDUE = "OVERDUE"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"


# Column shape mirrors prisma/schema.prisma `model Equipment`. The previous
# version had `type`, `isCritical`, `notes`, `isActive`, `inspectionFrequency`
# columns that do not exist in the Prisma-managed `Equipment` table — every
# query that hit the model raised UndefinedColumnError. The real columns are
# `category`, `criticality` (string), `active`, `frequency`, etc.
class Equipment(Base, IdMixin):
    __tablename__ = "Equipment"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    subCategory: Mapped[str | None] = mapped_column(String)
    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False)
    departmentId: Mapped[str | None] = mapped_column(String)
    location: Mapped[str] = mapped_column(String, nullable=False)

    # ─── Identification ─────
    make: Mapped[str | None] = mapped_column(String)
    modelNumber: Mapped[str | None] = mapped_column(String)
    serialNumber: Mapped[str | None] = mapped_column(String)
    manufacturer: Mapped[str | None] = mapped_column(String)
    commissioningDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decommissionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    statutoryRegistrationNumber: Mapped[str | None] = mapped_column(String)

    # Criticality is a string code (A/B/C/D) in Prisma — not a boolean.
    criticality: Mapped[str | None] = mapped_column(String)

    # ─── Cached inspection state ─────
    lastInspectionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nextInspectionDue: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ─── Legacy single-frequency fields (kept for back-compat) ─────
    frequency: Mapped[InspectionFrequency] = mapped_column(
        Enum(InspectionFrequency, name="InspectionFrequency", native_enum=False),
        default=InspectionFrequency.MONTHLY,
        nullable=False,
    )
    checklistTemplate: Mapped[str | None] = mapped_column(Text)

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    inspections: Mapped[list[Inspection]] = relationship(back_populates="equipment")


class Inspection(Base, IdMixin):
    __tablename__ = "Inspection"

    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    equipmentId: Mapped[str] = mapped_column(ForeignKey("Equipment.id"), nullable=False, index=True)
    # plantId is NOT NULL in Prisma — without this declared SQLAlchemy
    # omits it from INSERT and Postgres rejects with NotNullViolation.
    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False, index=True)
    inspectorId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    scheduledDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completedDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checklistResult: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(String)
    observations: Mapped[str | None] = mapped_column(Text)
    followUpRequired: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[InspectionStatus] = mapped_column(
        Enum(InspectionStatus, name="InspectionStatus", native_enum=False), default=InspectionStatus.SCHEDULED
    )
    # Prisma's Inspection has createdAt + updatedAt (the latter @updatedAt).
    # Defaults are application-managed (Prisma client side) so we replicate
    # that here — server_default for createdAt, default+onupdate for updatedAt.
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    equipment: Mapped[Equipment] = relationship(back_populates="inspections")
