"""Pydantic schemas for ERM Tier 3 (Controls · Vendor · Insurance). camelCase API contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ═══════════════════════════════════════════════════════════════════════════════
# Internal Controls
# ═══════════════════════════════════════════════════════════════════════════════
class ControlUpsert(BaseModel):
    name: str = Field(min_length=3)
    description: str = ""
    controlType: Literal["PREVENTIVE", "DETECTIVE", "CORRECTIVE", "DIRECTIVE"]
    nature: Literal["MANUAL", "AUTOMATED", "IT_DEPENDENT_MANUAL"]
    frequency: Literal["CONTINUOUS", "DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "ANNUAL", "EVENT_DRIVEN"]
    category: Literal["FINANCIAL_REPORTING", "OPERATIONAL", "COMPLIANCE", "IT_GENERAL", "ENTITY_LEVEL"]
    controlOwnerId: str
    processName: str | None = None
    siteId: str | None = None
    isKeyControl: bool = False
    assertions: list[str] = []
    controlDesignNotes: str = ""


class ControlListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    controlCode: str
    name: str
    controlType: str
    nature: str
    frequency: str
    category: str
    controlOwnerId: str
    controlOwnerName: str | None = None
    siteId: str | None = None
    siteName: str | None = None
    isKeyControl: bool
    currentDesignRating: str | None = None
    currentOperatingRating: str | None = None
    lastTestDate: datetime | None = None
    nextTestDueDate: datetime | None = None
    testOverdue: bool = False
    openDeficiencyCount: int = 0
    mappedRiskCount: int = 0
    isActive: bool = True
    updatedAt: datetime | None = None


class MappingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    controlId: str
    riskId: str | None = None
    processId: str | None = None
    obligationId: str | None = None
    mitigationStrength: str
    coverageNotes: str = ""
    targetType: str = "RISK"        # RISK | PROCESS | OBLIGATION (derived)
    targetCode: str | None = None   # resolved code
    targetLabel: str | None = None


class MappingUpsert(BaseModel):
    riskId: str | None = None
    processId: str | None = None
    obligationId: str | None = None
    mitigationStrength: Literal["PRIMARY", "SECONDARY", "COMPENSATING"]
    coverageNotes: str = ""


class TestPlanUpsert(BaseModel):
    testCycleLabel: str
    testMethod: Literal["INQUIRY", "OBSERVATION", "INSPECTION", "REPERFORMANCE"]
    sampleSizePlanned: int = Field(ge=1, default=1)
    testFrequencyPerYear: int = Field(ge=1, default=1)
    assignedTesterId: str
    scheduledDate: datetime


class TestPlanOut(TestPlanUpsert):
    model_config = ConfigDict(from_attributes=True)
    id: str
    controlId: str
    assignedTesterName: str | None = None


class TestCreate(BaseModel):
    testPlanId: str | None = None
    testType: Literal["DESIGN", "OPERATING"]
    testDate: datetime
    method: Literal["INQUIRY", "OBSERVATION", "INSPECTION", "REPERFORMANCE"]
    sampleSize: int = Field(ge=1, default=1)
    exceptionsFound: int = Field(ge=0, default=0)
    conclusion: Literal["EFFECTIVE", "DEFICIENT", "SIGNIFICANT_DEFICIENCY", "MATERIAL_WEAKNESS"]
    workpaperNotes: str = Field(min_length=1)
    evidenceAttachmentIds: list[str] = []
    # optional inline deficiency fields when conclusion != EFFECTIVE
    deficiencyDescription: str | None = None
    deficiencyRootCause: str | None = None
    identifiedRiskImpact: str | None = None


class TestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    controlId: str
    testPlanId: str | None = None
    testType: str
    testDate: datetime
    testerId: str
    testerName: str | None = None
    method: str
    sampleSize: int
    exceptionsFound: int
    conclusion: str
    workpaperNotes: str
    evidenceAttachmentIds: list[str] = []
    deficiencyId: str | None = None


class DeficiencyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    deficiencyCode: str
    controlId: str
    controlCode: str | None = None
    controlName: str | None = None
    sourceTestId: str
    severity: str
    description: str
    rootCause: str | None = None
    remediationCapaId: str | None = None
    remediationCapaState: str | None = None
    status: str
    identifiedRiskImpact: str | None = None
    reportedToAuditCommittee: bool
    auditCommitteeReference: str | None = None
    ageDays: int = 0
    createdAt: datetime


class DeficiencyListResponse(BaseModel):
    items: list[DeficiencyOut]
    total: int
    severityCounts: dict[str, int] = {}


class ControlDetail(ControlListItem):
    description: str = ""
    assertions: list[str] = []
    controlDesignNotes: str = ""
    processName: str | None = None
    mappings: list[MappingOut] = []
    testPlans: list[TestPlanOut] = []
    tests: list[TestOut] = []
    deficiencies: list[DeficiencyOut] = []
    createdAt: datetime


class ControlListResponse(BaseModel):
    items: list[ControlListItem]
    total: int
    categoryCounts: dict[str, int] = {}


class ControlsDashboard(BaseModel):
    keyControls: int
    testedThisCyclePct: float
    effectivePct: float
    openDeficiencies: int
    materialWeaknesses: int
    overdueTests: int
    ratingDistribution: dict[str, int] = {}     # EFFECTIVE/DEFICIENT/NOT_ASSESSED
    deficiencyBySeverity: dict[str, int] = {}
    overdueList: list[dict[str, Any]] = []
    unreportedMaterialWeaknesses: list[dict[str, Any]] = []


class MatrixCell(BaseModel):
    controlId: str
    controlCode: str
    name: str
    mitigationStrength: str
    operatingRating: str | None = None


class MatrixRow(BaseModel):
    riskId: str
    riskCode: str
    title: str
    residualBand: str | None = None
    controls: list[MatrixCell] = []
    hasPrimaryControl: bool = False
    primaryControlDeficient: bool = False


class RiskControlMatrix(BaseModel):
    rows: list[MatrixRow] = []
    orphanControls: list[dict[str, Any]] = []    # controls mapped to nothing


class DeficiencyReport(BaseModel):
    auditCommitteeReference: str = Field(min_length=1)


# ═══════════════════════════════════════════════════════════════════════════════
# Vendor / Third-Party Risk
# ═══════════════════════════════════════════════════════════════════════════════
class DomainScoreIn(BaseModel):
    domainKey: str
    rawScore: int = Field(ge=1, le=5)
    weightPct: float
    evidenceNotes: str | None = None


class FindingIn(BaseModel):
    severity: Literal["OBSERVATION", "CONCERN", "CRITICAL_GAP"]
    description: str = Field(min_length=1)
    targetCloseDate: datetime | None = None


class FindingOut(BaseModel):
    id: str
    lens: str
    severity: str
    description: str
    capaId: str | None = None
    targetCloseDate: datetime | None = None


class VendorUpsert(BaseModel):
    legalName: str = Field(min_length=2)
    category: str
    criticality: Literal["STRATEGIC", "CRITICAL", "IMPORTANT", "ROUTINE"]
    tier: Literal["TIER_1", "TIER_2", "TIER_3"]
    siteScope: list[str] = []
    relationshipOwnerId: str
    annualSpendInr: float | None = None
    isSingleSource: bool = False
    linkedProcessIds: list[str] = []
    linkedRiskIds: list[str] = []
    masterDataRef: str | None = None


class VendorListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    vendorCode: str
    masterDataRef: str | None = None
    legalName: str
    category: str
    criticality: str
    tier: str
    relationshipOwnerId: str
    relationshipOwnerName: str | None = None
    annualSpendInr: float | None = None
    isSingleSource: bool
    onboardingStatus: str
    currentRiskScore: float | None = None
    currentRiskBand: str | None = None
    currentEsgScore: float | None = None
    currentEsgBand: str | None = None
    nextReviewDate: datetime | None = None
    reviewOverdue: bool = False
    isActive: bool = True
    updatedAt: datetime | None = None


class AssessmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    vendorId: str
    lens: str
    assessmentDate: datetime
    assessorId: str
    assessorName: str | None = None
    method: str
    domainScores: list[dict[str, Any]] = []
    weightedScore: float
    band: str
    summaryNotes: str = ""
    validUntil: datetime
    isCurrent: bool
    findings: list[dict[str, Any]] = []


class VendorDetail(VendorListItem):
    siteScope: list[str] = []
    linkedProcessIds: list[str] = []
    linkedRiskIds: list[str] = []
    linkedRisks: list[dict[str, Any]] = []
    linkedProcesses: list[dict[str, Any]] = []
    assessments: list[AssessmentOut] = []
    createdAt: datetime


class VendorListResponse(BaseModel):
    items: list[VendorListItem]
    total: int
    riskBandCounts: dict[str, int] = {}
    esgBandCounts: dict[str, int] = {}


class AssessmentCreate(BaseModel):
    lens: Literal["RISK", "ESG"]
    assessmentDate: datetime
    method: Literal["SELF_ASSESSMENT", "DESK_REVIEW", "ONSITE_AUDIT", "THIRD_PARTY_RATING"]
    domainScores: list[DomainScoreIn]
    summaryNotes: str = ""
    validUntil: datetime
    findings: list[FindingIn] = []


class ScoringConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    lens: str
    domains: list[dict[str, Any]] = []
    bandThresholds: list[dict[str, Any]] = []


class OnboardingChange(BaseModel):
    onboardingStatus: Literal["PROSPECT", "DUE_DILIGENCE", "APPROVED", "CONDITIONAL", "SUSPENDED", "OFFBOARDED"]
    note: str = ""


class VendorDashboard(BaseModel):
    activeVendors: int
    strategicCritical: int
    highCriticalRisk: int
    laggingEsg: int
    singleSource: int
    overdueReviews: int
    riskBandDistribution: dict[str, int] = {}
    esgBandDistribution: dict[str, int] = {}
    spendWeightedLaggingPct: float = 0.0
    onboardingPipeline: dict[str, int] = {}


class EsgPortfolio(BaseModel):
    totalSpend: float
    spendByBand: list[dict[str, Any]] = []        # {band, spend, pct, colorHex}
    spendByCategory: list[dict[str, Any]] = []
    laggingWatchlist: list[dict[str, Any]] = []
    laggingSpendPct: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Insurance & Risk Transfer
# ═══════════════════════════════════════════════════════════════════════════════
POLICY_TYPES = Literal[
    "PROPERTY_FIRE", "BUSINESS_INTERRUPTION", "MARINE_TRANSIT", "LIABILITY_PUBLIC", "LIABILITY_PRODUCT",
    "DIRECTORS_OFFICERS", "CYBER", "EMPLOYEE_GROUP", "MARINE_CARGO", "MACHINERY_BREAKDOWN", "ENVIRONMENTAL_LIABILITY", "OTHER",
]


class PolicyUpsert(BaseModel):
    policyName: str = Field(min_length=3)
    policyType: POLICY_TYPES
    insurerName: str
    brokerName: str | None = None
    policyNumber: str
    siteScope: list[str] = []
    sumInsuredInr: float = Field(ge=0)
    premiumAnnualInr: float = Field(ge=0)
    deductibleInr: float | None = None
    coverageStartDate: datetime
    coverageEndDate: datetime
    renewalLeadDays: int = 45
    keyExclusions: list[str] = []
    coveredRiskIds: list[str] = []
    coveredProcessIds: list[str] = []
    ownerId: str


class PolicyListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    policyCode: str
    policyName: str
    policyType: str
    insurerName: str
    policyNumber: str
    sumInsuredInr: float
    premiumAnnualInr: float
    coverageEndDate: datetime
    status: str
    daysToExpiry: int | None = None
    coveredRiskCount: int = 0
    openClaimCount: int = 0
    ownerId: str
    ownerName: str | None = None
    isActive: bool = True
    updatedAt: datetime | None = None


class ClaimOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    claimCode: str
    policyId: str
    policyCode: str | None = None
    lossEventId: str | None = None
    lossEventCode: str | None = None
    claimDate: datetime
    description: str
    claimedAmountInr: float
    status: str
    settledAmountInr: float | None = None
    settlementDate: datetime | None = None
    remarks: str | None = None


class PolicyDetail(PolicyListItem):
    brokerName: str | None = None
    siteScope: list[str] = []
    deductibleInr: float | None = None
    coverageStartDate: datetime
    renewalLeadDays: int = 45
    keyExclusions: list[str] = []
    coveredRiskIds: list[str] = []
    coveredProcessIds: list[str] = []
    coveredRisks: list[dict[str, Any]] = []
    coveredProcesses: list[dict[str, Any]] = []
    claims: list[ClaimOut] = []
    createdAt: datetime


class PolicyListResponse(BaseModel):
    items: list[PolicyListItem]
    total: int
    statusCounts: dict[str, int] = {}


class ClaimCreate(BaseModel):
    policyId: str
    lossEventId: str | None = None
    claimDate: datetime
    description: str = Field(min_length=1)
    claimedAmountInr: float = Field(ge=0)


class ClaimUpdate(BaseModel):
    status: Literal["INTIMATED", "SURVEYOR_APPOINTED", "UNDER_ASSESSMENT", "APPROVED", "PARTIALLY_SETTLED", "SETTLED", "REPUDIATED"]
    settledAmountInr: float | None = None
    settlementDate: datetime | None = None
    remarks: str | None = None


class GapLineIn(BaseModel):
    riskId: str
    isInsurable: bool = True
    coveredByPolicyIds: list[str] = []
    gapType: Literal["FULLY_COVERED", "PARTIALLY_COVERED", "UNCOVERED", "UNINSURABLE_ACCEPTED"]
    gapNotes: str = ""
    recommendedAction: str | None = None


class CoverageGapUpsert(BaseModel):
    assessmentCycleLabel: str = Field(min_length=2)
    reviewDate: datetime
    lines: list[GapLineIn] = []
    summaryNotes: str = ""


class CoverageGapOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    assessmentCycleLabel: str
    reviewDate: datetime
    reviewedBy: str
    reviewedByName: str | None = None
    lines: list[dict[str, Any]] = []
    summaryNotes: str = ""
    uncoveredCount: int = 0
    totalCriticalRisks: int = 0
    createdAt: datetime


class InsuranceDashboard(BaseModel):
    activePolicies: int
    totalSumInsured: float
    annualPremium: float
    expiringSoon: int
    openClaimsValue: float
    uncoveredCriticalRisks: int
    renewalCalendar: list[dict[str, Any]] = []
    coverageByType: list[dict[str, Any]] = []
    openClaims: list[dict[str, Any]] = []


__all__ = [k for k in dict(globals()) if k[0].isupper()]
