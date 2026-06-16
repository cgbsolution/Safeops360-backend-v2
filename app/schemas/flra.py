from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.flra import FLRAStatus

RISK_LEVELS = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


# ─── Sub-payloads — Hazard Analysis ───────────────────────────────────


class StepHazardInput(BaseModel):
    """One hazard row inside a job step (5×5 risk matrix)."""

    model_config = ConfigDict(extra="ignore")

    hazardDescription: str = Field(min_length=1)
    hazardCategory: str  # FK by id to MasterItem(HAZARD_CATEGORY)
    energySource: str | None = None  # FK by id to MasterItem(ENERGY_SOURCE)

    initialLikelihood: int = Field(ge=1, le=5)
    initialSeverity: int = Field(ge=1, le=5)
    controlMeasures: str = Field(min_length=1)
    residualLikelihood: int = Field(ge=1, le=5)
    residualSeverity: int = Field(ge=1, le=5)


class JobStepInput(BaseModel):
    """One job step containing one or more hazards."""

    model_config = ConfigDict(extra="ignore")

    sequence: int = Field(ge=1)
    stepDescription: str = Field(min_length=1)
    hazards: list[StepHazardInput] = Field(min_length=1)


# ─── Sub-payloads — Fitness Declarations ─────────────────────────────


class FitnessDeclarationInput(BaseModel):
    """Per-crew-member fitness self-declaration."""

    model_config = ConfigDict(extra="ignore")

    userId: str
    isFit: bool
    hasMedicalCondition: bool = False
    conditionsDeclared: str | None = None
    hadAdequateRest: bool
    underInfluenceCheck: bool
    notes: str | None = None


# ─── Create payloads ──────────────────────────────────────────────────


class FLRACreate(BaseModel):
    """Multi-step FLRA wizard payload. Legacy `hazards` field stays for
    back-compat; new clients send structured `jobSteps`."""

    model_config = ConfigDict(extra="ignore")

    permitId: str | None = None
    plantId: str
    date: datetime
    location: str = Field(min_length=1)
    jobDescription: str = Field(min_length=10)
    teamMemberIds: list[str] = Field(min_length=1)
    toolboxTalkById: str
    toolboxTalkConfirmed: bool = False
    hazards: str = "[]"  # legacy JSON-encoded list — kept for back-compat

    # ─── Commit 3 wizard fields ───
    isStandalone: bool = False
    departmentId: str | None = None
    areaCode: str | None = None
    specificLocation: str | None = None
    gpsLatitude: float | None = None
    gpsLongitude: float | None = None
    startTime: datetime | None = None
    jobIsRoutine: bool | None = None

    toolboxTalkConducted: bool = False
    toolboxTalkConductedAt: datetime | None = None
    toolboxTalkTopics: list[str] | None = None
    toolboxTalkLanguage: str | None = None

    ppeChecklistResponses: dict[str, Any] | None = None
    toolsCheckedResponses: dict[str, Any] | None = None
    exitRoutesIdentified: str | None = None
    emergencyContactsConfirmed: bool = False

    jobSteps: list[JobStepInput] = []
    fitnessDeclarations: list[FitnessDeclarationInput] = []


class FLRARedoRequest(BaseModel):
    reason: str = Field(min_length=5)


class FLRASignRequest(BaseModel):
    """Per-crew sign-off body. Refusal flow takes the same endpoint."""

    model_config = ConfigDict(extra="ignore")

    refusedToSign: bool = False
    refusalReason: str | None = None
    escalatedToId: str | None = None

    @field_validator("refusalReason")
    @classmethod
    def reason_required_when_refused(cls, v: str | None, info: Any) -> str | None:
        if info.data.get("refusedToSign") and not (v and v.strip()):
            raise ValueError("Refusal reason is required when refusing to sign.")
        return v


# ─── Read-back ────────────────────────────────────────────────────────


class CrewSignatureOut(BaseModel):
    id: str
    userId: str
    signed: bool
    signedAt: datetime | None
    trainingValidAtSignature: bool
    trainingExpiresAt: datetime | None
    refusedToSign: bool = False
    refusalReason: str | None = None
    refusalEscalatedToId: str | None = None
    refusalEscalatedAt: datetime | None = None

    model_config = {"from_attributes": True}


class FLRAOut(BaseModel):
    id: str
    number: str
    permitId: str | None
    plantId: str
    date: datetime
    location: str
    jobDescription: str
    leaderId: str
    hazards: str
    toolboxTalkById: str | None
    toolboxTalkConfirmed: bool
    status: FLRAStatus
    completedAt: datetime | None
    supersededById: str | None
    supersededReason: str | None
    createdAt: datetime
    updatedAt: datetime

    # Commit 1+ additions
    isStandalone: bool = False
    departmentId: str | None = None
    areaCode: str | None = None
    specificLocation: str | None = None
    gpsLatitude: float | None = None
    gpsLongitude: float | None = None
    startTime: datetime | None = None
    jobIsRoutine: bool | None = None
    toolboxTalkConducted: bool = False
    toolboxTalkConductedAt: datetime | None = None
    toolboxTalkLanguage: str | None = None
    emergencyContactsConfirmed: bool = False
    exitRoutesIdentified: str | None = None

    model_config = {"from_attributes": True}
