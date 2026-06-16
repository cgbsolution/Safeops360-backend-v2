from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.permit import PermitStatus, PermitType


# ─── Sub-payloads for the wizard's multi-row sub-forms ────────────────


class IsolationInput(BaseModel):
    """One row from PTW Step 4 — Isolations."""
    model_config = ConfigDict(extra="ignore")

    isolationType: str   # FK by id to MasterItem(ISOLATION_TYPE)
    description: str = Field(min_length=1)
    isolationPointTag: str = Field(min_length=1)
    lotoTagNumber: str | None = None


class ToolEquipmentInput(BaseModel):
    """One row from PTW Step 5 — Tools/Equipment used by the crew."""
    model_config = ConfigDict(extra="ignore")

    equipmentId: str | None = None
    freeTextDescription: str | None = None


class SubjectEquipmentInput(BaseModel):
    """One row from PTW Step 5 — Equipment being worked on."""
    model_config = ConfigDict(extra="ignore")

    equipmentId: str
    workNature: Literal["INSPECTION", "REPAIR", "REPLACEMENT", "MODIFICATION"]


class GasTestParameterSpec(BaseModel):
    """One parameter in the gas test plan (PTW Step 6)."""
    model_config = ConfigDict(extra="allow")

    parameter: str  # O2 | LEL | CO | H2S | OTHER
    lowLimit: float | None = None
    highLimit: float | None = None
    unit: str   # %, ppm, etc.


class GasTestPlanInput(BaseModel):
    """1-to-1 gas test plan attached to the permit (PTW Step 6)."""
    model_config = ConfigDict(extra="ignore")

    refreshFrequencyMinutes: int = Field(ge=15, le=480, default=120)
    parametersToTest: list[GasTestParameterSpec]
    instrumentSerial: str | None = None
    instrumentLastCalibrated: datetime | None = None


class CrewMemberInput(BaseModel):
    """One row from PTW Step 3 — Work crew."""
    model_config = ConfigDict(extra="ignore")

    userId: str
    role: Literal["OPERATOR", "HELPER", "SUPERVISOR", "TECHNICIAN", "WORKER", "CONTRACTOR"] = "WORKER"


class PermitCreate(BaseModel):
    """Create-permit payload from the 8-step wizard. All Step 1 / Step 2 /
    Step 3 / Step 8 fields are required; Steps 4-7 are conditional on the
    permit type's metadata (e.g. gas test plan only for Hot Work / Confined
    Space). Legacy fields are kept for back-compat with older clients."""
    model_config = ConfigDict(extra="ignore")

    # ─── Required core ───
    type: PermitType
    plantId: str
    location: str = Field(min_length=1)
    scopeOfWork: str = Field(min_length=10)
    validFrom: datetime
    validTo: datetime
    issuerId: str
    receiverId: str

    # ─── Step 1/2 additions ───
    departmentId: str | None = None
    areaId: str | None = None
    specificLocation: str | None = None
    gpsLatitude: float | None = None
    gpsLongitude: float | None = None
    workOrderNumber: str | None = None
    attachedDrawingIds: list[str] = []

    # ─── Step 3 additions ───
    workCrew: list[CrewMemberInput] = []
    fireWatchPersonId: str | None = None
    standbyPersonId: str | None = None

    # ─── Step 4: Isolations ───
    isolations: list[IsolationInput] = []

    # ─── Step 5: PPE & Equipment ───
    requiredPpe: list[str] | None = None  # PPE codes — accepted; legacy ppeChecklist also kept
    toolsEquipment: list[ToolEquipmentInput] = []
    subjectEquipment: list[SubjectEquipmentInput] = []

    # ─── Step 6: Gas Test Plan ───
    gasTestPlan: GasTestPlanInput | None = None

    # ─── Step 7: Additional Controls ───
    weatherConditionsAtIssue: str | None = None
    windSpeedKmh: float | None = None
    adjacentAreaNotifications: dict[str, Any] | None = None

    # ─── Legacy fields (kept for back-compat with single-page form) ───
    contractorName: str | None = None
    isolationsRequired: str | None = None  # CSV
    ppeChecklist: str | None = None        # JSON string
    gasTestRequired: bool = False
    gasTestResult: str | None = None
    o2Level: str | None = None
    lelLevel: str | None = None
    h2sLevel: str | None = None
    fireWatchRequired: bool = False
    rescuePlan: str | None = None


class PermitOut(BaseModel):
    id: str
    number: str
    type: PermitType
    plantId: str
    areaId: str | None
    location: str
    scopeOfWork: str
    validFrom: datetime
    validTo: datetime
    originatorId: str
    issuerId: str | None
    receiverId: str | None
    contractorName: str | None
    status: PermitStatus
    issuerApprovedAt: datetime | None
    safetyApprovedAt: datetime | None
    plantHeadApprovedAt: datetime | None
    closedAt: datetime | None
    suspendedAt: datetime | None
    suspendedReason: str | None
    expiredAt: datetime | None
    createdAt: datetime
    updatedAt: datetime

    # Phase-1+ additions (read-back)
    validityHours: int | None = None
    departmentId: str | None = None
    specificLocation: str | None = None
    gpsLatitude: float | None = None
    gpsLongitude: float | None = None
    workOrderNumber: str | None = None
    fireWatchPersonId: str | None = None
    standbyPersonId: str | None = None
    weatherConditionsAtIssue: str | None = None
    windSpeedKmh: float | None = None
    activatedAt: datetime | None = None
    activatedById: str | None = None
    currentActiveFlraId: str | None = None
    isCurrentlySuspended: bool = False
    returnedAt: datetime | None = None
    returnedById: str | None = None
    siteVerifiedAt: datetime | None = None
    siteVerifiedById: str | None = None
    closingRemark: str | None = None

    model_config = {"from_attributes": True}


class SuspendRequest(BaseModel):
    reason: str = Field(min_length=1)


class ResumeRequest(BaseModel):
    comments: str | None = None


class AdminResetRequest(BaseModel):
    status: str  # DRAFT or SUBMITTED only
