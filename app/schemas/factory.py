"""Pydantic schemas for the Facilities module (Factory Profile Master).

camelCase throughout; doubles as the API contract the Next.js frontend mirrors
in src/app/(dashboard)/facilities/lib.ts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Extension-layer Out schemas embedded in FactoryProfileDetail (one-way import —
# factory_ext does not import this module, so there is no cycle).
from app.schemas.factory_ext import (
    EquipmentOut as FxEquipmentOut,
    HazmatOut as FxHazmatOut,
    LifecycleEventOut as FxLifecycleEventOut,
    RegulatoryOut as FxRegulatoryOut,
)

FactoryStatusLit = Literal[
    "OPERATIONAL", "UNDER_CONSTRUCTION", "PARTIAL_OPERATION", "SHUTDOWN", "DECOMMISSIONED"
]
OwnershipTypeLit = Literal["OWNED", "LEASED", "CONTRACT_MANUFACTURING", "JOINT_VENTURE"]
ProfileStatusLit = Literal["DRAFT", "ACTIVE", "REVIEW_DUE"]
BuildingTypeLit = Literal[
    "PRODUCTION", "WAREHOUSE", "ADMIN_OFFICE", "UTILITY", "CANTEEN",
    "DORMITORY", "ETP_PLANT", "BOILER_HOUSE", "STORE", "OTHER",
]
CertificationTypeLit = Literal[
    "SA8000", "ISO_9001", "ISO_14001", "ISO_45001", "WRAP",
    "BSCI", "OEKO_TEX", "GOTS", "SEDEX_SMETA", "OTHER",
]
CertStatusLit = Literal["VALID", "EXPIRING_SOON", "EXPIRED", "UNDER_RENEWAL", "SUSPENDED"]
ContactRoleLit = Literal[
    "FACTORY_MANAGER", "SAFETY_OFFICER", "COMPLIANCE_OFFICER", "HR_HEAD", "ENVIRONMENT_OFFICER", "OTHER",
]
ComplianceFlagLit = Literal["COMPLIANT", "ATTENTION", "NON_COMPLIANT", "NOT_ASSESSED"]


class RegistrationNo(BaseModel):
    type: str
    number: str


# ════════════════════════════════════════════════════════════════════════════
# Buildings
# ════════════════════════════════════════════════════════════════════════════
class BuildingCreate(BaseModel):
    buildingName: str = Field(min_length=1)
    buildingType: BuildingTypeLit = "PRODUCTION"
    floors: int = Field(1, ge=1)
    areaSqm: float | None = Field(None, ge=0)
    maxOccupancy: int | None = Field(None, ge=0)
    currentOccupancy: int | None = Field(None, ge=0)
    yearBuilt: int | None = None
    assemblyPoint: str | None = None
    emergencyExits: int | None = Field(None, ge=0)
    occupancyCertificateNo: str | None = None
    isActive: bool = True


class BuildingUpdate(BaseModel):
    buildingName: str | None = None
    buildingType: BuildingTypeLit | None = None
    floors: int | None = None
    areaSqm: float | None = None
    maxOccupancy: int | None = None
    currentOccupancy: int | None = None
    yearBuilt: int | None = None
    assemblyPoint: str | None = None
    emergencyExits: int | None = None
    occupancyCertificateNo: str | None = None
    isActive: bool | None = None


class BuildingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    factoryProfileId: str
    siteId: str
    buildingName: str
    buildingType: str
    floors: int
    areaSqm: float | None = None
    maxOccupancy: int | None = None
    currentOccupancy: int | None = None
    yearBuilt: int | None = None
    assemblyPoint: str | None = None
    emergencyExits: int | None = None
    occupancyCertificateNo: str | None = None
    isActive: bool
    updatedAt: datetime | None = None


# ════════════════════════════════════════════════════════════════════════════
# Workforce Composition (SA8000 lens)
# ════════════════════════════════════════════════════════════════════════════
class WorkforceCompositionCreate(BaseModel):
    asOfDate: datetime | None = None  # defaults to now
    permanentCount: int = Field(0, ge=0)
    contractCount: int = Field(0, ge=0)
    apprenticeTraineeCount: int = Field(0, ge=0)
    maleCount: int = Field(0, ge=0)
    femaleCount: int = Field(0, ge=0)
    otherGenderCount: int = Field(0, ge=0)
    migrantWorkerCount: int | None = Field(None, ge=0)
    differentlyAbledCount: int | None = Field(None, ge=0)
    # child-labour evidence (SA8000 Element 1)
    youngestWorkerAge: int | None = Field(None, ge=0)
    workersUnder18Count: int = Field(0, ge=0)
    minHiringAgePolicy: int | None = Field(None, ge=0)
    notes: str | None = None


class WorkforceCompositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    factoryProfileId: str
    siteId: str
    asOfDate: datetime
    isCurrent: bool
    permanentCount: int
    contractCount: int
    apprenticeTraineeCount: int
    maleCount: int
    femaleCount: int
    otherGenderCount: int
    migrantWorkerCount: int | None = None
    differentlyAbledCount: int | None = None
    totalCount: int
    # child-labour evidence (SA8000 Element 1)
    youngestWorkerAge: int | None = None
    workersUnder18Count: int = 0
    minHiringAgePolicy: int | None = None
    # derived (persisted)
    contractPct: float = 0
    femalePct: float = 0
    migrantPct: float | None = None
    # computed enrichment (set in the router)
    genderTotal: int = 0
    genderMismatch: bool = False
    childLabourFlag: bool = False  # under-18 present AND youngest < min hiring age
    notes: str | None = None
    updatedAt: datetime | None = None


# ════════════════════════════════════════════════════════════════════════════
# Production Process
# ════════════════════════════════════════════════════════════════════════════
class ProductionProcessCreate(BaseModel):
    processName: str = Field(min_length=1)
    processCategory: str | None = None
    description: str | None = None
    sequenceOrder: int | None = None
    shiftPattern: str | None = None
    installedCapacity: str | None = None
    keyHazards: list[str] = []
    isActive: bool = True


class ProductionProcessUpdate(BaseModel):
    processName: str | None = None
    processCategory: str | None = None
    description: str | None = None
    sequenceOrder: int | None = None
    shiftPattern: str | None = None
    installedCapacity: str | None = None
    keyHazards: list[str] | None = None
    isActive: bool | None = None


class ProductionProcessOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    factoryProfileId: str
    siteId: str
    processName: str
    processCategory: str | None = None
    description: str | None = None
    sequenceOrder: int | None = None
    shiftPattern: str | None = None
    installedCapacity: str | None = None
    keyHazards: list[str] = []
    isActive: bool
    updatedAt: datetime | None = None


# ════════════════════════════════════════════════════════════════════════════
# Factory Certification (status engine)
# ════════════════════════════════════════════════════════════════════════════
class FactoryCertificationCreate(BaseModel):
    certificationType: CertificationTypeLit
    certificateNo: str | None = None
    issuingBody: str | None = None
    issueDate: datetime | None = None
    expiryDate: datetime | None = None
    renewalLeadDays: int = Field(60, ge=0)
    # Manual override only for UNDER_RENEWAL / SUSPENDED; VALID/EXPIRING_SOON/
    # EXPIRED are always derived from the dates.
    status: CertStatusLit | None = None
    scopeNotes: str | None = None
    attachmentIds: list[str] = []


class FactoryCertificationUpdate(BaseModel):
    certificationType: CertificationTypeLit | None = None
    certificateNo: str | None = None
    issuingBody: str | None = None
    issueDate: datetime | None = None
    expiryDate: datetime | None = None
    renewalLeadDays: int | None = None
    status: CertStatusLit | None = None
    scopeNotes: str | None = None
    attachmentIds: list[str] | None = None


class FactoryCertificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    factoryProfileId: str
    siteId: str
    certificationType: str
    certificateNo: str | None = None
    issuingBody: str | None = None
    issueDate: datetime | None = None
    expiryDate: datetime | None = None
    renewalLeadDays: int
    status: str  # effective status, computed in the router
    daysToExpiry: int | None = None  # computed
    scopeNotes: str | None = None
    attachmentIds: list[str] = []
    updatedAt: datetime | None = None


# ════════════════════════════════════════════════════════════════════════════
# Factory Contact
# ════════════════════════════════════════════════════════════════════════════
class FactoryContactCreate(BaseModel):
    role: ContactRoleLit = "OTHER"
    name: str = Field(min_length=1)
    phone: str | None = None
    email: str | None = None
    isPrimary: bool = False


class FactoryContactUpdate(BaseModel):
    role: ContactRoleLit | None = None
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    isPrimary: bool | None = None


class FactoryContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    factoryProfileId: str
    siteId: str
    role: str
    name: str
    phone: str | None = None
    email: str | None = None
    isPrimary: bool
    updatedAt: datetime | None = None


# ════════════════════════════════════════════════════════════════════════════
# Social-Compliance Profile (SA8000 policy/standing — 1:1 with factory)
# ════════════════════════════════════════════════════════════════════════════
class SocialComplianceProfileUpsert(BaseModel):
    """Create-or-update payload. All fields optional so a partial PATCH-style
    submit only touches the supplied fields; on create, missing flags default to
    NOT_ASSESSED (handled in the router)."""

    asOfDate: datetime | None = None
    minimumWageCompliant: ComplianceFlagLit | None = None
    lowestMonthlyWageInr: int | None = Field(None, ge=0)
    statutoryMinimumWageInr: int | None = Field(None, ge=0)
    wagesPaidOnTime: ComplianceFlagLit | None = None
    standardWeeklyHours: int | None = Field(None, ge=0)
    maxWeeklyOvertimeHours: int | None = Field(None, ge=0)
    overtimeVoluntary: ComplianceFlagLit | None = None
    weeklyRestDayProvided: ComplianceFlagLit | None = None
    unionOrWorkerCommitteePresent: ComplianceFlagLit | None = None
    collectiveBargainingAgreement: bool | None = None
    noDepositOrDocumentRetention: ComplianceFlagLit | None = None
    grievanceMechanismPresent: ComplianceFlagLit | None = None
    antiDiscriminationPolicy: ComplianceFlagLit | None = None
    sa8000AwarenessTrainingPct: float | None = Field(None, ge=0, le=100)
    socialComplianceOwnerId: str | None = None
    lastSocialAuditDate: datetime | None = None
    nextReviewDate: datetime | None = None


class SocialComplianceProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    factoryProfileId: str
    siteId: str
    asOfDate: datetime
    minimumWageCompliant: str
    lowestMonthlyWageInr: int | None = None
    statutoryMinimumWageInr: int | None = None
    wagesPaidOnTime: str
    standardWeeklyHours: int | None = None
    maxWeeklyOvertimeHours: int | None = None
    overtimeVoluntary: str
    weeklyRestDayProvided: str
    unionOrWorkerCommitteePresent: str
    collectiveBargainingAgreement: bool
    noDepositOrDocumentRetention: str
    grievanceMechanismPresent: str
    antiDiscriminationPolicy: str
    sa8000AwarenessTrainingPct: float | None = None
    socialComplianceOwnerId: str | None = None
    lastSocialAuditDate: datetime | None = None
    nextReviewDate: datetime | None = None
    overallSocialComplianceFlag: str
    updatedAt: datetime | None = None


# ════════════════════════════════════════════════════════════════════════════
# Compliance snapshot (live metrics read from existing engines — Phase D)
# ════════════════════════════════════════════════════════════════════════════
class SnapshotMetrics(BaseModel):
    auditComplianceScorePct: float | None = None
    openFindings: int = 0
    criticalFindings: int = 0
    openCapas: int = 0
    overdueCapas: int = 0
    openObligations: int = 0
    overdueObligations: int = 0
    certsExpiringCount: int = 0
    incidentCount12m: int = 0
    lastAuditDate: datetime | None = None
    computedAt: datetime | None = None


# ── Facility rollup blocks (read-model projection — Compliance & Audit tab) ──
# Every new block is a LIVE, site-scoped read from an existing engine. The
# contract below mirrors the build-prompt FacilityMetricProvider shape, kept
# pragmatic: tiles + drill rows + a neutral/degraded path. `state` drives the RAG
# colour; `neutral` = not-enabled / no-data (never a zero that reads as a pass).
TileStateLit = Literal["good", "watch", "breach", "neutral"]
DeltaDirectionLit = Literal["up", "down", "flat"]
RowToneLit = Literal["positive", "warning", "critical", "muted"]


class ModuleDeepLink(BaseModel):
    module: str
    route: str
    query: dict[str, str] = {}


class KpiDelta(BaseModel):
    priorValue: float | str | None = None
    direction: DeltaDirectionLit = "flat"
    # None ⇒ neutral metric (e.g. obligations) — render no RAG tint on the delta.
    isImprovement: bool | None = None
    displayPct: float | None = None  # signed % change where meaningful


class FacilityTile(BaseModel):
    id: str
    label: str
    value: float | int | str | None = None
    unit: str | None = None
    state: TileStateLit = "neutral"
    delta: KpiDelta | None = None
    drillTo: ModuleDeepLink | None = None


class FacilityRollupRow(BaseModel):
    id: str
    primaryText: str
    secondaryText: str | None = None
    statusLabel: str | None = None
    statusTone: RowToneLit = "muted"
    trailingText: str | None = None
    drillTo: ModuleDeepLink | None = None


class FacilityMetricBlock(BaseModel):
    domainKey: str                       # 'environment' | 'training' | 'certifications' | ...
    enabled: bool = True                 # false ⇒ render the neutral "not enabled" card
    degraded: bool = False               # provider failed ⇒ "data refreshing" badge, not an error
    title: str
    caption: str                         # "Live from the … engine — site-scoped."
    tiles: list[FacilityTile] = []
    rows: list[FacilityRollupRow] = []
    emptyText: str | None = None         # enabled but no rows in period
    notEnabledText: str | None = None    # shown when enabled = false
    lastRefreshedAt: datetime | None = None
    drillTo: ModuleDeepLink | None = None


class ComplianceTabResponse(BaseModel):
    metrics: SnapshotMetrics
    # Time dimension — prior-period snapshot the frontend diffs the strip against.
    priorMetrics: SnapshotMetrics | None = None
    periodRef: str | None = None         # e.g. "2026-Q2"
    priorPeriodRef: str | None = None    # e.g. "2026-Q1"
    audits: list[dict] = []
    findings: list[dict] = []
    capas: list[dict] = []
    obligations: list[dict] = []
    incidents: list[dict] = []
    # New live rollup blocks. Null ⇒ not assembled this request (social is also
    # null when omitted on non-garment sites).
    environment: FacilityMetricBlock | None = None       # P1
    training: FacilityMetricBlock | None = None          # P1
    certifications: FacilityMetricBlock | None = None     # P1
    socialCompliance: FacilityMetricBlock | None = None   # P2 — garment-gated
    operationalRisk: FacilityMetricBlock | None = None    # P2 — live / point-in-time
    # True if any provider degraded — frontend shows a stale badge, never an error.
    degraded: bool = False


# ════════════════════════════════════════════════════════════════════════════
# Factory Profile
# ════════════════════════════════════════════════════════════════════════════
class FactoryProfileCreate(BaseModel):
    siteId: str  # Plant.id — mandatory 1:1 link
    factoryName: str = Field(min_length=2)
    factoryCode: str | None = None  # auto-generated when omitted
    status: FactoryStatusLit = "OPERATIONAL"
    ownershipType: OwnershipTypeLit = "OWNED"
    addressLine: str = ""
    city: str = ""
    state: str = ""
    pincode: str = ""
    latitude: float | None = None
    longitude: float | None = None
    establishedYear: int | None = None
    factoryLicenseNo: str | None = None
    factoryLicenseValidUntil: datetime | None = None
    registrationNos: list[RegistrationNo] = []
    applicableActs: list[str] = []
    pollutionControlBoard: str | None = None
    totalLandAreaSqm: float | None = None
    builtUpAreaSqm: float | None = None
    buildingCount: int | None = None  # manual when no Building rows entered
    primaryIndustry: str = "Garments / Textile"
    # Quick-add child records from the wizard (all optional — can be added later).
    buildings: list[BuildingCreate] = []
    workforce: WorkforceCompositionCreate | None = None  # initial composition
    processes: list[ProductionProcessCreate] = []


class FactoryProfileUpdate(BaseModel):
    factoryName: str | None = None
    status: FactoryStatusLit | None = None
    ownershipType: OwnershipTypeLit | None = None
    addressLine: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    establishedYear: int | None = None
    factoryLicenseNo: str | None = None
    factoryLicenseValidUntil: datetime | None = None
    registrationNos: list[RegistrationNo] | None = None
    applicableActs: list[str] | None = None
    pollutionControlBoard: str | None = None
    totalLandAreaSqm: float | None = None
    builtUpAreaSqm: float | None = None
    buildingCount: int | None = None
    primaryIndustry: str | None = None
    profileStatus: ProfileStatusLit | None = None


class FactoryProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    siteId: str
    siteName: str | None = None  # enriched from Plant
    factoryCode: str
    factoryName: str
    status: str
    ownershipType: str
    addressLine: str
    city: str
    state: str
    pincode: str
    latitude: float | None = None
    longitude: float | None = None
    establishedYear: int | None = None
    factoryLicenseNo: str | None = None
    factoryLicenseValidUntil: datetime | None = None
    registrationNos: list[RegistrationNo] = []
    applicableActs: list[str] = []
    pollutionControlBoard: str | None = None
    totalLandAreaSqm: float | None = None
    builtUpAreaSqm: float | None = None
    buildingCount: int
    totalEmployees: int
    primaryIndustry: str
    profileStatus: str
    lastReviewedAt: datetime | None = None
    nextReviewDate: datetime | None = None
    # lifecycle workflow (governance approval state)
    lifecycleStage: str = "INITIATED"
    lifecycleStageOwnerRole: str | None = None
    lifecycleUpdatedAt: datetime | None = None
    # cert roll-up (computed in the list endpoint)
    certCount: int = 0
    certsExpiringCount: int = 0  # EXPIRING_SOON + EXPIRED
    # live compliance metrics (from the LIVE snapshot; null until first recompute)
    metrics: SnapshotMetrics | None = None
    updatedAt: datetime | None = None


class FactoryProfileDetail(FactoryProfileOut):
    """Profile + its building register, current workforce (+ history),
    production processes, and the extension layer — equipment / hazardous
    materials / regulatory registrations / lifecycle events (F-02 detail page)."""

    buildings: list[BuildingOut] = []
    currentWorkforce: WorkforceCompositionOut | None = None
    workforceHistory: list[WorkforceCompositionOut] = []
    processes: list[ProductionProcessOut] = []
    certifications: list[FactoryCertificationOut] = []
    contacts: list[FactoryContactOut] = []
    socialCompliance: SocialComplianceProfileOut | None = None
    # extension layer (Facilities build spec)
    equipment: list[FxEquipmentOut] = []
    hazardousMaterials: list[FxHazmatOut] = []
    regulatoryRegistrations: list[FxRegulatoryOut] = []
    lifecycleEvents: list[FxLifecycleEventOut] = []


class FactoryProfileListResponse(BaseModel):
    items: list[FactoryProfileOut]
    total: int
    # roll-ups powering the F-01 KPI strip + filter chips
    totalBuildings: int = 0
    totalEmployees: int = 0
    certsExpiring: int = 0  # group-wide EXPIRING_SOON + EXPIRED
    groupComplianceScore: float | None = None  # avg of factories with a score
    groupOpenCapas: int = 0
    groupOverdueCapas: int = 0
    statusCounts: dict[str, int] = {}
    stateCounts: dict[str, int] = {}


# ════════════════════════════════════════════════════════════════════════════
# Group registers (W-01 register view + the three Reports-tile CSV exports)
# ════════════════════════════════════════════════════════════════════════════
class SocialComplianceRegisterRow(BaseModel):
    """One factory: current workforce + social-compliance profile, flattened with
    the computed flags. Powers the W-01 table and the Workforce/SA8000 CSV."""

    factoryProfileId: str
    factoryCode: str
    factoryName: str
    state: str
    city: str
    asOfDate: datetime | None = None
    # workforce
    totalWorkforce: int = 0
    permanentCount: int = 0
    permanentPct: float = 0
    contractCount: int = 0
    contractPct: float = 0
    apprenticeTraineeCount: int = 0
    maleCount: int = 0
    femaleCount: int = 0
    femalePct: float = 0
    otherGenderCount: int = 0
    migrantWorkerCount: int | None = None
    migrantPct: float | None = None
    differentlyAbledCount: int | None = None
    youngestWorkerAge: int | None = None
    workersUnder18Count: int = 0
    minHiringAgePolicy: int | None = None
    childLabourFlag: bool = False
    # social-compliance
    hasSocialProfile: bool = False
    minimumWageCompliant: str = "NOT_ASSESSED"
    lowestMonthlyWageInr: int | None = None
    statutoryMinimumWageInr: int | None = None
    wagesPaidOnTime: str = "NOT_ASSESSED"
    standardWeeklyHours: int | None = None
    maxWeeklyOvertimeHours: int | None = None
    overtimeVoluntary: str = "NOT_ASSESSED"
    weeklyRestDayProvided: str = "NOT_ASSESSED"
    unionOrWorkerCommitteePresent: str = "NOT_ASSESSED"
    collectiveBargainingAgreement: bool = False
    noDepositOrDocumentRetention: str = "NOT_ASSESSED"
    grievanceMechanismPresent: str = "NOT_ASSESSED"
    antiDiscriminationPolicy: str = "NOT_ASSESSED"
    sa8000AwarenessTrainingPct: float | None = None
    lastSocialAuditDate: datetime | None = None
    overallSocialComplianceFlag: str = "NOT_ASSESSED"
    # derived element-level signals for the mini-indicators + exception lens
    wageFlag: bool = False  # wage element ATTENTION/NON_COMPLIANT
    overtimeFlag: bool = False  # max OT > 12 (SA8000 cap)
    foaFlag: bool = False  # freedom-of-association element flagged
    effectiveFlag: str = "NOT_ASSESSED"  # worst-of(overall, child-labour) — the chip


class SocialComplianceRollup(BaseModel):
    factoryCount: int = 0
    totalWorkforce: int = 0
    permanentCount: int = 0
    contractCount: int = 0
    apprenticeTraineeCount: int = 0
    maleCount: int = 0
    femaleCount: int = 0
    otherGenderCount: int = 0
    migrantWorkerCount: int = 0
    differentlyAbledCount: int = 0
    contractPct: float = 0
    femalePct: float = 0
    migrantPct: float = 0
    # effective-flag → factory count (COMPLIANT / ATTENTION / NON_COMPLIANT / NOT_ASSESSED)
    flagCounts: dict[str, int] = {}
    childLabourFlagCount: int = 0
    overtimeFlagCount: int = 0
    wageFlagCount: int = 0
    foaFlagCount: int = 0


class SocialComplianceRegisterResponse(BaseModel):
    items: list[SocialComplianceRegisterRow] = []
    rollup: SocialComplianceRollup = SocialComplianceRollup()


class BuildingRegisterRow(BaseModel):
    factoryCode: str
    factoryName: str
    state: str
    buildingName: str
    buildingType: str
    floors: int
    areaSqm: float | None = None
    maxOccupancy: int | None = None
    currentOccupancy: int | None = None
    assemblyPoint: str | None = None
    emergencyExits: int | None = None
    yearBuilt: int | None = None
    occupancyCertificateNo: str | None = None


class BuildingRegisterResponse(BaseModel):
    items: list[BuildingRegisterRow] = []
    buildingCount: int = 0
    totalAreaSqm: float = 0


class CertificationRegisterRow(BaseModel):
    certId: str
    factoryProfileId: str
    factoryCode: str
    factoryName: str
    state: str
    certificationType: str
    certificateNo: str | None = None
    issuingBody: str | None = None
    issueDate: datetime | None = None
    expiryDate: datetime | None = None
    status: str
    daysToExpiry: int | None = None
    scopeNotes: str | None = None


class CertificationRegisterResponse(BaseModel):
    items: list[CertificationRegisterRow] = []
    certCount: int = 0
    expiringWithin90Days: int = 0
    expiredCount: int = 0
