"""Pydantic schemas for ERM Phase 3 (BCM + Scenario). camelCase; API contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DIMS = Literal["FINANCIAL", "REPUTATIONAL", "REGULATORY", "SAFETY", "BUSINESS_INTERRUPTION"]


# ── BIA ──────────────────────────────────────────────────────────────────────
class ImpactProfileRow(BaseModel):
    dimension: DIMS
    at4h: int = Field(ge=1, le=5)
    at24h: int = Field(ge=1, le=5)
    at7d: int = Field(ge=1, le=5)
    at30d: int = Field(ge=1, le=5)


class DependencyUpsert(BaseModel):
    dependencyType: Literal["UPSTREAM_PROCESS", "IT_SYSTEM", "EQUIPMENT", "VENDOR", "PEOPLE_SKILL", "UTILITY", "FACILITY"]
    name: str
    description: str | None = None
    isSinglePointOfFailure: bool = False
    workaround: str | None = None
    workaroundDurationHours: int | None = None
    linkedEntityRef: str | None = None


class DependencyOut(DependencyUpsert):
    model_config = ConfigDict(from_attributes=True)
    id: str
    processId: str
    unmitigatedSpof: bool = False


class ProcessUpsert(BaseModel):
    name: str = Field(min_length=3)
    description: str = ""
    siteId: str | None = None
    ownerId: str
    departmentName: str = ""
    rtoHours: int = Field(ge=0)
    rpoHours: int | None = None
    mtpdHours: int = Field(ge=0)
    peakPeriods: str | None = None
    impactProfile: list[ImpactProfileRow] = []
    linkedRiskIds: list[str] = []
    criticalityOverride: str | None = None
    criticalityOverrideJustification: str | None = None


class ProcessListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    processCode: str
    name: str
    siteId: str | None = None
    siteName: str | None = None
    ownerId: str
    ownerName: str | None = None
    departmentName: str = ""
    rtoHours: int
    rpoHours: int | None = None
    mtpdHours: int
    criticality: str
    biaStatus: str
    nextBiaReviewDate: datetime | None = None
    reviewOverdue: bool = False
    unmitigatedSpofCount: int = 0
    planCoverageCount: int = 0
    isCovered: bool = False
    linkedRiskIds: list[str] = []
    updatedAt: datetime | None = None


class ProcessDetail(ProcessListItem):
    description: str = ""
    peakPeriods: str | None = None
    impactProfile: list[dict[str, Any]] = []
    criticalityOverrideJustification: str | None = None
    approvedBy: str | None = None
    lastBiaDate: datetime | None = None
    dependencies: list[DependencyOut] = []
    coveringPlans: list[dict[str, Any]] = []
    linkedRisks: list[dict[str, Any]] = []
    createdAt: datetime


class ProcessListResponse(BaseModel):
    items: list[ProcessListItem]
    total: int
    criticalityCounts: dict[str, int] = {}


class BcmDashboard(BaseModel):
    criticalProcesses: int
    coveragePct: float
    coveredCritical: int
    totalCritical: int
    coverageGaps: list[dict[str, Any]] = []  # uncovered VITAL/ESSENTIAL processes
    unmitigatedSpofs: int
    plansReviewDue: int
    exercisesOverdue: int
    openExerciseCapas: int
    exerciseProgramme: list[dict[str, Any]] = []  # 12-month timeline
    recentCrises: list[dict[str, Any]] = []
    activeCrises: int = 0


class DepMapNode(BaseModel):
    id: str
    label: str
    nodeType: str  # PROCESS | dependency type
    criticality: str | None = None
    isSpof: bool = False
    siteId: str | None = None


class DepMapEdge(BaseModel):
    id: str
    source: str  # processId
    target: str  # dependency node id
    dependencyType: str
    isSpof: bool = False


class DependencyMap(BaseModel):
    nodes: list[DepMapNode] = []
    edges: list[DepMapEdge] = []


# ── Plans ────────────────────────────────────────────────────────────────────
class PlanSectionIn(BaseModel):
    orderIndex: int = 0
    heading: str
    contentRichText: str = ""
    attachments: list[str] = []


class RecoveryTaskIn(BaseModel):
    orderIndex: int = 0
    title: str
    detail: str | None = None
    responsibleRoleName: str
    targetHoursFromActivation: int = 0


class PlanUpsert(BaseModel):
    title: str = Field(min_length=3)
    planType: Literal["BUSINESS_CONTINUITY", "DISASTER_RECOVERY_IT", "CRISIS_MANAGEMENT", "EMERGENCY_RESPONSE_LINK"]
    siteId: str | None = None
    ownerId: str
    coveredProcessIds: list[str] = []
    scopeStatement: str = ""
    activationCriteria: list[str] = []
    sections: list[PlanSectionIn] = []
    strategySummary: str = ""
    fserPlanRef: str | None = None
    recoveryTasks: list[RecoveryTaskIn] = []


class RecoveryTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    planId: str
    orderIndex: int
    title: str
    detail: str | None = None
    responsibleRoleName: str
    targetHoursFromActivation: int


class PlanListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    planCode: str
    title: str
    planType: str
    siteId: str | None = None
    siteName: str | None = None
    ownerId: str
    ownerName: str | None = None
    coveredProcessCount: int = 0
    version: int
    status: str
    healthChip: str = "UNKNOWN"  # HEALTHY | STALE | AT_RISK | DRAFT
    nextReviewDate: datetime | None = None
    lastExercisedAt: datetime | None = None
    exerciseOverdue: bool = False
    updatedAt: datetime | None = None


class PlanDetail(PlanListItem):
    scopeStatement: str = ""
    activationCriteria: list[str] = []
    sections: list[dict[str, Any]] = []
    strategySummary: str = ""
    fserPlanRef: str | None = None
    versionSnapshots: list[dict[str, Any]] = []
    recoveryTasks: list[RecoveryTaskOut] = []
    coveredProcesses: list[dict[str, Any]] = []
    approvedBy: str | None = None
    approvedAt: datetime | None = None
    openExerciseCapas: int = 0
    createdAt: datetime


class PlanListResponse(BaseModel):
    items: list[PlanListItem]
    total: int
    statusCounts: dict[str, int] = {}


# ── Crisis ───────────────────────────────────────────────────────────────────
class TeamRoleUpsert(BaseModel):
    roleName: str
    siteId: str | None = None
    primaryUserId: str
    alternateUserId: str
    responsibilities: str = ""
    escalationOrder: int = 0


class TeamRoleOut(TeamRoleUpsert):
    model_config = ConfigDict(from_attributes=True)
    id: str
    primaryUserName: str | None = None
    alternateUserName: str | None = None
    vacancy: bool = False


class CallTreeUpsert(BaseModel):
    name: str
    siteId: str | None = None
    nodes: list[dict[str, Any]] = []


class CallTreeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    siteId: str | None = None
    nodes: list[dict[str, Any]] = []
    publishedAt: datetime | None = None
    staleContacts: int = 0


class CrisisActivate(BaseModel):
    title: str = Field(min_length=3)
    siteId: str | None = None
    activatedPlanIds: list[str] = []
    severityLevel: int = Field(ge=1, le=3)
    linkedRiskIds: list[str] = []
    linkedIncidentId: str | None = None


class LogEntryCreate(BaseModel):
    entryType: Literal["DECISION", "ACTION", "COMMUNICATION", "STATUS_UPDATE", "TASK_CHECK"]
    content: str = Field(min_length=1)
    recoveryTaskId: str | None = None


class LogEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    crisisId: str
    timestamp: datetime
    enteredBy: str
    enteredByName: str | None = None
    entryType: str
    content: str
    recoveryTaskId: str | None = None


class CrisisListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    crisisCode: str
    title: str
    siteId: str | None = None
    siteName: str | None = None
    status: str
    severityLevel: int
    activatedAt: datetime
    activatedByName: str | None = None
    standDownAt: datetime | None = None
    durationMinutes: int | None = None
    logEntryCount: int = 0
    postCrisisReviewDone: bool = False


class CrisisDetail(CrisisListItem):
    activatedPlanIds: list[str] = []
    linkedRiskIds: list[str] = []
    linkedIncidentId: str | None = None
    reviewNote: str | None = None
    reviewCapaId: str | None = None
    cachedPlanContent: list[dict[str, Any]] = []
    recoveryTasks: list[dict[str, Any]] = []  # merged from activated plans + check status
    logEntries: list[LogEntryOut] = []
    teamRoster: list[dict[str, Any]] = []
    fserPanel: dict[str, Any] | None = None  # null/unavailable degrades gracefully
    createdAt: datetime


class SeverityChange(BaseModel):
    severityLevel: int = Field(ge=1, le=3)


class CrisisClose(BaseModel):
    reviewNote: str | None = None
    reviewCapaId: str | None = None


# ── Exercises ──────────────────────────────────────────────────────────────────
class ExerciseUpsert(BaseModel):
    title: str = Field(min_length=3)
    exerciseType: Literal["DESK_CHECK", "TABLETOP", "SIMULATION", "FULL_INTERRUPTION_TEST", "CALL_TREE_TEST"]
    scheduledDate: datetime
    siteId: str | None = None
    testedPlanIds: list[str] = []
    testedScenarioId: str | None = None
    facilitatorId: str
    participants: list[str] = []
    objectives: list[str] = []


class FindingCreate(BaseModel):
    description: str = Field(min_length=1)
    severity: Literal["OBSERVATION", "MINOR_GAP", "MAJOR_GAP"]


class FindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    exerciseId: str
    description: str
    severity: str
    capaId: str | None = None


class ExerciseComplete(BaseModel):
    outcome: Literal["MET_OBJECTIVES", "PARTIALLY_MET", "NOT_MET"]
    conductedDate: datetime | None = None
    rtoAchievedHours: int | None = None
    reportRichText: str = ""


class ExerciseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    exerciseCode: str
    title: str
    exerciseType: str
    scheduledDate: datetime
    siteId: str | None = None
    siteName: str | None = None
    testedPlanIds: list[str] = []
    testedScenarioId: str | None = None
    facilitatorId: str
    facilitatorName: str | None = None
    participants: list[str] = []
    objectives: list[str] = []
    status: str
    conductedDate: datetime | None = None
    outcome: str | None = None
    rtoAchievedHours: int | None = None
    callTreeStats: dict[str, Any] | None = None
    reportRichText: str | None = None
    findings: list[FindingOut] = []
    openCapaCount: int = 0


class ExerciseListResponse(BaseModel):
    items: list[ExerciseOut]
    total: int
    statusCounts: dict[str, int] = {}


# ── Scenario / Horizon ─────────────────────────────────────────────────────────
class WhatIfAdj(BaseModel):
    riskId: str
    stressedLikelihood: int = Field(ge=1, le=5)
    stressedImpact: int = Field(ge=1, le=5)


class ImpactEstimate(BaseModel):
    dimension: DIMS
    estimatedLevel: int = Field(ge=1, le=5)
    estimateBasisNotes: str = ""
    estimatedGrossInr: float | None = None


class ScenarioUpsert(BaseModel):
    title: str = Field(min_length=3)
    category: str
    narrative: str = ""
    probabilityQualitative: Literal["REMOTE", "POSSIBLE", "PLAUSIBLE", "LIKELY"] = "POSSIBLE"
    timeHorizon: Literal["0_12_MONTHS", "1_3_YEARS", "3_PLUS_YEARS"] = "1_3_YEARS"
    affectedRiskIds: list[str] = []
    affectedProcessIds: list[str] = []
    impactEstimates: list[ImpactEstimate] = []
    whatIfAdjustments: list[WhatIfAdj] = []


class ScenarioOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    scenarioCode: str
    title: str
    category: str
    narrative: str = ""
    probabilityQualitative: str
    timeHorizon: str
    affectedRiskIds: list[str] = []
    affectedProcessIds: list[str] = []
    impactEstimates: list[dict[str, Any]] = []
    whatIfAdjustments: list[dict[str, Any]] = []
    mitigationReadiness: str
    status: str
    lastReviewedAt: datetime | None = None
    topImpactLevel: int | None = None
    updatedAt: datetime | None = None


class StressedCell(BaseModel):
    likelihood: int
    impact: int
    count: int
    band: str
    riskIds: list[str] = []


class StressedHeatMap(BaseModel):
    scenarioId: str
    scenarioTitle: str
    baseline: list[StressedCell] = []
    stressed: list[StressedCell] = []
    movements: list[dict[str, Any]] = []  # {riskId, riskCode, fromL, fromI, toL, toI}


class HorizonUpsert(BaseModel):
    title: str = Field(min_length=3)
    description: str = ""
    category: str
    signalStrength: Literal["WEAK", "EMERGING", "STRONG"] = "WEAK"
    potentialCategoryIds: list[str] = []
    reviewDate: datetime | None = None


class HorizonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    description: str = ""
    category: str
    signalStrength: str
    potentialCategoryIds: list[str] = []
    watchedBy: str
    watchedByName: str | None = None
    reviewDate: datetime
    disposition: str | None = None
    promotedEntityId: str | None = None
    dispositionNote: str | None = None
    reviewOverdue: bool = False


class HorizonDisposition(BaseModel):
    disposition: Literal["PROMOTED_TO_SCENARIO", "PROMOTED_TO_RISK", "DISMISSED"]
    note: str = ""


__all__ = [k for k in dict(globals()) if k[0].isupper()]
