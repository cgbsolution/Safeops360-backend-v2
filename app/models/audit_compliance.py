"""Audit & Compliance Management.

SQLAlchemy mirror of the Prisma `AuditCheckpointLibrary` / `AuditTemplate` /
`ComplianceAudit` / `AuditCheckpointResponse` models in
[safeops_360/prisma/schema.prisma](../../../safeops_360/prisma/schema.prisma)
section "Audit & Compliance Management".

Schema is owned by Prisma. This file lets the SQLAlchemy-side router read/write
the same tables. camelCase column names are required to match. FKs to Plant /
User are plain scalar strings (matching the other vertical modules); the only
relationship is ComplianceAudit -> its checkpoint response rows.
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


class AuditCheckpointLibrary(Base, IdMixin):
    """Master checklist per industry — categories + checkpoints as JSON."""

    __tablename__ = "AuditCheckpointLibrary"

    industryCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    industryName: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False, default="2026.1")
    categories: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    checkpointCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class AuditTemplate(Base, IdMixin):
    """Tenant preset — which checkpoints an audit type pulls in + config."""

    __tablename__ = "AuditTemplate"

    tenantId: Mapped[str | None] = mapped_column(String)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    auditType: Mapped[str] = mapped_column(String, nullable=False)
    baseIndustry: Mapped[str] = mapped_column(String, nullable=False, index=True)
    checkpointConfiguration: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    scoring: Mapped[dict | None] = mapped_column(JSON)
    workflow: Mapped[dict | None] = mapped_column(JSON)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[str] = mapped_column(String, nullable=False, default="1.0")
    createdByUserId: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class ComplianceAudit(Base, IdMixin):
    """One audit instance carrying the full lifecycle via `status`."""

    __tablename__ = "ComplianceAudit"

    tenantId: Mapped[str | None] = mapped_column(String)
    auditNumber: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    templateId: Mapped[str | None] = mapped_column(String)
    industryCode: Mapped[str] = mapped_column(String, nullable=False)
    auditType: Mapped[str] = mapped_column(String, nullable=False)

    scopeDepartments: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    scopeAreas: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    scopeDescription: Mapped[str] = mapped_column(Text, nullable=False, default="")

    scheduledDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scheduledStartTime: Mapped[str] = mapped_column(String, nullable=False, default="09:00")
    estimatedDurationHours: Mapped[float] = mapped_column(Float, nullable=False, default=2)

    leadAuditorUserId: Mapped[str] = mapped_column(String, nullable=False)
    coAuditors: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    auditees: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    plantManagerUserId: Mapped[str | None] = mapped_column(String)

    status: Mapped[str] = mapped_column(String, nullable=False, default="scheduled")
    actualStartAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actualEndAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submittedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Recomputed snapshot — only at submit / review / close.
    score: Mapped[dict | None] = mapped_column(JSON)

    # Denormalized rollups (drive the programme list without opening JSON).
    totalCheckpoints: Mapped[int | None] = mapped_column(Integer)
    answeredCheckpoints: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overallCompliancePct: Mapped[float | None] = mapped_column(Float)
    auditPassed: Mapped[bool | None] = mapped_column(Boolean)
    openCapaCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    criticalFailureCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    openingRemarks: Mapped[str] = mapped_column(Text, nullable=False, default="")
    closingRemarks: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Plant Head final sign-off { reviewer_user_id, decision, comments, accepted_at }.
    plantHeadAcceptance: Mapped[dict | None] = mapped_column(JSON)
    # Mirror link into the shared CAMS engine (set on first submit).
    camsEngagementId: Mapped[str | None] = mapped_column(String)

    isRecurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    createdByUserId: Mapped[str] = mapped_column(String, nullable=False)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    responses: Mapped[list["AuditCheckpointResponse"]] = relationship(
        back_populates="audit", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_ComplianceAudit_plant_status", "plantId", "status"),
    )


class AuditCheckpointResponse(Base, IdMixin):
    """One row per checkpoint per audit. Per-row partial-save + routing."""

    __tablename__ = "AuditCheckpointResponse"

    auditId: Mapped[str] = mapped_column(
        ForeignKey("ComplianceAudit.id", ondelete="CASCADE"), nullable=False, index=True
    )
    audit: Mapped[ComplianceAudit] = relationship(back_populates="responses")
    plantId: Mapped[str] = mapped_column(String, nullable=False)

    # Denormalized checkpoint definition (snapshot from the library).
    checkpointCode: Mapped[str] = mapped_column(String, nullable=False)
    checkpointQuestion: Mapped[str] = mapped_column(Text, nullable=False)
    guidance: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requirementReference: Mapped[str] = mapped_column(String, nullable=False, default="")
    standard: Mapped[str] = mapped_column(String, nullable=False, default="")
    categoryId: Mapped[str] = mapped_column(String, nullable=False)
    categoryName: Mapped[str] = mapped_column(String, nullable=False)
    categoryColor: Mapped[str] = mapped_column(String, nullable=False, default="")
    criticality: Mapped[str] = mapped_column(String, nullable=False, default="major")
    responseType: Mapped[str] = mapped_column(String, nullable=False, default="pass_partial_fail")
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Auditor-added ad-hoc checkpoint (not from the industry library).
    isCustom: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Denormalized per-checkpoint rules.
    requiresPhotoOnFail: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    autoTriggerCapaOnFail: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    capaSeverity: Mapped[str | None] = mapped_column(String)
    linkedSafeopsModule: Mapped[str | None] = mapped_column(String)

    routedToUserId: Mapped[str | None] = mapped_column(String)

    # Lifecycle sub-documents.
    auditorResponse: Mapped[dict | None] = mapped_column(JSON)
    auditeeResponse: Mapped[dict | None] = mapped_column(JSON)
    auditorReview: Mapped[dict | None] = mapped_column(JSON)  # auditor verifies the auditee response
    plantManagerReview: Mapped[dict | None] = mapped_column(JSON)  # DEPRECATED — superseded by auditorReview
    capa: Mapped[dict | None] = mapped_column(JSON)

    overallStatus: Mapped[str] = mapped_column(String, nullable=False, default="not_answered")
    answeredAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("auditId", "checkpointCode", name="uq_AuditCheckpointResponse_audit_code"),
        Index("ix_AuditCheckpointResponse_audit_category", "auditId", "categoryId"),
        Index("ix_AuditCheckpointResponse_audit_routed", "auditId", "routedToUserId"),
    )


__all__ = [
    "AuditCheckpointLibrary",
    "AuditTemplate",
    "ComplianceAudit",
    "AuditCheckpointResponse",
]
