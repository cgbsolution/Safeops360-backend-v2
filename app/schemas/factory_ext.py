"""Pydantic schemas for the Facilities extension layer — Equipment, Hazardous
Materials, Regulatory Registrations, and the Lifecycle workflow.

Self-contained (no import from ``schemas/factory.py``) so ``schemas/factory.py``
can import these for the FactoryProfileDetail extension without a cycle.
camelCase throughout; mirrors src/app/(dashboard)/facilities/lib.ts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ── shared literals ──────────────────────────────────────────────────────────
EquipmentStatusLit = Literal["ACTIVE", "IDLE", "DOWN", "RETIRED"]
HazardLevelLit = Literal["LOW", "MEDIUM", "HIGH"]
HazmatClassLit = Literal["LOW", "MEDIUM", "HIGH"]
GhsSignalLit = Literal["DANGER", "WARNING", "NONE"]
RegClassificationLit = Literal["SCHEDULED_SUBSTANCE", "HIGH_HAZARD", "NOTIFIED", "OTHER"]
PcbStatusLit = Literal["REGISTERED", "PENDING", "NOT_REGISTERED"]
RegistrationTypeLit = Literal[
    "FACTORY_ACT", "ESI", "PF", "GST", "FIRE_LICENSE", "PCB", "BOILER", "BUILDING_CERT", "OTHER"
]
RenewalFrequencyLit = Literal["ANNUAL", "BIENNIAL", "TRIENNIAL", "ONEOFF", "ONGOING"]
RegStatusLit = Literal["VALID", "EXPIRING_SOON", "EXPIRED", "PENDING_RENEWAL", "SUSPENDED"]
ImpactLit = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
LifecycleStageLit = Literal["INITIATED", "EXECUTION", "VALIDATION", "ACTIVE", "ARCHIVED"]
InspectionResultLit = Literal["PASS", "FAIL", "CONDITIONAL_PASS"]


# ════════════════════════════════════════════════════════════════════════════
# Equipment
# ════════════════════════════════════════════════════════════════════════════
class EquipmentCreate(BaseModel):
    equipmentName: str = Field(min_length=1)
    assetCode: str | None = None
    category: str | None = None
    buildingId: str | None = None
    manufacturer: str | None = None
    modelNumber: str | None = None
    serialNumber: str | None = None
    installationDate: datetime | None = None
    warrantyExpiryDate: datetime | None = None
    capacity: float | None = Field(None, gt=0)
    capacityUnit: str | None = None
    status: EquipmentStatusLit = "ACTIVE"
    operatingHoursPerDay: float | None = Field(None, ge=0, le=24)
    hazardLevel: HazardLevelLit = "LOW"
    puwerRequired: bool = False
    puwerLastInspection: datetime | None = None
    puwerNextDue: datetime | None = None
    lolerRequired: bool = False
    lolerLastInspection: datetime | None = None
    lolerNextDue: datetime | None = None
    electricalSafetyRequired: bool = False
    electricalLastCheck: datetime | None = None
    electricalNextDue: datetime | None = None
    noiseAssessmentRequired: bool = False
    noiseLastTest: datetime | None = None
    noiseMeasurementDb: float | None = Field(None, ge=0)
    lastMaintenanceDate: datetime | None = None
    lastMaintenanceType: str | None = None
    nextScheduledDate: datetime | None = None
    certifiedOperators: list[dict[str, Any]] = []
    spareParts: list[dict[str, Any]] = []
    notes: str | None = None


class EquipmentUpdate(BaseModel):
    equipmentName: str | None = None
    assetCode: str | None = None
    category: str | None = None
    buildingId: str | None = None
    manufacturer: str | None = None
    modelNumber: str | None = None
    serialNumber: str | None = None
    installationDate: datetime | None = None
    warrantyExpiryDate: datetime | None = None
    capacity: float | None = None
    capacityUnit: str | None = None
    status: EquipmentStatusLit | None = None
    operatingHoursPerDay: float | None = None
    hazardLevel: HazardLevelLit | None = None
    puwerRequired: bool | None = None
    puwerLastInspection: datetime | None = None
    puwerNextDue: datetime | None = None
    lolerRequired: bool | None = None
    lolerLastInspection: datetime | None = None
    lolerNextDue: datetime | None = None
    electricalSafetyRequired: bool | None = None
    electricalLastCheck: datetime | None = None
    electricalNextDue: datetime | None = None
    noiseAssessmentRequired: bool | None = None
    noiseLastTest: datetime | None = None
    noiseMeasurementDb: float | None = None
    nextScheduledDate: datetime | None = None
    certifiedOperators: list[dict[str, Any]] | None = None
    spareParts: list[dict[str, Any]] | None = None
    notes: str | None = None


class MaintenanceRecord(BaseModel):
    """POST .../equipment/{id}/maintenance — records one maintenance event and
    rolls the cached maintenance state forward."""

    date: datetime | None = None  # defaults to now
    maintenanceType: str = Field(min_length=1)
    downtimeHours: float = Field(0, ge=0)
    nextScheduledDate: datetime | None = None
    notes: str | None = None


class EquipmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    factoryProfileId: str
    siteId: str
    buildingId: str | None = None
    equipmentName: str
    assetCode: str | None = None
    category: str | None = None
    manufacturer: str | None = None
    modelNumber: str | None = None
    serialNumber: str | None = None
    installationDate: datetime | None = None
    warrantyExpiryDate: datetime | None = None
    capacity: float | None = None
    capacityUnit: str | None = None
    status: str
    operatingHoursPerDay: float | None = None
    hazardLevel: str
    puwerRequired: bool
    puwerLastInspection: datetime | None = None
    puwerNextDue: datetime | None = None
    lolerRequired: bool
    lolerLastInspection: datetime | None = None
    lolerNextDue: datetime | None = None
    electricalSafetyRequired: bool
    electricalLastCheck: datetime | None = None
    electricalNextDue: datetime | None = None
    noiseAssessmentRequired: bool
    noiseLastTest: datetime | None = None
    noiseMeasurementDb: float | None = None
    lastMaintenanceDate: datetime | None = None
    lastMaintenanceType: str | None = None
    nextScheduledDate: datetime | None = None
    downtimeHoursYtd: float
    lastInspectionDate: datetime | None = None
    lastInspectionResult: str | None = None  # PASS | FAIL | CONDITIONAL_PASS
    certifiedOperators: list[dict[str, Any]] = []
    spareParts: list[dict[str, Any]] = []
    notes: str | None = None
    updatedAt: datetime | None = None
    # ── computed on read ──
    complianceStatus: str = "NA"  # OK | ATTENTION | OVERDUE | NA
    nextComplianceDue: datetime | None = None
    overdueRegimes: list[str] = []  # e.g. ["PUWER", "LOLER"]
    operatorCertGapFlag: bool = False  # HIGH hazard + no valid certified operator


class InspectionCreate(BaseModel):
    """POST .../equipment/{id}/inspections — records one statutory inspection and
    rolls the equipment's cached inspection state + regime next-dues forward."""

    inspectionDate: datetime | None = None  # defaults to now
    inspectorName: str = Field(min_length=1, max_length=100)
    result: InspectionResultLit
    findings: str | None = None


class EquipmentInspectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    factoryProfileId: str
    equipmentId: str
    siteId: str
    inspectionDate: datetime
    inspectorName: str
    result: str
    findings: str | None = None
    createdAt: datetime | None = None


class InspectionResponse(BaseModel):
    """Mirrors the build-spec shape { inspection, updatedEquipment } so the row
    can refresh its Last-maint / Next-due / Compliance cells in place."""

    inspection: EquipmentInspectionOut
    updatedEquipment: EquipmentOut


# ════════════════════════════════════════════════════════════════════════════
# Hazardous Material
# ════════════════════════════════════════════════════════════════════════════
class HazmatCreate(BaseModel):
    chemicalName: str = Field(min_length=1)
    casNumber: str | None = None
    regulatoryClassification: RegClassificationLit | None = None
    hazmatClassification: HazmatClassLit = "LOW"
    ghsSignalWord: GhsSignalLit | None = None
    ghsHazardClasses: list[str] = []
    ghsPictograms: list[str] = []
    quantityStored: float = Field(0, ge=0)
    unit: str | None = None
    maxAllowableQty: float | None = Field(None, gt=0)
    reorderLevel: float | None = Field(None, ge=0)
    storageBuilding: str | None = None
    storageRoom: str | None = None
    containerType: str | None = None
    containerCount: int | None = Field(None, ge=0)
    secondaryContainmentPresent: bool = False
    secondaryContainmentVolume: float | None = Field(None, ge=0)
    ventilationAvailable: bool = False
    signagePresent: bool = False
    issueDate: datetime | None = None
    expiryDate: datetime | None = None
    batchLotNumber: str | None = None
    sdsDocId: str | None = None
    sdsVersion: str | None = None
    sdsGhsCompliant: bool = False
    ppeRequired: list[str] = []
    incompatibleSubstances: list[str] = []
    spillKitLocation: str | None = None
    emergencyContact: str | None = None
    handlersTrainedCount: int = Field(0, ge=0)
    handlersTotalCount: int = Field(0, ge=0)
    pcbNotificationRequired: bool = False
    pcbRegistrationStatus: PcbStatusLit = "NOT_REGISTERED"
    notes: str | None = None


class HazmatUpdate(BaseModel):
    chemicalName: str | None = None
    casNumber: str | None = None
    regulatoryClassification: RegClassificationLit | None = None
    hazmatClassification: HazmatClassLit | None = None
    ghsSignalWord: GhsSignalLit | None = None
    ghsHazardClasses: list[str] | None = None
    ghsPictograms: list[str] | None = None
    quantityStored: float | None = None
    unit: str | None = None
    maxAllowableQty: float | None = None
    reorderLevel: float | None = None
    storageBuilding: str | None = None
    storageRoom: str | None = None
    containerType: str | None = None
    containerCount: int | None = None
    secondaryContainmentPresent: bool | None = None
    secondaryContainmentVolume: float | None = None
    ventilationAvailable: bool | None = None
    signagePresent: bool | None = None
    issueDate: datetime | None = None
    expiryDate: datetime | None = None
    batchLotNumber: str | None = None
    sdsDocId: str | None = None
    sdsVersion: str | None = None
    sdsGhsCompliant: bool | None = None
    ppeRequired: list[str] | None = None
    incompatibleSubstances: list[str] | None = None
    spillKitLocation: str | None = None
    emergencyContact: str | None = None
    handlersTrainedCount: int | None = None
    handlersTotalCount: int | None = None
    pcbNotificationRequired: bool | None = None
    pcbRegistrationStatus: PcbStatusLit | None = None
    notes: str | None = None


class HazmatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    factoryProfileId: str
    siteId: str
    chemicalName: str
    casNumber: str | None = None
    regulatoryClassification: str | None = None
    hazmatClassification: str
    ghsSignalWord: str | None = None
    ghsHazardClasses: list[str] = []
    ghsPictograms: list[str] = []
    quantityStored: float
    unit: str | None = None
    maxAllowableQty: float | None = None
    reorderLevel: float | None = None
    storageBuilding: str | None = None
    storageRoom: str | None = None
    containerType: str | None = None
    containerCount: int | None = None
    secondaryContainmentPresent: bool
    secondaryContainmentVolume: float | None = None
    ventilationAvailable: bool
    signagePresent: bool
    issueDate: datetime | None = None
    expiryDate: datetime | None = None
    batchLotNumber: str | None = None
    sdsDocId: str | None = None
    sdsVersion: str | None = None
    sdsGhsCompliant: bool
    ppeRequired: list[str] = []
    incompatibleSubstances: list[str] = []
    spillKitLocation: str | None = None
    emergencyContact: str | None = None
    handlersTrainedCount: int
    handlersTotalCount: int
    pcbNotificationRequired: bool
    pcbRegistrationStatus: str
    notes: str | None = None
    updatedAt: datetime | None = None
    # ── computed on read ──
    shelfLifeStatus: str = "NA"  # VALID | EXPIRING_SOON | EXPIRED | NA
    daysToExpiry: int | None = None
    utilisationPct: float | None = None  # quantityStored / maxAllowableQty
    overCapacity: bool = False
    reorderReached: bool = False
    containmentRequiredVolume: float | None = None  # 110% of stored
    containmentOk: bool | None = None  # None when no secondary containment present
    trainingStatus: str = "NA"  # ALL_TRAINED | PARTIALLY_TRAINED | NOT_TRAINED | NA
    sdsMissingFlag: bool = False  # HIGH hazard w/o an SDS reference


# ════════════════════════════════════════════════════════════════════════════
# Regulatory Registration
# ════════════════════════════════════════════════════════════════════════════
class RegulatoryCreate(BaseModel):
    registrationType: RegistrationTypeLit
    registrationName: str = Field(min_length=1)
    registrationNumber: str | None = None
    issuingAuthority: str | None = None
    issueDate: datetime | None = None
    expiryDate: datetime | None = None
    renewalFrequency: RenewalFrequencyLit = "ANNUAL"
    lastRenewedDate: datetime | None = None
    nextRenewalDue: datetime | None = None
    renewalInProgress: bool = False
    renewalAgencyContact: str | None = None
    renewalEstimatedCost: float | None = Field(None, ge=0)
    renewalNotes: str | None = None
    alertThresholdDays: int = Field(90, ge=0, le=365)
    complianceImpactIfExpired: ImpactLit = "MEDIUM"
    documentationIds: list[str] = []


class RegulatoryUpdate(BaseModel):
    registrationType: RegistrationTypeLit | None = None
    registrationName: str | None = None
    registrationNumber: str | None = None
    issuingAuthority: str | None = None
    issueDate: datetime | None = None
    expiryDate: datetime | None = None
    renewalFrequency: RenewalFrequencyLit | None = None
    lastRenewedDate: datetime | None = None
    nextRenewalDue: datetime | None = None
    renewalInProgress: bool | None = None
    renewalAgencyContact: str | None = None
    renewalEstimatedCost: float | None = None
    renewalNotes: str | None = None
    alertThresholdDays: int | None = None
    complianceImpactIfExpired: ImpactLit | None = None
    documentationIds: list[str] | None = None


class MarkRenewedRequest(BaseModel):
    """POST .../regulatory/{id}/mark-renewed — record a completed renewal."""

    newExpiryDate: datetime
    renewalCost: float | None = Field(None, ge=0)
    documentId: str | None = None
    notes: str | None = None


class RegulatoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    factoryProfileId: str
    siteId: str
    registrationType: str
    registrationName: str
    registrationNumber: str | None = None
    issuingAuthority: str | None = None
    issueDate: datetime | None = None
    expiryDate: datetime | None = None
    renewalFrequency: str
    lastRenewedDate: datetime | None = None
    nextRenewalDue: datetime | None = None
    status: str
    renewalInProgress: bool
    renewalAgencyContact: str | None = None
    renewalEstimatedCost: float | None = None
    renewalNotes: str | None = None
    alertThresholdDays: int
    complianceImpactIfExpired: str
    documentationIds: list[str] = []
    updatedAt: datetime | None = None
    # ── computed on read ──
    daysToExpiry: int | None = None


# ════════════════════════════════════════════════════════════════════════════
# Lifecycle workflow
# ════════════════════════════════════════════════════════════════════════════
class LifecycleEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    factoryProfileId: str
    siteId: str
    fromStage: str | None = None
    toStage: str
    action: str
    performedBy: str | None = None
    performedByRole: str | None = None
    comment: str | None = None
    validations: dict[str, Any] = {}
    issues: list[dict[str, Any]] = []
    createdAt: datetime | None = None


class AdvanceStageRequest(BaseModel):
    toStage: LifecycleStageLit
    comment: str | None = None
    validations: dict[str, bool] = {}


class RequestRevisionsRequest(BaseModel):
    comment: str = Field(min_length=1)
    issues: list[dict[str, Any]] = []  # [{ section, issue }]
    priority: Literal["HIGH", "MEDIUM", "LOW"] = "MEDIUM"


class LifecycleStatusOut(BaseModel):
    factoryProfileId: str
    lifecycleStage: str
    lifecycleStageOwnerRole: str | None = None
    lifecycleUpdatedAt: datetime | None = None
    allowedNextStages: list[str] = []
    canRequestRevisions: bool = False
    events: list[LifecycleEventOut] = []
