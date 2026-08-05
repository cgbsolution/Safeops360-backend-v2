"""Pydantic schemas for CAMS (Compliance & Audit Management System).

camelCase throughout; doubles as the API contract the Next.js frontend mirrors
in src/app/(dashboard)/cams/lib-cams.ts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ENGAGEMENT_TYPES = (
    "INTERNAL_AUDIT",
    "COMPLIANCE_AUDIT",
    "INSPECTION",
    "SUPPLIER_AUDIT",
    "LAYERED_PROCESS_AUDIT",
    "MANAGEMENT_REVIEW",
)
EngagementTypeLit = Literal[
    "INTERNAL_AUDIT", "COMPLIANCE_AUDIT", "INSPECTION",
    "SUPPLIER_AUDIT", "LAYERED_PROCESS_AUDIT", "MANAGEMENT_REVIEW",
]
FrequencyLit = Literal["WEEKLY", "MONTHLY", "QUARTERLY", "HALF_YEARLY", "ANNUAL", "CUSTOM_DAYS"]


# ════════════════════════════════════════════════════════════════════════════
# Audit Types (Shared Service ③ — config)
# ════════════════════════════════════════════════════════════════════════════
class AuditTypeUpsert(BaseModel):
    name: str = Field(min_length=2)
    engagementType: EngagementTypeLit
    defaultTemplateId: str | None = None
    defaultRecurrence: str | None = None
    requiresAssetRef: bool = False
    requiresAuditorCompetency: list[str] = []
    # WP-49: the audit type is the configuration home.
    #   scoringRules          per-type pass mark + critical gate (F-22)
    #   regimeCode            buyer-regime vocabulary (WP-47)
    #   competenceEnforcement WARN | BLOCK when an assignee lacks a competency
    scoringRules: dict | None = None
    regimeCode: str | None = None
    competenceEnforcement: str = "WARN"
    standardRefs: list[str] = []
    isActive: bool = True


class AuditTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    typeCode: str
    name: str
    engagementType: str
    defaultTemplateId: str | None = None
    defaultTemplateName: str | None = None
    defaultRecurrence: str | None = None
    requiresAssetRef: bool
    requiresAuditorCompetency: list[str] = []
    # WP-49: the audit type is the configuration home.
    #   scoringRules          per-type pass mark + critical gate (F-22)
    #   regimeCode            buyer-regime vocabulary (WP-47)
    #   competenceEnforcement WARN | BLOCK when an assignee lacks a competency
    scoringRules: dict | None = None
    regimeCode: str | None = None
    competenceEnforcement: str = "WARN"
    standardRefs: list[str] = []
    isActive: bool
    engagementCount: int = 0
    updatedAt: datetime | None = None


# ════════════════════════════════════════════════════════════════════════════
# Engagements (Shared Service ① — the engine)
# ════════════════════════════════════════════════════════════════════════════
class EngagementCreate(BaseModel):
    title: str = Field(min_length=3)
    engagementType: EngagementTypeLit
    auditTypeId: str | None = None
    standardRefs: list[str] = []
    siteId: str | None = None
    areaOrAssetRef: str | None = None
    scopeStatement: str = ""
    leadAuditorId: str
    auditTeamIds: list[str] = []
    auditeeOwnerId: str | None = None
    plannedDate: datetime
    scheduledStart: datetime | None = None
    scheduledEnd: datetime | None = None
    templateId: str | None = None
    riskBasis: Literal["ROUTINE", "RISK_TRIGGERED", "REGULATORY_REQUIRED", "CUSTOMER_REQUIRED"] | None = None
    triggeringRiskId: str | None = None
    # Provenance — set by a consumer module (Fire / PPE / Pharma / EPC); null = CAMS-native
    sourceModule: str | None = None


class EngagementUpdate(BaseModel):
    title: str | None = None
    scopeStatement: str | None = None
    standardRefs: list[str] | None = None
    siteId: str | None = None
    areaOrAssetRef: str | None = None
    leadAuditorId: str | None = None
    auditTeamIds: list[str] | None = None
    auditeeOwnerId: str | None = None
    plannedDate: datetime | None = None
    scheduledStart: datetime | None = None
    scheduledEnd: datetime | None = None
    templateId: str | None = None
    riskBasis: str | None = None


class EngagementTransition(BaseModel):
    # Target status; gates enforced server-side.
    toStatus: Literal[
        "SCHEDULED", "IN_PROGRESS", "FIELDWORK_COMPLETE",
        "FINDINGS_REVIEW", "REPORT_ISSUED", "CLOSED", "CANCELLED",
    ]
    note: str = ""


class EngagementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    engagementCode: str
    title: str
    engagementType: str
    auditTypeId: str | None = None
    auditTypeName: str | None = None
    standardRefs: list[str] = []
    siteId: str | None = None
    siteName: str | None = None
    areaOrAssetRef: str | None = None
    scopeStatement: str = ""
    leadAuditorId: str
    leadAuditorName: str | None = None
    auditTeamIds: list[str] = []
    auditeeOwnerId: str | None = None
    auditeeOwnerName: str | None = None
    plannedDate: datetime
    scheduledStart: datetime | None = None
    scheduledEnd: datetime | None = None
    conductedDate: datetime | None = None
    templateId: str | None = None
    templateName: str | None = None
    templateVersionUsed: int | None = None
    status: str
    riskBasis: str | None = None
    triggeringRiskId: str | None = None
    overallResult: str | None = None
    scorePercent: float | None = None
    nextScheduledDate: datetime | None = None
    sourceModule: str | None = None
    findingCount: int = 0
    openFindingCount: int = 0
    ncCount: int = 0
    updatedAt: datetime | None = None


class EngagementListResponse(BaseModel):
    items: list[EngagementOut]
    total: int
    statusCounts: dict[str, int] = {}
    typeCounts: dict[str, int] = {}


# ════════════════════════════════════════════════════════════════════════════
# Recurrence
# ════════════════════════════════════════════════════════════════════════════
class RecurrenceUpsert(BaseModel):
    auditTypeId: str | None = None
    templateId: str | None = None
    siteScope: list[str] = []
    frequency: FrequencyLit
    customIntervalDays: int | None = None
    leadTimeDays: int = 14
    defaultLeadAuditorId: str | None = None
    isActive: bool = True


class RecurrenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    auditTypeId: str | None = None
    auditTypeName: str | None = None
    templateId: str | None = None
    siteScope: list[str] = []
    frequency: str
    customIntervalDays: int | None = None
    leadTimeDays: int
    defaultLeadAuditorId: str | None = None
    isActive: bool
    lastGeneratedAt: datetime | None = None
    updatedAt: datetime | None = None


# ════════════════════════════════════════════════════════════════════════════
# Templates / Checklist Engine (Shared Service ②)
# ════════════════════════════════════════════════════════════════════════════
QuestionTypeLit = Literal[
    "YES_NO_NA", "CONFORM_NC_NA", "RATING_SCALE", "NUMERIC",
    "SINGLE_SELECT", "MULTI_SELECT", "TEXT", "PHOTO_REQUIRED", "SIGNATURE",
]


class QuestionIn(BaseModel):
    id: str | None = None  # preserved across builder saves where present
    orderIndex: int = 0
    text: str = Field(min_length=1)
    questionType: QuestionTypeLit = "CONFORM_NC_NA"
    isMandatory: bool = True
    standardClauseRef: str | None = None
    guidance: str | None = None
    weight: float | None = None
    ncTriggersFinding: bool = True
    evidenceRequiredOnNc: bool = False
    options: list[str] | None = None


class SectionIn(BaseModel):
    id: str | None = None
    orderIndex: int = 0
    title: str = Field(min_length=1)
    weightPct: float | None = None
    questions: list[QuestionIn] = []


class ScoringConfig(BaseModel):
    mode: Literal["PERCENT_CONFORMANCE", "WEIGHTED_SCORE", "PASS_FAIL", "NONE"] = "PERCENT_CONFORMANCE"
    passThresholdPercent: float | None = 80
    ncWeighting: dict[str, float] | None = None  # {minor, major, critical}


class TemplateCreate(BaseModel):
    name: str = Field(min_length=3)
    description: str = ""
    applicableEngagementTypes: list[EngagementTypeLit] = []
    standardRefs: list[str] = []
    scoringConfig: ScoringConfig = ScoringConfig()
    ownerId: str
    isGlobal: bool = True
    siteId: str | None = None


class TemplateSave(BaseModel):
    """Full structure replace used by the builder (DRAFT only)."""
    name: str | None = None
    description: str | None = None
    applicableEngagementTypes: list[EngagementTypeLit] | None = None
    standardRefs: list[str] | None = None
    scoringConfig: ScoringConfig | None = None
    isGlobal: bool | None = None
    siteId: str | None = None
    sections: list[SectionIn] | None = None


class QuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    orderIndex: int
    text: str
    questionType: str
    isMandatory: bool
    standardClauseRef: str | None = None
    guidance: str | None = None
    weight: float | None = None
    ncTriggersFinding: bool
    evidenceRequiredOnNc: bool
    options: list[str] | None = None


class SectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    orderIndex: int
    title: str
    weightPct: float | None = None
    questions: list[QuestionOut] = []


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    templateCode: str
    name: str
    description: str = ""
    applicableEngagementTypes: list[str] = []
    standardRefs: list[str] = []
    version: int
    status: str
    approvedBy: str | None = None
    approvedByName: str | None = None
    approvedAt: datetime | None = None
    parentTemplateId: str | None = None
    scoringConfig: dict[str, Any] = {}
    ownerId: str
    ownerName: str | None = None
    isGlobal: bool
    siteId: str | None = None
    sectionCount: int = 0
    questionCount: int = 0
    clauseCount: int = 0
    updatedAt: datetime | None = None


class TemplateDetail(TemplateOut):
    sections: list[SectionOut] = []


class TemplateListResponse(BaseModel):
    items: list[TemplateOut]
    total: int
    statusCounts: dict[str, int] = {}


class ClauseRef(BaseModel):
    standard: str
    clause: str  # "ISO 45001:8.1.2"
    title: str


# ════════════════════════════════════════════════════════════════════════════
# Checklist execution (runner)
# ════════════════════════════════════════════════════════════════════════════
class AnswerIn(BaseModel):
    questionId: str
    value: Any = None  # string | number | bool | string[]
    conformance: Literal["CONFORM", "NC", "NA"] | None = None
    evidenceAttachmentIds: list[str] = []
    note: str = ""
    # NC severity the auditor assigned (drives finding severity); optional.
    ncSeverity: Literal["MINOR_NC", "MAJOR_NC", "CRITICAL_NC", "OBSERVATION"] | None = None


class ChecklistSave(BaseModel):
    answers: list[AnswerIn] = []
    complete: bool = False  # true = finalise (compute score, spawn findings, → FIELDWORK_COMPLETE)


class RunnerQuestion(QuestionOut):
    sectionId: str
    sectionTitle: str
    # current answer (if any)
    value: Any = None
    conformance: str | None = None
    note: str = ""
    evidenceAttachmentIds: list[str] = []
    findingId: str | None = None


class ChecklistRunner(BaseModel):
    engagementId: str
    engagementCode: str
    engagementTitle: str
    status: str
    templateId: str | None = None
    templateName: str | None = None
    templateVersionUsed: int | None = None
    scoringConfig: dict[str, Any] = {}
    sections: list[dict[str, Any]] = []  # [{id, title, weightPct, questions:[RunnerQuestion]}]
    completedBy: str | None = None
    completedAt: datetime | None = None
    scorePercent: float | None = None
    overallResult: str | None = None


# ════════════════════════════════════════════════════════════════════════════
# Findings (Shared Service ③)
# ════════════════════════════════════════════════════════════════════════════
SeverityLit = Literal["OBSERVATION", "MINOR_NC", "MAJOR_NC", "CRITICAL_NC", "OPPORTUNITY_FOR_IMPROVEMENT"]


class FindingCreate(BaseModel):
    engagementId: str
    title: str = Field(min_length=3)
    description: str = ""
    severity: SeverityLit = "MINOR_NC"
    standardClauseRef: str | None = None
    areaOrAssetRef: str | None = None
    ownerId: str | None = None
    dueDate: datetime | None = None
    sourceQuestionId: str | None = None


class FindingUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    severity: SeverityLit | None = None
    standardClauseRef: str | None = None
    ownerId: str | None = None
    dueDate: datetime | None = None
    rootCauseMethod: str | None = None
    rootCauseSummary: str | None = None
    status: Literal["OPEN", "CAPA_RAISED", "IN_REMEDIATION", "VERIFICATION", "CLOSED", "ACCEPTED_RISK"] | None = None
    verificationNote: str | None = None
    evidenceAttachmentIds: list[str] | None = None


class FindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    findingCode: str
    engagementId: str
    engagementCode: str | None = None
    engagementTitle: str | None = None
    sourceQuestionId: str | None = None
    title: str
    description: str = ""
    severity: str
    standardClauseRef: str | None = None
    siteId: str | None = None
    siteName: str | None = None
    areaOrAssetRef: str | None = None
    ownerId: str | None = None
    ownerName: str | None = None
    rootCauseMethod: str | None = None
    rootCauseSummary: str | None = None
    capaId: str | None = None
    capaNumber: str | None = None
    capaState: str | None = None
    status: str
    isRepeatFinding: bool
    repeatOfFindingId: str | None = None
    dueDate: datetime | None = None
    closedBy: str | None = None
    closedAt: datetime | None = None
    verificationNote: str | None = None
    evidenceAttachmentIds: list[str] = []
    ageDays: int = 0
    capaRequired: bool = False  # MAJOR/CRITICAL must carry a CAPA before close
    createdAt: datetime | None = None
    updatedAt: datetime | None = None


class FindingListResponse(BaseModel):
    items: list[FindingOut]
    total: int
    severityCounts: dict[str, int] = {}
    statusCounts: dict[str, int] = {}
    repeatCount: int = 0


# ════════════════════════════════════════════════════════════════════════════
# Analytics & Benchmarking (C-13)
# ════════════════════════════════════════════════════════════════════════════
class BenchmarkRow(BaseModel):
    siteId: str | None = None
    siteName: str | None = None
    auditsPlanned: int = 0
    auditsConducted: int = 0
    completionRatePct: float = 0
    avgScorePct: float | None = None
    findingCount: int = 0
    findingDensity: float = 0  # findings per conducted audit (fair comparison)
    majorCriticalCount: int = 0
    repeatCount: int = 0


class ClauseConformanceRow(BaseModel):
    clause: str
    assessments: int
    nonConformances: int
    conformancePct: float


class ParetoRow(BaseModel):
    key: str
    label: str
    count: int


class AnalyticsOut(BaseModel):
    programme: dict[str, Any] = {}      # {planned, scheduled, inProgress, fieldworkComplete, reportIssued, closed, cancelled, overdue, total, completionRatePct}
    findingsBySeverity: dict[str, int] = {}
    repeatFindingRatePct: float = 0
    avgClosureDays: float | None = None
    openFindingCount: int = 0
    byType: dict[str, int] = {}
    bySourceModule: dict[str, int] = {}  # provenance: CAMS-native vs Fire/PPE/Pharma/EPC
    benchmarkingBySite: list[BenchmarkRow] = []
    clauseConformance: list[ClauseConformanceRow] = []
    paretoByClause: list[ParetoRow] = []
    capaOverduePct: float = 0


# ════════════════════════════════════════════════════════════════════════════
# Compliance Tracker (C-12)
# ════════════════════════════════════════════════════════════════════════════
class ComplianceLinkCreate(BaseModel):
    engagementId: str | None = None
    findingId: str | None = None
    obligationId: str
    linkType: Literal["VERIFIES", "BREACHES", "EVIDENCES"]
    notes: str = ""


class ComplianceLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    engagementId: str | None = None
    engagementCode: str | None = None
    findingId: str | None = None
    findingCode: str | None = None
    obligationId: str
    linkType: str
    notes: str = ""
    createdAt: datetime | None = None


class ObligationCoverageRow(BaseModel):
    obligationId: str
    obligationCode: str
    title: str
    regulatorName: str = ""
    siteId: str | None = None
    siteName: str | None = None
    status: str
    validUntil: datetime | None = None
    verifiedByAudit: bool = False
    lastVerifyingEngagementCode: str | None = None
    openNcCount: int = 0
    links: list[ComplianceLinkOut] = []


class ComplianceTrackerOut(BaseModel):
    totalObligations: int = 0
    verifiedByAuditCount: int = 0
    verifiedPct: float = 0
    openNcCount: int = 0
    statusCounts: dict[str, int] = {}
    rows: list[ObligationCoverageRow] = []


# ════════════════════════════════════════════════════════════════════════════
# CAPA — surfaced AUDIT-source view (C-14)
# ════════════════════════════════════════════════════════════════════════════
class AuditCapaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    capaNumber: str
    title: str
    state: str
    severity: str
    priority: str
    sourceTypeCode: str
    primaryOwnerUserId: str | None = None
    primaryOwnerName: str | None = None
    closureTargetDate: datetime | None = None
    sourceReferenceId: str | None = None
    sourceReferenceUrl: str | None = None
    findingCode: str | None = None
    engagementCode: str | None = None
    overdueDays: int = 0
    createdAt: datetime | None = None


class AuditCapaListResponse(BaseModel):
    items: list[AuditCapaOut] = []
    total: int = 0
    stateCounts: dict[str, int] = {}
    overdueCount: int = 0
    openCount: int = 0


__all__ = [k for k in dict(globals()) if k[0].isupper()]
