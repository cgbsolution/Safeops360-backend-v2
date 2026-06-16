"""ERM Phase 3 — Business Continuity (ISO 22301) + Scenario Analysis.

Mirrors the Phase 3 Prisma family. camelCase columns. Plain-String refs to
existing tables. CrisisLogEntry is append-only (no updatedAt).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


def _c():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _u():
    return mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)


# ── BIA ──────────────────────────────────────────────────────────────────────
class BusinessProcess(Base, IdMixin):
    __tablename__ = "BusinessProcess"
    processCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    siteId: Mapped[str | None] = mapped_column(String)
    ownerId: Mapped[str] = mapped_column(String, nullable=False)
    departmentName: Mapped[str] = mapped_column(String, nullable=False, default="")
    rtoHours: Mapped[int] = mapped_column(Integer, nullable=False)
    rpoHours: Mapped[int | None] = mapped_column(Integer)
    mtpdHours: Mapped[int] = mapped_column(Integer, nullable=False)
    criticality: Mapped[str] = mapped_column(String, nullable=False, default="IMPORTANT")
    criticalityOverrideJustification: Mapped[str | None] = mapped_column(Text)
    peakPeriods: Mapped[str | None] = mapped_column(String)
    impactProfile: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    linkedRiskIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    biaStatus: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    approvedBy: Mapped[str | None] = mapped_column(String)
    lastBiaDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nextBiaReviewDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dependencies: Mapped[list["ProcessDependency"]] = relationship(back_populates="process", cascade="all, delete-orphan")
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_BusinessProcess_site_crit", "siteId", "criticality"), Index("ix_BusinessProcess_bia", "biaStatus"), Index("ix_BusinessProcess_owner", "ownerId"))


class ProcessDependency(Base, IdMixin):
    __tablename__ = "ProcessDependency"
    processId: Mapped[str] = mapped_column(ForeignKey("BusinessProcess.id", ondelete="CASCADE"), nullable=False)
    process: Mapped[BusinessProcess] = relationship(back_populates="dependencies")
    dependencyType: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    isSinglePointOfFailure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    workaround: Mapped[str | None] = mapped_column(Text)
    workaroundDurationHours: Mapped[int | None] = mapped_column(Integer)
    linkedEntityRef: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_ProcessDependency_process", "processId"), Index("ix_ProcessDependency_spof", "isSinglePointOfFailure"))


# ── Plans ────────────────────────────────────────────────────────────────────
class ContinuityPlan(Base, IdMixin):
    __tablename__ = "ContinuityPlan"
    planCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    planType: Mapped[str] = mapped_column(String, nullable=False)
    siteId: Mapped[str | None] = mapped_column(String)
    ownerId: Mapped[str] = mapped_column(String, nullable=False)
    coveredProcessIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    scopeStatement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    activationCriteria: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    approvedBy: Mapped[str | None] = mapped_column(String)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nextReviewDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sections: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    strategySummary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fserPlanRef: Mapped[str | None] = mapped_column(String)
    versionSnapshots: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    lastExercisedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recoveryTasks: Mapped[list["RecoveryTask"]] = relationship(back_populates="plan", cascade="all, delete-orphan")
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_ContinuityPlan_site_status", "siteId", "status"), Index("ix_ContinuityPlan_type", "planType"), Index("ix_ContinuityPlan_status", "status"))


class RecoveryTask(Base, IdMixin):
    __tablename__ = "RecoveryTask"
    planId: Mapped[str] = mapped_column(ForeignKey("ContinuityPlan.id", ondelete="CASCADE"), nullable=False)
    plan: Mapped[ContinuityPlan] = relationship(back_populates="recoveryTasks")
    orderIndex: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    responsibleRoleName: Mapped[str] = mapped_column(String, nullable=False)
    targetHoursFromActivation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    createdAt: Mapped[datetime] = _c()
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_RecoveryTask_plan_order", "planId", "orderIndex"),)


# ── Crisis ───────────────────────────────────────────────────────────────────
class CrisisTeamRole(Base, IdMixin):
    __tablename__ = "CrisisTeamRole"
    roleName: Mapped[str] = mapped_column(String, nullable=False)
    siteId: Mapped[str | None] = mapped_column(String)
    primaryUserId: Mapped[str] = mapped_column(String, nullable=False)
    alternateUserId: Mapped[str] = mapped_column(String, nullable=False)
    responsibilities: Mapped[str] = mapped_column(Text, nullable=False, default="")
    escalationOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_CrisisTeamRole_site_order", "siteId", "escalationOrder"),)


class CallTree(Base, IdMixin):
    __tablename__ = "CallTree"
    name: Mapped[str] = mapped_column(String, nullable=False)
    siteId: Mapped[str | None] = mapped_column(String)
    nodes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    publishedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_CallTree_site", "siteId"),)


class CrisisEvent(Base, IdMixin):
    __tablename__ = "CrisisEvent"
    crisisCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    siteId: Mapped[str | None] = mapped_column(String)
    activatedPlanIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    linkedRiskIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    linkedIncidentId: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVATED")
    activatedBy: Mapped[str] = mapped_column(String, nullable=False)
    activatedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    severityLevel: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    standDownAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    postCrisisReviewDone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reviewNote: Mapped[str | None] = mapped_column(Text)
    reviewCapaId: Mapped[str | None] = mapped_column(String)
    cachedPlanContent: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    callTreeAck: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    logEntries: Mapped[list["CrisisLogEntry"]] = relationship(back_populates="crisis", cascade="all, delete-orphan")
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_CrisisEvent_site_status", "siteId", "status"), Index("ix_CrisisEvent_status", "status"))


class CrisisLogEntry(Base, IdMixin):
    """Append-only legal record — server timestamp, no updatedAt, no edit/delete API."""
    __tablename__ = "CrisisLogEntry"
    crisisId: Mapped[str] = mapped_column(ForeignKey("CrisisEvent.id", ondelete="CASCADE"), nullable=False)
    crisis: Mapped[CrisisEvent] = relationship(back_populates="logEntries")
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    enteredBy: Mapped[str] = mapped_column(String, nullable=False)
    entryType: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    recoveryTaskId: Mapped[str | None] = mapped_column(String)
    __table_args__ = (Index("ix_CrisisLogEntry_crisis_ts", "crisisId", "timestamp"),)


# ── Exercises ──────────────────────────────────────────────────────────────────
class BcExercise(Base, IdMixin):
    __tablename__ = "BcExercise"
    exerciseCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    exerciseType: Mapped[str] = mapped_column(String, nullable=False)
    scheduledDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    siteId: Mapped[str | None] = mapped_column(String)
    testedPlanIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    testedScenarioId: Mapped[str | None] = mapped_column(String)
    facilitatorId: Mapped[str] = mapped_column(String, nullable=False)
    participants: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    objectives: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PLANNED")
    conductedDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String)
    rtoAchievedHours: Mapped[int | None] = mapped_column(Integer)
    callTreeStats: Mapped[dict | None] = mapped_column(JSON)
    reportRichText: Mapped[str | None] = mapped_column(Text)
    findings: Mapped[list["ExerciseFinding"]] = relationship(back_populates="exercise", cascade="all, delete-orphan")
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_BcExercise_site_status", "siteId", "status"), Index("ix_BcExercise_sched", "scheduledDate"), Index("ix_BcExercise_status", "status"))


class ExerciseFinding(Base, IdMixin):
    __tablename__ = "ExerciseFinding"
    exerciseId: Mapped[str] = mapped_column(ForeignKey("BcExercise.id", ondelete="CASCADE"), nullable=False)
    exercise: Mapped[BcExercise] = relationship(back_populates="findings")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    capaId: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_ExerciseFinding_exercise", "exerciseId"),)


# ── Scenario / Horizon ─────────────────────────────────────────────────────────
class Scenario(Base, IdMixin):
    __tablename__ = "Scenario"
    scenarioCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False, default="")
    probabilityQualitative: Mapped[str] = mapped_column(String, nullable=False, default="POSSIBLE")
    timeHorizon: Mapped[str] = mapped_column(String, nullable=False, default="1_3_YEARS")
    affectedRiskIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    affectedProcessIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    impactEstimates: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    whatIfAdjustments: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    mitigationReadiness: Mapped[str] = mapped_column(String, nullable=False, default="NO_PLAN")
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    lastReviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_Scenario_cat_status", "category", "status"), Index("ix_Scenario_status", "status"))


class HorizonItem(Base, IdMixin):
    __tablename__ = "HorizonItem"
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String, nullable=False)
    signalStrength: Mapped[str] = mapped_column(String, nullable=False, default="WEAK")
    potentialCategoryIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    watchedBy: Mapped[str] = mapped_column(String, nullable=False)
    reviewDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    disposition: Mapped[str | None] = mapped_column(String)
    promotedEntityId: Mapped[str | None] = mapped_column(String)
    dispositionNote: Mapped[str | None] = mapped_column(Text)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_HorizonItem_signal", "signalStrength"), Index("ix_HorizonItem_watcher", "watchedBy"))


__all__ = [
    "BusinessProcess", "ProcessDependency", "ContinuityPlan", "RecoveryTask",
    "CrisisTeamRole", "CallTree", "CrisisEvent", "CrisisLogEntry",
    "BcExercise", "ExerciseFinding", "Scenario", "HorizonItem",
]
