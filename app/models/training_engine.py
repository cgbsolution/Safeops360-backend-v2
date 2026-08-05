"""Training & Competency Engine — SQLAlchemy models (Trigger + Assignment +
Content Adapter, spec §B/§C/§D).

Additive layer over the Skill-Matrix models (app/models/competency_matrix.py).
Every table is keyed on ``competencyId`` (the "skill node"); the rule engine
never reads ``contentType`` / ``vendorId`` — that is what decouples content
vendors from the logic (spec §C decoupling requirement).

House conventions (same as alerts.py / competency_matrix.py): camelCase columns
to match the Prisma-owned schema, references to pre-existing tables are plain
``String`` FK-by-value columns (no cross-module ``relationship()``), ``state``/
``status``/``type`` fields are String not Enum (D2). Schema is applied by
prisma/apply-training-engine-ddl.ts (hand-DDL, idempotent) — NEVER db push.
Mirrored in schema.prisma for Prisma-client typing + seed-script access.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Float,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


# ── HazardToSkillMapping — admin-configurable classification → competency ──
class HazardToSkillMapping(Base, IdMixin):
    __tablename__ = "HazardToSkillMapping"

    plantId: Mapped[str | None] = mapped_column(String, index=True)  # null = global

    sourceModule: Mapped[str] = mapped_column(String, nullable=False, default="ANY")
    classificationField: Mapped[str] = mapped_column(String, nullable=False)
    classificationValue: Mapped[str] = mapped_column(String, nullable=False)
    matchMode: Mapped[str] = mapped_column(String, nullable=False, default="exact")

    competencyId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    priority: Mapped[int] = mapped_column(Integer, default=100)
    notes: Mapped[str | None] = mapped_column(Text)
    isActive: Mapped[bool] = mapped_column(Boolean, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


# ── TrainingRuleConfig — tenant/plant-configurable thresholds & windows ──
class TrainingRuleConfig(Base, IdMixin):
    __tablename__ = "TrainingRuleConfig"

    plantId: Mapped[str | None] = mapped_column(String, index=True)  # null = global default

    thresholdCount: Mapped[int] = mapped_column(Integer, default=3)
    thresholdWindowDays: Mapped[int] = mapped_column(Integer, default=90)

    severitySifImmediate: Mapped[bool] = mapped_column(Boolean, default=True)
    severityThreshold: Mapped[str] = mapped_column(String, default="HIGH")

    recertWindowDays: Mapped[int] = mapped_column(Integer, default=30)
    assignmentDueDays: Mapped[int] = mapped_column(Integer, default=30)
    correlationWindowDays: Mapped[int] = mapped_column(Integer, default=90)

    # Person-risk analytics (repeat-involvement flag)
    personFlagThreshold: Mapped[int] = mapped_column(Integer, default=2)
    personFlagWindowDays: Mapped[int] = mapped_column(Integer, default=365)
    personRiskElevated: Mapped[int] = mapped_column(Integer, default=3)
    personRiskHigh: Mapped[int] = mapped_column(Integer, default=6)
    personRiskCritical: Mapped[int] = mapped_column(Integer, default=10)

    isActive: Mapped[bool] = mapped_column(Boolean, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
    updatedBy: Mapped[str | None] = mapped_column(String)


# ── TrainingAssignment — first-class assignment with provenance ──────────
class TrainingAssignment(Base, IdMixin):
    __tablename__ = "TrainingAssignment"

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    personUserId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    competencyId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    source: Mapped[str] = mapped_column(String, nullable=False)  # threshold_rule|severity_rule|recert_rule|manual
    ruleType: Mapped[str | None] = mapped_column(String)

    sourceModule: Mapped[str | None] = mapped_column(String)
    sourceRecordId: Mapped[str | None] = mapped_column(String)
    sourceRecordRef: Mapped[str | None] = mapped_column(String)
    triggerMappingId: Mapped[str | None] = mapped_column(String)

    provenance: Mapped[dict | None] = mapped_column(JSON)

    contentId: Mapped[str | None] = mapped_column(String)

    assignedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    assignedByUserId: Mapped[str | None] = mapped_column(String)
    dueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String, default="assigned", index=True)

    isMandatory: Mapped[bool] = mapped_column(Boolean, default=False)
    dismissible: Mapped[bool] = mapped_column(Boolean, default=True)

    escalationFlag: Mapped[bool] = mapped_column(Boolean, default=False)
    escalatedToUserId: Mapped[str | None] = mapped_column(String)

    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completionEvidenceType: Mapped[str | None] = mapped_column(String)
    completionEvidenceId: Mapped[str | None] = mapped_column(String)
    completionNote: Mapped[str | None] = mapped_column(Text)
    competencyRecordId: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


# ── TrainingContent — vendor-decoupled content adapter (spec §C) ─────────
class TrainingContent(Base, IdMixin):
    __tablename__ = "TrainingContent"

    competencyId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    contentType: Mapped[str] = mapped_column(String, nullable=False)  # video|document|quiz|vr_package|ar_package|external_link
    deliveryMode: Mapped[str] = mapped_column(String, nullable=False)  # hosted|external_redirect|local_package
    contentRef: Mapped[str] = mapped_column(String, nullable=False)  # opaque

    vendorId: Mapped[str | None] = mapped_column(String, index=True)  # null = demo/placeholder
    vendorName: Mapped[str | None] = mapped_column(String)

    durationMinutes: Mapped[int | None] = mapped_column(Integer)
    passingScore: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String, default="en")

    isActive: Mapped[bool] = mapped_column(Boolean, default=True)
    isPrimary: Mapped[bool] = mapped_column(Boolean, default=False)
    plantId: Mapped[str | None] = mapped_column(String)  # null = global catalog

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


# ── TrainingTriggerEvent — dedicated outbox for the rule engine ──────────
class TrainingTriggerEvent(Base, IdMixin):
    __tablename__ = "TrainingTriggerEvent"

    plantId: Mapped[str | None] = mapped_column(String)

    sourceModule: Mapped[str] = mapped_column(String, nullable=False)
    sourceRecordId: Mapped[str] = mapped_column(String, nullable=False)
    sourceRecordRef: Mapped[str | None] = mapped_column(String)
    eventType: Mapped[str] = mapped_column(String, nullable=False, default="classification_saved")

    classification: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    occurredAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    processedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    processingError: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── TrainingCorrelationPoint — the defensible data asset (spec §D) ───────
class TrainingCorrelationPoint(Base, IdMixin):
    __tablename__ = "TrainingCorrelationPoint"

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    competencyId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    personUserId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    assignmentId: Mapped[str | None] = mapped_column(String)

    sourceModule: Mapped[str | None] = mapped_column(String)
    sourceRecordId: Mapped[str | None] = mapped_column(String)
    sourceRecordRef: Mapped[str | None] = mapped_column(String)

    trainingCompletedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    windowDays: Mapped[int] = mapped_column(Integer, default=90)

    preWindowCount: Mapped[int] = mapped_column(Integer, default=0)
    postWindowCount: Mapped[int | None] = mapped_column(Integer)
    computedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── WorkerTrainingFlag — the person-risk analytic ────────────────────────────
class WorkerTrainingFlag(Base, IdMixin):
    __tablename__ = "WorkerTrainingFlag"

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    personUserId: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)

    riskScore: Mapped[float] = mapped_column(Float, default=0.0)
    riskBand: Mapped[str] = mapped_column(String, default="elevated")
    windowDays: Mapped[int] = mapped_column(Integer, default=365)

    incidentCount: Mapped[int] = mapped_column(Integer, default=0)
    nearMissCount: Mapped[int] = mapped_column(Integer, default=0)
    observationCount: Mapped[int] = mapped_column(Integer, default=0)
    sifCount: Mapped[int] = mapped_column(Integer, default=0)
    totalEvents: Mapped[int] = mapped_column(Integer, default=0)

    contributingRecords: Mapped[list | None] = mapped_column(JSON)
    recommendedCompetencies: Mapped[list | None] = mapped_column(JSON)
    mappedCompetencyIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    assignmentIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    status: Mapped[str] = mapped_column(String, default="flagged", index=True)

    flaggedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lastEvaluatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledgedBy: Mapped[str | None] = mapped_column(String)
    acknowledgedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clearedBy: Mapped[str | None] = mapped_column(String)
    clearedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clearReason: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


__all__ = [
    "HazardToSkillMapping",
    "TrainingRuleConfig",
    "TrainingAssignment",
    "TrainingContent",
    "TrainingTriggerEvent",
    "TrainingCorrelationPoint",
    "WorkerTrainingFlag",
]
