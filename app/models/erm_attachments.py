"""ERM attachment models — supporting documents on an EnterpriseRisk and
evidence files on a Control.

Cloned from `IncidentAttachment` (app/models/incident.py). Two-phase Supabase
signed-URL upload; the row is created at init and captioned at complete.

Parents (EnterpriseRisk, Control) are referenced by plain-String ForeignKey.
Deliberately NO back_populates on the parent side — the parent models are
off-limits to this change, so the relationship is one-directional here.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin
from app.models.user import User


class RiskAttachment(Base, IdMixin):
    __tablename__ = "RiskAttachment"

    riskId: Mapped[str] = mapped_column(
        ForeignKey("EnterpriseRisk.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String, nullable=False)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    storagePath: Mapped[str] = mapped_column(String, nullable=False)
    fileSize: Mapped[int] = mapped_column(Integer, nullable=False)
    mimeType: Mapped[str] = mapped_column(String, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)
    uploadedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    uploadedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    uploadedBy: Mapped[User] = relationship(foreign_keys=[uploadedById], lazy="joined")


class ControlAttachment(Base, IdMixin):
    __tablename__ = "ControlAttachment"

    controlId: Mapped[str] = mapped_column(
        ForeignKey("Control.id", ondelete="CASCADE"), nullable=False, index=True
    )
    controlTestId: Mapped[str | None] = mapped_column(String)
    category: Mapped[str] = mapped_column(String, nullable=False)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    storagePath: Mapped[str] = mapped_column(String, nullable=False)
    fileSize: Mapped[int] = mapped_column(Integer, nullable=False)
    mimeType: Mapped[str] = mapped_column(String, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)
    # For review-schedule evidence — the date of the control review this file
    # substantiates.
    reviewDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploadedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    uploadedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    uploadedBy: Mapped[User] = relationship(foreign_keys=[uploadedById], lazy="joined")
