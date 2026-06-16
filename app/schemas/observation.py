from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.observation import (
    ObservationCategory,
    ObservationStatus,
    ObservationType,
    Severity,
)


class ObservationCreate(BaseModel):
    # extra="ignore" so any stray form fields (location, correctiveAction —
    # carried over from the Prisma era — or anything else the form sends
    # that the schema doesn't enumerate) are silently dropped instead of
    # rejected with a 422.
    model_config = ConfigDict(extra="ignore")

    plantId: str
    areaId: str | None = None
    type: ObservationType
    category: ObservationCategory
    severity: Severity = Severity.LOW
    description: str = Field(min_length=10)
    immediateAction: str | None = None
    # responsiblePersonId is now assigned by the Section Head during the
    # CHECKER step, not by the observer at creation time. Kept optional
    # here so direct API callers can still set it if they want to.
    responsiblePersonId: str | None = None
    targetDate: datetime | None = None
    date: datetime


class ObservationUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: ObservationStatus | None = None
    closingRemark: str | None = None
    responsiblePersonId: str | None = None
    targetDate: datetime | None = None


class ObservationOut(BaseModel):
    id: str
    number: str
    date: datetime
    type: ObservationType
    category: ObservationCategory
    severity: Severity
    plantId: str
    areaId: str | None
    observerId: str
    responsiblePersonId: str | None
    description: str
    immediateAction: str | None
    targetDate: datetime | None
    closingRemark: str | None
    closedAt: datetime | None
    status: ObservationStatus
    createdAt: datetime
    updatedAt: datetime
    # AI agent outputs persisted by the workflow engine (rule_triage_on_submit
    # + rule_lessons_distribution). Shape: [{ruleId, ruleName, fired, data}].
    # The mobile / web clients render whatever the rules emitted; an empty
    # array or `null` means no agent has fired yet.
    closureTriggers: list[dict] | None = None

    model_config = {"from_attributes": True}


class ObservationListResponse(BaseModel):
    items: list[ObservationOut]
    total: int
