"""HIRA — Hazard Identification and Risk Assessment.

Phase 1 of the IMS expansion. Mirrors the Prisma models in
[safeops_360/prisma/schema.prisma](../../../safeops_360/prisma/schema.prisma)
section "HIRA — Hazard Identification and Risk Assessment".

Schema is owned by Prisma (no Alembic migration runs against these tables);
this file exists so SQLAlchemy-side routers can read/write the same tables.

Design notes:
- camelCase column names — must match Prisma; otherwise Postgres rejects
  with "column does not exist".
- updatedAt uses `default=func.now(), onupdate=func.now()` (NOT
  server_default) because Prisma's `@updatedAt` is client-managed; the DB
  column has no default. Routes that serialise these via model_validate
  MUST `await db.refresh(x)` first.
- Enum-like columns are kept as String to match the Prisma "literal-string"
  style used by Workflow / Incident. The router validates values.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from app.models.masters import Department
    from app.models.plant import Area, Plant


# ─────────────────────────────────────────────────────────────────────
# Risk matrix master + scales + cells
# ─────────────────────────────────────────────────────────────────────


class RiskMatrix(Base, IdMixin):
    __tablename__ = "RiskMatrix"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    likelihoodLevels: Mapped[int] = mapped_column(Integer, nullable=False)
    severityLevels: Mapped[int] = mapped_column(Integer, nullable=False)

    # JSON: { routine: "MODERATE", non_routine: "MODERATE", emergency: "LOW" }
    acceptableResidual: Mapped[dict] = mapped_column(JSON, nullable=False)

    # ALARP tolerability banding — maps each riskLevel to an ALARP region.
    # JSON: { "LOW": "BROADLY_ACCEPTABLE", "MODERATE": "TOLERABLE",
    #         "HIGH": "TOLERABLE", "CRITICAL": "UNACCEPTABLE" }
    # Nullable for backward-compat; the router falls back to DEFAULT_ALARP_BANDS.
    alarpBands: Mapped[dict | None] = mapped_column(JSON)

    controlHierarchyEnforced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    isDefault: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    isGlobal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    likelihoods: Mapped[list["RiskMatrixLikelihood"]] = relationship(back_populates="matrix", cascade="all, delete-orphan")
    severities: Mapped[list["RiskMatrixSeverity"]] = relationship(back_populates="matrix", cascade="all, delete-orphan")
    cells: Mapped[list["RiskMatrixCell"]] = relationship(back_populates="matrix", cascade="all, delete-orphan")


class RiskMatrixLikelihood(Base, IdMixin):
    __tablename__ = "RiskMatrixLikelihood"
    __table_args__ = (UniqueConstraint("matrixId", "score", name="RiskMatrixLikelihood_matrixId_score_key"),)

    matrixId: Mapped[str] = mapped_column(ForeignKey("RiskMatrix.id", ondelete="CASCADE"), nullable=False, index=True)
    matrix: Mapped[RiskMatrix] = relationship(back_populates="likelihoods")

    score: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    frequencyGuidance: Mapped[str | None] = mapped_column(String)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class RiskMatrixSeverity(Base, IdMixin):
    __tablename__ = "RiskMatrixSeverity"
    __table_args__ = (UniqueConstraint("matrixId", "score", name="RiskMatrixSeverity_matrixId_score_key"),)

    matrixId: Mapped[str] = mapped_column(ForeignKey("RiskMatrix.id", ondelete="CASCADE"), nullable=False, index=True)
    matrix: Mapped[RiskMatrix] = relationship(back_populates="severities")

    score: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    healthSafetyGuidance: Mapped[str | None] = mapped_column(Text)
    propertyDamageGuidance: Mapped[str | None] = mapped_column(Text)
    environmentalGuidance: Mapped[str | None] = mapped_column(Text)
    reputationGuidance: Mapped[str | None] = mapped_column(Text)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class RiskMatrixCell(Base, IdMixin):
    __tablename__ = "RiskMatrixCell"
    __table_args__ = (
        UniqueConstraint(
            "matrixId",
            "likelihoodScore",
            "severityScore",
            name="RiskMatrixCell_matrixId_likelihoodScore_severityScore_key",
        ),
    )

    matrixId: Mapped[str] = mapped_column(ForeignKey("RiskMatrix.id", ondelete="CASCADE"), nullable=False, index=True)
    matrix: Mapped[RiskMatrix] = relationship(back_populates="cells")

    likelihoodScore: Mapped[int] = mapped_column(Integer, nullable=False)
    severityScore: Mapped[int] = mapped_column(Integer, nullable=False)
    riskScore: Mapped[int] = mapped_column(Integer, nullable=False)
    # LOW | MODERATE | HIGH | CRITICAL
    riskLevel: Mapped[str] = mapped_column(String, nullable=False, index=True)
    colorHex: Mapped[str] = mapped_column(String, nullable=False)
    actionRequired: Mapped[str] = mapped_column(Text, nullable=False)
    responseTimeDays: Mapped[int] = mapped_column(Integer, nullable=False)


# ─────────────────────────────────────────────────────────────────────
# Hazard + control libraries
# ─────────────────────────────────────────────────────────────────────


class HiraHazard(Base, IdMixin):
    __tablename__ = "HiraHazard"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    subcategory: Mapped[str | None] = mapped_column(String)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    typicalHarmPotential: Mapped[list] = mapped_column(JSON, nullable=False)
    typicalAffectedPersons: Mapped[list] = mapped_column(JSON, nullable=False)
    energyForm: Mapped[str | None] = mapped_column(String)

    oshaStandard: Mapped[str | None] = mapped_column(String)
    factoriesActSection: Mapped[str | None] = mapped_column(String)
    isStandard: Mapped[str | None] = mapped_column(String)
    isoReference: Mapped[str | None] = mapped_column(String)

    typicalControlsSuggested: Mapped[list | None] = mapped_column(JSON)

    # Permit gate. When true, an entry hazard row built from this library
    # hazard surfaces a "Create PTW" prompt. permitTypes narrows which
    # PermitType the drafted permit defaults to (null/empty => originator picks).
    requiresPermit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    permitTypes: Mapped[list | None] = mapped_column(JSON)

    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    isGlobal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class HiraControl(Base, IdMixin):
    __tablename__ = "HiraControl"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # ELIMINATION | SUBSTITUTION | ENGINEERING | ADMINISTRATIVE | PPE
    hierarchy: Mapped[str] = mapped_column(String, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    verificationMethod: Mapped[str | None] = mapped_column(String)
    verificationFrequency: Mapped[str | None] = mapped_column(String)

    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    isGlobal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────
# HiraStudy + study children
# ─────────────────────────────────────────────────────────────────────


class HiraStudy(Base, IdMixin):
    __tablename__ = "HiraStudy"
    __table_args__ = (
        Index("ix_HiraStudy_plantId_status", "plantId", "status"),
        Index("ix_HiraStudy_status_nextScheduledReviewDate", "status", "nextScheduledReviewDate"),
        Index("ix_HiraStudy_plantId_departmentId", "plantId", "departmentId"),
    )

    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False, index=True)
    departmentId: Mapped[str | None] = mapped_column(ForeignKey("Department.id"))
    areaId: Mapped[str | None] = mapped_column(ForeignKey("Area.id"))
    # PLANT | AREA | DEPARTMENT | ACTIVITY | EQUIPMENT | PROCESS
    scopeType: Mapped[str] = mapped_column(String, nullable=False)
    activityIds: Mapped[list | None] = mapped_column(JSON)
    equipmentIds: Mapped[list | None] = mapped_column(JSON)
    processCode: Mapped[str | None] = mapped_column(String)

    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    riskMatrixId: Mapped[str] = mapped_column(ForeignKey("RiskMatrix.id"), nullable=False)

    teamLeaderId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)

    # DRAFT | IN_PROGRESS | TEAM_REVIEW | APPROVAL_PENDING | APPROVED | ACTIVE | SUPERSEDED | ARCHIVED
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT", index=True)
    initiatedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    targetCompletionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approvedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    effectiveFrom: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nextScheduledReviewDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    reviewFrequency: Mapped[str] = mapped_column(String, nullable=False, default="ANNUAL")
    customReviewMonths: Mapped[int | None] = mapped_column(Integer)

    supersedesStudyId: Mapped[str | None] = mapped_column(ForeignKey("HiraStudy.id"), unique=True)
    supersessionReason: Mapped[str | None] = mapped_column(Text)

    applicableRegulations: Mapped[list | None] = mapped_column(JSON)
    regulatoryReviewRequired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    aggregateMetrics: Mapped[dict | None] = mapped_column(JSON)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    updatedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))

    team: Mapped[list["HiraStudyTeamMember"]] = relationship(back_populates="study", cascade="all, delete-orphan")
    entries: Mapped[list["HiraEntry"]] = relationship(back_populates="study", cascade="all, delete-orphan")
    attachments: Mapped[list["HiraStudyAttachment"]] = relationship(back_populates="study", cascade="all, delete-orphan")

    # Single-sided lookups used by the list endpoint (selectinload). No
    # back_populates — Plant / Department / Area do not need a back-ref
    # for the queries we run.
    plant: Mapped["Plant"] = relationship("Plant", foreign_keys=[plantId])
    department: Mapped["Department | None"] = relationship("Department", foreign_keys=[departmentId])
    area: Mapped["Area | None"] = relationship("Area", foreign_keys=[areaId])


class HiraStudyTeamMember(Base, IdMixin):
    __tablename__ = "HiraStudyTeamMember"
    __table_args__ = (UniqueConstraint("studyId", "userId", name="HiraStudyTeamMember_studyId_userId_key"),)

    studyId: Mapped[str] = mapped_column(ForeignKey("HiraStudy.id", ondelete="CASCADE"), nullable=False, index=True)
    study: Mapped[HiraStudy] = relationship(back_populates="team")
    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)

    # FACILITATOR | SUBJECT_MATTER_EXPERT | OPERATOR_REP | SAFETY_OFFICER | DEPARTMENT_HEAD | EXTERNAL_CONSULTANT
    teamRole: Mapped[str] = mapped_column(String, nullable=False)
    department: Mapped[str | None] = mapped_column(String)

    signedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signedNote: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class HiraStudyAttachment(Base, IdMixin):
    __tablename__ = "HiraStudyAttachment"

    studyId: Mapped[str] = mapped_column(ForeignKey("HiraStudy.id", ondelete="CASCADE"), nullable=False, index=True)
    study: Mapped[HiraStudy] = relationship(back_populates="attachments")

    fileName: Mapped[str] = mapped_column(String, nullable=False)
    fileUrl: Mapped[str] = mapped_column(String, nullable=False)
    fileSize: Mapped[int | None] = mapped_column(Integer)
    mimeType: Mapped[str | None] = mapped_column(String)
    category: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    uploadedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    uploadedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)


# ─────────────────────────────────────────────────────────────────────
# HiraEntry + entry children
# ─────────────────────────────────────────────────────────────────────


class HiraEntry(Base, IdMixin):
    __tablename__ = "HiraEntry"
    __table_args__ = (UniqueConstraint("studyId", "sequenceNumber", name="HiraEntry_studyId_sequenceNumber_key"),)

    studyId: Mapped[str] = mapped_column(ForeignKey("HiraStudy.id", ondelete="CASCADE"), nullable=False, index=True)
    study: Mapped[HiraStudy] = relationship(back_populates="entries")

    sequenceNumber: Mapped[int] = mapped_column(Integer, nullable=False)
    groupLabel: Mapped[str | None] = mapped_column(String)

    activityDescription: Mapped[str] = mapped_column(Text, nullable=False)
    areaId: Mapped[str | None] = mapped_column(ForeignKey("Area.id"))
    area: Mapped["Area | None"] = relationship("Area", foreign_keys=[areaId])
    subLocation: Mapped[str | None] = mapped_column(String)
    gpsLatitude: Mapped[float | None] = mapped_column(Float)
    gpsLongitude: Mapped[float | None] = mapped_column(Float)

    # ROUTINE | NON_ROUTINE | EMERGENCY
    routine: Mapped[str] = mapped_column(String, nullable=False)
    # CONTINUOUS | DAILY | WEEKLY | MONTHLY | OCCASIONAL | RARE
    frequency: Mapped[str] = mapped_column(String, nullable=False)
    typicalDurationMin: Mapped[int | None] = mapped_column(Integer)

    personsEmployees: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    personsContractors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    personsVisitors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    personsPublic: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    affectedPersonGroups: Mapped[str | None] = mapped_column(Text)

    equipmentUsed: Mapped[list | None] = mapped_column(JSON)
    materialsUsed: Mapped[list | None] = mapped_column(JSON)
    energySourcesPresent: Mapped[list | None] = mapped_column(JSON)

    # Initial risk
    initialLikelihoodId: Mapped[str] = mapped_column(ForeignKey("RiskMatrixLikelihood.id"), nullable=False)
    initialLikelihoodScore: Mapped[int] = mapped_column(Integer, nullable=False)
    initialLikelihoodRationale: Mapped[str | None] = mapped_column(Text)
    initialSeverityId: Mapped[str] = mapped_column(ForeignKey("RiskMatrixSeverity.id"), nullable=False)
    initialSeverityScore: Mapped[int] = mapped_column(Integer, nullable=False)
    initialSeverityRationale: Mapped[str | None] = mapped_column(Text)
    initialRiskScore: Mapped[int] = mapped_column(Integer, nullable=False)
    initialRiskLevel: Mapped[str] = mapped_column(String, nullable=False, index=True)
    initialRiskColor: Mapped[str | None] = mapped_column(String)

    # Residual risk — null until assessed
    residualLikelihoodId: Mapped[str | None] = mapped_column(ForeignKey("RiskMatrixLikelihood.id"))
    residualLikelihoodScore: Mapped[int | None] = mapped_column(Integer)
    residualLikelihoodRationale: Mapped[str | None] = mapped_column(Text)
    residualSeverityId: Mapped[str | None] = mapped_column(ForeignKey("RiskMatrixSeverity.id"))
    residualSeverityScore: Mapped[int | None] = mapped_column(Integer)
    residualSeverityRationale: Mapped[str | None] = mapped_column(Text)
    residualRiskScore: Mapped[int | None] = mapped_column(Integer)
    residualRiskLevel: Mapped[str | None] = mapped_column(String, index=True)
    residualRiskColor: Mapped[str | None] = mapped_column(String)
    residualAcceptable: Mapped[bool | None] = mapped_column(Boolean)
    residualAcceptanceRationale: Mapped[str | None] = mapped_column(Text)
    # When True, the residual likelihood/severity are DERIVED from the existing
    # controls (hierarchy + effectiveness) rather than hand-picked on the matrix.
    # Null on legacy rows → treated as a manual override so their hand-set
    # residual is never silently recomputed. See routers/hira.py.
    residualAutoCalculated: Mapped[bool | None] = mapped_column(Boolean)

    # ALARP banding — region derived from the risk level via matrix.alarpBands.
    # UNACCEPTABLE | TOLERABLE | BROADLY_ACCEPTABLE. Computed server-side.
    initialAlarpRegion: Mapped[str | None] = mapped_column(String, index=True)
    residualAlarpRegion: Mapped[str | None] = mapped_column(String, index=True)

    # ALARP demonstration (required for a residual in the TOLERABLE region).
    # NOT_REQUIRED (broadly acceptable / unacceptable) | REQUIRED | DEMONSTRATED
    alarpStatus: Mapped[str | None] = mapped_column(String, index=True)
    alarpFurtherControlsConsidered: Mapped[bool | None] = mapped_column(Boolean)
    alarpFurtherControlsDescription: Mapped[str | None] = mapped_column(Text)
    alarpRiskReductionBenefit: Mapped[str | None] = mapped_column(Text)
    # Cost/effort band of the further controls: LOW | MEDIUM | HIGH | VERY_HIGH
    alarpCostBand: Mapped[str | None] = mapped_column(String)
    # Verdict: is the cost/effort grossly disproportionate to the benefit?
    # True  => further reduction not reasonably practicable => risk is ALARP.
    # False => further reduction IS practicable => risk not yet ALARP.
    alarpGrosslyDisproportionate: Mapped[bool | None] = mapped_column(Boolean)
    alarpJustification: Mapped[str | None] = mapped_column(Text)
    alarpDemonstratedById: Mapped[str | None] = mapped_column(String)
    alarpDemonstratedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Target (forecast) risk — the projected residual once the RECOMMENDED
    # additional controls are implemented. Hand-picked by the assessor on the
    # matrix (not auto-derived). Drives the ALARP risk-reduction pathway view.
    # Stored as plain ids (no relationship) — the UI selects by score.
    targetLikelihoodId: Mapped[str | None] = mapped_column(String)
    targetLikelihoodScore: Mapped[int | None] = mapped_column(Integer)
    targetSeverityId: Mapped[str | None] = mapped_column(String)
    targetSeverityScore: Mapped[int | None] = mapped_column(Integer)
    targetRiskScore: Mapped[int | None] = mapped_column(Integer)
    targetRiskLevel: Mapped[str | None] = mapped_column(String)
    targetRiskColor: Mapped[str | None] = mapped_column(String)
    targetAlarpRegion: Mapped[str | None] = mapped_column(String, index=True)
    targetRationale: Mapped[str | None] = mapped_column(Text)

    # Unacceptable-risk override (ALARP governance). An Unacceptable (e.g.
    # CRITICAL) residual can NOT be approved through the normal path. The only
    # way to APPROVE it is an elevated, time-bounded override recorded here by a
    # holder of HIRA.OVERRIDE_UNACCEPTABLE (Plant Head / Corporate HSE). Cleared
    # automatically when a material change moves the risk basis, and expired by
    # the review scheduler at unacceptableOverrideExpiresAt.
    unacceptableOverrideById: Mapped[str | None] = mapped_column(String)
    unacceptableOverrideAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unacceptableOverrideJustification: Mapped[str | None] = mapped_column(Text)
    unacceptableOverrideExpiresAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    triggersTrainingProgramIds: Mapped[list | None] = mapped_column(JSON)
    triggersInspectionTypeIds: Mapped[list | None] = mapped_column(JSON)
    influencesPtwRiskLevel: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    influencesPtwPermitTypes: Mapped[list | None] = mapped_column(JSON)
    linkedEmergencyProcIds: Mapped[list | None] = mapped_column(JSON)
    linkedEnvironmentalAspects: Mapped[list | None] = mapped_column(JSON)

    lastReviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastReviewedById: Mapped[str | None] = mapped_column(String)
    nextReviewDue: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    reviewCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lastReviewType: Mapped[str | None] = mapped_column(String)
    triggeredByRecordId: Mapped[str | None] = mapped_column(String)

    # DRAFT | IN_REVIEW | PENDING_REAPPROVAL | APPROVED | ACTIVE | FLAGGED_FOR_REVIEW | SUPERSEDED | ARCHIVED
    # PENDING_REAPPROVAL = was APPROVED/ACTIVE, a material change withdrew the
    # sign-off; distinct from IN_REVIEW (never-yet-approved).
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT", index=True)

    versionNumber: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    isCurrentVersion: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    parentVersionId: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    updatedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))

    hazards: Mapped[list["HiraEntryHazard"]] = relationship(back_populates="entry", cascade="all, delete-orphan")
    existingControls: Mapped[list["HiraEntryControl"]] = relationship(back_populates="entry", cascade="all, delete-orphan")
    recommendedControls: Mapped[list["HiraEntryRecommendedControl"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )
    regulationRefs: Mapped[list["HiraEntryRegulationRef"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )
    reviewCycles: Mapped[list["HiraReviewCycle"]] = relationship(back_populates="entry", cascade="all, delete-orphan")
    versions: Mapped[list["HiraVersion"]] = relationship(back_populates="entry", cascade="all, delete-orphan")
    capas: Mapped[list["HiraCapa"]] = relationship(back_populates="entry", cascade="all, delete-orphan")

    @property
    def unacceptableOverrideActive(self) -> bool:
        """True while a recorded Unacceptable-risk override is still in force
        (present and not past its review/expiry date)."""
        exp = self.unacceptableOverrideExpiresAt
        if self.unacceptableOverrideById is None or exp is None:
            return False
        from datetime import datetime, timezone

        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp > datetime.now(timezone.utc)


class HiraEntryHazard(Base, IdMixin):
    __tablename__ = "HiraEntryHazard"
    __table_args__ = (UniqueConstraint("entryId", "hazardId", name="HiraEntryHazard_entryId_hazardId_key"),)

    entryId: Mapped[str] = mapped_column(ForeignKey("HiraEntry.id", ondelete="CASCADE"), nullable=False, index=True)
    entry: Mapped[HiraEntry] = relationship(back_populates="hazards")
    hazardId: Mapped[str] = mapped_column(ForeignKey("HiraHazard.id"), nullable=False)
    # Forward relationship to the hazard library row. get_entry() eager-loads
    # this (selectinload(HiraEntryHazard.hazard)) to denormalise hazard
    # code/category/name for the editor & report. Uni-directional — HiraHazard
    # needs no back-reference to entry links.
    hazard: Mapped["HiraHazard"] = relationship("HiraHazard", foreign_keys=[hazardId])

    contextualDescription: Mapped[str | None] = mapped_column(Text)
    potentialHarm: Mapped[list | None] = mapped_column(JSON)
    affectedPersons: Mapped[list | None] = mapped_column(JSON)
    consequence: Mapped[str | None] = mapped_column(Text)

    # Hazard-row-grain regulatory citation. Deliberately separate from the
    # entry-level HiraEntryRegulationRef list: auditors expect the instrument
    # cited against the hazard, not only against the activity. Seeded from the
    # library hazard's factoriesActSection/oshaStandard/isStandard/isoReference
    # when the row is added from the library, then freely overridable.
    regulationRef: Mapped[str | None] = mapped_column(String(200))
    regulationSection: Mapped[str | None] = mapped_column(String(120))

    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class HiraEntryControl(Base, IdMixin):
    __tablename__ = "HiraEntryControl"

    entryId: Mapped[str] = mapped_column(ForeignKey("HiraEntry.id", ondelete="CASCADE"), nullable=False, index=True)
    entry: Mapped[HiraEntry] = relationship(back_populates="existingControls")
    controlId: Mapped[str | None] = mapped_column(ForeignKey("HiraControl.id"))

    hierarchy: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    effectiveness: Mapped[str | None] = mapped_column(String)
    verificationMethod: Mapped[str | None] = mapped_column(String)
    verificationFreq: Mapped[str | None] = mapped_column(String)
    responsibleRole: Mapped[str | None] = mapped_column(String)
    evidenceAttached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    documentReference: Mapped[str | None] = mapped_column(String)

    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class HiraEntryRecommendedControl(Base, IdMixin):
    __tablename__ = "HiraEntryRecommendedControl"
    __table_args__ = (
        Index("ix_HiraRecommendedControl_entryId_status", "entryId", "status"),
    )

    entryId: Mapped[str] = mapped_column(ForeignKey("HiraEntry.id", ondelete="CASCADE"), nullable=False, index=True)
    entry: Mapped[HiraEntry] = relationship(back_populates="recommendedControls")

    hierarchy: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)

    targetLikelihoodReduction: Mapped[int | None] = mapped_column(Integer)
    targetSeverityReduction: Mapped[int | None] = mapped_column(Integer)
    estimatedCostBand: Mapped[str | None] = mapped_column(String)

    proposedImplementationDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responsibleId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))

    # PROPOSED | APPROVED | IN_PROGRESS | IMPLEMENTED | DEFERRED | REJECTED
    status: Mapped[str] = mapped_column(String, nullable=False, default="PROPOSED")

    capaId: Mapped[str | None] = mapped_column(ForeignKey("HiraCapa.id"))

    # Proof that a proposal actually landed. Mirrors HiraEntryControl exactly
    # (same types, same ungated availability) — a recommendation that reaches
    # IMPLEMENTED without a document reference is the audit finding this closes.
    evidenceAttached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    documentReference: Mapped[str | None] = mapped_column(String(500))

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class HiraEntryRegulationRef(Base, IdMixin):
    __tablename__ = "HiraEntryRegulationRef"

    entryId: Mapped[str] = mapped_column(ForeignKey("HiraEntry.id", ondelete="CASCADE"), nullable=False, index=True)
    entry: Mapped[HiraEntry] = relationship(back_populates="regulationRefs")

    regulation: Mapped[str] = mapped_column(String, nullable=False)
    section: Mapped[str | None] = mapped_column(String)
    requirementSummary: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class HiraCapa(Base, IdMixin):
    __tablename__ = "HiraCapa"
    __table_args__ = (
        Index("ix_HiraCapa_entryId_status", "entryId", "status"),
        Index("ix_HiraCapa_ownerId_status", "ownerId", "status"),
    )

    entryId: Mapped[str] = mapped_column(ForeignKey("HiraEntry.id", ondelete="CASCADE"), nullable=False, index=True)
    entry: Mapped[HiraEntry] = relationship(back_populates="capas")
    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    description: Mapped[str] = mapped_column(Text, nullable=False)
    controlHierarchy: Mapped[str | None] = mapped_column(String)

    ownerId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)

    targetDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # OPEN | IN_PROGRESS | COMPLETED | VERIFIED | CLOSED | CANCELLED
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN", index=True)
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completionNote: Mapped[str | None] = mapped_column(Text)

    verifierId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    verifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verifyMethod: Mapped[str | None] = mapped_column(String)

    effectiveness: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class HiraReviewCycle(Base, IdMixin):
    __tablename__ = "HiraReviewCycle"
    __table_args__ = (
        Index("ix_HiraReviewCycle_assignedToId_status", "assignedToId", "status"),
        Index("ix_HiraReviewCycle_status_scheduledFor", "status", "scheduledFor"),
    )

    entryId: Mapped[str] = mapped_column(ForeignKey("HiraEntry.id", ondelete="CASCADE"), nullable=False, index=True)
    entry: Mapped[HiraEntry] = relationship(back_populates="reviewCycles")

    scheduledFor: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # SCHEDULE | INCIDENT | MOC | AUDIT_FINDING | MANUAL | REGULATORY_CHANGE | NEAR_MISS | OBSERVATION
    triggeredBy: Mapped[str] = mapped_column(String, nullable=False)
    triggerReferenceId: Mapped[str | None] = mapped_column(String)

    # SCHEDULED | IN_PROGRESS | COMPLETED | SKIPPED
    status: Mapped[str] = mapped_column(String, nullable=False, default="SCHEDULED", index=True)

    assignedToId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    assignedRole: Mapped[str | None] = mapped_column(String)

    startedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))

    # NO_CHANGE_REQUIRED | MINOR_REVISION | MAJOR_REVISION | NEW_ENTRY_CREATED | ENTRY_ARCHIVED
    outcome: Mapped[str | None] = mapped_column(String)
    outcomeNotes: Mapped[str | None] = mapped_column(Text)

    changesMade: Mapped[list | None] = mapped_column(JSON)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class HiraVersion(Base, IdMixin):
    """Immutable snapshot of a HiraEntry. Never UPDATE or DELETE."""

    __tablename__ = "HiraVersion"
    __table_args__ = (UniqueConstraint("entryId", "versionNumber", name="HiraVersion_entryId_versionNumber_key"),)

    entryId: Mapped[str] = mapped_column(ForeignKey("HiraEntry.id", ondelete="CASCADE"), nullable=False, index=True)
    entry: Mapped[HiraEntry] = relationship(back_populates="versions")

    versionNumber: Mapped[int] = mapped_column(Integer, nullable=False)

    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    changes: Mapped[list] = mapped_column(JSON, nullable=False)

    changeReason: Mapped[str] = mapped_column(Text, nullable=False)
    # SCHEDULED_REVIEW | INCIDENT_REVIEW | MOC | CORRECTION | AUDIT_FINDING | INITIAL_APPROVAL
    changeTrigger: Mapped[str] = mapped_column(String, nullable=False)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)


__all__ = [
    "RiskMatrix",
    "RiskMatrixLikelihood",
    "RiskMatrixSeverity",
    "RiskMatrixCell",
    "HiraHazard",
    "HiraControl",
    "HiraStudy",
    "HiraStudyTeamMember",
    "HiraStudyAttachment",
    "HiraEntry",
    "HiraEntryHazard",
    "HiraEntryControl",
    "HiraEntryRecommendedControl",
    "HiraEntryRegulationRef",
    "HiraCapa",
    "HiraReviewCycle",
    "HiraVersion",
]
