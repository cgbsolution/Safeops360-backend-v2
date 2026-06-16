"""Pydantic schemas for the HIRA module.

Read schemas are complete for Phase 2 list / detail. Create/Update schemas
are present for the foundation but the full editor (Phase 3) will likely
extend them with additional optional fields as the UI grows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ─────────────────────────────────────────────────────────────────────
# Risk matrix
# ─────────────────────────────────────────────────────────────────────


class RiskMatrixLikelihoodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    matrixId: str
    score: int
    label: str
    description: str
    frequencyGuidance: str | None = None
    sortOrder: int


class RiskMatrixSeverityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    matrixId: str
    score: int
    label: str
    description: str
    healthSafetyGuidance: str | None = None
    propertyDamageGuidance: str | None = None
    environmentalGuidance: str | None = None
    reputationGuidance: str | None = None
    sortOrder: int


class RiskMatrixCellOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    likelihoodScore: int
    severityScore: int
    riskScore: int
    riskLevel: str
    colorHex: str
    actionRequired: str
    responseTimeDays: int


class RiskMatrixOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    description: str | None = None
    likelihoodLevels: int
    severityLevels: int
    acceptableResidual: dict[str, Any]
    controlHierarchyEnforced: bool
    isActive: bool
    isDefault: bool
    isGlobal: bool
    likelihoods: list[RiskMatrixLikelihoodOut] = []
    severities: list[RiskMatrixSeverityOut] = []
    cells: list[RiskMatrixCellOut] = []


# ─────────────────────────────────────────────────────────────────────
# Hazard / control library
# ─────────────────────────────────────────────────────────────────────


class HiraHazardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    category: str
    subcategory: str | None
    name: str
    description: str
    typicalHarmPotential: list[Any]
    typicalAffectedPersons: list[Any]
    energyForm: str | None
    oshaStandard: str | None
    factoriesActSection: str | None
    isStandard: str | None
    isoReference: str | None
    typicalControlsSuggested: list[Any] | None
    isActive: bool
    isGlobal: bool


class HiraControlOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    hierarchy: str
    description: str
    verificationMethod: str | None
    verificationFrequency: str | None
    isActive: bool
    isGlobal: bool


# ─────────────────────────────────────────────────────────────────────
# Study + study children — Create / Update / Out
# ─────────────────────────────────────────────────────────────────────


class HiraStudyTeamMemberCreate(BaseModel):
    userId: str
    teamRole: str = Field(description="FACILITATOR | SUBJECT_MATTER_EXPERT | OPERATOR_REP | SAFETY_OFFICER | DEPARTMENT_HEAD | EXTERNAL_CONSULTANT")
    department: str | None = None


class HiraStudyCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    plantId: str
    departmentId: str | None = None
    areaId: str | None = None
    scopeType: str = Field(description="PLANT | AREA | DEPARTMENT | ACTIVITY | EQUIPMENT | PROCESS")
    activityIds: list[str] | None = None
    equipmentIds: list[str] | None = None
    processCode: str | None = None

    title: str = Field(min_length=4)
    description: str | None = None

    riskMatrixId: str
    teamLeaderId: str
    team: list[HiraStudyTeamMemberCreate] = []

    targetCompletionDate: datetime | None = None
    reviewFrequency: str = "ANNUAL"
    customReviewMonths: int | None = None

    applicableRegulations: list[str] | None = None
    regulatoryReviewRequired: bool = False


class HiraStudyUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str | None = None
    description: str | None = None
    targetCompletionDate: datetime | None = None
    reviewFrequency: str | None = None
    customReviewMonths: int | None = None
    applicableRegulations: list[str] | None = None
    regulatoryReviewRequired: bool | None = None
    teamLeaderId: str | None = None
    supersedesStudyId: str | None = None
    supersessionReason: str | None = None


class HiraStudyTeamMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    userId: str
    teamRole: str
    department: str | None
    signedAt: datetime | None
    signedNote: str | None = None


class HiraStudyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    number: str
    plantId: str
    departmentId: str | None
    areaId: str | None
    scopeType: str
    activityIds: list[Any] | None
    equipmentIds: list[Any] | None
    processCode: str | None
    title: str
    description: str | None
    riskMatrixId: str
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
    supersedesStudyId: str | None
    supersessionReason: str | None
    applicableRegulations: list[Any] | None
    regulatoryReviewRequired: bool
    aggregateMetrics: dict[str, Any] | None
    createdAt: datetime
    createdById: str
    updatedAt: datetime
    updatedById: str | None
    team: list[HiraStudyTeamMemberOut] = []


class HiraStudyListItem(BaseModel):
    """Compact row for the list view — includes denormalised display fields
    (plant/department/area/teamLeader names + entry count) so the frontend
    doesn't need a second round-trip per row. Returned as-is for table render."""

    model_config = ConfigDict(from_attributes=True)
    id: str
    number: str
    plantId: str
    departmentId: str | None
    areaId: str | None = None
    title: str
    scopeType: str | None = None
    status: str
    initiatedAt: datetime
    effectiveFrom: datetime | None = None
    nextScheduledReviewDate: datetime | None
    aggregateMetrics: dict[str, Any] | None
    teamLeaderId: str
    # Denormalised display fields
    plantName: str | None = None
    departmentName: str | None = None
    areaName: str | None = None
    teamLeaderName: str | None = None
    entryCount: int = 0


class HiraStudyListResponse(BaseModel):
    items: list[HiraStudyListItem]
    total: int
    # Aggregate counters the list page renders without a second round-trip.
    statusCounts: dict[str, int] = {}


# ─────────────────────────────────────────────────────────────────────
# Entry — children: hazards, controls, recommended controls, refs
# ─────────────────────────────────────────────────────────────────────


class HiraEntryHazardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    hazardId: str
    contextualDescription: str | None
    potentialHarm: list[Any] | None
    affectedPersons: list[Any] | None
    consequence: str | None = None
    sortOrder: int
    # Denormalised hazard library fields so the editor renders without a
    # second lookup.
    hazardCode: str | None = None
    hazardCategory: str | None = None
    hazardName: str | None = None


class HiraEntryControlOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    controlId: str | None
    hierarchy: str
    description: str
    effectiveness: str | None
    verificationMethod: str | None
    verificationFreq: str | None
    responsibleRole: str | None
    evidenceAttached: bool
    documentReference: str | None = None
    sortOrder: int


class HiraEntryRecommendedControlOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    hierarchy: str
    description: str
    rationale: str | None
    targetLikelihoodReduction: int | None
    targetSeverityReduction: int | None
    estimatedCostBand: str | None
    proposedImplementationDate: datetime | None
    responsibleId: str | None
    status: str
    capaId: str | None


class HiraEntryRegulationRefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    regulation: str
    section: str | None
    requirementSummary: str | None


class HiraEntryHazardCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    hazardId: str
    contextualDescription: str | None = None
    consequence: str | None = None


class HiraEntryCreate(BaseModel):
    """Minimal create — Phase 2 vertical slice.

    Phase 3 expands with all 9 editor sections. Anything not listed here
    is set to default (status=DRAFT, isCurrentVersion=true, versionNumber=1)
    in the service.
    """

    model_config = ConfigDict(extra="ignore")

    studyId: str
    sequenceNumber: int | None = None  # auto-assigned by router if omitted
    groupLabel: str | None = None

    activityDescription: str = Field(min_length=4)
    areaId: str | None = None
    subLocation: str | None = None
    routine: str
    frequency: str
    typicalDurationMin: int | None = None

    personsEmployees: int = 0
    personsContractors: int = 0
    personsVisitors: int = 0
    personsPublic: int = 0
    affectedPersonGroups: str | None = None

    equipmentUsed: list[str] | None = None
    materialsUsed: list[str] | None = None
    energySourcesPresent: list[str] | None = None

    initialLikelihoodId: str
    initialSeverityId: str
    initialLikelihoodRationale: str | None = None
    initialSeverityRationale: str | None = None

    hazards: list[HiraEntryHazardCreate] = []


class HiraEntryUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    activityDescription: str | None = None
    routine: str | None = None
    frequency: str | None = None
    typicalDurationMin: int | None = None
    affectedPersonGroups: str | None = None

    initialLikelihoodId: str | None = None
    initialSeverityId: str | None = None
    initialLikelihoodRationale: str | None = None
    initialSeverityRationale: str | None = None

    residualLikelihoodId: str | None = None
    residualSeverityId: str | None = None
    residualLikelihoodRationale: str | None = None
    residualSeverityRationale: str | None = None
    residualAcceptanceRationale: str | None = None

    personsEmployees: int | None = None
    personsContractors: int | None = None
    personsVisitors: int | None = None
    personsPublic: int | None = None
    gpsLatitude: float | None = None
    gpsLongitude: float | None = None
    subLocation: str | None = None
    areaId: str | None = None
    equipmentUsed: list[str] | None = None
    materialsUsed: list[str] | None = None
    energySourcesPresent: list[str] | None = None
    triggersTrainingProgramIds: list[str] | None = None
    triggersInspectionTypeIds: list[str] | None = None
    influencesPtwRiskLevel: bool | None = None
    influencesPtwPermitTypes: list[str] | None = None
    linkedEmergencyProcIds: list[str] | None = None
    linkedEnvironmentalAspects: list[str] | None = None


class HiraCapaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    entryId: str
    number: str
    description: str
    controlHierarchy: str | None = None
    ownerId: str
    targetDate: datetime | None = None
    status: str
    completedAt: datetime | None = None
    completionNote: str | None = None
    verifierId: str | None = None
    verifiedAt: datetime | None = None
    verifyMethod: str | None = None
    effectiveness: str | None = None
    createdAt: datetime
    updatedAt: datetime


class HiraEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    studyId: str
    sequenceNumber: int
    groupLabel: str | None
    activityDescription: str
    areaId: str | None
    subLocation: str | None
    routine: str
    frequency: str
    typicalDurationMin: int | None
    personsEmployees: int
    personsContractors: int
    personsVisitors: int
    personsPublic: int
    affectedPersonGroups: str | None = None
    equipmentUsed: list[Any] | None
    materialsUsed: list[Any] | None
    energySourcesPresent: list[Any] | None
    initialLikelihoodId: str
    initialLikelihoodScore: int
    initialLikelihoodRationale: str | None
    initialSeverityId: str
    initialSeverityScore: int
    initialSeverityRationale: str | None
    initialRiskScore: int
    initialRiskLevel: str
    initialRiskColor: str | None
    residualLikelihoodId: str | None
    residualLikelihoodScore: int | None
    residualLikelihoodRationale: str | None
    residualSeverityId: str | None
    residualSeverityScore: int | None
    residualSeverityRationale: str | None
    residualRiskScore: int | None
    residualRiskLevel: str | None
    residualRiskColor: str | None
    residualAcceptable: bool | None
    residualAcceptanceRationale: str | None
    triggersTrainingProgramIds: list[Any] | None
    triggersInspectionTypeIds: list[Any] | None
    influencesPtwRiskLevel: bool
    influencesPtwPermitTypes: list[Any] | None
    linkedEmergencyProcIds: list[Any] | None
    linkedEnvironmentalAspects: list[Any] | None
    lastReviewedAt: datetime | None
    nextReviewDue: datetime | None
    reviewCount: int
    lastReviewType: str | None
    lastReviewedById: str | None = None
    parentVersionId: str | None = None
    triggeredByRecordId: str | None
    status: str
    versionNumber: int
    isCurrentVersion: bool
    createdAt: datetime
    createdById: str
    updatedAt: datetime
    updatedById: str | None
    hazards: list[HiraEntryHazardOut] = []
    existingControls: list[HiraEntryControlOut] = []
    recommendedControls: list[HiraEntryRecommendedControlOut] = []
    regulationRefs: list[HiraEntryRegulationRefOut] = []
    capas: list[HiraCapaOut] = []


class HiraEntryListItem(BaseModel):
    """Compact row for entry list inside study detail view."""

    model_config = ConfigDict(from_attributes=True)
    id: str
    sequenceNumber: int
    groupLabel: str | None
    activityDescription: str
    initialRiskLevel: str
    initialRiskScore: int = 0
    initialRiskColor: str | None
    residualRiskLevel: str | None
    residualRiskScore: int | None = None
    residualRiskColor: str | None
    residualAcceptable: bool | None
    status: str
    lastReviewedAt: datetime | None
    nextReviewDue: datetime | None
    hazardCount: int = 0
    existingControlCount: int = 0
    recommendedControlCount: int = 0


class HiraEntryListResponse(BaseModel):
    items: list[HiraEntryListItem]
    total: int


class HiraStudyDetailResponse(BaseModel):
    """Full payload the study detail page renders. Includes the study,
    its team with user names, the risk matrix metadata, denormalised
    display names, AND the entry list — so the page renders from one
    backend call.
    """

    model_config = ConfigDict(from_attributes=True)
    study: HiraStudyOut
    entries: list[HiraEntryListItem]
    plantName: str | None = None
    departmentName: str | None = None
    areaName: str | None = None
    teamLeaderName: str | None = None
    approvedByName: str | None = None
    createdByName: str | None = None
    teamMemberNames: dict[str, str] = {}
    riskMatrix: dict[str, Any] | None = None


# ─────────────────────────────────────────────────────────────────────
# Child-list replace payloads — for PUT endpoints
# ─────────────────────────────────────────────────────────────────────


class HiraEntryControlReplaceItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    controlId: str | None = None
    hierarchy: str
    description: str
    effectiveness: str | None = None
    verificationMethod: str | None = None
    verificationFreq: str | None = None
    responsibleRole: str | None = None
    evidenceAttached: bool = False
    documentReference: str | None = None
    sortOrder: int = 0


class HiraEntryControlReplaceRequest(BaseModel):
    controls: list[HiraEntryControlReplaceItem]


class HiraEntryRecommendedControlReplaceItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    hierarchy: str
    description: str
    rationale: str | None = None
    targetLikelihoodReduction: int | None = None
    targetSeverityReduction: int | None = None
    estimatedCostBand: str | None = None
    proposedImplementationDate: datetime | None = None
    responsibleId: str | None = None
    status: str = "PROPOSED"
    capaId: str | None = None


class HiraEntryRecommendedControlReplaceRequest(BaseModel):
    controls: list[HiraEntryRecommendedControlReplaceItem]


class HiraEntryRegulationRefReplaceItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    regulation: str
    section: str | None = None
    requirementSummary: str | None = None


class HiraEntryRegulationRefReplaceRequest(BaseModel):
    refs: list[HiraEntryRegulationRefReplaceItem]


# ─────────────────────────────────────────────────────────────────────
# Review cycle
# ─────────────────────────────────────────────────────────────────────


class HiraReviewCycleOut(BaseModel):
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
    changesMade: list[Any] | None = None
    createdAt: datetime


class HiraReviewCycleListItem(BaseModel):
    """Enriched row for the review-cycles list page. Includes denormalised
    entry and study fields so the list renders without extra round-trips."""

    model_config = ConfigDict(from_attributes=True)
    id: str
    entryId: str
    scheduledFor: datetime
    triggeredBy: str
    triggerReferenceId: str | None
    status: str
    assignedToId: str
    outcome: str | None
    createdAt: datetime
    # Denormalised from HiraEntry
    entryTitle: str | None = None
    entrySequenceNumber: int | None = None
    # Denormalised from HiraStudy
    studyNumber: str | None = None
    studyTitle: str | None = None


class HiraReviewCycleBulkNoChangeRequest(BaseModel):
    cycleIds: list[str] = Field(min_length=1)
    notes: str | None = None


class HiraReviewCycleSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    outcome: str = Field(description="NO_CHANGE_REQUIRED | MINOR_REVISION | MAJOR_REVISION | NEW_ENTRY_CREATED | ENTRY_ARCHIVED")
    outcomeNotes: str = Field(min_length=1)


# ─────────────────────────────────────────────────────────────────────
# Version / history
# ─────────────────────────────────────────────────────────────────────


class HiraVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    entryId: str
    versionNumber: int
    snapshot: dict[str, Any]
    # JSON column — seeded records may store list or dict; accept either
    changes: Any
    changeReason: str
    changeTrigger: str
    createdAt: datetime
    createdById: str


# ─────────────────────────────────────────────────────────────────────
# Integration responses
# ─────────────────────────────────────────────────────────────────────


class HiraIntegrationEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    sequenceNumber: int
    activityDescription: str
    initialRiskLevel: str
    initialRiskScore: int
    residualRiskLevel: str | None
    residualRiskScore: int | None
    residualAcceptable: bool | None = None
    studyId: str
    studyNumber: str
    studyTitle: str
    hazards: list[dict[str, Any]] = []
    existingControls: list[dict[str, Any]] | None = None
    influencesPtwRiskLevel: bool | None = None
    influencesPtwPermitTypes: list[Any] | None = None


class HiraIntegrationForFlraResponse(BaseModel):
    entries: list[HiraIntegrationEntry]
    count: int


class HiraIntegrationForPtwResponse(BaseModel):
    entries: list[HiraIntegrationEntry]
    count: int
    gatingBlockers: int
    highCount: int
    advisory: str | None


class HiraInspectionPriorityResult(BaseModel):
    multiplier: float
    rationale: str
    sourceEntries: list[dict[str, Any]]


# ─────────────────────────────────────────────────────────────────────
# Dashboard aggregates
# ─────────────────────────────────────────────────────────────────────


class HiraDashboardCoverage(BaseModel):
    totalDepartments: int
    coveredDepartments: int
    coveragePct: int


class HiraDashboardReviewCompliance(BaseModel):
    overdue: int
    dueSoon30Days: int
    completedLast90Days: int


class HiraDashboardHighRisk(BaseModel):
    high: int
    critical: int
    total: int


class HiraDashboardRiskReduction(BaseModel):
    initialTotal: int
    residualTotal: int
    reductionPct: int


class HiraDashboardTopHazard(BaseModel):
    category: str
    count: int


class HiraEntryHazardReplaceItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    hazardId: str
    contextualDescription: str | None = None
    potentialHarm: list[Any] | None = None
    affectedPersons: list[Any] | None = None
    consequence: str | None = None
    sortOrder: int = 0


class HiraCapaCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    description: str
    controlHierarchy: str | None = None
    ownerId: str
    targetDate: datetime | None = None


class HiraCapaUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str | None = None
    completionNote: str | None = None
    completedAt: datetime | None = None
    verifierId: str | None = None
    verifiedAt: datetime | None = None
    verifyMethod: str | None = None
    effectiveness: str | None = None


class HiraStudyTeamMemberSignRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    signedNote: str | None = None


class HiraStudyTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str | None = None


class HiraEntryTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str | None = None


__all__ = [
    "RiskMatrixOut",
    "RiskMatrixLikelihoodOut",
    "RiskMatrixSeverityOut",
    "RiskMatrixCellOut",
    "HiraHazardOut",
    "HiraControlOut",
    "HiraStudyCreate",
    "HiraStudyUpdate",
    "HiraStudyOut",
    "HiraStudyTeamMemberCreate",
    "HiraStudyTeamMemberOut",
    "HiraStudyListItem",
    "HiraStudyListResponse",
    "HiraEntryHazardCreate",
    "HiraEntryCreate",
    "HiraEntryUpdate",
    "HiraEntryOut",
    "HiraEntryHazardOut",
    "HiraEntryControlOut",
    "HiraEntryRecommendedControlOut",
    "HiraEntryRegulationRefOut",
    "HiraEntryListItem",
    "HiraEntryListResponse",
    "HiraEntryControlReplaceItem",
    "HiraEntryControlReplaceRequest",
    "HiraEntryRecommendedControlReplaceItem",
    "HiraEntryRecommendedControlReplaceRequest",
    "HiraEntryRegulationRefReplaceItem",
    "HiraEntryRegulationRefReplaceRequest",
    "HiraReviewCycleListItem",
    "HiraReviewCycleBulkNoChangeRequest",
    "HiraReviewCycleOut",
    "HiraReviewCycleSubmitRequest",
    "HiraVersionOut",
    "HiraIntegrationEntry",
    "HiraIntegrationForFlraResponse",
    "HiraIntegrationForPtwResponse",
    "HiraInspectionPriorityResult",
    "HiraDashboardCoverage",
    "HiraDashboardReviewCompliance",
    "HiraDashboardHighRisk",
    "HiraDashboardRiskReduction",
    "HiraDashboardTopHazard",
    "HiraStudyDetailResponse",
    "HiraEntryHazardReplaceItem",
    "HiraCapaCreate",
    "HiraCapaUpdate",
    "HiraCapaOut",
    "HiraStudyTeamMemberSignRequest",
    "HiraStudyTransitionRequest",
    "HiraEntryTransitionRequest",
]
