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


class SubCategoryUpdate(BaseModel):
    # code is immutable (rollup rules reference targetSubCategoryCode) — edit the
    # rest. All optional so the form can patch a single field.
    name: str | None = None
    description: str | None = None
    isActive: bool | None = None


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


TimeHorizon = Literal["ONE_YEAR", "THREE_YEAR", "FIVE_YEAR"]


class AssessmentCreate(BaseModel):
    assessmentType: Literal["INHERENT", "RESIDUAL"]
    likelihood: int = Field(ge=1, le=5)
    impactScores: list[ImpactScore] = []  # may be omitted for a control-derived residual
    rationale: str = Field(min_length=1)
    # ── ADVANCED quantification (all optional; ordinal-only assessments still work) ──
    likelihoodPct: float | None = Field(default=None, ge=0, le=100)
    financialBestInr: float | None = Field(default=None, ge=0)
    financialExpectedInr: float | None = Field(default=None, ge=0)
    financialWorstInr: float | None = Field(default=None, ge=0)
    timeHorizon: TimeHorizon | None = None
    # RESIDUAL only — compute likelihood/impact from mapped control effectiveness
    # instead of typing them in. When true, impactScores may be omitted.
    deriveFromControls: bool = False
    # Required when an asserted residual is materially MORE OPTIMISTIC than the
    # control-derived residual (a governed override; needs approver authority).
    overrideJustification: str | None = None


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
    likelihoodPct: float | None = None
    financialBestInr: float | None = None
    financialExpectedInr: float | None = None
    financialWorstInr: float | None = None
    expectedLossInr: float | None = None
    unexpectedLossInr: float | None = None
    timeHorizon: str | None = None
    derivedFromControls: bool = False
    controlEffectivenessPct: float | None = None
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
    correlationStrength: float = Field(default=0.5, ge=0, le=1)
    # Fraction of the SOURCE risk's expected loss that lands on the TARGET when the
    # source materialises — drives correlated-exposure aggregation.
    impactFactor: float = Field(default=0.0, ge=0, le=1)


class RiskLinkageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    sourceRiskId: str
    targetRiskId: str
    linkageType: str
    notes: str = ""
    correlationStrength: float = 0.5
    impactFactor: float = 0.0


class PropagationTarget(BaseModel):
    riskId: str
    riskCode: str
    title: str
    linkageType: str
    correlationStrength: float
    impactFactor: float
    baseResidualExpectedLossInr: float = 0.0
    addedExpectedLossInr: float = 0.0  # incremental EL if the source fires
    stressedExpectedLossInr: float = 0.0


class PropagationResult(BaseModel):
    sourceRiskId: str
    sourceRiskCode: str
    sourceExpectedLossInr: float = 0.0
    directTargets: list[PropagationTarget] = []
    totalAddedExpectedLossInr: float = 0.0  # knock-on EL across the network
    affectedCount: int = 0


class CorrelatedExposureResponse(BaseModel):
    standaloneExpectedLossInr: float = 0.0  # naive Σ (assumes independence)
    correlatedExpectedLossInr: float = 0.0  # Σ + knock-on from linkages
    diversificationGapInr: float = 0.0      # correlated − standalone (the hidden exposure)
    topContagionSources: list[PropagationResult] = []
    linkageCount: int = 0


# ─────────────────────────────────────────────────────────────────────
# Probabilistic — Monte Carlo / VaR + reverse stress
# ─────────────────────────────────────────────────────────────────────
class MonteCarloBucket(BaseModel):
    bucketFromInr: float
    bucketToInr: float | None = None
    count: int
    pct: float


class MonteCarloResponse(BaseModel):
    iterations: int
    riskCount: int
    meanLossInr: float
    p50LossInr: float
    p90LossInr: float
    p95LossInr: float
    p99LossInr: float  # value-at-risk (99%)
    maxLossInr: float
    expectedLossInr: float  # analytic Σ(p×expected) for comparison
    correlated: bool = False  # did the sim use the RiskLinkage contagion graph
    linkageCount: int = 0
    independentP99LossInr: float = 0.0  # P99 with correlation OFF — the comparison
    contagionTailUpliftInr: float = 0.0  # extra tail VaR that correlation reveals
    distribution: list[MonteCarloBucket] = []


class ReverseStressRow(BaseModel):
    riskId: str
    riskCode: str
    title: str
    worstLossInr: float
    residualBand: str | None = None


class ReverseStressResponse(BaseModel):
    thresholdInr: float
    breached: bool
    minRisksToBreach: int | None = None
    combinedWorstLossInr: float
    breakingCombination: list[ReverseStressRow] = []
    portfolioWorstCaseInr: float
    headroomInr: float


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
    # ── ADVANCED: monetary exposure + control-derived residual + target ──
    inherentExpectedLossInr: float | None = None
    residualExpectedLossInr: float | None = None
    residualWorstLossInr: float | None = None
    controlEffectivenessPct: float | None = None
    derivedResidualScore: int | None = None
    derivedResidualBand: str | None = None
    residualIsOverride: bool = False
    residualOverrideVariance: int | None = None
    controlAlert: bool = False
    kriAlert: bool = False
    incidentAlert: bool = False
    targetScore: int | None = None
    targetBand: str | None = None
    targetExpectedLossInr: float | None = None
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
    achievedResidualReduction: int | None = None
    reductionShortfall: bool | None = None
    baselineResidualScore: int | None = None
    costInr: float | None = None
    expectedLossReductionInr: float | None = None
    riskReductionPerRupee: float | None = None
    transferPolicyId: str | None = None
    completionPercent: int | None = None
    isOpen: bool
    overdue: bool = False


class ControlContribution(BaseModel):
    controlId: str
    controlCode: str
    name: str
    controlType: str
    mitigationStrength: str
    rating: str
    axis: str  # LIKELIHOOD | IMPACT
    contribution: float
    designFactor: float | None = None
    operatingFactor: float | None = None
    operatingBasis: str | None = None  # how the operating factor was back-tested
    backTested: bool = False


class DerivedResidualOut(BaseModel):
    """Residual implied PURELY by mapped control effectiveness — the evidence that
    residual is derived, not guessed. Compared against the asserted residual."""
    inherentLikelihood: int | None = None
    inherentImpact: int | None = None
    inherentScore: int | None = None
    preventiveEffectivenessPct: float = 0.0  # cuts likelihood
    mitigatingEffectivenessPct: float = 0.0  # cuts impact
    combinedEffectivenessPct: float = 0.0
    derivedLikelihood: int | None = None
    derivedImpact: int | None = None
    derivedResidualScore: int | None = None
    derivedResidualBand: str | None = None
    derivedResidualExpectedLossInr: float | None = None
    assertedResidualScore: int | None = None
    overrideVariance: int | None = None
    mappedControlCount: int = 0
    ratedControlCount: int = 0
    backTestedControlCount: int = 0  # controls whose operating factor came from real test data
    contributingControls: list[ControlContribution] = []
    # Risk-reduction value of the control environment (probe 10): inherent − residual.
    inherentExpectedLossInr: float | None = None
    controlRiskReductionInr: float | None = None


class TargetSet(BaseModel):
    targetLikelihood: int = Field(ge=1, le=5)
    targetImpact: int = Field(ge=1, le=5)
    targetDate: datetime | None = None
    targetRationale: str | None = None
    financialExpectedInr: float | None = Field(default=None, ge=0)
    likelihoodPct: float | None = Field(default=None, ge=0, le=100)


# ─────────────────────────────────────────────────────────────────────
# Bow-tie causal model (causes → top event → consequences, barriers each side)
# ─────────────────────────────────────────────────────────────────────
BarrierStatus = Literal["WORKED", "FAILED", "ABSENT", "UNTESTED"]


class BowtieBarrier(BaseModel):
    id: str
    description: str
    barrierType: Literal["PREVENTIVE", "MITIGATING"]
    controlId: str | None = None       # link to the Control register
    controlCode: str | None = None
    status: BarrierStatus = "UNTESTED"


class BowtieThreat(BaseModel):
    id: str
    description: str
    preventiveBarriers: list[BowtieBarrier] = []


class BowtieConsequence(BaseModel):
    id: str
    description: str
    mitigatingBarriers: list[BowtieBarrier] = []


class BowtieModel(BaseModel):
    topEvent: str = ""
    threats: list[BowtieThreat] = []
    consequences: list[BowtieConsequence] = []


class ThreeLinesUpsert(BaseModel):
    firstLineOwnerId: str | None = None
    secondLineOwnerId: str | None = None
    thirdLineAssurance: str | None = None


# ─────────────────────────────────────────────────────────────────────
# Regulatory / framework alignment mapping
# ─────────────────────────────────────────────────────────────────────
class FrameworkClause(BaseModel):
    clause: str
    title: str
    capability: str          # the ERM feature that satisfies it
    status: Literal["MET", "PARTIAL", "GAP"]
    evidence: str = ""       # endpoint / module that demonstrates it


class FrameworkCoverage(BaseModel):
    framework: str
    version: str = ""
    metCount: int = 0
    partialCount: int = 0
    gapCount: int = 0
    coveragePct: float = 0.0
    clauses: list[FrameworkClause] = []


class FrameworkCoverageResponse(BaseModel):
    frameworks: list[FrameworkCoverage] = []
    overallCoveragePct: float = 0.0


class ExposureRow(BaseModel):
    rank: int
    id: str
    riskCode: str
    title: str
    categoryCode: str | None = None
    categoryName: str | None = None
    residualBand: str | None = None
    residualExpectedLossInr: float = 0.0
    residualWorstLossInr: float = 0.0
    pctOfTotal: float = 0.0
    cumulativePct: float = 0.0


class ExposureByCategory(BaseModel):
    categoryCode: str
    categoryName: str
    colorHex: str | None = None
    riskCount: int = 0
    expectedLossInr: float = 0.0
    pctOfTotal: float = 0.0


class ExposureBySite(BaseModel):
    plantId: str | None = None
    plantName: str
    riskCount: int = 0
    expectedLossInr: float = 0.0
    concentrationIndex: float = 0.0  # Herfindahl (0..1) within the site


class EnterpriseExposureResponse(BaseModel):
    totalExpectedLossInr: float = 0.0
    totalWorstLossInr: float = 0.0
    quantifiedRiskCount: int = 0
    unquantifiedRiskCount: int = 0
    topDrivers: list[ExposureRow] = []
    byCategory: list[ExposureByCategory] = []
    bySite: list[ExposureBySite] = []
    portfolioConcentrationIndex: float = 0.0  # Herfindahl across all risks (0..1)
    top5SharePct: float = 0.0


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
    targetLikelihood: int | None = None
    targetImpact: int | None = None
    targetDate: datetime | None = None
    targetRationale: str | None = None
    controlAlertAt: datetime | None = None
    incidentAlertReason: str | None = None
    derivedResidual: DerivedResidualOut | None = None
    openAppetiteBreaches: list[dict[str, Any]] = []  # I-14: breaches this risk triggered
    bowtie: BowtieModel | None = None
    firstLineOwnerId: str | None = None
    firstLineOwnerName: str | None = None
    secondLineOwnerId: str | None = None
    secondLineOwnerName: str | None = None
    thirdLineAssurance: str | None = None
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
    costInr: float | None = Field(default=None, ge=0)  # treatment spend — risk-reduction-per-cost
    transferPolicyId: str | None = None  # TRANSFER → bind to an InsurancePolicy
    acceptanceJustification: str | None = None  # required for TOLERATE
    completionPercent: int = Field(default=0, ge=0, le=100)  # initial mitigation progress


class TreatmentProgress(BaseModel):
    """Update mitigation progress (% completion). At 100% the residual is
    auto-recalculated (post-mitigation) — see PATCH /treatments/{id}/progress."""
    completionPercent: int = Field(ge=0, le=100)
    note: str | None = None


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


class DepartmentBarSegment(BaseModel):
    """Department / Business-Unit risk summary (Page Industries §1b)."""
    businessUnit: str
    low: int = 0
    medium: int = 0
    high: int = 0
    critical: int = 0
    total: int = 0


class RootCauseSummary(BaseModel):
    """Top root cause contributing to risks (Page Industries §1c). Sourced from
    approved RCA records via the causal-analytics engine."""
    label: str
    categoryCode: str | None = None
    categoryName: str | None = None
    occurrences: int = 0
    riskReach: int = 0
    isRecurringDriver: bool = False


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


class PendingApprovalItem(BaseModel):
    type: str  # RISK | TREATMENT
    code: str | None = None
    title: str | None = None
    href: str
    state: str | None = None
    ownerName: str | None = None


class DashboardSummary(BaseModel):
    totalActiveRisks: int
    criticalResidual: int
    highResidual: int
    mediumResidual: int = 0
    lowResidual: int = 0
    overdueReviews: int
    openTreatments: int
    overdueTreatments: int = 0
    mitigationProgressPct: float = 0.0  # avg % completion across open treatments (§1d)
    escalatedThisQuarter: int
    pendingApprovals: int = 0  # §7.5 items awaiting a governance decision
    pendingApprovalItems: list[PendingApprovalItem] = []
    inherentHeatMap: list[HeatMapCell] = []
    residualHeatMap: list[HeatMapCell] = []
    categoryBars: list[CategoryBarSegment] = []
    departmentBars: list[DepartmentBarSegment] = []  # §1b department/BU summary
    topRootCauses: list[RootCauseSummary] = []  # §1c top root causes
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
    correlationStrength: float = 0.5
    impactFactor: float = 0.0


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
    reductionShortfall: bool | None = None
    costInr: float | None = None
    expectedLossReductionInr: float | None = None
    riskReductionPerRupee: float | None = None
    completionPercent: int | None = None


class TreatmentTrackerResponse(BaseModel):
    items: list[TreatmentTrackerRow]
    total: int
    openCount: int
    overdueCount: int
    closedThisQuarter: int
    avgClosureDays: float | None = None
    totalExpectedLossReductionInr: float = 0.0
    totalTreatmentCostInr: float = 0.0
    portfolioRiskReductionPerRupee: float | None = None


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
    exposure: EnterpriseExposureResponse | None = None  # ₹ exposure for the board (SEBI Reg 21)
    monteCarlo: MonteCarloResponse | None = None  # VaR P99 for the board
    generatedAt: datetime
    tenantName: str = "Meridian Manufacturing Limited"


__all__ = [k for k in dict(globals()) if k[0].isupper()]
