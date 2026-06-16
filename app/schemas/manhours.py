from datetime import datetime

from pydantic import BaseModel, Field


class ManhoursCreate(BaseModel):
    plantId: str
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
    headcount: int = Field(ge=0)
    manhoursWorked: int = Field(ge=0)
    contractorManhours: int = Field(ge=0, default=0)
    ltiCount: int = Field(ge=0, default=0)
    mtcCount: int = Field(ge=0, default=0)
    fatalCount: int = Field(ge=0, default=0)
    lostDays: int = Field(ge=0, default=0)
    notes: str | None = None


class ManhoursOut(BaseModel):
    id: str
    plantId: str
    year: int
    month: int
    headcount: int
    manhoursWorked: int
    contractorManhours: int
    ltiCount: int
    mtcCount: int
    fatalCount: int
    lostDays: int
    ltifr: float | None
    trir: float | None
    severityRate: float | None
    submittedById: str | None
    submittedAt: datetime | None
    notes: str | None
    createdAt: datetime
    updatedAt: datetime

    model_config = {"from_attributes": True}
