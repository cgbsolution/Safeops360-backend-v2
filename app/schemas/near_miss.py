from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.near_miss import NearMissStatus
from app.models.observation import Severity

#: Matches MIN_MANUAL_NAME in schemas/observation_sla.py — the two modules take
#: hand-typed people through the same UI component and must agree on what
#: counts as a name.
MIN_MANUAL_NAME = 2


class NearMissPersonInput(BaseModel):
    """Sub-payload for personsInvolved / personsPotentiallyAffected /
    witnesses arrays.

    Two shapes. A USER row carries `userId` and links to the directory. A
    MANUAL row carries `name` (and usually `code`, the works number) and no id
    at all — the report form hand-types the people on a near miss, so this is
    what it sends. Trusted because there is nothing to check it against, which
    is also why a MANUAL row links to nothing downstream.
    """

    partyType: Literal["USER", "MANUAL"] = "USER"
    userId: str | None = None
    name: str | None = None
    code: str | None = None
    role: str | None = None
    proximityToHazard: str | None = None  # only for affected
    statementCaptured: bool = False  # only for witnesses

    def validated(self) -> "NearMissPersonInput":
        if self.partyType == "MANUAL":
            if self.userId:
                raise ValueError(
                    "A manually entered person carries no userId — "
                    "pick them from the directory instead."
                )
            if len((self.name or "").strip()) < MIN_MANUAL_NAME:
                raise ValueError(
                    "A manually entered person needs a name of at least "
                    f"{MIN_MANUAL_NAME} characters."
                )
        elif not self.userId:
            raise ValueError("partyType USER requires userId.")
        return self


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
    departmentName: str | None = None  # free-text pick from the site department list
    shiftId: str | None = None  # MasterItem(SHIFT) id, or a NEAR_MISS_SHIFT code (GS/FS/SS/NS)

    # Reporter context
    reporterType: Literal["EMPLOYEE", "CONTRACTOR", "EXTERNAL", "ANONYMOUS"] | None = None
    isAnonymous: bool = False

    # Activity
    activityBeingPerformed: str | None = None
    activityIsRoutine: bool | None = None
    activity: str | None = None  # legacy free-text
    immediateAction: str | None = None

    # Equipment & contractor
    # None = not asked, [] = "no equipment involved", [...] = the typed items.
    equipmentInvolved: list[str] | None = None
    equipmentId: str | None = None
    contractorCompanyId: str | None = None

    # Severity & consequence
    potentialConsequence: str | None = None  # legacy CSV (back-compat)
    potentialConsequences: list[PotentialConsequenceItem] | None = None
    multipleWorkersAggravator: bool = False

    # Hazard — the printed grid is tick-any-number; the single MasterItem id
    # and energySource are the legacy shape and still accepted.
    hazardCategories: list[str] | None = None
    hazardCategoryOther: str | None = None
    hazardCategory: str | None = None
    energySource: str | None = None

    # Near miss category — one pictogram tile, free text for "Others"
    nearMissCategory: str | None = None
    nearMissCategoryDetail: str | None = None

    # Risk Calculator (RR = L × S) — two 1-3 scales off the site's card. The
    # rating and category are recomputed server-side; a client that sends them
    # is ignored, so a tampered payload cannot under-rate a near miss.
    riskProbability: int | None = Field(default=None, ge=1, le=3)
    riskSeverityLevel: int | None = Field(default=None, ge=1, le=3)
    riskSeverityDescription: str | None = None

    # Risk matrix (5 × 5) — the separate section further down the form
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
    departmentName: str | None = None
    shiftId: str | None = None
    hazardCategories: list[str] | None = None
    hazardCategoryOther: str | None = None
    nearMissCategory: str | None = None
    nearMissCategoryDetail: str | None = None
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
    departmentName: str | None
    shiftId: str | None

    reporterType: str | None
    isAnonymous: bool

    # Activity
    activityBeingPerformed: str | None
    activityIsRoutine: bool | None
    activity: str | None
    immediateAction: str | None

    equipmentInvolved: list[str] | None
    equipmentId: str | None
    contractorCompanyId: str | None

    # Severity & consequence
    potentialSeverity: Severity
    potentialConsequence: str | None
    potentialConsequences: list[dict[str, Any]] | None
    multipleWorkersAggravator: bool

    hazardCategories: list[str] | None
    hazardCategoryOther: str | None
    hazardCategory: str | None
    energySource: str | None

    nearMissCategory: str | None
    nearMissCategoryDetail: str | None

    riskProbability: int | None
    riskSeverityLevel: int | None
    riskSeverityDescription: str | None
    riskRating: int | None
    riskCategory: str | None

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
