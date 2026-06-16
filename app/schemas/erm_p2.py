"""Pydantic schemas for ERM Phase 2 (KRI / Appetite / Compliance / Loss).

camelCase throughout; doubles as the API contract the Next.js frontend mirrors.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ════════════════════════════════════════════════════════════════════════════
# KRI
# ════════════════════════════════════════════════════════════════════════════
class MetricCatalogEntry(BaseModel):
    key: str
    sourceModule: str
    label: str
    unit: str
    direction: str
    frequency: str
    previewValue: float | None = None


class KriThresholds(BaseModel):
    thresholdGreen: float
    thresholdAmber: float


class KriUpsert(BaseModel):
    name: str = Field(min_length=3)
    description: str = ""
    categoryId: str
    linkedRiskIds: list[str] = []
    unit: str
    direction: Literal["HIGHER_IS_WORSE", "LOWER_IS_WORSE"] = "HIGHER_IS_WORSE"
    frequency: Literal["WEEKLY", "MONTHLY", "QUARTERLY"] = "MONTHLY"
    feedType: Literal["MANUAL", "MODULE_FED", "API"] = "MANUAL"
    metricProviderKey: str | None = None
    thresholdGreen: float
    thresholdAmber: float
    ownerId: str
    graceDays: int = 7
    isActive: bool = True


class KriOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    kriCode: str
    name: str
    description: str = ""
    categoryId: str
    categoryCode: str | None = None
    categoryName: str | None = None
    categoryColor: str | None = None
    linkedRiskIds: list[str] = []
    linkedRiskCount: int = 0
    unit: str
    direction: str
    frequency: str
    feedType: str
    metricProviderKey: str | None = None
    thresholdGreen: float
    thresholdAmber: float
    ownerId: str
    ownerName: str | None = None
    isActive: bool
    graceDays: int
    currentStatus: str
    currentValue: float | None = None
    apiToken: str | None = None
    sparkline: list[dict[str, Any]] = []  # [{periodLabel, value, status}]
    openBreaches: int = 0
    updatedAt: datetime | None = None


class KriListResponse(BaseModel):
    items: list[KriOut]
    total: int
    statusCounts: dict[str, int] = {}
    breachesOpen: int = 0


class ReadingCreate(BaseModel):
    periodLabel: str
    periodEnd: datetime | None = None
    value: float
    notes: str = ""


class BulkReadingRow(BaseModel):
    kriId: str
    periodLabel: str
    periodEnd: datetime | None = None
    value: float
    notes: str = ""


class ReadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    kriId: str
    periodLabel: str
    periodEnd: datetime
    value: float
    status: str
    source: str
    enteredBy: str | None = None
    enteredByName: str | None = None
    notes: str | None = None
    isCurrent: bool
    createdAt: datetime


class KriBreachOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    kriId: str
    kriCode: str | None = None
    kriName: str | None = None
    readingId: str | None = None
    breachType: str
    acknowledgedBy: str | None = None
    acknowledgedByName: str | None = None
    acknowledgedAt: datetime | None = None
    resolutionNotes: str | None = None
    status: str
    createdAt: datetime


class BreachAck(BaseModel):
    resolutionNotes: str = ""
    resolve: bool = False


class KriDetail(KriOut):
    readings: list[ReadingOut] = []
    breaches: list[KriBreachOut] = []
    linkedRisks: list[dict[str, Any]] = []  # {id, riskCode, title, residualBand}
    thresholdAnnotations: list[dict[str, Any]] = []


# ════════════════════════════════════════════════════════════════════════════
# Appetite
# ════════════════════════════════════════════════════════════════════════════
class ToleranceBand(BaseModel):
    bandType: Literal["MAX_RESIDUAL_SCORE", "MAX_CRITICAL_COUNT", "MAX_HIGH_PLUS_COUNT", "MAX_RED_KRI_COUNT"]
    thresholdValue: float


class AppetiteUpsert(BaseModel):
    categoryId: str
    statementText: str = Field(min_length=1)
    appetiteLevel: Literal["AVERSE", "MINIMAL", "CAUTIOUS", "OPEN", "SEEKING"]
    toleranceBands: list[ToleranceBand] = []


class AppetiteApprove(BaseModel):
    approvalReference: str = Field(min_length=1)


class AppetiteStatementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    categoryId: str
    categoryCode: str | None = None
    categoryName: str | None = None
    categoryColor: str | None = None
    statementText: str
    appetiteLevel: str
    version: int
    status: str
    approvedBy: str | None = None
    approvedByName: str | None = None
    approvalReference: str | None = None
    approvedAt: datetime | None = None
    effectiveFrom: datetime | None = None
    toleranceBands: list[dict[str, Any]] = []
    updatedAt: datetime | None = None


class BandGauge(BaseModel):
    bandType: str
    thresholdValue: float
    observedValue: float
    state: str  # WITHIN | APPROACHING | BREACH


class AppetiteDashRow(BaseModel):
    categoryId: str
    categoryCode: str | None = None
    categoryName: str | None = None
    categoryColor: str | None = None
    appetiteLevel: str | None = None
    statementExcerpt: str = ""
    statementId: str | None = None
    status: str | None = None
    gauges: list[BandGauge] = []
    openBreaches: int = 0


class AppetiteBreachOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    appetiteStatementId: str
    categoryId: str
    categoryCode: str | None = None
    categoryName: str | None = None
    bandType: str
    observedValue: float
    thresholdValue: float
    triggeringEntityIds: list[str] = []
    triggeringEntities: list[dict[str, Any]] = []  # {id, type, code, title}
    detectedAt: datetime
    status: str
    committeeDecision: str | None = None
    decisionBy: str | None = None
    decisionByName: str | None = None
    reviewByDate: datetime | None = None
    ageDays: int = 0


class BreachDecision(BaseModel):
    action: Literal["UNDER_REVIEW", "TREATMENT_MANDATED", "TEMPORARILY_ACCEPTED", "RESOLVED"]
    committeeDecision: str = ""
    reviewByDate: datetime | None = None


# ════════════════════════════════════════════════════════════════════════════
# Compliance
# ════════════════════════════════════════════════════════════════════════════
class ObligationUpsert(BaseModel):
    title: str = Field(min_length=3)
    obligationType: Literal["LICENCE", "CONSENT", "RETURN_FILING", "STATUTORY_DUTY", "REGISTRATION"]
    statuteReference: str = ""
    regulatorName: str = ""
    siteId: str | None = None
    ownerId: str
    frequency: Literal["ONE_TIME", "MONTHLY", "QUARTERLY", "HALF_YEARLY", "ANNUAL", "PERIODIC_RENEWAL"]
    validFrom: datetime | None = None
    validUntil: datetime | None = None
    renewalLeadDays: int = 60
    conditions: list[str] = []
    linkedRiskIds: list[str] = []
    isActive: bool = True


class ObligationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    obligationCode: str
    title: str
    obligationType: str
    statuteReference: str = ""
    regulatorName: str = ""
    siteId: str | None = None
    siteName: str | None = None
    ownerId: str
    ownerName: str | None = None
    frequency: str
    validFrom: datetime | None = None
    validUntil: datetime | None = None
    renewalLeadDays: int
    conditions: list[str] = []
    linkedRiskIds: list[str] = []
    status: str
    isActive: bool
    openTaskCount: int = 0
    nextDueDate: datetime | None = None
    updatedAt: datetime | None = None


class ObligationListResponse(BaseModel):
    items: list[ObligationOut]
    total: int
    statusCounts: dict[str, int] = {}
    typeCounts: dict[str, int] = {}


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    obligationId: str
    obligationCode: str | None = None
    obligationTitle: str | None = None
    taskType: str
    periodLabel: str
    dueDate: datetime
    status: str
    attestedBy: str | None = None
    attestedByName: str | None = None
    attestedAt: datetime | None = None
    verifiedBy: str | None = None
    verifiedByName: str | None = None
    verifiedAt: datetime | None = None
    capaId: str | None = None
    waiverJustification: str | None = None
    remarks: str | None = None
    overdueDays: int = 0
    attachmentCount: int = 0


class TaskAttest(BaseModel):
    remarks: str = ""


class TaskWaive(BaseModel):
    waiverJustification: str = Field(min_length=1)


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    taskId: str
    fileName: str
    storagePath: str
    mimeType: str | None = None
    caption: str | None = None
    uploadedById: str
    uploadedByName: str | None = None
    uploadedAt: datetime


class ObligationDetail(ObligationOut):
    tasks: list[TaskOut] = []
    attachments: list[AttachmentOut] = []
    linkedRisks: list[dict[str, Any]] = []


class ComplianceDashboard(BaseModel):
    totalObligations: int
    compliantPct: float
    dueSoon: int
    overdue: int
    underRenewal: int
    typeCounts: dict[str, int] = {}
    siteSplit: dict[str, int] = {}
    renewalCalendar: list[dict[str, Any]] = []  # {obligationCode, title, validUntil, daysToExpiry, status}
    overdueTable: list[dict[str, Any]] = []


# ════════════════════════════════════════════════════════════════════════════
# Loss Events
# ════════════════════════════════════════════════════════════════════════════
class LossUpsert(BaseModel):
    title: str = Field(min_length=3)
    description: str = ""
    eventDate: datetime
    siteId: str | None = None
    categoryId: str
    subCategoryId: str | None = None
    linkedRiskIds: list[str] = []
    isNearMiss: bool = False
    grossLossInr: float = 0
    recoveredInr: float = 0
    potentialLossInr: float | None = None
    lossTypes: list[str] = []


class LossClose(BaseModel):
    closureNotes: str = Field(min_length=1)


class LossEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    eventCode: str
    title: str
    description: str = ""
    eventDate: datetime
    siteId: str | None = None
    siteName: str | None = None
    categoryId: str
    categoryCode: str | None = None
    categoryName: str | None = None
    categoryColor: str | None = None
    subCategoryId: str | None = None
    linkedRiskIds: list[str] = []
    source: str
    sourceIncidentId: str | None = None
    isNearMiss: bool
    grossLossInr: float
    recoveredInr: float
    netLossInr: float
    potentialLossInr: float | None = None
    lossTypes: list[str] = []
    status: str
    closureNotes: str | None = None
    sourceUpdatedFlag: bool = False
    updatedAt: datetime | None = None


class LossListResponse(BaseModel):
    items: list[LossEventOut]
    total: int
    statusCounts: dict[str, int] = {}
    netLossTotal: float = 0
    nearMissPotentialTotal: float = 0


class CalibrationRow(BaseModel):
    riskId: str
    riskCode: str
    title: str
    categoryCode: str | None = None
    residualScore: int | None = None
    residualBand: str | None = None
    actualNetLoss12m: float
    lossEventCount: int
    flag: str | None = None  # UNDERSCORED | WATCH | null


class LossAnalytics(BaseModel):
    netLossByCategory: list[dict[str, Any]] = []  # {categoryCode, categoryName, colorHex, netLoss}
    lossTrendByQuarter: list[dict[str, Any]] = []  # {quarter, netLoss}
    topLosses: list[dict[str, Any]] = []
    nearMissPotential: list[dict[str, Any]] = []
    calibration: list[CalibrationRow] = []


__all__ = [k for k in dict(globals()) if k[0].isupper()]
