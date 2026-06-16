"""Pydantic schemas for the Enterprise Risk Management (ERM) module.

camelCase throughout (no alias_generator) to match the DB / frontend. These
models double as the API contract the Next.js frontend mirrors as TS types.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ─────────────────────────────────────────────────────────────────────
# Taxonomy
# ─────────────────────────────────────────────────────────────────────
class RiskSubCategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    categoryId: str
    code: str
    name: str
    description: str = ""
    isActive: bool


class RiskCategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    description: str = ""
    colorHex: str
    displayOrder: int
    isSystemCategory: bool
    isActive: bool
    subCategories: list[RiskSubCategoryOut] = []


class CategoryUpsert(BaseModel):
    code: str
    name: str
    description: str = ""
    colorHex: str
    displayOrder: int = 0
    isActive: bool = True


class SubCategoryUpsert(BaseModel):
    categoryId: str
    code: str
    name: str
    description: str = ""
    isActive: bool = True


# ─────────────────────────────────────────────────────────────────────
# Scoring matrix
# ─────────────────────────────────────────────────────────────────────
class ScoringMatrixOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    version: int
    isDefault: bool
    isActive: bool
    likelihoodLevels: list[dict[str, Any]] = []
    impactLevels: list[dict[str, Any]] = []
    ratingBands: list[dict[str, Any]] = []
    notes: str | None = None


class MatrixUpdate(BaseModel):
    name: str | None = None
    likelihoodLevels: list[dict[str, Any]] | None = None
    impactLevels: list[dict[str, Any]] | None = None
    ratingBands: list[dict[str, Any]] | None = None
    notes: str | None = None


class MatrixRebandPreview(BaseModel):
    affectedAssessments: int
    message: str


# ─────────────────────────────────────────────────────────────────────
# Assessments
# ─────────────────────────────────────────────────────────────────────
ImpactDimension = Literal[
    "FINANCIAL", "SAFETY", "REPUTATIONAL", "REGULATORY", "BUSINESS_INTERRUPTION"
]


class ImpactScore(BaseModel):
    dimension: ImpactDimension
    level: int = Field(ge=1, le=5)


class AssessmentCreate(BaseModel):
    assessmentType: Literal["INHERENT", "RESIDUAL"]
    likelihood: int = Field(ge=1, le=5)
    impactScores: list[ImpactScore]
    rationale: str = Field(min_length=1)


class RiskAssessmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    riskId: str
    matrixConfigId: str | None = None
    matrixVersion: int | None = None
    assessmentType: str
    likelihood: int
    impactScores: list[dict[str, Any]] = []
    dominantImpactDimension: str
    overallImpact: int
    totalScore: int
    ratingBand: str
    assessmentDate: datetime
    assessedBy: str
    assessedByName: str | None = None
    rationale: str
    isCurrent: bool
    createdAt: datetime


# ─────────────────────────────────────────────────────────────────────
# Linkages
# ─────────────────────────────────────────────────────────────────────
class LinkageCreate(BaseModel):
    sourceRiskId: str
    targetRiskId: str
    linkageType: Literal["TRIGGERS", "AMPLIFIES", "CORRELATED"]
    notes: str = ""


class RiskLinkageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    sourceRiskId: str
    targetRiskId: str
    linkageType: str
    notes: str = ""


# ─────────────────────────────────────────────────────────────────────
# Enterprise risk
# ─────────────────────────────────────────────────────────────────────
class RiskCreate(BaseModel):
    title: str = Field(min_length=4, max_length=160)
    description: str = ""
    categoryId: str
    subCategoryId: str | None = None
    orgLevel: Literal["ENTERPRISE", "BUSINESS_UNIT", "FUNCTION", "SITE"] = "ENTERPRISE"
    businessUnit: str | None = None
    plantId: str | None = None
    riskOwnerId: str
    riskChampionId: str
    velocity: Literal["SLOW", "MODERATE", "FAST", "VERY_FAST"] = "MODERATE"
    appetiteThreshold: int | None = None
    tags: list[str] = []
    causes: list[str] = []
    consequences: list[str] = []
    existingControls: list[str] = []
    reviewOverrideDays: int | None = None
    # Optional inline initial assessment (wizard step 4)
    inherentAssessment: AssessmentCreate | None = None
    residualAssessment: AssessmentCreate | None = None


class RiskUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    description: str | None = None
    categoryId: str | None = None
    subCategoryId: str | None = None
    orgLevel: str | None = None
    businessUnit: str | None = None
    plantId: str | None = None
    riskOwnerId: str | None = None
    riskChampionId: str | None = None
    velocity: str | None = None
    appetiteThreshold: int | None = None
    tags: list[str] | None = None
    causes: list[str] | None = None
    consequences: list[str] | None = None
    existingControls: list[str] | None = None
    nextReviewDate: datetime | None = None
    version: int | None = None  # optimistic lock token


class StateActionBody(BaseModel):
    justification: str | None = None
    notes: str | None = None


class RiskListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    riskCode: str
    title: str
    categoryId: str
    categoryCode: str | None = None
    categoryName: str | None = None
    categoryColor: str | None = None
    subCategoryCode: str | None = None
    orgLevel: str
    businessUnit: str | None = None
    plantId: str | None = None
    plantName: str | None = None
    riskOwnerId: str
    riskOwnerName: str | None = None
    riskChampionId: str
    riskChampionName: str | None = None
    lifecycleState: str
    velocity: str
    sourceType: str
    inherentScore: int | None = None
    inherentBand: str | None = None
    residualLikelihood: int | None = None
    residualImpact: int | None = None
    residualScore: int | None = None
    residualBand: str | None = None
    priorResidualScore: int | None = None
    priorResidualBand: str | None = None
    nextReviewDate: datetime | None = None
    reviewOverdueDays: int = 0
    reviewBadge: str | None = None  # null | AMBER | RED
    openTreatments: int = 0
    appetiteThreshold: int | None = None
    updatedAt: datetime


class RiskListResponse(BaseModel):
    items: list[RiskListItem]
    total: int
    categoryCounts: dict[str, int] = {}
    bandCounts: dict[str, int] = {}
    stateCounts: dict[str, int] = {}


class ContributingEntry(BaseModel):
    id: str
    sourceModule: str
    sourceRegisterEntryId: str
    sourceRef: str | None = None
    contributingScore: int
    contributingBand: str | None = None
    drilldownUrl: str | None = None


class TreatmentOut(BaseModel):
    id: str
    capaNumber: str
    title: str
    treatmentStrategy: str
    state: str
    primaryOwnerUserId: str | None = None
    primaryOwnerName: str | None = None
    closureTargetDate: datetime | None = None
    expectedResidualReduction: int | None = None
    isOpen: bool
    overdue: bool = False


class RiskDetail(RiskListItem):
    description: str = ""
    description_html: str | None = None
    tags: list[str] = []
    causes: list[str] = []
    consequences: list[str] = []
    existingControls: list[str] = []
    appetiteThreshold: int | None = None
    identifiedDate: datetime
    rollupRuleId: str | None = None
    closureJustification: str | None = None
    acceptanceJustification: str | None = None
    acceptedBy: str | None = None
    acceptedByName: str | None = None
    acceptedAt: datetime | None = None
    escalatedAt: datetime | None = None
    isRollup: bool = False
    version: int = 1
    currentInherent: RiskAssessmentOut | None = None
    currentResidual: RiskAssessmentOut | None = None
    assessmentHistory: list[RiskAssessmentOut] = []
    treatments: list[TreatmentOut] = []
    linkages: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    contributingEntries: list[ContributingEntry] = []
    createdAt: datetime


# ─────────────────────────────────────────────────────────────────────
# Treatments (CAPA RISK_TREATMENT extension)
# ─────────────────────────────────────────────────────────────────────
class TreatmentCreate(BaseModel):
    treatmentStrategy: Literal["TREAT", "TOLERATE", "TRANSFER", "TERMINATE"]
    title: str = ""
    description: str = ""
    primaryOwnerUserId: str | None = None
    dueDate: datetime | None = None
    expectedResidualReduction: int | None = None
    acceptanceJustification: str | None = None  # required for TOLERATE


# ─────────────────────────────────────────────────────────────────────
# Reviews
# ─────────────────────────────────────────────────────────────────────
class ReviewCreate(BaseModel):
    outcome: Literal["NO_CHANGE", "RESCORED", "ESCALATED", "RECOMMEND_CLOSURE"]
    notes: str = Field(min_length=1)
    newAssessment: AssessmentCreate | None = None  # when outcome = RESCORED


class RiskReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    riskId: str
    reviewDate: datetime
    reviewedBy: str
    reviewedByName: str | None = None
    outcome: str
    notes: str
    newAssessmentId: str | None = None


class ReviewCalendarItem(BaseModel):
    riskId: str
    riskCode: str
    title: str
    residualBand: str | None = None
    nextReviewDate: datetime | None = None
    overdueDays: int = 0
    reviewBadge: str | None = None
    riskOwnerId: str
    riskOwnerName: str | None = None


# ─────────────────────────────────────────────────────────────────────
# Rollup engine
# ─────────────────────────────────────────────────────────────────────
class RollupFilterCriteria(BaseModel):
    siteIds: list[str] | None = None
    minRiskBand: Literal["HIGH", "CRITICAL"] | None = None
    sourceModules: list[Literal["HIRA", "EAI", "QUALITY_NCR"]] | None = None


class RollupRuleUpsert(BaseModel):
    name: str
    filterCriteria: RollupFilterCriteria = RollupFilterCriteria()
    aggregationMode: Literal["GROUPED", "ONE_TO_ONE"] = "GROUPED"
    targetSubCategoryCode: str
    scoringMode: Literal["MAX", "WEIGHTED_AVERAGE"] = "MAX"
    isActive: bool = True


class RollupRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    sourceRegister: str
    filterCriteria: dict[str, Any] = {}
    aggregationMode: str
    targetCategoryCode: str
    targetSubCategoryCode: str
    scoringMode: str
    isActive: bool
    lastRunAt: datetime | None = None
    lastRunSummary: dict[str, Any] | None = None
    linkedEntryCount: int = 0


class RollupPreviewEntry(BaseModel):
    id: str
    sourceModule: str
    plantId: str
    activityDescription: str
    residualBand: str | None = None
    residualScore: int | None = None


class RollupPreviewResult(BaseModel):
    matched: int
    entries: list[RollupPreviewEntry] = []


class RollupRunResult(BaseModel):
    created: int
    updated: int
    unlinked: int
    matched: int
    enterpriseRiskIds: list[str] = []


# ─────────────────────────────────────────────────────────────────────
# Dashboards / heat map
# ─────────────────────────────────────────────────────────────────────
class HeatMapCell(BaseModel):
    likelihood: int
    impact: int
    count: int
    score: int
    band: str
    riskIds: list[str] = []


class CategoryBarSegment(BaseModel):
    categoryCode: str
    categoryName: str
    colorHex: str
    low: int = 0
    medium: int = 0
    high: int = 0
    critical: int = 0
    total: int = 0


class TopRiskRow(BaseModel):
    rank: int
    id: str
    riskCode: str
    title: str
    categoryCode: str | None = None
    categoryName: str | None = None
    categoryColor: str | None = None
    residualScore: int | None = None
    residualBand: str | None = None
    trend: str = "FLAT"  # UP | DOWN | FLAT (residual vs prior quarter)
    trendDelta: int = 0
    riskOwnerId: str
    riskOwnerName: str | None = None
    daysToReview: int | None = None


class MovementRow(BaseModel):
    id: str
    riskCode: str
    title: str
    fromBand: str | None = None
    toBand: str | None = None
    direction: str  # UP | DOWN


class DashboardSummary(BaseModel):
    totalActiveRisks: int
    criticalResidual: int
    highResidual: int
    overdueReviews: int
    openTreatments: int
    escalatedThisQuarter: int
    inherentHeatMap: list[HeatMapCell] = []
    residualHeatMap: list[HeatMapCell] = []
    categoryBars: list[CategoryBarSegment] = []
    topRisks: list[TopRiskRow] = []
    movement: list[MovementRow] = []


class NetworkNode(BaseModel):
    id: str
    riskCode: str
    title: str
    categoryCode: str | None = None
    categoryColor: str | None = None
    residualScore: int | None = None
    residualBand: str | None = None
    lifecycleState: str


class NetworkEdge(BaseModel):
    id: str
    source: str
    target: str
    linkageType: str
    notes: str = ""


class NetworkGraph(BaseModel):
    nodes: list[NetworkNode] = []
    edges: list[NetworkEdge] = []


# ─────────────────────────────────────────────────────────────────────
# Treatment tracker
# ─────────────────────────────────────────────────────────────────────
class TreatmentTrackerRow(BaseModel):
    id: str
    capaNumber: str
    title: str
    treatmentStrategy: str
    riskId: str
    riskCode: str
    riskTitle: str
    parentResidualBand: str | None = None
    state: str
    primaryOwnerUserId: str | None = None
    primaryOwnerName: str | None = None
    closureTargetDate: datetime | None = None
    overdue: bool = False
    expectedResidualReduction: int | None = None
    achievedResidualReduction: int | None = None


class TreatmentTrackerResponse(BaseModel):
    items: list[TreatmentTrackerRow]
    total: int
    openCount: int
    overdueCount: int
    closedThisQuarter: int
    avgClosureDays: float | None = None


# ─────────────────────────────────────────────────────────────────────
# Board pack
# ─────────────────────────────────────────────────────────────────────
class BoardPackUpsert(BaseModel):
    title: str
    quarterLabel: str
    periodStart: datetime | None = None
    periodEnd: datetime | None = None
    sections: dict[str, Any] = {}
    commentary: dict[str, str] = {}


class BoardPackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    quarterLabel: str
    periodStart: datetime
    periodEnd: datetime
    status: str
    sections: dict[str, Any] = {}
    commentary: dict[str, Any] = {}
    snapshotHash: str | None = None
    generatedAt: datetime | None = None
    publishedAt: datetime | None = None
    publishedBy: str | None = None
    createdAt: datetime
    updatedAt: datetime


class BoardPackRender(BaseModel):
    pack: BoardPackOut
    summary: DashboardSummary
    topRisks: list[TopRiskRow] = []
    acceptanceLog: list[dict[str, Any]] = []
    escalations: list[dict[str, Any]] = []
    newRisks: list[dict[str, Any]] = []
    movement: list[MovementRow] = []
    generatedAt: datetime
    tenantName: str = "Meridian Manufacturing Limited"


__all__ = [k for k in dict(globals()) if k[0].isupper()]
