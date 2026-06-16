"""SQLAlchemy mirror of the Skill Matrix (competency-state) Prisma models.

Phase A of the Skill Matrix module. Hand-mirrored, camelCase columns to match
Prisma's column naming (same convention as training.py / masters.py). Prisma
owns the migration and is the source of truth for constraints/indexes; this
mirror exists so the FastAPI layer can read/write the same tables.

Additive-only stance (see SKILL_MATRIX_PHASE0.md §2): relationship()s are
declared ONLY among these new tables. References to pre-existing tables
(User, Plant, TrainingProgram, TrainingCertificate, TrainingAssessment,
HiraEntry, Permit, Role, Department) are plain String columns (FK-by-value) —
so importing this module couples to nothing outside it, and no existing model
changes. `state` is a String, not an Enum (D2).
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
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


# ─── Competency master (spec §3.1) ────────────────────────────────────


class Competency(Base, IdMixin):
    __tablename__ = "Competency"

    plantId: Mapped[str | None] = mapped_column(String, index=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    category: Mapped[str] = mapped_column(String, nullable=False)
    subcategory: Mapped[str | None] = mapped_column(String)

    validationMethods: Mapped[list | None] = mapped_column(JSON, nullable=False)

    relatedTrainingProgramIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    defaultValidityMonths: Mapped[int] = mapped_column(Integer, nullable=False)
    preExpiryWarningDays: Mapped[int] = mapped_column(Integer, default=90)
    gracePeriodDays: Mapped[int] = mapped_column(Integer, default=30)

    prerequisiteCompetencyIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    supersededByCompetencyIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    supersededAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    regulatoryReferences: Mapped[list | None] = mapped_column(JSON)

    enablesRoleIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    enablesPermitTypes: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    enablesActivityTypes: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    reValidationWorkflow: Mapped[str] = mapped_column(String, default="assessment_required")

    isActive: Mapped[bool] = mapped_column(Boolean, default=True)
    isGlobal: Mapped[bool] = mapped_column(Boolean, default=False)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    createdByUserId: Mapped[str] = mapped_column(String, nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )

    records: Mapped[list[CompetencyRecord]] = relationship(back_populates="competency")
    roleRequirements: Mapped[list[RoleCompetencyRequirement]] = relationship(
        back_populates="competency"
    )


# ─── Skill — sub-component of a competency (spec §3.1) ────────────────


class Skill(Base, IdMixin):
    __tablename__ = "Skill"

    plantId: Mapped[str | None] = mapped_column(String, index=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String)

    contributesToCompetencyIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    isActive: Mapped[bool] = mapped_column(Boolean, default=True)
    isGlobal: Mapped[bool] = mapped_column(Boolean, default=False)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


# ─── RoleDefinition — job/position master (spec §3.1, D3) ─────────────


class RoleDefinition(Base, IdMixin):
    __tablename__ = "RoleDefinition"

    # null = global template role definition (applies to all plants).
    plantId: Mapped[str | None] = mapped_column(String, index=True)
    roleMasterId: Mapped[str | None] = mapped_column(String)  # FK-by-value Role.id
    roleName: Mapped[str] = mapped_column(String, nullable=False)

    appliesToDepartments: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    appliesToPlants: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    minimumExperience: Mapped[dict | None] = mapped_column(JSON)
    medicalFitnessRequirements: Mapped[list | None] = mapped_column(JSON)
    authorityLimits: Mapped[dict | None] = mapped_column(JSON)

    isActive: Mapped[bool] = mapped_column(Boolean, default=True)
    effectiveFrom: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    supersededByDefinitionId: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )

    requiredCompetencies: Mapped[list[RoleCompetencyRequirement]] = relationship(
        back_populates="roleDefinition", cascade="all, delete-orphan"
    )
    assignments: Mapped[list[PersonRoleAssignment]] = relationship(
        back_populates="roleDefinition"
    )


# ─── RoleCompetencyRequirement (child of RoleDefinition, spec §3.1) ───


class RoleCompetencyRequirement(Base, IdMixin):
    __tablename__ = "RoleCompetencyRequirement"

    roleDefinitionId: Mapped[str] = mapped_column(
        ForeignKey("RoleDefinition.id", ondelete="CASCADE"), nullable=False, index=True
    )
    competencyId: Mapped[str] = mapped_column(
        ForeignKey("Competency.id"), nullable=False, index=True
    )

    requirementType: Mapped[str] = mapped_column(String, nullable=False)
    conditionalLogic: Mapped[str | None] = mapped_column(String)
    gracePeriodForNewHiresDays: Mapped[int] = mapped_column(Integer, default=0)
    rationale: Mapped[str | None] = mapped_column(Text)

    roleDefinition: Mapped[RoleDefinition] = relationship(back_populates="requiredCompetencies")
    competency: Mapped[Competency] = relationship(back_populates="roleRequirements")


# ─── CompetencyRecord — the validated state for a person (spec §3.2) ──


class CompetencyRecord(Base, IdMixin):
    __tablename__ = "CompetencyRecord"
    __table_args__ = (
        UniqueConstraint("personUserId", "competencyId", name="uq_competency_record_person"),
    )

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    personUserId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    competencyId: Mapped[str] = mapped_column(
        ForeignKey("Competency.id"), nullable=False, index=True
    )

    state: Mapped[str] = mapped_column(String, default="not_yet_attempted")

    currentValidatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    currentValidatedByUserId: Mapped[str | None] = mapped_column(String)
    currentValidationMethod: Mapped[str | None] = mapped_column(String)
    currentScore: Mapped[float | None] = mapped_column(Float)
    externalCertificateReference: Mapped[str | None] = mapped_column(String)
    externalCertificateAuthority: Mapped[str | None] = mapped_column(String)
    externalCertificateUrl: Mapped[str | None] = mapped_column(String)
    currentEvidenceAttachments: Mapped[list | None] = mapped_column(JSON)
    validFrom: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validUntil: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    conditions: Mapped[str | None] = mapped_column(Text)
    restrictions: Mapped[str | None] = mapped_column(Text)

    requiredValidationsTotal: Mapped[int] = mapped_column(Integer, default=0)
    requiredValidationsCompleted: Mapped[int] = mapped_column(Integer, default=0)
    lastProgressEventAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimatedCompletionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    nextRevalidationDue: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    revalidationHistory: Mapped[list | None] = mapped_column(JSON)
    suspensionHistory: Mapped[list | None] = mapped_column(JSON)

    relatedTrainingRecords: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    relatedAssessments: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    relatedSupervisions: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    createdByUserId: Mapped[str] = mapped_column(String, nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
    updatedByUserId: Mapped[str | None] = mapped_column(String)
    versionNumber: Mapped[int] = mapped_column(Integer, default=1)

    competency: Mapped[Competency] = relationship(back_populates="records")
    attempts: Mapped[list[CompetencyValidationAttempt]] = relationship(
        back_populates="record", cascade="all, delete-orphan"
    )
    versions: Mapped[list[CompetencyRecordVersion]] = relationship(
        back_populates="record", cascade="all, delete-orphan"
    )
    assessments: Mapped[list[CompetencyAssessment]] = relationship(
        back_populates="record", cascade="all, delete-orphan"
    )
    supervisions: Mapped[list[SupervisedPerformanceRecord]] = relationship(
        back_populates="record", cascade="all, delete-orphan"
    )


# ─── CompetencyValidationAttempt (child, spec §3.2) ───────────────────


class CompetencyValidationAttempt(Base, IdMixin):
    __tablename__ = "CompetencyValidationAttempt"

    recordId: Mapped[str] = mapped_column(
        ForeignKey("CompetencyRecord.id", ondelete="CASCADE"), nullable=False, index=True
    )
    method: Mapped[str] = mapped_column(String, nullable=False)
    startedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    assessorUserId: Mapped[str | None] = mapped_column(String)
    evidenceAttachments: Mapped[list | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)

    record: Mapped[CompetencyRecord] = relationship(back_populates="attempts")


# ─── CompetencyRecordVersion — immutable audit/version (D8) ───────────


class CompetencyRecordVersion(Base, IdMixin):
    __tablename__ = "CompetencyRecordVersion"
    __table_args__ = (
        UniqueConstraint("recordId", "versionNumber", name="uq_competency_version"),
    )

    recordId: Mapped[str] = mapped_column(
        ForeignKey("CompetencyRecord.id", ondelete="CASCADE"), nullable=False, index=True
    )
    versionNumber: Mapped[int] = mapped_column(Integer, nullable=False)

    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=False)
    changes: Mapped[list | None] = mapped_column(JSON, nullable=False)

    changeReason: Mapped[str] = mapped_column(Text, nullable=False)
    changeTrigger: Mapped[str] = mapped_column(String, nullable=False)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    createdById: Mapped[str] = mapped_column(String, nullable=False)

    record: Mapped[CompetencyRecord] = relationship(back_populates="versions")


# ─── PersonRoleAssignment — companion to UserRole (spec §3.2, D4) ─────


class PersonRoleAssignment(Base, IdMixin):
    __tablename__ = "PersonRoleAssignment"

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    personUserId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    roleDefinitionId: Mapped[str] = mapped_column(
        ForeignKey("RoleDefinition.id"), nullable=False, index=True
    )

    isPrimary: Mapped[bool] = mapped_column(Boolean, default=False)

    assignedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    assignedByUserId: Mapped[str] = mapped_column(String, nullable=False)
    assignmentRationale: Mapped[str | None] = mapped_column(Text)

    effectiveFrom: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    effectiveUntil: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    competencyAssessmentAtAssignment: Mapped[dict | None] = mapped_column(JSON)
    operatingUnderGracePeriod: Mapped[bool] = mapped_column(Boolean, default=False)
    gracePeriodExpires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String, default="active")
    statusHistory: Mapped[list | None] = mapped_column(JSON)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )

    roleDefinition: Mapped[RoleDefinition] = relationship(back_populates="assignments")


# ─── CompetencyAssessment — standalone assessment (spec §3.2, D5) ─────


class CompetencyAssessment(Base, IdMixin):
    __tablename__ = "CompetencyAssessment"

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    recordId: Mapped[str] = mapped_column(
        ForeignKey("CompetencyRecord.id", ondelete="CASCADE"), nullable=False, index=True
    )
    personUserId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    competencyId: Mapped[str] = mapped_column(String, nullable=False)

    assessmentType: Mapped[str] = mapped_column(String, nullable=False)
    scheduledAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    conductedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    location: Mapped[str | None] = mapped_column(String)

    assessorUserId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    assessorRole: Mapped[str | None] = mapped_column(String)

    assessmentTemplateId: Mapped[str | None] = mapped_column(String)
    questionsCount: Mapped[int] = mapped_column(Integer, default=0)
    durationMinutes: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String, default="scheduled")
    rawScore: Mapped[float | None] = mapped_column(Float)
    maximumScore: Mapped[float | None] = mapped_column(Float)
    percentageScore: Mapped[float | None] = mapped_column(Float)
    minimumPassScore: Mapped[float] = mapped_column(Float, default=0)
    result: Mapped[str | None] = mapped_column(String)

    scoringBreakdown: Mapped[list | None] = mapped_column(JSON)

    assessorObservations: Mapped[str | None] = mapped_column(Text)
    assesseeFeedback: Mapped[str | None] = mapped_column(Text)
    evidenceAttachments: Mapped[list | None] = mapped_column(JSON)

    competencyValidated: Mapped[bool] = mapped_column(Boolean, default=False)
    remedialActionsRequired: Mapped[str | None] = mapped_column(Text)
    reAssessmentEligibleFrom: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sourceTrainingAssessmentId: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    record: Mapped[CompetencyRecord] = relationship(back_populates="assessments")


# ─── SupervisedPerformanceRecord — OJT (spec §3.2) ────────────────────


class SupervisedPerformanceRecord(Base, IdMixin):
    __tablename__ = "SupervisedPerformanceRecord"

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    recordId: Mapped[str] = mapped_column(
        ForeignKey("CompetencyRecord.id", ondelete="CASCADE"), nullable=False, index=True
    )
    personUserId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    competencyId: Mapped[str] = mapped_column(String, nullable=False)

    activityDescription: Mapped[str] = mapped_column(Text, nullable=False)
    activityDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activityLocation: Mapped[str | None] = mapped_column(String)
    hiraEntryId: Mapped[str | None] = mapped_column(String)
    permitId: Mapped[str | None] = mapped_column(String)

    supervisorUserId: Mapped[str] = mapped_column(String, nullable=False)
    supervisorCompetencyToSupervise: Mapped[str | None] = mapped_column(String)

    performanceRating: Mapped[str] = mapped_column(String, nullable=False)
    observations: Mapped[dict | None] = mapped_column(JSON)

    contributesToValidation: Mapped[bool] = mapped_column(Boolean, default=True)
    attemptNumber: Mapped[int] = mapped_column(Integer, default=1)

    evidenceAttachments: Mapped[list | None] = mapped_column(JSON)
    supervisorSignatureAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superviseeAcknowledgmentAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    record: Mapped[CompetencyRecord] = relationship(back_populates="supervisions")


# ─── RecertificationCycle (spec §4.4) ─────────────────────────────────


class RecertificationCycle(Base, IdMixin):
    __tablename__ = "RecertificationCycle"

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    cycleNumber: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="DRAFT")

    scopeCompetencyIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    scopeRoleIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    scopeDepartmentIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    scopePlantIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    windowStart: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    windowEnd: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ownerUserId: Mapped[str] = mapped_column(String, nullable=False)
    affectedPersonsCount: Mapped[int] = mapped_column(Integer, default=0)
    completedCount: Mapped[int] = mapped_column(Integer, default=0)

    summary: Mapped[dict | None] = mapped_column(JSON)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    createdByUserId: Mapped[str] = mapped_column(String, nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closedByUserId: Mapped[str | None] = mapped_column(String)

    tasks: Mapped[list[RecertificationTask]] = relationship(
        back_populates="cycle", cascade="all, delete-orphan"
    )


# ─── RecertificationTask (child) ──────────────────────────────────────


class RecertificationTask(Base, IdMixin):
    __tablename__ = "RecertificationTask"

    cycleId: Mapped[str] = mapped_column(
        ForeignKey("RecertificationCycle.id", ondelete="CASCADE"), nullable=False, index=True
    )
    personUserId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    competencyId: Mapped[str] = mapped_column(String, nullable=False)
    recordId: Mapped[str | None] = mapped_column(String)

    revalidationMethod: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="not_started")

    scheduledAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    cycle: Mapped[RecertificationCycle] = relationship(back_populates="tasks")
