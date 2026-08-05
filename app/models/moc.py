"""SQLAlchemy mirror of the MOC (Management of Change) Prisma models.

Phase 1 of the MOC module (4th IMS module). Hand-mirrored, camelCase columns to
match Prisma's column naming (same convention as competency_matrix.py). Prisma
owns the migration (20260603120000_moc_foundation) and is the source of truth
for constraints/indexes; this mirror lets the FastAPI layer read/write the
tables.

Additive-only: relationship()s are declared ONLY among these new tables.
References to pre-existing tables (User, Plant, Department, Capa, …) are plain
String columns (FK-by-value). `status`/`category`/`classification` are Strings,
not Enums — transition rules live in the service layer.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


# ─── ChangeRequest — the core MOC entity (spec §3.1) ──────────────────


class ChangeRequest(Base, IdMixin):
    __tablename__ = "ChangeRequest"

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    category: Mapped[str] = mapped_column(String, nullable=False)
    subcategory: Mapped[str | None] = mapped_column(String)
    classification: Mapped[str] = mapped_column(String, default="minor")

    isTemporary: Mapped[bool] = mapped_column(Boolean, default=False)
    temporaryExpiryDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    origin: Mapped[str] = mapped_column(String, nullable=False)
    originSourceType: Mapped[str | None] = mapped_column(String)
    originSourceId: Mapped[str | None] = mapped_column(String)

    departmentId: Mapped[str | None] = mapped_column(String)
    affectedDepartments: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    affectedLocations: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    affectedEquipmentIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    affectedProcesses: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    affectedRoles: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    initiatedByUserId: Mapped[str] = mapped_column(String, nullable=False)
    initiatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    businessJustification: Mapped[str | None] = mapped_column(Text)
    expectedBenefits: Mapped[str | None] = mapped_column(Text)
    costEstimate: Mapped[float | None] = mapped_column(Float)
    costCurrency: Mapped[str] = mapped_column(String, default="INR")

    proposedImplementationDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    targetCompletionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actualImplementationDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actualCompletionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String, default="draft")

    safetyRiskLevel: Mapped[str | None] = mapped_column(String)
    environmentalRiskLevel: Mapped[str | None] = mapped_column(String)
    qualityRiskLevel: Mapped[str | None] = mapped_column(String)
    operationalRiskLevel: Mapped[str | None] = mapped_column(String)
    overallResidualRisk: Mapped[str | None] = mapped_column(String)

    pssrRequired: Mapped[bool] = mapped_column(Boolean, default=False)
    pssrOutcome: Mapped[str | None] = mapped_column(String)
    pssrConductedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    returnToNormalCompletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    spawnedFromCapaId: Mapped[str | None] = mapped_column(String)
    supersededByMocId: Mapped[str | None] = mapped_column(String)

    # ── Gensuite-parity extension (5-step wizard) — additive/nullable; column
    #    names match Prisma exactly. See prisma/apply-moc-ddl.ts for the DDL. ──
    urgency: Mapped[str] = mapped_column(String, default="standard")
    emergencyRetroApprovalDueAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    linkedMocIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    psmApplicable: Mapped[bool] = mapped_column(Boolean, default=False)
    psmDetails: Mapped[dict | None] = mapped_column(JSON)
    riskMatrixPre: Mapped[dict | None] = mapped_column(JSON)
    riskMatrixResidual: Mapped[dict | None] = mapped_column(JSON)
    hazardCategories: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    mitigations: Mapped[str | None] = mapped_column(Text)

    departmentImpact: Mapped[dict | None] = mapped_column(JSON)
    trainingRequired: Mapped[bool] = mapped_column(Boolean, default=False)
    trainingCertificateId: Mapped[str | None] = mapped_column(String)

    pssrChecklist: Mapped[dict | None] = mapped_column(JSON)
    effectivenessReview: Mapped[dict | None] = mapped_column(JSON)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
    versionNumber: Mapped[int] = mapped_column(Integer, default=1)

    approvalSteps: Mapped[list[MocApprovalStep]] = relationship(
        back_populates="changeRequest", cascade="all, delete-orphan"
    )
    dependentRecords: Mapped[list[MocDependentRecord]] = relationship(
        back_populates="changeRequest", cascade="all, delete-orphan"
    )
    stateHistory: Mapped[list[MocStateHistory]] = relationship(
        back_populates="changeRequest", cascade="all, delete-orphan"
    )
    impactAssessment: Mapped[MocImpactAssessment | None] = relationship(
        back_populates="changeRequest", cascade="all, delete-orphan", uselist=False
    )
    attachments: Mapped[list[MocAttachment]] = relationship(
        back_populates="changeRequest", cascade="all, delete-orphan"
    )


# ─── MocApprovalStep — classification-driven approval chain ───────────


class MocApprovalStep(Base, IdMixin):
    __tablename__ = "MocApprovalStep"

    changeRequestId: Mapped[str] = mapped_column(
        ForeignKey("ChangeRequest.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    specificUserId: Mapped[str | None] = mapped_column(String)
    isRequired: Mapped[bool] = mapped_column(Boolean, default=True)

    decision: Mapped[str] = mapped_column(String, default="pending")
    decidedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decidedByUserId: Mapped[str | None] = mapped_column(String)
    rationale: Mapped[str | None] = mapped_column(Text)
    conditions: Mapped[str | None] = mapped_column(Text)

    changeRequest: Mapped[ChangeRequest] = relationship(back_populates="approvalSteps")


# ─── MocDependentRecord — registers this change affects ───────────────


class MocDependentRecord(Base, IdMixin):
    __tablename__ = "MocDependentRecord"

    changeRequestId: Mapped[str] = mapped_column(
        ForeignKey("ChangeRequest.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recordType: Mapped[str] = mapped_column(String, nullable=False)
    recordId: Mapped[str | None] = mapped_column(String)
    recordReference: Mapped[str] = mapped_column(String, nullable=False)
    impactType: Mapped[str] = mapped_column(String, nullable=False)
    impactDescription: Mapped[str | None] = mapped_column(Text)

    updateStatus: Mapped[str] = mapped_column(String, default="not_started")
    updatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updatedByUserId: Mapped[str | None] = mapped_column(String)
    updateEvidence: Mapped[str | None] = mapped_column(String)

    changeRequest: Mapped[ChangeRequest] = relationship(back_populates="dependentRecords")


# ─── MocStateHistory — immutable state-transition log ─────────────────


class MocStateHistory(Base, IdMixin):
    __tablename__ = "MocStateHistory"

    changeRequestId: Mapped[str] = mapped_column(
        ForeignKey("ChangeRequest.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fromState: Mapped[str | None] = mapped_column(String)
    toState: Mapped[str] = mapped_column(String, nullable=False)
    transitionedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    transitionedByUserId: Mapped[str | None] = mapped_column(String)
    rationale: Mapped[str | None] = mapped_column(Text)

    changeRequest: Mapped[ChangeRequest] = relationship(back_populates="stateHistory")


# ─── MocImpactAssessment — 1:1 structured analysis ────────────────────


class MocImpactAssessment(Base, IdMixin):
    __tablename__ = "MocImpactAssessment"

    changeRequestId: Mapped[str] = mapped_column(
        ForeignKey("ChangeRequest.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    assessorUserId: Mapped[str | None] = mapped_column(String)
    assessorRole: Mapped[str | None] = mapped_column(String)
    methodology: Mapped[str | None] = mapped_column(String)
    dimensions: Mapped[dict | None] = mapped_column(JSON)

    recommendedClassification: Mapped[str | None] = mapped_column(String)
    pssrRequired: Mapped[bool] = mapped_column(Boolean, default=False)
    rollbackPlanRequired: Mapped[bool] = mapped_column(Boolean, default=False)

    assessmentDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewedByUserId: Mapped[str | None] = mapped_column(String)
    reviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )

    changeRequest: Mapped[ChangeRequest] = relationship(back_populates="impactAssessment")


# ─── MocAttachment — supporting docs (Supabase two-phase signed-URL) ──


class MocAttachment(Base, IdMixin):
    __tablename__ = "MocAttachment"

    changeRequestId: Mapped[str] = mapped_column(
        ForeignKey("ChangeRequest.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String, nullable=False)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    storagePath: Mapped[str] = mapped_column(String, nullable=False)
    fileSize: Mapped[int] = mapped_column(Integer, nullable=False)
    mimeType: Mapped[str] = mapped_column(String, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)
    # FK-by-value → User.id (resolved to a display name in the router).
    uploadedById: Mapped[str] = mapped_column(String, nullable=False)
    uploadedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    changeRequest: Mapped[ChangeRequest] = relationship(back_populates="attachments")


# ─── MocFreeze — administrative block on new submissions ──────────────


class MocFreeze(Base, IdMixin):
    __tablename__ = "MocFreeze"

    plantIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    departmentIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    categoryFilters: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    classificationFilters: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    reason: Mapped[str] = mapped_column(String, nullable=False)
    reasonDetail: Mapped[str | None] = mapped_column(Text)

    startsAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    endsAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    exceptionsAllowed: Mapped[bool] = mapped_column(Boolean, default=False)
    exceptionApprovalAuthority: Mapped[str | None] = mapped_column(String)

    imposedByUserId: Mapped[str] = mapped_column(String, nullable=False)
    imposedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    liftedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    liftedByUserId: Mapped[str | None] = mapped_column(String)

    isActive: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
