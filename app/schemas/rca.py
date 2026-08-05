"""Pydantic schemas for the ERM Cross-Domain RCA & Causal Intelligence module.

camelCase throughout (no alias_generator) to match the DB / frontend. These
models double as the API contract the Next.js frontend mirrors as TS types.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RiskDomain = Literal[
    "OPERATIONAL", "FINANCIAL", "COMPLIANCE", "EXTERNAL",
    "REPUTATIONAL", "CYBER", "STRATEGIC", "ESG",
]
RcaOriginType = Literal["EVENT", "RISK", "LOSS_EVENT"]
RcaMethodology = Literal[
    "FIVE_WHY", "FISHBONE", "FTA", "BOWTIE", "TAPROOT", "CAUSE_MAP", "NARRATIVE",
]
RcaStatus = Literal["DRAFT", "IN_ANALYSIS", "PEER_REVIEW", "APPROVED", "SUPERSEDED"]
CausalRole = Literal["ROOT", "CONTRIBUTING", "DIRECT"]
Confidence = Literal["CONFIRMED", "PROBABLE", "POSSIBLE"]
ContributionType = Literal["CAUSED", "ELEVATED", "REVEALED", "RECURRING_DRIVER"]


# ─────────────────────────────────────────────────────────────────────
# Taxonomy
# ─────────────────────────────────────────────────────────────────────
class SubCauseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    categoryId: str
    code: str
    name: str
    description: str = ""
    applicableDomains: list[str] = []
    synonyms: list[str] = []
    isActive: bool


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    description: str = ""
    colorHex: str
    displayOrder: int
    isActive: bool
    subCauses: list[SubCauseOut] = []


class CategoryUpsert(BaseModel):
    code: str
    name: str
    description: str = ""
    colorHex: str = "#475569"
    displayOrder: int = 0
    isActive: bool = True


class CategoryUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    colorHex: str | None = None
    displayOrder: int | None = None
    isActive: bool | None = None


class SubCauseUpsert(BaseModel):
    categoryId: str
    code: str
    name: str
    description: str = ""
    applicableDomains: list[RiskDomain] = []
    synonyms: list[str] = []
    isActive: bool = True


class SubCauseUpdate(BaseModel):
    categoryId: str | None = None  # allow re-parenting to another enterprise category
    name: str | None = None
    description: str | None = None
    applicableDomains: list[RiskDomain] | None = None
    synonyms: list[str] | None = None
    isActive: bool | None = None


# ─────────────────────────────────────────────────────────────────────
# Tagged causes + risk links
# ─────────────────────────────────────────────────────────────────────
class IdentifiedCauseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    subCauseId: str
    enterpriseCategoryId: str
    causalRole: CausalRole
    description: str | None = None
    confidence: Confidence | None = None
    sortOrder: int = 0
    # enriched
    subCauseName: str | None = None
    subCauseCode: str | None = None
    categoryName: str | None = None
    categoryCode: str | None = None


class CauseTagIn(BaseModel):
    subCauseId: str
    causalRole: CausalRole = "CONTRIBUTING"
    description: str | None = None
    confidence: Confidence | None = None
    sortOrder: int = 0


class RiskLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    riskId: str
    contributionType: ContributionType
    weight: float | None = None
    note: str | None = None
    # enriched
    riskCode: str | None = None
    riskTitle: str | None = None
    riskResidualBand: str | None = None


class RiskLinkIn(BaseModel):
    riskId: str
    contributionType: ContributionType = "CAUSED"
    weight: float | None = Field(default=None, ge=0, le=1)
    note: str | None = None


# ─────────────────────────────────────────────────────────────────────
# RootCauseAnalysis
# ─────────────────────────────────────────────────────────────────────
class LinkedRiskRef(BaseModel):
    riskId: str
    riskCode: str | None = None
    riskTitle: str | None = None


class RcaListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    rcaCode: str
    title: str
    originType: RcaOriginType
    primaryDomain: RiskDomain
    methodology: RcaMethodology
    status: RcaStatus
    analystId: str
    plantId: str | None = None
    occurrenceDate: datetime | None = None
    createdAt: datetime
    causeCount: int = 0
    linkedRiskCount: int = 0
    # Traceability — the originating record + every risk this RCA touches.
    sourceEventId: str | None = None
    sourceRiskId: str | None = None
    sourceLossEventId: str | None = None
    sourceCode: str | None = None
    sourceHref: str | None = None
    linkedRisks: list[LinkedRiskRef] = []


class RcaListResponse(BaseModel):
    items: list[RcaListItem] = []
    total: int = 0


class RcaDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    rcaCode: str
    title: str
    originType: RcaOriginType
    sourceEventId: str | None = None
    sourceRiskId: str | None = None
    sourceLossEventId: str | None = None
    primaryDomain: RiskDomain
    methodology: RcaMethodology
    status: RcaStatus
    analysisPayload: dict = {}
    narrative: str | None = None
    analystId: str
    approverId: str | None = None
    approvedAt: datetime | None = None
    occurrenceDate: datetime | None = None
    plantId: str | None = None
    createdAt: datetime
    updatedAt: datetime
    identifiedCauses: list[IdentifiedCauseOut] = []
    riskLinks: list[RiskLinkOut] = []
    capaIds: list[str] = []
    # enriched origin context
    sourceLabel: str | None = None


class RcaCreateRisk(BaseModel):
    """Path B — open an RCA directly on an EnterpriseRisk (no incident)."""
    sourceRiskId: str
    title: str
    methodology: RcaMethodology = "FIVE_WHY"
    narrative: str | None = None
    occurrenceDate: datetime | None = None


class RcaCreateLoss(BaseModel):
    """Path C — open an RCA on a LossEvent (financial/compliance/cyber/…)."""
    sourceLossEventId: str
    title: str
    methodology: RcaMethodology = "FIVE_WHY"
    narrative: str | None = None
    occurrenceDate: datetime | None = None


class RcaCreateEvent(BaseModel):
    """Path A — expose an RCA from an operational event (incident). The incident
    remains system-of-record; analysisPayload is snapshotted from it."""
    sourceEventId: str
    title: str | None = None
    methodology: RcaMethodology | None = None


class RcaUpdate(BaseModel):
    title: str | None = None
    methodology: RcaMethodology | None = None
    analysisPayload: dict | None = None
    narrative: str | None = None
    occurrenceDate: datetime | None = None
    primaryDomain: RiskDomain | None = None


class ApproveIn(BaseModel):
    note: str | None = None


class RaiseCapaIn(BaseModel):
    title: str
    problem: str
    severity: str = "MODERATE"
    priority: str = "HIGH"
    ownerId: str | None = None
    dueDays: int = 90


# ─────────────────────────────────────────────────────────────────────
# Analytics (computed from RCA records)
# ─────────────────────────────────────────────────────────────────────
class CauseAnalytic(BaseModel):
    subCauseId: str
    subCauseCode: str
    subCauseName: str
    enterpriseCategoryId: str
    categoryCode: str
    categoryName: str
    occurrences: int          # RCAs citing this sub-cause
    riskReach: int            # distinct risks driven — the "combination" metric
    domainSpread: int         # distinct domains — the cross-domain headline
    domains: list[str] = []
    rcaCount: int
    isRecurringDriver: bool = False


class CategoryRollup(BaseModel):
    enterpriseCategoryId: str
    categoryCode: str
    categoryName: str
    colorHex: str
    occurrences: int
    riskReach: int
    domainSpread: int
    domains: list[str] = []
    subCauseCount: int


class CauseAnalyticsResponse(BaseModel):
    computedAt: datetime
    periodStart: datetime | None = None
    periodEnd: datetime | None = None
    domainFilter: str | None = None
    causes: list[CauseAnalytic] = []
    categories: list[CategoryRollup] = []
    recurringDriverThreshold: int = 2
    note: str = "Computed from approved RCA records."


class CauseDetailRisk(BaseModel):
    riskId: str
    riskCode: str | None = None
    riskTitle: str | None = None
    residualBand: str | None = None
    residualScore: int | None = None


class CauseDetailRca(BaseModel):
    rcaId: str
    rcaCode: str
    title: str
    originType: str | None = None
    primaryDomain: str | None = None


class CauseDetailResponse(BaseModel):
    computedAt: datetime
    subCauseId: str
    subCauseCode: str
    subCauseName: str
    categoryCode: str = ""
    categoryName: str = ""
    occurrences: int = 0
    riskReach: int = 0
    domainSpread: int = 0
    domains: list[str] = []
    risks: list[CauseDetailRisk] = []
    rcas: list[CauseDetailRca] = []
    note: str = "Computed from approved RCA records."


class ContributingCause(BaseModel):
    subCauseId: str
    subCauseName: str
    categoryCode: str
    categoryName: str
    count: int
    rcaCodes: list[str] = []
    latestOccurrence: datetime | None = None


class ContributingCausesResponse(BaseModel):
    riskId: str
    causes: list[ContributingCause] = []
    note: str = "Computed from approved RCA records."


# ─────────────────────────────────────────────────────────────────────
# Cause-to-Risk map
# ─────────────────────────────────────────────────────────────────────
class GraphNode(BaseModel):
    id: str
    type: Literal["cause", "category", "risk"]
    label: str
    sublabel: str | None = None
    domain: str | None = None
    colorHex: str | None = None
    band: str | None = None


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    contributionType: str | None = None
    weight: float | None = None


class CauseRiskGraph(BaseModel):
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    focusSubCauseId: str | None = None
