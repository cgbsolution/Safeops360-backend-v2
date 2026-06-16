"""EAI — Environmental Aspect & Impact register (HIRA Phase 2).

Mirrors the Prisma `Eai*` models in
[safeops_360/prisma/schema.prisma](../../../safeops_360/prisma/schema.prisma)
section "EAI — Environmental Aspect & Impact Register".

Schema is owned by Prisma. This file lets SQLAlchemy-side routers read/
write the same tables. camelCase column names match Prisma exactly.

Tables in this file:
  - EaiAspectCategory, EaiAspect, EaiReceptor, EaiRegulation,
    EnvironmentalImpactMatrix + scales + cells — master data
  - EaiStudy, EaiStudyTeamMember — study container
  - EaiEntry, EaiEntryAspect, EaiEntryImpact, EaiEntryControl,
    EaiEntryRecommendedControl, EaiComplianceObligation,
    EaiEntryRegulationRef — register row + children
  - EaiReviewCycle — periodic / triggered review tracking
  - EaiVersion — immutable version history
  - EaiFeatureFlag — per-plant Phase 2 opt-in flags
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


# ─────────────────────────────────────────────────────────────────────
# Master data
# ─────────────────────────────────────────────────────────────────────


class EaiAspectCategory(Base, IdMixin):
    __tablename__ = "EaiAspectCategory"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    iconKey: Mapped[str | None] = mapped_column(String)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class EaiAspect(Base, IdMixin):
    __tablename__ = "EaiAspect"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    categoryId: Mapped[str] = mapped_column(ForeignKey("EaiAspectCategory.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    typicalReceptors: Mapped[list] = mapped_column(JSON, nullable=False)
    typicalImpacts: Mapped[list | None] = mapped_column(JSON)
    typicalRegulations: Mapped[list | None] = mapped_column(JSON)
    typicalControls: Mapped[list | None] = mapped_column(JSON)
    typicallySignificant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    isGlobal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class EaiReceptor(Base, IdMixin):
    __tablename__ = "EaiReceptor"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EaiRegulation(Base, IdMixin):
    __tablename__ = "EaiRegulation"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String, nullable=False, default="INDIA")
    section: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    authority: Mapped[str | None] = mapped_column(String)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EnvironmentalImpactMatrix(Base, IdMixin):
    __tablename__ = "EnvironmentalImpactMatrix"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)

    likelihoodLevels: Mapped[int] = mapped_column(Integer, nullable=False)
    magnitudeLevels: Mapped[int] = mapped_column(Integer, nullable=False)

    significanceThresholds: Mapped[dict] = mapped_column(JSON, nullable=False)
    acceptableResidual: Mapped[dict] = mapped_column(JSON, nullable=False)

    controlHierarchyEnforced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    isDefault: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    isGlobal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    likelihoods: Mapped[list["EnvironmentalImpactMatrixLikelihood"]] = relationship(
        back_populates="matrix", cascade="all, delete-orphan"
    )
    magnitudes: Mapped[list["EnvironmentalImpactMatrixMagnitude"]] = relationship(
        back_populates="matrix", cascade="all, delete-orphan"
    )
    cells: Mapped[list["EnvironmentalImpactMatrixCell"]] = relationship(
        back_populates="matrix", cascade="all, delete-orphan"
    )

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class EnvironmentalImpactMatrixLikelihood(Base, IdMixin):
    __tablename__ = "EnvironmentalImpactMatrixLikelihood"
    __table_args__ = (
        UniqueConstraint("matrixId", "score", name="EnvironmentalImpactMatrixLikelihood_matrixId_score_key"),
    )

    matrixId: Mapped[str] = mapped_column(
        ForeignKey("EnvironmentalImpactMatrix.id", ondelete="CASCADE"), nullable=False
    )
    matrix: Mapped[EnvironmentalImpactMatrix] = relationship(back_populates="likelihoods")
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    occurrenceGuidance: Mapped[str | None] = mapped_column(String)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EnvironmentalImpactMatrixMagnitude(Base, IdMixin):
    __tablename__ = "EnvironmentalImpactMatrixMagnitude"
    __table_args__ = (
        UniqueConstraint("matrixId", "score", name="EnvironmentalImpactMatrixMagnitude_matrixId_score_key"),
    )

    matrixId: Mapped[str] = mapped_column(
        ForeignKey("EnvironmentalImpactMatrix.id", ondelete="CASCADE"), nullable=False
    )
    matrix: Mapped[EnvironmentalImpactMatrix] = relationship(back_populates="magnitudes")
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    geographicGuidance: Mapped[str | None] = mapped_column(String)
    reversibilityGuidance: Mapped[str | None] = mapped_column(String)
    durationGuidance: Mapped[str | None] = mapped_column(String)
    legalGuidance: Mapped[str | None] = mapped_column(String)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EnvironmentalImpactMatrixCell(Base, IdMixin):
    __tablename__ = "EnvironmentalImpactMatrixCell"
    __table_args__ = (
        UniqueConstraint(
            "matrixId", "likelihoodScore", "magnitudeScore",
            name="EnvironmentalImpactMatrixCell_matrixId_likelihoodScore_magnitudeScore_key",
        ),
    )

    matrixId: Mapped[str] = mapped_column(
        ForeignKey("EnvironmentalImpactMatrix.id", ondelete="CASCADE"), nullable=False
    )
    matrix: Mapped[EnvironmentalImpactMatrix] = relationship(back_populates="cells")
    likelihoodScore: Mapped[int] = mapped_column(Integer, nullable=False)
    magnitudeScore: Mapped[int] = mapped_column(Integer, nullable=False)
    impactScore: Mapped[int] = mapped_column(Integer, nullable=False)
    impactLevel: Mapped[str] = mapped_column(String, nullable=False)
    colorHex: Mapped[str] = mapped_column(String, nullable=False)
    actionRequired: Mapped[str] = mapped_column(String, nullable=False)
    responseTimeDays: Mapped[int] = mapped_column(Integer, nullable=False)


# ─────────────────────────────────────────────────────────────────────
# EaiStudy
# ─────────────────────────────────────────────────────────────────────


class EaiStudy(Base, IdMixin):
    __tablename__ = "EaiStudy"

    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False, index=True)
    departmentId: Mapped[str | None] = mapped_column(ForeignKey("Department.id"), index=True)
    areaId: Mapped[str | None] = mapped_column(ForeignKey("Area.id"))
    scopeType: Mapped[str] = mapped_column(String, nullable=False)
    activityIds: Mapped[list | None] = mapped_column(JSON)
    processCode: Mapped[str | None] = mapped_column(String)

    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)

    impactMatrixId: Mapped[str] = mapped_column(ForeignKey("EnvironmentalImpactMatrix.id"), nullable=False)

    teamLeaderId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)

    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT", index=True)
    initiatedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    targetCompletionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approvedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    effectiveFrom: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nextScheduledReviewDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    reviewFrequency: Mapped[str] = mapped_column(String, nullable=False, default="ANNUAL")
    customReviewMonths: Mapped[int | None] = mapped_column(Integer)

    applicableRegulations: Mapped[list | None] = mapped_column(JSON)
    regulatoryReviewRequired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    aggregateMetrics: Mapped[dict | None] = mapped_column(JSON)

    team: Mapped[list["EaiStudyTeamMember"]] = relationship(back_populates="study", cascade="all, delete-orphan")
    entries: Mapped[list["EaiEntry"]] = relationship(back_populates="study", cascade="all, delete-orphan")

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    updatedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))


class EaiStudyTeamMember(Base, IdMixin):
    __tablename__ = "EaiStudyTeamMember"
    __table_args__ = (UniqueConstraint("studyId", "userId", name="EaiStudyTeamMember_studyId_userId_key"),)

    studyId: Mapped[str] = mapped_column(ForeignKey("EaiStudy.id", ondelete="CASCADE"), nullable=False, index=True)
    study: Mapped[EaiStudy] = relationship(back_populates="team")
    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    teamRole: Mapped[str] = mapped_column(String, nullable=False)
    department: Mapped[str | None] = mapped_column(String)
    signedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signedNote: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ─────────────────────────────────────────────────────────────────────
# EaiEntry
# ─────────────────────────────────────────────────────────────────────


class EaiEntry(Base, IdMixin):
    __tablename__ = "EaiEntry"
    __table_args__ = (UniqueConstraint("studyId", "sequenceNumber", name="EaiEntry_studyId_sequenceNumber_key"),)

    studyId: Mapped[str] = mapped_column(ForeignKey("EaiStudy.id", ondelete="CASCADE"), nullable=False, index=True)
    study: Mapped[EaiStudy] = relationship(back_populates="entries")

    sequenceNumber: Mapped[int] = mapped_column(Integer, nullable=False)
    groupLabel: Mapped[str | None] = mapped_column(String)

    activityDescription: Mapped[str] = mapped_column(Text, nullable=False)
    areaId: Mapped[str | None] = mapped_column(ForeignKey("Area.id"))
    subLocation: Mapped[str | None] = mapped_column(String)
    occurrence: Mapped[str] = mapped_column(String, nullable=False)
    frequency: Mapped[str] = mapped_column(String, nullable=False)
    typicalDurationMin: Mapped[int | None] = mapped_column(Integer)

    equipmentUsed: Mapped[list | None] = mapped_column(JSON)
    materialsUsed: Mapped[list | None] = mapped_column(JSON)
    processInputs: Mapped[list | None] = mapped_column(JSON)

    initialLikelihoodId: Mapped[str] = mapped_column(
        ForeignKey("EnvironmentalImpactMatrixLikelihood.id"), nullable=False
    )
    initialLikelihoodScore: Mapped[int] = mapped_column(Integer, nullable=False)
    initialLikelihoodRationale: Mapped[str | None] = mapped_column(Text)
    initialMagnitudeId: Mapped[str] = mapped_column(
        ForeignKey("EnvironmentalImpactMatrixMagnitude.id"), nullable=False
    )
    initialMagnitudeScore: Mapped[int] = mapped_column(Integer, nullable=False)
    initialMagnitudeRationale: Mapped[str | None] = mapped_column(Text)
    initialImpactScore: Mapped[int] = mapped_column(Integer, nullable=False)
    initialImpactLevel: Mapped[str] = mapped_column(String, nullable=False, index=True)
    initialImpactColor: Mapped[str | None] = mapped_column(String)
    initialSignificant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    residualLikelihoodId: Mapped[str | None] = mapped_column(ForeignKey("EnvironmentalImpactMatrixLikelihood.id"))
    residualLikelihoodScore: Mapped[int | None] = mapped_column(Integer)
    residualLikelihoodRationale: Mapped[str | None] = mapped_column(Text)
    residualMagnitudeId: Mapped[str | None] = mapped_column(ForeignKey("EnvironmentalImpactMatrixMagnitude.id"))
    residualMagnitudeScore: Mapped[int | None] = mapped_column(Integer)
    residualMagnitudeRationale: Mapped[str | None] = mapped_column(Text)
    residualImpactScore: Mapped[int | None] = mapped_column(Integer)
    residualImpactLevel: Mapped[str | None] = mapped_column(String, index=True)
    residualImpactColor: Mapped[str | None] = mapped_column(String)
    residualAcceptable: Mapped[bool | None] = mapped_column(Boolean)
    residualAcceptanceRationale: Mapped[str | None] = mapped_column(Text)
    residualSignificant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    legalComplianceStatus: Mapped[str | None] = mapped_column(String)

    linkedHiraEntryIds: Mapped[list | None] = mapped_column(JSON)
    triggersTrainingProgramIds: Mapped[list | None] = mapped_column(JSON)
    triggersInspectionTypeIds: Mapped[list | None] = mapped_column(JSON)
    triggersComplianceTaskIds: Mapped[list | None] = mapped_column(JSON)

    lastReviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastReviewedById: Mapped[str | None] = mapped_column(String)
    nextReviewDue: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lastReviewType: Mapped[str | None] = mapped_column(String)
    triggeredByRecordId: Mapped[str | None] = mapped_column(String)

    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT", index=True)

    versionNumber: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    isCurrentVersion: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    parentVersionId: Mapped[str | None] = mapped_column(String)

    aspects: Mapped[list["EaiEntryAspect"]] = relationship(back_populates="entry", cascade="all, delete-orphan")
    impacts: Mapped[list["EaiEntryImpact"]] = relationship(back_populates="entry", cascade="all, delete-orphan")
    existingControls: Mapped[list["EaiEntryControl"]] = relationship(back_populates="entry", cascade="all, delete-orphan")
    recommendedControls: Mapped[list["EaiEntryRecommendedControl"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )
    complianceObligations: Mapped[list["EaiComplianceObligation"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )
    regulationRefs: Mapped[list["EaiEntryRegulationRef"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    updatedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))


class EaiEntryAspect(Base, IdMixin):
    __tablename__ = "EaiEntryAspect"
    __table_args__ = (UniqueConstraint("entryId", "aspectId", name="EaiEntryAspect_entryId_aspectId_key"),)

    entryId: Mapped[str] = mapped_column(ForeignKey("EaiEntry.id", ondelete="CASCADE"), nullable=False, index=True)
    entry: Mapped[EaiEntry] = relationship(back_populates="aspects")
    aspectId: Mapped[str] = mapped_column(ForeignKey("EaiAspect.id"), nullable=False)

    contextualDescription: Mapped[str | None] = mapped_column(Text)
    quantification: Mapped[dict | None] = mapped_column(JSON)
    occurrence: Mapped[str | None] = mapped_column(String)

    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EaiEntryImpact(Base, IdMixin):
    __tablename__ = "EaiEntryImpact"

    entryId: Mapped[str] = mapped_column(ForeignKey("EaiEntry.id", ondelete="CASCADE"), nullable=False, index=True)
    entry: Mapped[EaiEntry] = relationship(back_populates="impacts")

    description: Mapped[str] = mapped_column(Text, nullable=False)
    affectedReceptor: Mapped[str] = mapped_column(String, nullable=False)
    impactType: Mapped[str] = mapped_column(String, nullable=False)
    reversibility: Mapped[str] = mapped_column(String, nullable=False)
    geographicExtent: Mapped[str] = mapped_column(String, nullable=False)
    temporalExtent: Mapped[str] = mapped_column(String, nullable=False)

    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EaiEntryControl(Base, IdMixin):
    __tablename__ = "EaiEntryControl"

    entryId: Mapped[str] = mapped_column(ForeignKey("EaiEntry.id", ondelete="CASCADE"), nullable=False, index=True)
    entry: Mapped[EaiEntry] = relationship(back_populates="existingControls")

    hierarchy: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    effectiveness: Mapped[str | None] = mapped_column(String)
    verificationMethod: Mapped[str | None] = mapped_column(String)
    verificationFreq: Mapped[str | None] = mapped_column(String)
    responsibleRole: Mapped[str | None] = mapped_column(String)
    evidenceAttached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    monitoringPoint: Mapped[str | None] = mapped_column(String)
    monitoringParameter: Mapped[str | None] = mapped_column(String)
    monitoringFrequency: Mapped[str | None] = mapped_column(String)

    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EaiEntryRecommendedControl(Base, IdMixin):
    __tablename__ = "EaiEntryRecommendedControl"

    entryId: Mapped[str] = mapped_column(ForeignKey("EaiEntry.id", ondelete="CASCADE"), nullable=False, index=True)
    entry: Mapped[EaiEntry] = relationship(back_populates="recommendedControls")

    hierarchy: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)

    targetLikelihoodReduction: Mapped[int | None] = mapped_column(Integer)
    targetMagnitudeReduction: Mapped[int | None] = mapped_column(Integer)
    estimatedCostBand: Mapped[str | None] = mapped_column(String)

    proposedImplementationDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responsibleUserId: Mapped[str | None] = mapped_column(String)

    status: Mapped[str] = mapped_column(String, nullable=False, default="PROPOSED")

    capaId: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class EaiComplianceObligation(Base, IdMixin):
    __tablename__ = "EaiComplianceObligation"

    entryId: Mapped[str] = mapped_column(ForeignKey("EaiEntry.id", ondelete="CASCADE"), nullable=False, index=True)
    entry: Mapped[EaiEntry] = relationship(back_populates="complianceObligations")

    regulationCode: Mapped[str] = mapped_column(String, nullable=False)
    section: Mapped[str | None] = mapped_column(String)
    parameter: Mapped[str] = mapped_column(String, nullable=False)
    permittedLimit: Mapped[str] = mapped_column(String, nullable=False)
    monitoringFrequency: Mapped[str] = mapped_column(String, nullable=False)
    reportingAuthority: Mapped[str | None] = mapped_column(String)
    reportingFrequency: Mapped[str | None] = mapped_column(String)
    nextMonitoringDue: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastMonitoringDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastMonitoringResult: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class EaiEntryRegulationRef(Base, IdMixin):
    __tablename__ = "EaiEntryRegulationRef"

    entryId: Mapped[str] = mapped_column(ForeignKey("EaiEntry.id", ondelete="CASCADE"), nullable=False, index=True)
    entry: Mapped[EaiEntry] = relationship(back_populates="regulationRefs")

    regulationCode: Mapped[str] = mapped_column(String, nullable=False)
    section: Mapped[str | None] = mapped_column(String)
    requirementSummary: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EaiReviewCycle(Base, IdMixin):
    __tablename__ = "EaiReviewCycle"

    entryId: Mapped[str] = mapped_column(ForeignKey("EaiEntry.id", ondelete="CASCADE"), nullable=False, index=True)

    scheduledFor: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    triggeredBy: Mapped[str] = mapped_column(String, nullable=False)
    triggerReferenceId: Mapped[str | None] = mapped_column(String)

    status: Mapped[str] = mapped_column(String, nullable=False, default="SCHEDULED", index=True)

    assignedToId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    assignedRole: Mapped[str | None] = mapped_column(String)

    startedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))

    outcome: Mapped[str | None] = mapped_column(String)
    outcomeNotes: Mapped[str | None] = mapped_column(Text)

    changesMade: Mapped[list | None] = mapped_column(JSON)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EaiVersion(Base, IdMixin):
    __tablename__ = "EaiVersion"
    __table_args__ = (UniqueConstraint("entryId", "versionNumber", name="EaiVersion_entryId_versionNumber_key"),)

    entryId: Mapped[str] = mapped_column(ForeignKey("EaiEntry.id", ondelete="CASCADE"), nullable=False, index=True)

    versionNumber: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    changes: Mapped[list] = mapped_column(JSON, nullable=False)

    changeReason: Mapped[str] = mapped_column(Text, nullable=False)
    changeTrigger: Mapped[str] = mapped_column(String, nullable=False)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)


class EaiFeatureFlag(Base, IdMixin):
    __tablename__ = "EaiFeatureFlag"

    plantId: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    eaiRegisterEnabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    combinedRegisterEnabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    riskDashboardEnabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hiraAssistantV2Enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    enabledAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enabledById: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = [
    "EaiAspectCategory",
    "EaiAspect",
    "EaiReceptor",
    "EaiRegulation",
    "EnvironmentalImpactMatrix",
    "EnvironmentalImpactMatrixLikelihood",
    "EnvironmentalImpactMatrixMagnitude",
    "EnvironmentalImpactMatrixCell",
    "EaiStudy",
    "EaiStudyTeamMember",
    "EaiEntry",
    "EaiEntryAspect",
    "EaiEntryImpact",
    "EaiEntryControl",
    "EaiEntryRecommendedControl",
    "EaiComplianceObligation",
    "EaiEntryRegulationRef",
    "EaiReviewCycle",
    "EaiVersion",
    "EaiFeatureFlag",
]
