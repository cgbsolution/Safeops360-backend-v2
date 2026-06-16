"""EAI — Pydantic schemas for the Environmental Aspect & Impact register.

Mirrors the layout of app/schemas/hira.py so the EAI router can reuse
the HIRA wiring pattern. Domain naming follows ISO 14001:2015 §6.1.2
(aspects → impacts, likelihood × magnitude, significance determination).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ─────────────────────────────────────────────────────────────────────
# Masters
# ─────────────────────────────────────────────────────────────────────


class EaiAspectCategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    description: str | None
    sortOrder: int
    iconKey: str | None
    isActive: bool


class EaiAspectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    categoryId: str
    name: str
    description: str
    typicalReceptors: list[str]
    typicalImpacts: list[dict[str, Any]] | None = None
    typicalRegulations: list[str] | None = None
    typicalControls: list[dict[str, Any]] | None = None
    typicallySignificant: bool
    isActive: bool
    isGlobal: bool


class EaiReceptorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    description: str | None
    sortOrder: int
    isActive: bool


class EaiRegulationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    jurisdiction: str
    section: str | None
    description: str | None
    authority: str | None
    sortOrder: int
    isActive: bool


class EnvironmentalImpactMatrixLikelihoodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    score: int
    label: str
    description: str
    occurrenceGuidance: str | None
    sortOrder: int


class EnvironmentalImpactMatrixMagnitudeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    score: int
    label: str
    description: str
    geographicGuidance: str | None
    reversibilityGuidance: str | None
    durationGuidance: str | None
    legalGuidance: str | None
    sortOrder: int


class EnvironmentalImpactMatrixCellOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    likelihoodScore: int
    magnitudeScore: int
    impactScore: int
    impactLevel: str
    colorHex: str
    actionRequired: str
    responseTimeDays: int


class EnvironmentalImpactMatrixOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    description: str | None
    likelihoodLevels: int
    magnitudeLevels: int
    significanceThresholds: dict[str, Any]
    acceptableResidual: dict[str, Any]
    controlHierarchyEnforced: bool
    isActive: bool
    isDefault: bool
    isGlobal: bool
    likelihoods: list[EnvironmentalImpactMatrixLikelihoodOut] = []
    magnitudes: list[EnvironmentalImpactMatrixMagnitudeOut] = []
    cells: list[EnvironmentalImpactMatrixCellOut] = []


# ─────────────────────────────────────────────────────────────────────
# Study
# ─────────────────────────────────────────────────────────────────────


class EaiStudyTeamMemberIn(BaseModel):
    userId: str
    teamRole: str
    department: str | None = None


class EaiStudyTeamMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    userId: str
    teamRole: str
    department: str | None
    signedAt: datetime | None
    signedNote: str | None


class EaiStudyCreate(BaseModel):
    plantId: str
    departmentId: str | None = None
    areaId: str | None = None
    scopeType: str
    activityIds: list[str] | None = None
    processCode: str | None = None
    title: str
    description: str | None = None
    impactMatrixId: str
    teamLeaderId: str
    team: list[EaiStudyTeamMemberIn] = []
    targetCompletionDate: datetime | None = None
    reviewFrequency: str = "ANNUAL"
    customReviewMonths: int | None = None
    applicableRegulations: list[str] | None = None
    regulatoryReviewRequired: bool = False


class EaiStudyUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    targetCompletionDate: datetime | None = None
    reviewFrequency: str | None = None
    customReviewMonths: int | None = None
    applicableRegulations: list[str] | None = None
    regulatoryReviewRequired: bool | None = None


class EaiStudyTransitionRequest(BaseModel):
    notes: str | None = None


class EaiStudyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    number: str
    plantId: str
    departmentId: str | None
    areaId: str | None
    scopeType: str
    activityIds: list[str] | None
    processCode: str | None
    title: str
    description: str | None
    impactMatrixId: str
    teamLeaderId: str
    status: str
    initiatedAt: datetime
    targetCompletionDate: datetime | None
    completedAt: datetime | None
    approvedAt: datetime | None
    approvedById: str | None
    effectiveFrom: datetime | None
    nextScheduledReviewDate: datetime | None
    reviewFrequency: str
    customReviewMonths: int | None
    applicableRegulations: list[str] | None
    regulatoryReviewRequired: bool
    aggregateMetrics: dict[str, Any] | None
    createdAt: datetime
    createdById: str
    updatedAt: datetime
    team: list[EaiStudyTeamMemberOut] = []


class EaiStudyListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    number: str
    title: str
    plantId: str
    departmentId: str | None
    areaId: str | None
    scopeType: str
    status: str
    initiatedAt: datetime
    nextScheduledReviewDate: datetime | None
    entryCount: int = 0
    significantCount: int = 0


class EaiStudyListResponse(BaseModel):
    items: list[EaiStudyListItem]
    total: int


# ─────────────────────────────────────────────────────────────────────
# Entry
# ─────────────────────────────────────────────────────────────────────


class EaiEntryAspectIn(BaseModel):
    aspectId: str
    contextualDescription: str | None = None
    quantification: dict[str, Any] | None = None
    occurrence: str | None = None
    sortOrder: int = 0


class EaiEntryAspectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    aspectId: str
    contextualDescription: str | None
    quantification: dict[str, Any] | None
    occurrence: str | None
    sortOrder: int


class EaiEntryImpactIn(BaseModel):
    description: str
    affectedReceptor: str
    impactType: str
    reversibility: str
    geographicExtent: str
    temporalExtent: str
    sortOrder: int = 0


class EaiEntryImpactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    description: str
    affectedReceptor: str
    impactType: str
    reversibility: str
    geographicExtent: str
    temporalExtent: str
    sortOrder: int


class EaiEntryControlIn(BaseModel):
    hierarchy: str
    description: str
    effectiveness: str | None = None
    verificationMethod: str | None = None
    verificationFreq: str | None = None
    responsibleRole: str | None = None
    evidenceAttached: bool = False
    monitoringPoint: str | None = None
    monitoringParameter: str | None = None
    monitoringFrequency: str | None = None
    sortOrder: int = 0


class EaiEntryControlOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    hierarchy: str
    description: str
    effectiveness: str | None
    verificationMethod: str | None
    verificationFreq: str | None
    responsibleRole: str | None
    evidenceAttached: bool
    monitoringPoint: str | None
    monitoringParameter: str | None
    monitoringFrequency: str | None
    sortOrder: int


class EaiEntryRecommendedControlIn(BaseModel):
    hierarchy: str
    description: str
    rationale: str | None = None
    targetLikelihoodReduction: int | None = None
    targetMagnitudeReduction: int | None = None
    estimatedCostBand: str | None = None
    proposedImplementationDate: datetime | None = None
    responsibleUserId: str | None = None


class EaiEntryRecommendedControlOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    hierarchy: str
    description: str
    rationale: str | None
    targetLikelihoodReduction: int | None
    targetMagnitudeReduction: int | None
    estimatedCostBand: str | None
    proposedImplementationDate: datetime | None
    responsibleUserId: str | None
    status: str
    capaId: str | None


class EaiComplianceObligationIn(BaseModel):
    regulationCode: str
    section: str | None = None
    parameter: str
    permittedLimit: str
    monitoringFrequency: str
    reportingAuthority: str | None = None
    reportingFrequency: str | None = None
    nextMonitoringDue: datetime | None = None


class EaiComplianceObligationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    regulationCode: str
    section: str | None
    parameter: str
    permittedLimit: str
    monitoringFrequency: str
    reportingAuthority: str | None
    reportingFrequency: str | None
    nextMonitoringDue: datetime | None
    lastMonitoringDate: datetime | None
    lastMonitoringResult: str | None
    status: str


class EaiEntryRegulationRefIn(BaseModel):
    regulationCode: str
    section: str | None = None
    requirementSummary: str | None = None


class EaiEntryRegulationRefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    regulationCode: str
    section: str | None
    requirementSummary: str | None


class EaiEntryCreate(BaseModel):
    activityDescription: str
    areaId: str | None = None
    subLocation: str | None = None
    occurrence: str
    frequency: str
    typicalDurationMin: int | None = None

    equipmentUsed: list[str] | None = None
    materialsUsed: list[str] | None = None
    processInputs: list[str] | None = None

    initialLikelihoodId: str
    initialMagnitudeId: str
    initialLikelihoodRationale: str | None = None
    initialMagnitudeRationale: str | None = None

    aspects: list[EaiEntryAspectIn] = Field(default_factory=list)
    impacts: list[EaiEntryImpactIn] = Field(default_factory=list)
    existingControls: list[EaiEntryControlIn] = Field(default_factory=list)
    recommendedControls: list[EaiEntryRecommendedControlIn] = Field(default_factory=list)
    complianceObligations: list[EaiComplianceObligationIn] = Field(default_factory=list)
    regulationRefs: list[EaiEntryRegulationRefIn] = Field(default_factory=list)


class EaiEntryUpdate(BaseModel):
    activityDescription: str | None = None
    areaId: str | None = None
    subLocation: str | None = None
    occurrence: str | None = None
    frequency: str | None = None
    typicalDurationMin: int | None = None
    equipmentUsed: list[str] | None = None
    materialsUsed: list[str] | None = None
    processInputs: list[str] | None = None

    initialLikelihoodId: str | None = None
    initialMagnitudeId: str | None = None
    initialLikelihoodRationale: str | None = None
    initialMagnitudeRationale: str | None = None

    residualLikelihoodId: str | None = None
    residualMagnitudeId: str | None = None
    residualLikelihoodRationale: str | None = None
    residualMagnitudeRationale: str | None = None
    residualAcceptanceRationale: str | None = None

    legalComplianceStatus: str | None = None

    linkedHiraEntryIds: list[str] | None = None

    changeReason: str | None = None
    changeTrigger: str | None = None

    status: str | None = None


class EaiEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    studyId: str
    sequenceNumber: int
    groupLabel: str | None
    activityDescription: str
    areaId: str | None
    subLocation: str | None
    occurrence: str
    frequency: str
    typicalDurationMin: int | None
    equipmentUsed: list[str] | None
    materialsUsed: list[str] | None
    processInputs: list[str] | None

    initialLikelihoodId: str
    initialLikelihoodScore: int
    initialLikelihoodRationale: str | None
    initialMagnitudeId: str
    initialMagnitudeScore: int
    initialMagnitudeRationale: str | None
    initialImpactScore: int
    initialImpactLevel: str
    initialImpactColor: str | None
    initialSignificant: bool

    residualLikelihoodId: str | None
    residualLikelihoodScore: int | None
    residualLikelihoodRationale: str | None
    residualMagnitudeId: str | None
    residualMagnitudeScore: int | None
    residualMagnitudeRationale: str | None
    residualImpactScore: int | None
    residualImpactLevel: str | None
    residualImpactColor: str | None
    residualAcceptable: bool | None
    residualAcceptanceRationale: str | None
    residualSignificant: bool

    legalComplianceStatus: str | None
    linkedHiraEntryIds: list[str] | None

    lastReviewedAt: datetime | None
    nextReviewDue: datetime | None
    reviewCount: int
    lastReviewType: str | None
    status: str
    versionNumber: int
    isCurrentVersion: bool

    aspects: list[EaiEntryAspectOut] = []
    impacts: list[EaiEntryImpactOut] = []
    existingControls: list[EaiEntryControlOut] = []
    recommendedControls: list[EaiEntryRecommendedControlOut] = []
    complianceObligations: list[EaiComplianceObligationOut] = []
    regulationRefs: list[EaiEntryRegulationRefOut] = []

    createdAt: datetime
    createdById: str
    updatedAt: datetime


class EaiEntryListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    studyId: str
    sequenceNumber: int
    activityDescription: str
    areaId: str | None
    occurrence: str
    initialImpactLevel: str
    initialImpactScore: int
    residualImpactLevel: str | None
    residualImpactScore: int | None
    residualSignificant: bool
    legalComplianceStatus: str | None
    status: str
    nextReviewDue: datetime | None
    updatedAt: datetime


class EaiEntryListResponse(BaseModel):
    items: list[EaiEntryListItem]
    total: int


# ─────────────────────────────────────────────────────────────────────
# Review
# ─────────────────────────────────────────────────────────────────────


class EaiReviewCycleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    entryId: str
    scheduledFor: datetime
    triggeredBy: str
    triggerReferenceId: str | None
    status: str
    assignedToId: str
    assignedRole: str | None
    startedAt: datetime | None
    completedAt: datetime | None
    completedById: str | None
    outcome: str | None
    outcomeNotes: str | None
    changesMade: list[dict[str, Any]] | None
    entryTitle: str | None = None
    entrySequenceNumber: int | None = None
    studyNumber: str | None = None
    studyTitle: str | None = None


class EaiReviewCycleSubmitRequest(BaseModel):
    outcome: str
    outcomeNotes: str | None = None
    changesMade: list[dict[str, Any]] | None = None


# ─────────────────────────────────────────────────────────────────────
# Versions
# ─────────────────────────────────────────────────────────────────────


class EaiVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    entryId: str
    versionNumber: int
    snapshot: dict[str, Any]
    changes: list[dict[str, Any]]
    changeReason: str
    changeTrigger: str
    createdAt: datetime
    createdById: str


# ─────────────────────────────────────────────────────────────────────
# Feature flag
# ─────────────────────────────────────────────────────────────────────


class EaiFeatureFlagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    plantId: str
    eaiRegisterEnabled: bool
    combinedRegisterEnabled: bool
    riskDashboardEnabled: bool
    hiraAssistantV2Enabled: bool
    enabledAt: datetime | None


class EaiFeatureFlagUpdate(BaseModel):
    eaiRegisterEnabled: bool | None = None
    combinedRegisterEnabled: bool | None = None
    riskDashboardEnabled: bool | None = None
    hiraAssistantV2Enabled: bool | None = None


# ─────────────────────────────────────────────────────────────────────
# Dashboard widgets — used by Risk Aggregation Dashboard (Phase 3)
# ─────────────────────────────────────────────────────────────────────


class EaiDashboardCoverage(BaseModel):
    departmentsTotal: int
    departmentsWithActiveStudy: int
    coveragePercent: float


class EaiDashboardSignificant(BaseModel):
    total: int
    byLevel: dict[str, int]
    byCategory: dict[str, int]


class EaiBulkNoCycleRequest(BaseModel):
    cycleIds: list[str]


class BulkNoChangeResponse(BaseModel):
    updated: int
