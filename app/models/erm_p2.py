"""Enterprise Risk Management (ERM) — Phase 2 SQLAlchemy models.

Mirrors the Phase 2 Prisma family in schema.prisma (KRI / Appetite / Compliance
/ Loss). Schema owned by Prisma (db push). camelCase columns to match the DB.
References to existing tables (User/Plant/RiskCategory/EnterpriseRisk/Incident/
Capa) are plain String columns — no FKs to those.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


def _created():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated():
    return mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)


# ── KRI ─────────────────────────────────────────────────────────────────────
class KriDefinition(Base, IdMixin):
    __tablename__ = "KriDefinition"

    kriCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    categoryId: Mapped[str] = mapped_column(String, nullable=False)
    linkedRiskIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    unit: Mapped[str] = mapped_column(String, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False, default="HIGHER_IS_WORSE")
    frequency: Mapped[str] = mapped_column(String, nullable=False, default="MONTHLY")
    feedType: Mapped[str] = mapped_column(String, nullable=False, default="MANUAL")
    metricProviderKey: Mapped[str | None] = mapped_column(String)
    apiEndpointConfig: Mapped[str | None] = mapped_column(String)
    apiToken: Mapped[str | None] = mapped_column(String)
    thresholdGreen: Mapped[float] = mapped_column(Float, nullable=False)
    thresholdAmber: Mapped[float] = mapped_column(Float, nullable=False)
    ownerId: Mapped[str] = mapped_column(String, nullable=False)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    graceDays: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    currentStatus: Mapped[str] = mapped_column(String, nullable=False, default="NO_DATA")
    currentValue: Mapped[float | None] = mapped_column(Float)

    readings: Mapped[list["KriReading"]] = relationship(back_populates="kri", cascade="all, delete-orphan")
    breaches: Mapped[list["KriBreachEvent"]] = relationship(back_populates="kri", cascade="all, delete-orphan")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_KriDefinition_cat_active", "categoryId", "isActive"),
        Index("ix_KriDefinition_feed", "feedType"),
        Index("ix_KriDefinition_owner", "ownerId"),
        Index("ix_KriDefinition_status", "currentStatus"),
    )


class KriReading(Base, IdMixin):
    __tablename__ = "KriReading"

    kriId: Mapped[str] = mapped_column(ForeignKey("KriDefinition.id", ondelete="CASCADE"), nullable=False)
    kri: Mapped[KriDefinition] = relationship(back_populates="readings")
    periodLabel: Mapped[str] = mapped_column(String, nullable=False)
    periodEnd: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    enteredBy: Mapped[str | None] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(Text)
    isCurrent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (
        UniqueConstraint("kriId", "periodLabel", name="uq_KriReading_kri_period"),
        Index("ix_KriReading_kri_current", "kriId", "isCurrent"),
        Index("ix_KriReading_kri_end", "kriId", "periodEnd"),
    )


class KriBreachEvent(Base, IdMixin):
    __tablename__ = "KriBreachEvent"

    kriId: Mapped[str] = mapped_column(ForeignKey("KriDefinition.id", ondelete="CASCADE"), nullable=False)
    kri: Mapped[KriDefinition] = relationship(back_populates="breaches")
    readingId: Mapped[str | None] = mapped_column(String)
    breachType: Mapped[str] = mapped_column(String, nullable=False)
    acknowledgedBy: Mapped[str | None] = mapped_column(String)
    acknowledgedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolutionNotes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (
        Index("ix_KriBreachEvent_kri_status", "kriId", "status"),
        Index("ix_KriBreachEvent_status", "status"),
    )


# ── Appetite ────────────────────────────────────────────────────────────────
class AppetiteStatement(Base, IdMixin):
    __tablename__ = "AppetiteStatement"

    categoryId: Mapped[str] = mapped_column(String, nullable=False)
    statementText: Mapped[str] = mapped_column(Text, nullable=False)
    appetiteLevel: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    approvedBy: Mapped[str | None] = mapped_column(String)
    approvalReference: Mapped[str | None] = mapped_column(String)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effectiveFrom: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    toleranceBands: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    breaches: Mapped[list["AppetiteBreach"]] = relationship(back_populates="appetiteStatement", cascade="all, delete-orphan")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_AppetiteStatement_cat_status", "categoryId", "status"),
        Index("ix_AppetiteStatement_status", "status"),
    )


class AppetiteBreach(Base, IdMixin):
    __tablename__ = "AppetiteBreach"

    appetiteStatementId: Mapped[str] = mapped_column(ForeignKey("AppetiteStatement.id", ondelete="CASCADE"), nullable=False)
    appetiteStatement: Mapped[AppetiteStatement] = relationship(back_populates="breaches")
    categoryId: Mapped[str] = mapped_column(String, nullable=False)
    bandType: Mapped[str] = mapped_column(String, nullable=False)
    observedValue: Mapped[float] = mapped_column(Float, nullable=False)
    thresholdValue: Mapped[float] = mapped_column(Float, nullable=False)
    triggeringEntityIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    detectedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN")
    committeeDecision: Mapped[str | None] = mapped_column(Text)
    decisionBy: Mapped[str | None] = mapped_column(String)
    reviewByDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (
        Index("ix_AppetiteBreach_cat_status", "categoryId", "status"),
        Index("ix_AppetiteBreach_status", "status"),
    )


# ── Compliance ──────────────────────────────────────────────────────────────
class LegalObligation(Base, IdMixin):
    __tablename__ = "LegalObligation"

    obligationCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    obligationType: Mapped[str] = mapped_column(String, nullable=False)
    statuteReference: Mapped[str] = mapped_column(Text, nullable=False)
    regulatorName: Mapped[str] = mapped_column(String, nullable=False)
    siteId: Mapped[str | None] = mapped_column(String)
    ownerId: Mapped[str] = mapped_column(String, nullable=False)
    frequency: Mapped[str] = mapped_column(String, nullable=False)
    validFrom: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validUntil: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    renewalLeadDays: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    conditions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    linkedRiskIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String, nullable=False, default="COMPLIANT")
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    tasks: Mapped[list["ComplianceTask"]] = relationship(back_populates="obligation", cascade="all, delete-orphan")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_LegalObligation_site_status", "siteId", "status"),
        Index("ix_LegalObligation_type", "obligationType"),
        Index("ix_LegalObligation_owner", "ownerId"),
        Index("ix_LegalObligation_status", "status"),
    )


class ComplianceTask(Base, IdMixin):
    __tablename__ = "ComplianceTask"

    obligationId: Mapped[str] = mapped_column(ForeignKey("LegalObligation.id", ondelete="CASCADE"), nullable=False)
    obligation: Mapped[LegalObligation] = relationship(back_populates="tasks")
    taskType: Mapped[str] = mapped_column(String, nullable=False)
    periodLabel: Mapped[str] = mapped_column(String, nullable=False)
    dueDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    attestedBy: Mapped[str | None] = mapped_column(String)
    attestedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verifiedBy: Mapped[str | None] = mapped_column(String)
    verifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capaId: Mapped[str | None] = mapped_column(String)
    waiverJustification: Mapped[str | None] = mapped_column(Text)
    remarks: Mapped[str | None] = mapped_column(Text)

    attachments: Mapped[list["ComplianceAttachment"]] = relationship(back_populates="task", cascade="all, delete-orphan")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (
        UniqueConstraint("obligationId", "periodLabel", name="uq_ComplianceTask_obl_period"),
        Index("ix_ComplianceTask_obl_status", "obligationId", "status"),
        Index("ix_ComplianceTask_status_due", "status", "dueDate"),
    )


class ComplianceAttachment(Base, IdMixin):
    __tablename__ = "ComplianceAttachment"

    taskId: Mapped[str] = mapped_column(ForeignKey("ComplianceTask.id", ondelete="CASCADE"), nullable=False)
    task: Mapped[ComplianceTask] = relationship(back_populates="attachments")
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    storagePath: Mapped[str] = mapped_column(String, nullable=False)
    fileSize: Mapped[int | None] = mapped_column(Integer)
    mimeType: Mapped[str | None] = mapped_column(String)
    caption: Mapped[str | None] = mapped_column(String)
    uploadedById: Mapped[str] = mapped_column(String, nullable=False)
    uploadedAt: Mapped[datetime] = _created()
    deletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleteReason: Mapped[str | None] = mapped_column(String)

    __table_args__ = (Index("ix_ComplianceAttachment_task", "taskId"),)


# ── Loss Events ─────────────────────────────────────────────────────────────
class LossEvent(Base, IdMixin):
    __tablename__ = "LossEvent"

    eventCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    eventDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    siteId: Mapped[str | None] = mapped_column(String)
    categoryId: Mapped[str] = mapped_column(String, nullable=False)
    subCategoryId: Mapped[str | None] = mapped_column(String)
    linkedRiskIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source: Mapped[str] = mapped_column(String, nullable=False, default="MANUAL")
    sourceIncidentId: Mapped[str | None] = mapped_column(String)
    isNearMiss: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    grossLossInr: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    recoveredInr: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    netLossInr: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    potentialLossInr: Mapped[float | None] = mapped_column(Float)
    lossTypes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    closureNotes: Mapped[str | None] = mapped_column(Text)
    sourceUpdatedFlag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_LossEvent_cat_status", "categoryId", "status"),
        Index("ix_LossEvent_site", "siteId"),
        Index("ix_LossEvent_source", "source"),
        Index("ix_LossEvent_date", "eventDate"),
        Index("ix_LossEvent_incident", "sourceIncidentId"),
    )


__all__ = [
    "KriDefinition", "KriReading", "KriBreachEvent",
    "AppetiteStatement", "AppetiteBreach",
    "LegalObligation", "ComplianceTask", "ComplianceAttachment",
    "LossEvent",
]
