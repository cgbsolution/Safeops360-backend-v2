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

from app.models._base import Base, IdMixin, SoftDeleteMixin


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
    # Custom checkpoints added to this template (forks a version). See Prisma.
    customCheckpoints: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    parentTemplateId: Mapped[str | None] = mapped_column(String)
    scoring: Mapped[dict | None] = mapped_column(JSON)
    workflow: Mapped[dict | None] = mapped_column(JSON)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[str] = mapped_column(String, nullable=False, default="1.0")
    createdByUserId: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class ComplianceAudit(Base, IdMixin, SoftDeleteMixin):
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

    # Discipline scope (audit-lifecycle v2). Empty list = full library.
    selectedDisciplineIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    scopePresetUsed: Mapped[str | None] = mapped_column(String)
    materializedCheckpointCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    adHocCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

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

    isRecurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # WP-41 sign-off (docs/cams/09 §3.1). `AuditReport.signOffs` already existed
    # but was never written to — the report rendered "Awaiting sign-off" and
    # nothing could change that. Sign-off belongs on the ENGAGEMENT (it gates
    # closure, which happens before a final report exists); the report snapshot
    # freezes a copy at generation.
    # [{role, userId, name, designation, disciplineCode, signatureKind,
    #   signatureImage, typedName, statement, signedAt}]
    signOffs: Mapped[list | None] = mapped_column(JSON)

    # Governed reopen (docs/cams/09 §2.5). Before this, `close_audit` was
    # terminal and the only way to correct a closed record was a direct database
    # write — which leaves no trace in the product. A counted, approved, logged
    # reopen is strictly safer than an invisible one.
    reopenCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lastReopenedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastReopenReason: Mapped[str | None] = mapped_column(Text)

    createdByUserId: Mapped[str] = mapped_column(String, nullable=False)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    responses: Mapped[list["AuditCheckpointResponse"]] = relationship(
        back_populates="audit", cascade="all, delete-orphan"
    )
    reports: Mapped[list["AuditReport"]] = relationship(
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

    # Denormalized per-checkpoint rules.
    requiresPhotoOnFail: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    autoTriggerCapaOnFail: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    capaSeverity: Mapped[str | None] = mapped_column(String)
    linkedSafeopsModule: Mapped[str | None] = mapped_column(String)

    routedToUserId: Mapped[str | None] = mapped_column(String)

    # Per-checkpoint owner allocation (audit-lifecycle v2). assignedOwnerId is
    # the AUDITEE (responds to a finding); assignedAuditorId is the AUDITOR who
    # conducts the checkpoint (per-discipline auditor assignment).
    assignedOwnerId: Mapped[str | None] = mapped_column(String)
    assignedAuditorId: Mapped[str | None] = mapped_column(String)
    assignedById: Mapped[str | None] = mapped_column(String)
    assignedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Ad-hoc (custom) checkpoint added to this audit only.
    isAdHoc: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    addedById: Mapped[str | None] = mapped_column(String)

    # Two-axis state (additive to overallStatus).
    assessmentStatus: Mapped[str] = mapped_column(String, nullable=False, default="NOT_ASSESSED")
    workflowState: Mapped[str] = mapped_column(String, nullable=False, default="OPEN")
    currentRound: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Carousel capture (first-class).
    observation: Mapped[str | None] = mapped_column(Text)
    auditorNote: Mapped[str | None] = mapped_column(Text)
    orderIndex: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    auditorEvidenceIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    auditeeEvidenceIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    capaId: Mapped[str | None] = mapped_column(String)
    finalizedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Lifecycle sub-documents.
    auditorResponse: Mapped[dict | None] = mapped_column(JSON)
    auditeeResponse: Mapped[dict | None] = mapped_column(JSON)
    plantManagerReview: Mapped[dict | None] = mapped_column(JSON)
    capa: Mapped[dict | None] = mapped_column(JSON)

    overallStatus: Mapped[str] = mapped_column(String, nullable=False, default="not_answered")
    answeredAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    interactions: Mapped[list["CheckpointInteraction"]] = relationship(
        back_populates="checkpointInstance", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("auditId", "checkpointCode", name="uq_AuditCheckpointResponse_audit_code"),
        Index("ix_AuditCheckpointResponse_audit_category", "auditId", "categoryId"),
        Index("ix_AuditCheckpointResponse_audit_routed", "auditId", "routedToUserId"),
        Index("ix_AuditCheckpointResponse_audit_owner", "auditId", "assignedOwnerId"),
        Index("ix_AuditCheckpointResponse_audit_wfstate", "auditId", "workflowState"),
    )


class CheckpointInteraction(Base, IdMixin):
    """Append-only multi-round thread. One row per checkpoint state transition.
    Never updated or deleted; `timestamp` is server-set and immutable."""

    __tablename__ = "CheckpointInteraction"

    checkpointInstanceId: Mapped[str] = mapped_column(
        ForeignKey("AuditCheckpointResponse.id", ondelete="CASCADE"), nullable=False, index=True
    )
    checkpointInstance: Mapped[AuditCheckpointResponse] = relationship(back_populates="interactions")
    auditId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actorId: Mapped[str] = mapped_column(String, nullable=False)
    actorRole: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    evidenceIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    resultingState: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AuditReport(Base, IdMixin):
    """Immutable Interim / Final report snapshot."""

    __tablename__ = "AuditReport"

    auditId: Mapped[str] = mapped_column(
        ForeignKey("ComplianceAudit.id", ondelete="CASCADE"), nullable=False
    )
    audit: Mapped[ComplianceAudit] = relationship(back_populates="reports")
    siteId: Mapped[str] = mapped_column(String, nullable=False)
    reportType: Mapped[str] = mapped_column(String, nullable=False)  # INTERIM | FINAL
    reportCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    generatedById: Mapped[str] = mapped_column(String, nullable=False)
    generatedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    signOffs: Mapped[list | None] = mapped_column(JSON)
    pdfAttachmentId: Mapped[str | None] = mapped_column(String)
    isSuperseded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Full-length SHA-256 of the canonical snapshot (docs/cams/09 §2.5 gap 1).
    # The snapshot itself carries a 16-char prefix at `snapshot["snapshotHash"]`
    # for display and backward compatibility; 64 bits is thin against intent, so
    # the verifiable digest lives here. Null on reports generated before this
    # column existed — those verify as LEGACY_TRUNCATED, not as tampered.
    snapshotHashFull: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_AuditReport_audit_type", "auditId", "reportType"),
    )


__all__ = [
    "AuditCheckpointLibrary",
    "AuditTemplate",
    "ComplianceAudit",
    "AuditCheckpointResponse",
    "CheckpointInteraction",
    "AuditReport",
]
