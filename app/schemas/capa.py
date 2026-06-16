"""Pydantic schemas for the unified CAPA module.

Read schemas are complete for v1 list/detail. Full lifecycle write schemas
(create, RCA-submit, action-execute, verify, close, recurrence-check) land
in the Phase 4 backend session.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ─────────────────────────────────────────────────────────────────────
# Master data
# ─────────────────────────────────────────────────────────────────────


class CapaSourceCategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    description: str | None = None
    prefix: str
    sortOrder: int
    isActive: bool


class CapaSourceTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    description: str | None = None
    categoryId: str
    parentModuleLive: bool
    parentModuleName: str | None = None
    sortOrder: int
    isActive: bool


class CapaSubCategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    description: str | None = None


class CapaSlaProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    sourceTypeCode: str | None = None
    severity: str | None = None
    initialResponseHours: int
    rcaDueDays: int
    actionsPlannedDueDays: int
    closureTargetDays: int
    recurrenceCheckDays: int


class CapaVerificationMethodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    description: str | None = None


# ─────────────────────────────────────────────────────────────────────
# Capa
# ─────────────────────────────────────────────────────────────────────


class CapaActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    actionType: str
    description: str
    rationale: str | None = None
    ownerUserId: str
    ownerRole: str | None = None
    dueDate: datetime
    startedAt: datetime | None = None
    completedAt: datetime | None = None
    status: str
    evidenceOfCompletion: str | None = None
    costEstimate: float | None = None
    costEstimateCurrency: str | None = None
    approverUserId: str | None = None
    approvedAt: datetime | None = None
    sortOrder: int


class CapaRootCauseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    description: str
    category: str
    confidence: str
    sortOrder: int


class CapaContributorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    userId: str
    role: str | None = None
    contributionType: str


class CapaAttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    category: str
    fileName: str
    fileUrl: str
    fileSize: int | None = None
    mimeType: str | None = None
    description: str | None = None
    uploadedAt: datetime
    uploadedByUserId: str


class CapaCommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    body: str
    authorUserId: str
    commentType: str
    isInternal: bool
    createdAt: datetime


class CapaListItem(BaseModel):
    """Compact row for the list view — denormalised fields for table render."""

    model_config = ConfigDict(from_attributes=True)
    id: str
    capaNumber: str
    aliasNumber: str | None = None
    title: str
    plantId: str
    sourceCategoryCode: str | None = None
    sourceTypeCode: str
    sourceReferenceSummary: str | None = None
    severity: str
    priority: str
    state: str
    primaryOwnerUserId: str
    primaryOwnerName: str | None = None
    closureTargetDate: datetime | None = None
    detectedAt: datetime
    createdAt: datetime
    daysOpen: int = 0
    daysOverdue: int = 0
    actionCount: int = 0


class CapaListResponse(BaseModel):
    items: list[CapaListItem]
    total: int
    sourceCategoryCounts: dict[str, int] = {}
    stateCounts: dict[str, int] = {}
    severityCounts: dict[str, int] = {}


class CapaOut(BaseModel):
    """Full CAPA detail."""

    model_config = ConfigDict(from_attributes=True)
    id: str
    capaNumber: str
    aliasNumber: str | None = None
    legacySource: str | None = None
    legacyId: str | None = None
    title: str
    plantId: str

    sourceCategoryId: str
    sourceTypeId: str
    sourceTypeCode: str
    sourceReferenceId: str | None = None
    sourceReferenceUrl: str | None = None
    sourceReferenceSummary: str | None = None
    sourceMetadata: dict[str, Any] | None = None

    problemDescription: str
    problemImpact: str | None = None
    detectionMethod: str | None = None
    detectedAt: datetime
    detectedByUserId: str | None = None
    affectedAreas: list[Any] | None = None
    affectedDepartments: list[Any] | None = None
    affectedProducts: list[Any] | None = None
    affectedProcesses: list[Any] | None = None
    affectedCustomers: list[Any] | None = None

    primaryCategory: str
    subCategoryId: str | None = None
    actionType: str
    severity: str
    priority: str
    isRecurring: bool
    relatedCapaIds: list[Any] | None = None

    rcaMethodology: str | None = None
    rcaMethodologyRationale: str | None = None
    rcaCompleted: bool
    rcaSummary: str | None = None
    contributingFactors: list[Any] | None = None
    rcaCompletedAt: datetime | None = None
    rcaCompletedByUserId: str | None = None

    verificationMethodId: str | None = None
    verificationSuccessCriteria: str | None = None
    measurementPeriodDays: int
    verificationDueDate: datetime | None = None
    verificationCompletedAt: datetime | None = None
    verificationCompletedByUserId: str | None = None
    verificationResult: str | None = None
    verificationEvidence: str | None = None

    recurrenceCheckDueDate: datetime | None = None
    recurrenceCheckCompletedAt: datetime | None = None
    recurrenceDetected: bool | None = None

    state: str
    stateChangedAt: datetime
    stateChangedByUserId: str | None = None

    rcaDueDate: datetime | None = None
    correctiveActionDueDate: datetime | None = None
    preventiveActionDueDate: datetime | None = None
    closureTargetDate: datetime | None = None

    raisedByUserId: str
    raisedByRole: str | None = None
    primaryOwnerUserId: str
    primaryOwnerRole: str | None = None
    departmentOwnerId: str | None = None

    estimatedProblemCost: float | None = None
    estimatedActionsCost: float | None = None
    actualCost: float | None = None

    createdAt: datetime
    createdByUserId: str
    updatedAt: datetime
    updatedByUserId: str | None = None
    versionNumber: int
    closedByUserId: str | None = None
    closedAt: datetime | None = None

    actions: list[CapaActionOut] = []
    rootCauses: list[CapaRootCauseOut] = []
    contributors: list[CapaContributorOut] = []
    attachments: list[CapaAttachmentOut] = []
    comments: list[CapaCommentOut] = []


# ─────────────────────────────────────────────────────────────────────
# Create / Update shapes (used by the intake forms)
# ─────────────────────────────────────────────────────────────────────


class CapaUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str | None = None
    problemDescription: str | None = None
    problemImpact: str | None = None
    severity: str | None = None
    priority: str | None = None
    primaryOwnerUserId: str | None = None
    primaryCategory: str | None = None
    subCategoryCode: str | None = None
    closureTargetDate: datetime | None = None
    rcaDueDate: datetime | None = None
    state: str | None = None


class CapaSubmitRcaRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    rcaMethodology: str = Field(description="5_WHY | FISHBONE | FAULT_TREE | BOWTIE | TAP_ROOT | CAUSE_MAP | EIGHT_D | IS_IS_NOT | NONE_REQUIRED")
    rcaMethodologyRationale: str | None = None
    rcaSummary: str
    rootCauses: list[dict[str, Any]] = []  # [{description, category, confidence}]
    contributingFactors: list[str] | None = None


class CapaActionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    actionType: str  # IMMEDIATE_CONTAINMENT | CORRECTIVE | PREVENTIVE
    description: str
    rationale: str | None = None
    ownerUserId: str
    ownerRole: str | None = None
    dueDate: datetime
    costEstimate: float | None = None


class CapaActionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str | None = None
    evidenceOfCompletion: str | None = None
    completedAt: datetime | None = None
    startedAt: datetime | None = None


class CapaVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    verificationMethodCode: str | None = None
    verificationSuccessCriteria: str | None = None
    verificationResult: str = Field(description="EFFECTIVE | PARTIALLY_EFFECTIVE | INEFFECTIVE | INCONCLUSIVE")
    verificationEvidence: str
    measurementPeriodDays: int | None = None


class CapaCloseRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    closureNotes: str | None = None
    finalCost: float | None = None
    finalCostCurrency: str | None = None


class CapaRecurrenceCheckRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    recurrenceDetected: bool
    notes: str | None = None


class CapaPatternActionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # CONFIRM | DISMISS
    action: str
    plantId: str
    primaryCategory: str
    sourceTypeCode: str
    capaIds: list[str]
    rationale: str | None = None


class CapaCreate(BaseModel):
    """Universal CAPA creation. `sourceTypeCode` chooses the intake variant;
    `sourceMetadata` is shaped per spec §3.2."""

    model_config = ConfigDict(extra="ignore")
    plantId: str
    sourceTypeCode: str
    sourceReferenceId: str | None = None
    sourceReferenceUrl: str | None = None
    sourceReferenceSummary: str | None = None
    sourceMetadata: dict[str, Any] | None = None

    title: str = Field(min_length=4)
    problemDescription: str = Field(min_length=50)
    problemImpact: str | None = None
    detectionMethod: str | None = None
    detectedAt: datetime
    affectedAreas: list[str] | None = None
    affectedDepartments: list[str] | None = None

    primaryCategory: str
    subCategoryCode: str | None = None
    actionType: str = "CORRECTIVE_AND_PREVENTIVE"
    severity: str = "MODERATE"
    priority: str = "MODERATE"

    primaryOwnerUserId: str
