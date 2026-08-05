from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin, SoftDeleteMixin
from app.models.user import User


class IncidentType(str, enum.Enum):
    FIRST_AID = "FIRST_AID"
    MTC = "MTC"
    RWC = "RWC"
    LTI = "LTI"
    FATALITY = "FATALITY"
    PROPERTY_DAMAGE = "PROPERTY_DAMAGE"
    ENVIRONMENTAL = "ENVIRONMENTAL"
    FIRE = "FIRE"
    PROCESS_SAFETY = "PROCESS_SAFETY"
    HIPO_NEAR_MISS = "HIPO_NEAR_MISS"


class IncidentStatus(str, enum.Enum):
    REPORTED = "REPORTED"
    INVESTIGATION = "INVESTIGATION"
    CAPA_ASSIGNED = "CAPA_ASSIGNED"
    VERIFIED = "VERIFIED"
    CLOSED = "CLOSED"


class Incident(Base, IdMixin, SoftDeleteMixin):
    """SQLAlchemy mirror of the Prisma `Incident` model.

    Phase 1 of the production-depth refactor adds ~50 new columns covering
    precise occurrence timestamps, per-phase classification fields, statutory
    tracking, multi-cost breakdown, approvals, closure, effectiveness, and
    cross-module link arrays. Existing legacy columns are kept for back-compat
    reads.

    NB: the SQLAlchemy enum for `status` is declared with `native_enum=False`
    but the actual Postgres column IS a native enum (created by Prisma). Only
    write values from `IncidentStatus`. The same caveat applied to NearMiss —
    see app/models/near_miss.py for the cautionary tale."""

    __tablename__ = "Incident"

    # ─── Core (existing) ───
    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    type: Mapped[IncidentType] = mapped_column(
        Enum(IncidentType, name="IncidentType", native_enum=False), nullable=False
    )
    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False)
    areaId: Mapped[str | None] = mapped_column(ForeignKey("Area.id"))
    location: Mapped[str] = mapped_column(String, nullable=False)
    reporterId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)

    injuredPersonName: Mapped[str | None] = mapped_column(String)
    injuredPersonAge: Mapped[int | None] = mapped_column(Integer)
    injuredPersonDesignation: Mapped[str | None] = mapped_column(String)
    bodyPart: Mapped[str | None] = mapped_column(String)
    natureOfInjury: Mapped[str | None] = mapped_column(String)

    description: Mapped[str] = mapped_column(Text, nullable=False)
    immediateCause: Mapped[str | None] = mapped_column(Text)
    rootCauseMethod: Mapped[str | None] = mapped_column(String)
    rootCauseDetail: Mapped[str | None] = mapped_column(Text)
    rootCauseData: Mapped[dict | None] = mapped_column(JSON)
    rootCauseSummary: Mapped[str | None] = mapped_column(Text)
    correctiveActions: Mapped[str | None] = mapped_column(Text)
    preventiveActions: Mapped[str | None] = mapped_column(Text)

    lostDays: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    propertyDamageCost: Mapped[float | None] = mapped_column(Numeric(12, 2))

    status: Mapped[IncidentStatus] = mapped_column(
        Enum(IncidentStatus, name="IncidentStatus", native_enum=False),
        nullable=False,
        default=IncidentStatus.REPORTED,
    )
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )

    # ═══════════════════════════════════════════════════════════════════
    #  Production-depth refactor — Commit 1 schema additions
    # ═══════════════════════════════════════════════════════════════════

    # ─── Phase 1: precise occurrence + reporter context ───
    occurredAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reportedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reportingDelayMinutes: Mapped[int | None] = mapped_column(Integer)
    reporterRole: Mapped[str | None] = mapped_column(String)
    departmentId: Mapped[str | None] = mapped_column(ForeignKey("Department.id"))
    specificLocation: Mapped[str | None] = mapped_column(String)
    gpsLatitude: Mapped[float | None] = mapped_column(Float)
    gpsLongitude: Mapped[float | None] = mapped_column(Float)
    shiftId: Mapped[str | None] = mapped_column(String)  # FK by id to MasterItem(SHIFT)
    weatherConditions: Mapped[str | None] = mapped_column(String)

    initialDescription: Mapped[str | None] = mapped_column(Text)
    immediateAction: Mapped[str | None] = mapped_column(Text)

    activityBeingPerformed: Mapped[str | None] = mapped_column(String)
    activityIsRoutine: Mapped[bool | None] = mapped_column(Boolean)
    activePermitId: Mapped[str | None] = mapped_column(ForeignKey("Permit.id"))
    sourceNearMissId: Mapped[str | None] = mapped_column(ForeignKey("NearMiss.id"), unique=True)

    # ─── Phase 2: Classification ───
    classifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    classifiedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    severity: Mapped[str | None] = mapped_column(String)  # LOW | MEDIUM | HIGH | CRITICAL
    classificationRationale: Mapped[str | None] = mapped_column(Text)

    # ─── Incident Intelligence (Slice 1) ───
    # Feature 5 — numeric 5×5 severity scoring + escalation ledger. `severity`
    # (above) stays the derived label mirror; the numbers live here.
    #   { score, likelihoodOfRecurrence, consequenceScore, band,
    #     linkedRiskRegisterId, escalationTriggered, escalationLog[], computedAt }
    severityDetail: Mapped[dict | None] = mapped_column(JSON)
    # Feature 2 — AI-assist provenance. Every AI-touched field is marked here so
    # an auditor can see which content originated from a model.
    #   { summarySource, summary, summaryGeneratedAt,
    #     rootCauseSuggestion: { text, confidence, basedOnIncidentIds,
    #                            generatedAt, status } }
    aiAssist: Mapped[dict | None] = mapped_column(JSON)
    # Feature 1 (Slice 2) — the shared cause-analysis canvas model. Both the
    # Fishbone and 5-Why views read/write this one `causes[]` array (switching
    # method never loses data). `rootCauseData`/`rootCauses` stay in sync.
    #   { method, causes: [{ id, category, whyLevel, text, isRootCause,
    #     source, confidence, acceptedBy, acceptedAt, linkedCapaId }],
    #     lastEditedBy, lastEditedAt }
    causeAnalysis: Mapped[dict | None] = mapped_column(JSON)
    # Feature 4 (Slice 2) — statutory obligation determined at classification.
    #   { required: bool, forms: [formType], jurisdiction }
    statutoryObligation: Mapped[dict | None] = mapped_column(JSON)
    # Feature 8 (Slice 2) — derived cost-of-unsafety breakdown (computed on
    # closure). { directRepairCost, estimatedDowntimeCost,
    #   investigationLaborCost, estimatedInsuranceImpact, totalCost,
    #   costConfidence }
    costImpact: Mapped[dict | None] = mapped_column(JSON)

    # ─── Phase 2 / Statutory ───
    isReportable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reportableUnder: Mapped[list | None] = mapped_column(JSON)
    statutoryDeadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ─── Phase 3: Investigation team lead ───
    investigationTeamLead: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    investigationCharterDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ─── Phase 5: structured cause hierarchy ───
    immediateCauses: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    underlyingCauses: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    rootCauses: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    contributingFactors: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    # ─── Phase 7: Cost of incident ───
    costMedical: Mapped[float | None] = mapped_column(Numeric(14, 2))
    costPropertyDamage: Mapped[float | None] = mapped_column(Numeric(14, 2))
    costLostProduction: Mapped[float | None] = mapped_column(Numeric(14, 2))
    costInsurance: Mapped[float | None] = mapped_column(Numeric(14, 2))
    costLegalRegulatory: Mapped[float | None] = mapped_column(Numeric(14, 2))
    costOther: Mapped[float | None] = mapped_column(Numeric(14, 2))
    costTotal: Mapped[float | None] = mapped_column(Numeric(14, 2))

    # ─── Phase 8: Statutory submissions ───
    form18PreparedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    form18PreparedById: Mapped[str | None] = mapped_column(String)
    form18Submitted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    form18SubmissionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    form18SubmissionRef: Mapped[str | None] = mapped_column(String)
    dgfasliSubmitted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dgfasliSubmissionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cpcbSubmitted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cpcbSubmissionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    internalNotificationsSent: Mapped[dict | None] = mapped_column(JSON)

    # ─── Phase 9: Review approvals ───
    investigationReportSubmittedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hseManagerApprovedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hseManagerApprovedById: Mapped[str | None] = mapped_column(String)
    plantHeadApprovedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    plantHeadApprovedById: Mapped[str | None] = mapped_column(String)
    corporateHseApprovedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    corporateHseApprovedById: Mapped[str | None] = mapped_column(String)

    # ─── Phase 10: Closure ───
    closingRemark: Mapped[str | None] = mapped_column(Text)
    lessonsLearned: Mapped[str | None] = mapped_column(Text)
    lessonsDistributedTo: Mapped[dict | None] = mapped_column(JSON)
    closedById: Mapped[str | None] = mapped_column(String)

    # ─── 90-day Effectiveness Review ───
    effectivenessReviewDueAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effectivenessReviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effectivenessReviewedById: Mapped[str | None] = mapped_column(String)
    effectivenessRating: Mapped[int | None] = mapped_column(Integer)
    effectivenessNotes: Mapped[str | None] = mapped_column(Text)

    # ─── Cross-module linkages ───
    linkedObservationIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    linkedNearMissIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    linkedIncidentIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    triggeredCapaIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    contractorScoreImpact: Mapped[dict | None] = mapped_column(JSON)

    # Training trigger (incident_post_closure._rule_training_trigger)
    triggeredTrainingFor: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    triggeredTrainingKeywords: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    # ─── SLA tracking (per phase) ───
    initialReportSlaTargetAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    classificationSlaTargetAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    investigationSlaTargetAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capaSlaTargetAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ─── Child collections ───
    investigationTeam: Mapped[list[IncidentInvestigationMember]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    attachments: Mapped[list[IncidentAttachment]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    personsInvolved: Mapped[list[IncidentPerson]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    witnessStatements: Mapped[list[IncidentWitnessStatement]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    equipmentInvolved: Mapped[list[IncidentEquipment]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )

    # NOTE: the remaining 6 child models declared in Prisma —
    # IncidentReclassification, IncidentTimelineEvent, IncidentEvidence,
    # IncidentDocumentReview, IncidentCapa, IncidentComment — will be added
    # to this file in their respective commits (3, 4, 4, 4, 4, 6). Prisma
    # already created their tables; SQLAlchemy just doesn't read/write them
    # yet. That's fine — no foreign key on the parent points at them.


class IncidentInvestigationMember(Base, IdMixin):
    __tablename__ = "IncidentInvestigationMember"
    __table_args__ = (UniqueConstraint("incidentId", "userId", name="uq_incident_member"),)

    incidentId: Mapped[str] = mapped_column(ForeignKey("Incident.id", ondelete="CASCADE"), nullable=False)
    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String, nullable=False, default="MEMBER")  # LEAD | MEMBER

    incident: Mapped[Incident] = relationship(back_populates="investigationTeam")


class IncidentAttachment(Base, IdMixin):
    __tablename__ = "IncidentAttachment"

    incidentId: Mapped[str] = mapped_column(ForeignKey("Incident.id", ondelete="CASCADE"), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    capaRef: Mapped[str | None] = mapped_column(String)
    witnessRef: Mapped[str | None] = mapped_column(String)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    storagePath: Mapped[str] = mapped_column(String, nullable=False)
    fileSize: Mapped[int] = mapped_column(Integer, nullable=False)
    mimeType: Mapped[str] = mapped_column(String, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)
    exifData: Mapped[dict | None] = mapped_column(JSON)
    uploadedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    uploadedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    incident: Mapped[Incident] = relationship(back_populates="attachments")
    uploadedBy: Mapped[User] = relationship(foreign_keys=[uploadedById], lazy="joined")


# ═══════════════════════════════════════════════════════════════════════
#  Commit 2 child models — Phase 1 first-responder capture needs these.
# ═══════════════════════════════════════════════════════════════════════


class IncidentPerson(Base, IdMixin):
    """Persons involved in the incident — replaces the single
    `injuredPerson*` columns when an event involves multiple people. Each
    row is one person + their role (victim / injured / witness / responder /
    operator / supervisor) and any injury detail. The Phase 1 form lets
    the first responder add as many as needed."""

    __tablename__ = "IncidentPerson"

    incidentId: Mapped[str] = mapped_column(
        ForeignKey("Incident.id", ondelete="CASCADE"), nullable=False, index=True
    )
    userId: Mapped[str | None] = mapped_column(ForeignKey("User.id"), index=True)
    externalName: Mapped[str | None] = mapped_column(String)
    externalContact: Mapped[str | None] = mapped_column(String)

    role: Mapped[str] = mapped_column(String, nullable=False)  # VICTIM | INJURED | WITNESS | RESPONDER | OPERATOR | SUPERVISOR

    isContractor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    contractorCompanyId: Mapped[str | None] = mapped_column(ForeignKey("ContractorCompany.id"), index=True)

    isInjured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    bodyPartAffected: Mapped[str | None] = mapped_column(String)
    natureOfInjury: Mapped[str | None] = mapped_column(String)
    injurySeverity: Mapped[str | None] = mapped_column(String)  # MINOR | MAJOR | FATAL
    treatment: Mapped[str | None] = mapped_column(Text)
    hospitalName: Mapped[str | None] = mapped_column(String)
    daysOff: Mapped[int | None] = mapped_column(Integer)
    daysRestricted: Mapped[int | None] = mapped_column(Integer)
    returnToWorkDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    isFitForDuty: Mapped[bool | None] = mapped_column(Boolean)

    ppeWornAtTime: Mapped[dict | None] = mapped_column(JSON)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    incident: Mapped[Incident] = relationship(back_populates="personsInvolved")
    user: Mapped[User | None] = relationship(foreign_keys=[userId])


class IncidentWitnessStatement(Base, IdMixin):
    """Formal witness statements. Phase 1 captures just the witness name +
    role — the full statement text / signed PDF / audio recording are added
    later during Phase 3 investigation."""

    __tablename__ = "IncidentWitnessStatement"

    incidentId: Mapped[str] = mapped_column(
        ForeignKey("Incident.id", ondelete="CASCADE"), nullable=False, index=True
    )
    witnessUserId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    witnessName: Mapped[str] = mapped_column(String, nullable=False)
    witnessRole: Mapped[str | None] = mapped_column(String)
    statementText: Mapped[str | None] = mapped_column(Text)
    statementFileUrl: Mapped[str | None] = mapped_column(String)
    audioRecordingUrl: Mapped[str | None] = mapped_column(String)
    takenById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    takenAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    language: Mapped[str | None] = mapped_column(String)

    incident: Mapped[Incident] = relationship(back_populates="witnessStatements")


class IncidentEquipment(Base, IdMixin):
    """Equipment involved in the incident with damage assessment + repair
    status. Multiple equipment can be linked per incident (e.g. crane +
    load + power source)."""

    __tablename__ = "IncidentEquipment"

    incidentId: Mapped[str] = mapped_column(
        ForeignKey("Incident.id", ondelete="CASCADE"), nullable=False, index=True
    )
    equipmentId: Mapped[str] = mapped_column(ForeignKey("Equipment.id"), nullable=False, index=True)
    involvement: Mapped[str] = mapped_column(String, nullable=False)
    damageEstimate: Mapped[float | None] = mapped_column(Numeric(14, 2))
    repairStatus: Mapped[str | None] = mapped_column(String)

    incident: Mapped[Incident] = relationship(back_populates="equipmentInvolved")


class IncidentReclassification(Base, IdMixin):
    """Audit log for severity / type reclassifications.

    The most common case is MTC → LTI when a worker doesn't return after
    the expected days. Each row is immutable once written; the engine reads
    it to recompute statutory deadlines, escalate notifications, and (if
    needed) trigger urgent retroactive Form 18 submission when an LTI was
    misclassified as MTC and the 24-hour window has lapsed."""

    __tablename__ = "IncidentReclassification"

    incidentId: Mapped[str] = mapped_column(
        ForeignKey("Incident.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fromType: Mapped[str] = mapped_column(String, nullable=False)
    toType: Mapped[str] = mapped_column(String, nullable=False)
    fromSeverity: Mapped[str | None] = mapped_column(String)
    toSeverity: Mapped[str | None] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reclassifiedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reclassifiedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    triggersStatutoryUpdate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    incident: Mapped[Incident] = relationship()


# ═══════════════════════════════════════════════════════════════════════
#  Commit 4 child models — Phase 3 investigation form needs these.
# ═══════════════════════════════════════════════════════════════════════


class IncidentTimelineEvent(Base, IdMixin):
    """Chronological reconstruction of the incident. Investigators add
    entries from witnesses, CCTV, equipment data, document reviews. The
    detail-page timeline view orders rows by `sequence`."""

    __tablename__ = "IncidentTimelineEvent"

    incidentId: Mapped[str] = mapped_column(
        ForeignKey("Incident.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)  # WITNESS | CCTV | EQUIPMENT_DATA | INTERVIEW | DOCUMENT
    sourceReference: Mapped[str | None] = mapped_column(String)


class IncidentEvidence(Base, IdMixin):
    """Evidence collection — distinct from generic IncidentAttachment because
    evidence carries chain-of-custody fields (collectedBy + collectedAt +
    preservedFor) for legal defensibility. The `fileUrl` is a Supabase
    storage path, not a public URL."""

    __tablename__ = "IncidentEvidence"

    incidentId: Mapped[str] = mapped_column(
        ForeignKey("Incident.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    fileUrl: Mapped[str | None] = mapped_column(String)
    fileName: Mapped[str | None] = mapped_column(String)
    fileSize: Mapped[int | None] = mapped_column(Integer)
    mimeType: Mapped[str | None] = mapped_column(String)
    collectedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    collectedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    preservedFor: Mapped[str | None] = mapped_column(String)


class IncidentDocumentReview(Base, IdMixin):
    """Documents reviewed during investigation (SOPs, permits, training
    records, inspections, MOC, PSM). Each review carries a compliance
    verdict that drives root-cause analysis."""

    __tablename__ = "IncidentDocumentReview"

    incidentId: Mapped[str] = mapped_column(
        ForeignKey("Incident.id", ondelete="CASCADE"), nullable=False, index=True
    )
    documentType: Mapped[str] = mapped_column(String, nullable=False)
    documentReference: Mapped[str] = mapped_column(String, nullable=False)
    documentLinkId: Mapped[str | None] = mapped_column(String)
    reviewNotes: Mapped[str | None] = mapped_column(Text)
    complianceFinding: Mapped[str | None] = mapped_column(String)


class IncidentCapa(Base, IdMixin):
    """Multiple CAPAs per incident. Each has its own owner, target date,
    evidence, status, and 90-day effectiveness rating. Drives the
    `CAPA_FAN_OUT` workflow step where the engine spawns one assignee task
    per CAPA in parallel."""

    __tablename__ = "IncidentCapa"

    incidentId: Mapped[str] = mapped_column(
        ForeignKey("Incident.id", ondelete="CASCADE"), nullable=False, index=True
    )
    capaNumber: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)  # CORRECTIVE | PREVENTIVE
    rootCauseAddressed: Mapped[str | None] = mapped_column(String)
    # Feature 1 — back-link to the RCA cause node this CAPA was raised from
    # (the node inside rootCauseData carries the reverse `linkedCapaId`).
    linkedCauseId: Mapped[str | None] = mapped_column(String)
    ownerId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    targetDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING", index=True)
    evidenceUrls: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    evidenceDescription: Mapped[str | None] = mapped_column(Text)
    beforePhotoUrl: Mapped[str | None] = mapped_column(String)
    afterPhotoUrl: Mapped[str | None] = mapped_column(String)
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verifiedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    verifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effectivenessRating: Mapped[int | None] = mapped_column(Integer)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


class IncidentComment(Base, IdMixin):
    """Threaded comments. `isPrivilegedLegal` marks sensitive comments
    visible only to investigation team / HSE Manager / Plant Head /
    Corporate HSE / Legal — workers, supervisors, contractors cannot
    see them."""

    __tablename__ = "IncidentComment"

    incidentId: Mapped[str] = mapped_column(
        ForeignKey("Incident.id", ondelete="CASCADE"), nullable=False, index=True
    )
    authorId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    isPrivilegedLegal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
