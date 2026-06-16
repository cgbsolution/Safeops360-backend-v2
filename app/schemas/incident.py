from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.incident import IncidentStatus, IncidentType


# ─── Sub-payloads for Phase 1 multi-row sub-forms ─────────────────────


class IncidentPersonInput(BaseModel):
    """One row from the "People Involved" sub-form. Either userId (for
    internal employees / contractor workmen with a User account) or
    externalName (visitors / public / third parties) — at least one
    must be present, validated server-side."""

    model_config = ConfigDict(extra="ignore")

    userId: str | None = None
    externalName: str | None = None
    externalContact: str | None = None
    role: Literal[
        "VICTIM", "INJURED", "WITNESS", "RESPONDER", "OPERATOR", "SUPERVISOR"
    ]
    isContractor: bool = False
    contractorCompanyId: str | None = None

    # Injury detail (only when isInjured=true)
    isInjured: bool = False
    bodyPartAffected: str | None = None
    natureOfInjury: str | None = None
    injurySeverity: Literal["MINOR", "MAJOR", "FATAL"] | None = None
    treatment: str | None = None
    hospitalName: str | None = None
    daysOff: int | None = None
    ppeWornAtTime: dict[str, Any] | None = None


class IncidentWitnessInput(BaseModel):
    """Phase 1 captures just witness name + role; full statement / signed
    PDF / audio recording are added later during Phase 3 investigation."""

    model_config = ConfigDict(extra="ignore")

    witnessUserId: str | None = None
    witnessName: str
    witnessRole: str | None = None
    language: str | None = None  # English | Hindi | Bengali | Khasi


class IncidentEquipmentInput(BaseModel):
    """One row from the "Equipment Involved" sub-form."""

    model_config = ConfigDict(extra="ignore")

    equipmentId: str
    involvement: str = "DIRECTLY_INVOLVED"
    damageEstimate: float | None = None


class IncidentCreate(BaseModel):
    """Phase 1 Initial Report payload. Most new fields are optional so a
    quick mobile-first capture flow with just the essentials still
    validates. The classification + investigation phases populate the rest
    via PATCH on the detail page."""

    model_config = ConfigDict(extra="ignore")

    # ─── Required core ───
    type: IncidentType
    plantId: str
    location: str
    date: datetime  # legacy "incident date" — kept for back-compat writes
    description: str = Field(min_length=10)

    # ─── Phase 1 — precise occurrence + reporter context ───
    occurredAt: datetime | None = None  # if absent, server uses `date`
    departmentId: str | None = None
    areaId: str | None = None
    specificLocation: str | None = None
    gpsLatitude: float | None = None
    gpsLongitude: float | None = None
    shiftId: str | None = None
    weatherConditions: str | None = None
    initialDescription: str | None = None
    immediateAction: str | None = None
    activityBeingPerformed: str | None = None
    activityIsRoutine: bool | None = None

    # ─── Equipment / permit / source NM context ───
    activePermitId: str | None = None
    sourceNearMissId: str | None = None  # set when arriving via auto-promotion

    # ─── Multi sub-rows (Phase 1) ───
    personsInvolved: list[IncidentPersonInput] | None = None
    witnesses: list[IncidentWitnessInput] | None = None
    equipmentInvolved: list[IncidentEquipmentInput] | None = None

    # ─── Legacy single-injured-person fields (kept for back-compat) ───
    injuredPersonName: str | None = None
    injuredPersonAge: int | None = None
    injuredPersonDesignation: str | None = None
    bodyPart: str | None = None
    natureOfInjury: str | None = None

    # ─── Classification / RCA / CAPA fields (filled in later phases —
    #     accepted here for back-compat with the current single-form flow) ───
    immediateCause: str | None = None
    rootCauseMethod: str | None = None
    rootCauseData: dict[str, Any] | None = None
    rootCauseDetail: str | None = None
    correctiveActions: str | None = None
    preventiveActions: str | None = None
    lostDays: int = 0
    propertyDamageCost: float | None = None
    investigationTeamIds: list[str] = []


class IncidentUpdate(BaseModel):
    immediateCause: str | None = None
    rootCauseMethod: str | None = None
    rootCauseData: dict[str, Any] | None = None
    rootCauseDetail: str | None = None
    correctiveActions: str | None = None
    preventiveActions: str | None = None
    lostDays: int | None = None
    propertyDamageCost: float | None = None
    investigationTeamIds: list[str] | None = None

    # Phase 3 refinements — cause hierarchy + cost breakdown
    immediateCauses: list[str] | None = None
    underlyingCauses: list[str] | None = None
    rootCauses: list[str] | None = None
    contributingFactors: list[str] | None = None

    costMedical: float | None = None
    costPropertyDamage: float | None = None
    costLostProduction: float | None = None
    costInsurance: float | None = None
    costLegalRegulatory: float | None = None
    costOther: float | None = None


class IncidentClassifyRequest(BaseModel):
    """HSE Manager Phase 2 classification submission. Refines type +
    severity, sets statutory obligations, constitutes the investigation
    team. Submitting this also approves the Phase 2 workflow CHECKER step."""

    model_config = ConfigDict(extra="ignore")

    type: IncidentType
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    classificationRationale: str = Field(min_length=10)

    isReportable: bool
    reportableUnder: list[str] | None = None  # ["FACTORIES_ACT", "DGFASLI", "CPCB"]

    investigationTeamLead: str | None = None
    investigationTeamMemberIds: list[str] = []
    investigationCharterDate: datetime | None = None

    # Initial cost estimates (refined later)
    costPropertyDamage: float | None = None
    costLostProduction: float | None = None

    # Required to advance the Phase 2 workflow CHECKER step
    classificationTaskId: str
    comments: str | None = None


class IncidentCapaInput(BaseModel):
    """Create-or-update payload for one CAPA."""

    model_config = ConfigDict(extra="ignore")

    description: str = Field(min_length=10)
    type: Literal["CORRECTIVE", "PREVENTIVE"]
    rootCauseAddressed: str | None = None
    ownerId: str
    targetDate: datetime


class IncidentCapaOut(BaseModel):
    id: str
    incidentId: str
    capaNumber: str
    description: str
    type: str
    rootCauseAddressed: str | None
    ownerId: str
    targetDate: datetime
    status: str
    evidenceUrls: list[str] | None
    evidenceDescription: str | None
    beforePhotoUrl: str | None
    afterPhotoUrl: str | None
    completedAt: datetime | None
    verifiedById: str | None
    verifiedAt: datetime | None
    effectivenessRating: int | None
    createdAt: datetime
    updatedAt: datetime

    model_config = {"from_attributes": True}


class TimelineEventInput(BaseModel):
    """One row from the Investigation > Timeline tab."""
    model_config = ConfigDict(extra="ignore")

    sequence: int
    timestamp: datetime
    description: str = Field(min_length=5)
    source: Literal["WITNESS", "CCTV", "EQUIPMENT_DATA", "INTERVIEW", "DOCUMENT"]
    sourceReference: str | None = None


class TimelineEventOut(BaseModel):
    id: str
    incidentId: str
    sequence: int
    timestamp: datetime
    description: str
    source: str
    sourceReference: str | None

    model_config = {"from_attributes": True}


class EvidenceInput(BaseModel):
    """One row from the Investigation > Evidence tab. The file itself is
    uploaded to Supabase Storage via a separate upload flow; this row stores
    the metadata + storage path."""
    model_config = ConfigDict(extra="ignore")

    category: Literal["PHOTO", "VIDEO", "CCTV", "EQUIPMENT_DATA", "DOCUMENT", "SKETCH", "EXTERNAL_REPORT"]
    title: str = Field(min_length=2)
    description: str | None = None
    fileUrl: str | None = None
    fileName: str | None = None
    fileSize: int | None = None
    mimeType: str | None = None
    collectedAt: datetime | None = None
    preservedFor: Literal["legal", "regulatory", "internal"] | None = None


class EvidenceOut(BaseModel):
    id: str
    incidentId: str
    category: str
    title: str
    description: str | None
    fileUrl: str | None
    fileName: str | None
    fileSize: int | None
    mimeType: str | None
    collectedById: str | None
    collectedAt: datetime | None
    preservedFor: str | None

    model_config = {"from_attributes": True}


class WitnessStatementUpdate(BaseModel):
    """PATCH payload for filling in a Phase 1 witness row with the full
    statement text + signed-PDF / audio-recording URLs during Phase 3."""
    model_config = ConfigDict(extra="ignore")

    witnessRole: str | None = None
    statementText: str | None = None
    statementFileUrl: str | None = None
    audioRecordingUrl: str | None = None
    language: str | None = None


class WitnessStatementOut(BaseModel):
    id: str
    incidentId: str
    witnessUserId: str | None
    witnessName: str
    witnessRole: str | None
    statementText: str | None
    statementFileUrl: str | None
    audioRecordingUrl: str | None
    takenById: str
    takenAt: datetime
    language: str | None

    model_config = {"from_attributes": True}


class PersonUpdate(BaseModel):
    """PATCH payload to refine a Phase 1 person row during Phase 3 — return
    to work date, days off, fitness for duty, etc."""
    model_config = ConfigDict(extra="ignore")

    role: str | None = None
    isInjured: bool | None = None
    bodyPartAffected: str | None = None
    natureOfInjury: str | None = None
    injurySeverity: Literal["MINOR", "MAJOR", "FATAL"] | None = None
    treatment: str | None = None
    hospitalName: str | None = None
    daysOff: int | None = None
    daysRestricted: int | None = None
    returnToWorkDate: datetime | None = None
    isFitForDuty: bool | None = None
    ppeWornAtTime: dict[str, Any] | None = None


class PersonOut(BaseModel):
    id: str
    incidentId: str
    userId: str | None
    externalName: str | None
    role: str
    isContractor: bool
    contractorCompanyId: str | None
    isInjured: bool
    bodyPartAffected: str | None
    natureOfInjury: str | None
    injurySeverity: str | None
    treatment: str | None
    hospitalName: str | None
    daysOff: int | None
    daysRestricted: int | None
    returnToWorkDate: datetime | None
    isFitForDuty: bool | None

    model_config = {"from_attributes": True}


class EquipmentUpdate(BaseModel):
    """PATCH payload to refine equipment damage assessment + repair status."""
    model_config = ConfigDict(extra="ignore")

    involvement: str | None = None
    damageEstimate: float | None = None
    repairStatus: Literal[
        "PENDING", "IN_PROGRESS", "REPAIRED", "REPLACED", "DECOMMISSIONED"
    ] | None = None


class EquipmentOut(BaseModel):
    id: str
    incidentId: str
    equipmentId: str
    involvement: str
    damageEstimate: float | None
    repairStatus: str | None

    model_config = {"from_attributes": True}


class DocumentReviewInput(BaseModel):
    """One row from the Investigation > Documents Reviewed tab."""
    model_config = ConfigDict(extra="ignore")

    documentType: Literal["SOP", "PERMIT", "TRAINING_RECORD", "INSPECTION_RECORD", "MOC", "PSM"]
    documentReference: str = Field(min_length=2)
    documentLinkId: str | None = None
    reviewNotes: str | None = None
    complianceFinding: Literal["COMPLIANT", "NON_COMPLIANT", "NOT_APPLICABLE"] | None = None


class DocumentReviewOut(BaseModel):
    id: str
    incidentId: str
    documentType: str
    documentReference: str
    documentLinkId: str | None
    reviewNotes: str | None
    complianceFinding: str | None

    model_config = {"from_attributes": True}


class CommentInput(BaseModel):
    """Threaded comment on an incident. `isPrivilegedLegal=true` marks it as
    visible only to the investigation team / HSE Manager / Plant Head /
    Corporate HSE / Legal — workers and supervisors do NOT see these."""
    model_config = ConfigDict(extra="ignore")

    content: str = Field(min_length=1)
    isPrivilegedLegal: bool = False


class CommentOut(BaseModel):
    id: str
    incidentId: str
    authorId: str
    content: str
    isPrivilegedLegal: bool
    createdAt: datetime

    model_config = {"from_attributes": True}


class StatutorySubmissionUpdate(BaseModel):
    """Mark statutory submissions as filed. HSE Manager fills this on the
    Statutory tab as each regulator's response comes in. Submission date
    + reference number are captured for audit."""
    model_config = ConfigDict(extra="ignore")

    form18Submitted: bool | None = None
    form18SubmissionDate: datetime | None = None
    form18SubmissionRef: str | None = None

    dgfasliSubmitted: bool | None = None
    dgfasliSubmissionDate: datetime | None = None

    cpcbSubmitted: bool | None = None
    cpcbSubmissionDate: datetime | None = None


class IncidentReclassifyRequest(BaseModel):
    """HSE Manager reclassifies incident type and severity mid-flow.
    Most common: MTC → LTI when worker doesn't return after expected days.

    Side effects (handled by the router):
      • Audit row in IncidentReclassification
      • Recompute statutoryDeadline + isReportable + reportableUnder
      • Update Incident.type / Incident.severity
      • If was-MTC-now-LTI and Form 18 window crossed, flag for urgent retroactive submission
      • Re-notify Plant Head + Corporate HSE if escalating to High/Critical"""

    model_config = ConfigDict(extra="ignore")

    toType: IncidentType
    toSeverity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    reason: str = Field(min_length=10)


class IncidentOut(BaseModel):
    id: str
    number: str
    date: datetime
    type: IncidentType
    plantId: str
    areaId: str | None
    location: str
    reporterId: str
    description: str
    injuredPersonName: str | None
    injuredPersonAge: int | None
    bodyPart: str | None
    natureOfInjury: str | None
    immediateCause: str | None
    rootCauseMethod: str | None
    rootCauseData: dict[str, Any] | None
    rootCauseSummary: str | None
    correctiveActions: str | None
    preventiveActions: str | None
    lostDays: int
    propertyDamageCost: float | None
    status: IncidentStatus
    closedAt: datetime | None
    createdAt: datetime
    updatedAt: datetime

    # ─── Phase 1 additions ───
    occurredAt: datetime | None = None
    reportedAt: datetime | None = None
    reportingDelayMinutes: int | None = None
    departmentId: str | None = None
    specificLocation: str | None = None
    gpsLatitude: float | None = None
    gpsLongitude: float | None = None
    shiftId: str | None = None
    weatherConditions: str | None = None
    initialDescription: str | None = None
    immediateAction: str | None = None
    activityBeingPerformed: str | None = None
    activityIsRoutine: bool | None = None
    activePermitId: str | None = None
    sourceNearMissId: str | None = None
    initialReportSlaTargetAt: datetime | None = None
    linkedObservationIds: list[str] | None = None
    linkedNearMissIds: list[str] | None = None
    isReportable: bool = False
    statutoryDeadline: datetime | None = None
    severity: str | None = None

    model_config = {"from_attributes": True}


# ─── Attachments ─────────────────────────────────────────────────────────


class AttachmentInit(BaseModel):
    phase: str = Field(pattern="^init$")
    category: str
    fileName: str
    fileSize: int = Field(gt=0)
    mimeType: str
    capaRef: str | None = None
    witnessRef: str | None = None


class AttachmentComplete(BaseModel):
    phase: str = Field(pattern="^complete$")
    attachmentId: str
    caption: str | None = None
    exifData: dict[str, Any] | None = None


class AttachmentUploader(BaseModel):
    id: str
    name: str
    designation: str | None = None

    model_config = {"from_attributes": True}


class AttachmentOut(BaseModel):
    id: str
    incidentId: str
    category: str
    fileName: str
    fileSize: int
    mimeType: str
    caption: str | None
    exifData: dict[str, Any] | None
    uploadedAt: datetime
    uploadedById: str
    uploadedBy: AttachmentUploader | None = None

    model_config = {"from_attributes": True}
