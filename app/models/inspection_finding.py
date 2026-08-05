"""Read-only SQLAlchemy mirror of the Prisma `InspectionFinding` table.

The full inspection-findings lifecycle (birth on inspection submit, per-finding
`InspectionFindingCapa`, lifecycle transitions) lives on the Next.js/Prisma side.
This backend model exists only so FastAPI can read a finding and BRIDGE it into
the universal `Capa` engine — i.e. promote an inspection finding into the same
CAPA register (with SLA, escalation, audit chain, dashboards) as every other
module. We map only the columns that bridge needs; the table is created by
Prisma, so no DDL is required here.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class InspectionFinding(Base, IdMixin):
    __tablename__ = "InspectionFinding"

    findingNumber: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    inspectionId: Mapped[str] = mapped_column(ForeignKey("Inspection.id"), nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # FindingSeverity / FindingStatus are native PG enums; read as text.
    severity: Mapped[str] = mapped_column(String, nullable=False)
    isCritical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    ownerId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    dueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
