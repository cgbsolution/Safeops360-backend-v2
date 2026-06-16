"""SCR — Statutory Compliance Register (SCR-01).

SQLAlchemy mirror of the Prisma `RegisterMaster` / `RegisterEntry` models.
Registers are auto-populated FROM source modules (no manual entry); entries
are immutable (void, never delete) with a full audit trail. camelCase columns
match Prisma exactly. See SCR_Module_Build_Prompt §4.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


class RegisterMaster(Base, IdMixin):
    __tablename__ = "RegisterMaster"
    __table_args__ = (
        UniqueConstraint("registerCode", "plantId", name="RegisterMaster_registerCode_plantId_key"),
    )

    registerCode: Mapped[str] = mapped_column(String, nullable=False)
    registerName: Mapped[str] = mapped_column(String, nullable=False)
    legalAct: Mapped[str] = mapped_column(String, nullable=False)
    sectionRule: Mapped[str | None] = mapped_column(String)
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    sourceModule: Mapped[str] = mapped_column(String, nullable=False)
    sourceEventType: Mapped[str] = mapped_column(String, nullable=False)
    entryFrequency: Mapped[str] = mapped_column(String, nullable=False)
    submissionFrequency: Mapped[str] = mapped_column(String, nullable=False)
    nextSubmissionDue: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastSubmittedDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submissionAuthority: Mapped[str | None] = mapped_column(String)
    authorisedSignatoryRole: Mapped[str | None] = mapped_column(String)
    retentionPeriodYears: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    complianceStatus: Mapped[str] = mapped_column(String, nullable=False, default="COMPLIANT")

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    entries: Mapped[list["RegisterEntry"]] = relationship(
        back_populates="register", cascade="all, delete-orphan"
    )


class RegisterEntry(Base, IdMixin):
    __tablename__ = "RegisterEntry"
    __table_args__ = (
        UniqueConstraint("registerId", "sourceTransactionId", name="RegisterEntry_registerId_sourceTransactionId_key"),
    )

    registerId: Mapped[str] = mapped_column(
        ForeignKey("RegisterMaster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    register: Mapped[RegisterMaster] = relationship(back_populates="entries")

    sourceTransactionId: Mapped[str] = mapped_column(String, nullable=False)
    sourceModule: Mapped[str] = mapped_column(String, nullable=False)
    sourceRef: Mapped[str | None] = mapped_column(String)
    entryDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entryCreatedBy: Mapped[str] = mapped_column(String, nullable=False, default="SYSTEM")
    entryFieldsJson: Mapped[dict] = mapped_column(JSONB, nullable=False)
    isManualCorrection: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    correctionReason: Mapped[str | None] = mapped_column(String)
    correctionApprovedBy: Mapped[str | None] = mapped_column(String)
    isVoided: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    voidReason: Mapped[str | None] = mapped_column(String)
    auditTrail: Mapped[list] = mapped_column(JSONB, nullable=False)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = ["RegisterMaster", "RegisterEntry"]
