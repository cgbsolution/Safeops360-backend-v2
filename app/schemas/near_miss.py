from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.near_miss import NearMissStatus
from app.models.observation import Severity


class NearMissPersonInput(BaseModel):
    """Sub-payload for personsInvolved / personsPotentiallyAffected /
    witnesses arrays — the form sends `[{userId, role?}]` per person."""

    userId: str
    role: str | None = None
    proximityToHazard: str | None = None  # only for affected
    statementCaptured: bool = False  # only for witnesses


class PotentialConsequenceItem(BaseModel):
    """One element of the potentialConsequences array — see brief
    Section 4. Keeping this loose so future sub-rating shapes don't
    require a schema bump."""

    model_config = ConfigDict(extra="allow")

    type: str  # INJURY | PROPERTY_DAMAGE | ENVIRONMENTAL | PROCESS_LOSS | FIRE_EXPLOSION | MULTIPLE_WORKER_IMPACT | REPUTATION
    subRating: str | None = None  # for INJURY: MINOR | MAJOR | FATALITY_POTENTIAL
    costEstimate: float | None = None  # for PROPERTY_DAMAGE
    substanceEstimate: str | None = None  # for ENVIRONMENTAL
    downtimeHours: float | None = None  # for PROCESS_LOSS


class NearMissCreate(BaseModel):
    """Submission payload from the new (Commit 2) form. Most new fields
    are optional so a quick mobile capture flow with just the essentials
    still validates."""

    model_config = ConfigDict(extra="ignore")

    # Required core
    plantId: str
    date: datetime
    description: str = Field(min_length=10)
    potentialSeverity: Severity

    # Location
    areaId: str | None = None
    location: str | None = None  # legacy free-text (back-compat)
    specificLocation: str | None = None
    gpsLatitude: float | None = None
    gpsLongitude: float | None = None

    # Department & shift
    departmentId: str | None = None
    shiftId: str | None = None

    # Reporter context
    reporterType: Literal["EMPLOYEE", "CONTRACTOR", "EXTERNAL", "ANONYMOUS"] | None = None
    isAnonymous: bool = False

    # Activity
    activityBeingPerformed: str | None = None
    activityIsRoutine: bool | None = None
    activity: str | None = None  # legacy free-text
    immediateAction: str | None = None

    # Equipment & contractor
    equipmentId: str | None = None
    contractorCompanyId: str | None = None

    # Severity & consequence
    potentialConsequence: str | None = None  # legacy CSV (back-compat)
    potentialConsequences: list[PotentialConsequenceItem] | None = None
    multipleWorkersAggravator: bool = False

    # Hazard
    hazardCategory: str | None = None
    energySource: str | None = None

    # Risk matrix
    riskLikelihood: int | None = Field(default=None, ge=1, le=5)
    riskConsequence: int | None = Field(default=None, ge=1, le=5)

    # Reporter root-cause hint + barriers
    initialRootCauseCategory: str | None = None
    controlsThatFailed: str | None = None
    controlsThatWorked: str | None = None

    # Reporter recommendation
    recommendedActions: str | None = None
    suggestedActionOwnerId: str | None = None

    # Children — sent inline
    personsInvolved: list[NearMissPersonInput] | None = None
    personsPotentiallyAffected: list[NearMissPersonInput] | None = None
    witnesses: list[NearMissPersonInput] | None = None


class NearMissUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    actionOwnerId: str | None = None
    correctiveActions: str | None = None
    rootCauseCategory: str | None = None
    rootCauseDetail: str | None = None
    targetDate: datetime | None = None
    # ─── Editable core details ("edit while open"). All optional; applied only
    #     while the near miss is not CLOSED (router guard) under NEAR_MISS.UPDATE. ───
    description: str | None = Field(default=None, min_length=10)
    potentialSeverity: Severity | None = None
    areaId: str | None = None
    location: str | None = None
    specificLocation: str | None = None
    hazardCategory: str | None = None
    energySource: str | None = None
    activityBeingPerformed: str | None = None
    immediateAction: str | None = None


class NearMissPersonOut(BaseModel):
    id: str
    name: str
    designation: str | None = None

    model_config = ConfigDict(from_attributes=True)


class NearMissOut(BaseModel):
    id: str
    number: str
    date: datetime
    plantId: str
    areaId: str | None
    reporterId: str
    description: str

    # Location
    location: str | None
    specificLocation: str | None
    gpsLatitude: float | None
    gpsLongitude: float | None

    # Departmental / shift
    departmentId: str | None
    shiftId: str | None

    reporterType: str | None
    isAnonymous: bool

    # Activity
    activityBeingPerformed: str | None
    activityIsRoutine: bool | None
    activity: str | None
    immediateAction: str | None

    equipmentId: str | None
    contractorCompanyId: str | None

    # Severity & consequence
    potentialSeverity: Severity
    potentialConsequence: str | None
    potentialConsequences: list[dict[str, Any]] | None
    multipleWorkersAggravator: bool

    hazardCategory: str | None
    energySource: str | None

    riskLikelihood: int | None
    riskConsequence: int | None
    riskScore: int | None
    riskLevel: str | None

    initialRootCauseCategory: str | None
    controlsThatFailed: str | None
    controlsThatWorked: str | None

    recommendedActions: str | None
    suggestedActionOwnerId: str | None

    # Transitional CAPA fields
    rootCauseCategory: str | None
    rootCauseDetail: str | None
    correctiveActions: str | None
    actionOwnerId: str | None
    targetDate: datetime | None

    # Auto-detection / promotion
    isRepeat: bool
    activePermitId: str | None
    permitReviewFlagged: bool
    autoPromoteToIncident: bool
    promotedToIncident: bool
    promotedIncidentId: str | None
    promotedAt: datetime | None

    closedAt: datetime | None
    closingRemark: str | None
    lessonsLearned: str | None

    slaTargetAt: datetime | None
    slaActualClosedAt: datetime | None
    slaPerformance: str | None

    status: NearMissStatus
    createdAt: datetime
    updatedAt: datetime

    # AI agent outputs persisted by the workflow engine. Mirrors the
    # shape on Observation.closureTriggers — [{ruleId, ruleName, fired,
    # data}]. Empty / null means no agent has fired yet.
    closureTriggers: list[dict] | None = None

    model_config = {"from_attributes": True}


class MasterListItem(BaseModel):
    id: str
    code: str
    label: str
    sortOrder: int
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True)


class DepartmentOut(BaseModel):
    id: str
    plantId: str
    name: str
    code: str | None

    model_config = ConfigDict(from_attributes=True)


class ContractorCompanyOut(BaseModel):
    id: str
    name: str
    code: str | None
    score: int

    model_config = ConfigDict(from_attributes=True)
