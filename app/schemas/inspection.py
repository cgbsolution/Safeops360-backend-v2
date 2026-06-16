from datetime import datetime

from pydantic import BaseModel

from app.models.equipment import InspectionStatus


class InspectionCreate(BaseModel):
    equipmentId: str
    inspectorId: str | None = None
    scheduledDate: datetime


class InspectionUpdate(BaseModel):
    inspectorId: str | None = None
    scheduledDate: datetime | None = None
    checklistResult: str | None = None
    result: str | None = None
    observations: str | None = None
    followUpRequired: bool | None = None


class InspectionOut(BaseModel):
    id: str
    number: str
    equipmentId: str
    inspectorId: str | None
    scheduledDate: datetime
    completedDate: datetime | None
    checklistResult: str | None
    result: str | None
    observations: str | None
    followUpRequired: bool
    status: InspectionStatus
    createdAt: datetime
    updatedAt: datetime

    model_config = {"from_attributes": True}
