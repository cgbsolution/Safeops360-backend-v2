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


class PermitType(str, enum.Enum):
    HOT_WORK = "HOT_WORK"
    CONFINED_SPACE = "CONFINED_SPACE"
    WORK_AT_HEIGHT = "WORK_AT_HEIGHT"
    EXCAVATION = "EXCAVATION"
    ELECTRICAL_LOTO = "ELECTRICAL_LOTO"
    LIFTING = "LIFTING"
    GENERAL_COLD = "GENERAL_COLD"


class PermitStatus(str, enum.Enum):
    """Closed-loop PTW state machine:

        DRAFT → SUBMITTED (pending approval chain) → APPROVED → ISSUED →
        [receiver accepts] → ACTIVE (work in progress) →
        (SUSPENDED loop) → WORK_COMPLETED (receiver declares + outcome) →
        HANDBACK_INSPECTION (site walked + verified) → CLOSED (+ isArchived flag)

    CANCELLED is reachable pre-ACTIVE by the issuer/originator and from
    ACTIVE/SUSPENDED by HSE/Admin. REJECTED is an approver refusal during
    the approval chain.

    ISSUER_APPROVED / SAFETY_APPROVED / PLANT_HEAD_APPROVED are DEPRECATED —
    retained only because they exist on the native Postgres enum; the engine
    never writes them (per-step approval facts live on PermitApproval rows +
    the legacy *ApprovedAt timestamp columns)."""

    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    ISSUER_APPROVED = "ISSUER_APPROVED"        # deprecated — never written
    SAFETY_APPROVED = "SAFETY_APPROVED"        # deprecated — never written
    PLANT_HEAD_APPROVED = "PLANT_HEAD_APPROVED"  # deprecated — never written
    APPROVED = "APPROVED"
    ISSUED = "ISSUED"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    WORK_COMPLETED = "WORK_COMPLETED"
    HANDBACK_INSPECTION = "HANDBACK_INSPECTION"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class PermitOutcome(str, enum.Enum):
    """Structured close-out outcome, declared by the receiver at the
    Work Completed step. Stored as TEXT in Postgres (no native enum)."""

    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    STOPPED_INCIDENT = "STOPPED_INCIDENT"
    CANCELLED = "CANCELLED"


class PermitEvidenceAction(str, enum.Enum):
    """Action points in the permit lifecycle that capture field evidence
    (GPS + photo + signature). One PermitActionEvidence row per action.
    Stored as TEXT in Postgres."""

    APPROVE_ISSUER = "APPROVE_ISSUER"
    APPROVE_SAFETY = "APPROVE_SAFETY"
    APPROVE_PLANT_HEAD = "APPROVE_PLANT_HEAD"
    APPROVE = "APPROVE"            # custom/extra approval steps in bespoke workflows
    ISSUE = "ISSUE"                # manual-issue mode only (auto-issue records none)
    ACCEPT = "ACCEPT"
    ISOLATION_VERIFY = "ISOLATION_VERIFY"
    SUSPEND = "SUSPEND"
    RESUME = "RESUME"
    EXTEND = "EXTEND"
    WORK_COMPLETED_DECLARE = "WORK_COMPLETED_DECLARE"
    HANDBACK_INSPECT = "HANDBACK_INSPECT"
    CLOSE = "CLOSE"
    CANCEL = "CANCEL"
    REJECT = "REJECT"


class Permit(Base, IdMixin, SoftDeleteMixin):
    """SQLAlchemy mirror of the Prisma `Permit` model.

    Production-depth refactor (Commit 1) added ~22 new columns + child
    collections. Most new columns are nullable, so existing rows remain
    valid. The SQLAlchemy enum for status is `native_enum=False` but the
    Postgres column IS a native enum (created by Prisma) — only write
    values from `PermitStatus`."""

    __tablename__ = "Permit"

    # ─── Core (existing) ───
    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    type: Mapped[PermitType] = mapped_column(Enum(PermitType, name="PermitType", native_enum=False), nullable=False)
    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False)
    areaId: Mapped[str | None] = mapped_column(ForeignKey("Area.id"))
    location: Mapped[str] = mapped_column(String, nullable=False)
    scopeOfWork: Mapped[str] = mapped_column(Text, nullable=False)
    validFrom: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    validTo: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    originatorId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    issuerId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    receiverId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    contractorName: Mapped[str | None] = mapped_column(String)
    # Structured contractor link (kept alongside the legacy free-text
    # contractorName). Nullable FK; added by apply-contractor-links-ddl.ts.
    contractorCompanyId: Mapped[str | None] = mapped_column(ForeignKey("ContractorCompany.id"), index=True)

    # Provenance — set when this permit was raised from a HIRA hazard row via
    # the Create-PTW prompt. Both NULL for permits raised independently, so
    # presence of hiraEntryId is the "HIRA-driven permit" signal.
    hiraEntryId: Mapped[str | None] = mapped_column(ForeignKey("HiraEntry.id", ondelete="SET NULL"), index=True)
    hiraEntryHazardId: Mapped[str | None] = mapped_column(
        ForeignKey("HiraEntryHazard.id", ondelete="SET NULL")
    )

    isolationsRequired: Mapped[str | None] = mapped_column(Text)
    ppeChecklist: Mapped[str | None] = mapped_column(Text)
    gasTestRequired: Mapped[bool] = mapped_column(Boolean, default=False)
    gasTestResult: Mapped[str | None] = mapped_column(String)
    o2Level: Mapped[str | None] = mapped_column(String)
    lelLevel: Mapped[str | None] = mapped_column(String)
    h2sLevel: Mapped[str | None] = mapped_column(String)
    fireWatchRequired: Mapped[bool] = mapped_column(Boolean, default=False)
    rescuePlan: Mapped[str | None] = mapped_column(Text)

    issuerApprovedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safetyApprovedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    plantHeadApprovedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[PermitStatus] = mapped_column(
        Enum(PermitStatus, name="PermitStatus", native_enum=False), nullable=False, default=PermitStatus.DRAFT
    )
    rejectionReason: Mapped[str | None] = mapped_column(Text)
    suspendedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspendedReason: Mapped[str | None] = mapped_column(Text)
    expiredAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )

    # ═══════════════════════════════════════════════════════════════════
    #  Production-depth refactor — Commit 1 schema additions
    # ═══════════════════════════════════════════════════════════════════

    validityHours: Mapped[int | None] = mapped_column(Integer)
    workflowDefinitionCode: Mapped[str | None] = mapped_column(String)

    departmentId: Mapped[str | None] = mapped_column(ForeignKey("Department.id"))
    specificLocation: Mapped[str | None] = mapped_column(String)
    # Creation-time GPS captured by the originator's device. Action-level GPS
    # (approve / accept / complete / …) lives on PermitActionEvidence rows —
    # never overwrite these two columns after creation.
    gpsLatitude: Mapped[float | None] = mapped_column(Float)
    gpsLongitude: Mapped[float | None] = mapped_column(Float)
    workOrderNumber: Mapped[str | None] = mapped_column(String)
    # DEPRECATED: dangling string ids from before PermitAttachment was wired.
    # Kept mapped for old rows; new code writes PermitAttachment rows instead.
    attachedDrawingIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    fireWatchPersonId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    standbyPersonId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))

    weatherConditionsAtIssue: Mapped[str | None] = mapped_column(String)
    windSpeedKmh: Mapped[float | None] = mapped_column(Float)
    adjacentAreaNotifications: Mapped[dict | None] = mapped_column(JSON)

    # ─── Issue phase (APPROVED → ISSUED, auto-fired after final approval) ───
    issuedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issuedById: Mapped[str | None] = mapped_column(String)

    # ─── FLRA policy (closed-loop rebuild): FLRA is a conditional sub-flow.
    #     Resolved at creation from settings (PTW_FLRA_REQUIRED_DEFAULT /
    #     PTW_FLRA_REQUIRED_TYPES) or an explicit wizard override; snapshotted
    #     here so the workflow + activation gate are auditable per permit. ───
    flraRequired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    activatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activatedById: Mapped[str | None] = mapped_column(String)
    currentActiveFlraId: Mapped[str | None] = mapped_column(String)
    isCurrentlySuspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ─── Work Completed declaration (receiver) ───
    # `returnedAt`/`returnedById` are the legacy names for the same fact and
    # are kept in lockstep (returnedAt == workCompletedAt) for old read-sites.
    workCompletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workCompletedById: Mapped[str | None] = mapped_column(String)
    outcome: Mapped[PermitOutcome | None] = mapped_column(
        Enum(PermitOutcome, name="PermitOutcome", native_enum=False)
    )
    returnedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    returnedById: Mapped[str | None] = mapped_column(String)
    returnNotes: Mapped[str | None] = mapped_column(Text)
    # DEPRECATED: photos now live on PermitAttachment (category RETURN_PHOTO).
    returnPhotos: Mapped[dict | None] = mapped_column(JSON)

    # ─── Handback inspection (legacy name: site verification) ───
    siteVerifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    siteVerifiedById: Mapped[str | None] = mapped_column(String)
    siteVerificationChecklist: Mapped[dict | None] = mapped_column(JSON)
    # DEPRECATED: photos now live on PermitAttachment (category VERIFICATION_PHOTO).
    siteVerificationPhotos: Mapped[dict | None] = mapped_column(JSON)

    closedById: Mapped[str | None] = mapped_column(String)
    closingRemark: Mapped[str | None] = mapped_column(Text)

    # ─── Cancellation (distinct from REJECTED — see PermitStatus docstring) ───
    cancelledAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelledById: Mapped[str | None] = mapped_column(String)
    cancellationReason: Mapped[str | None] = mapped_column(Text)

    # ─── Archive (retention flag layered on CLOSED — not a lifecycle state) ───
    isArchived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    archivedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    autoExpiredAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expirationReason: Mapped[str | None] = mapped_column(String)

    triggeredObservations: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    triggeredIncidentId: Mapped[str | None] = mapped_column(String)
    conflictingPermitIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    # ─── Child collections ───
    workCrew: Mapped[list[PermitCrewMember]] = relationship(
        back_populates="permit", cascade="all, delete-orphan"
    )
    isolations: Mapped[list[PermitIsolation]] = relationship(
        back_populates="permit", cascade="all, delete-orphan"
    )
    toolsEquipment: Mapped[list[PermitToolEquipment]] = relationship(
        back_populates="permit", cascade="all, delete-orphan"
    )
    subjectEquipment: Mapped[list[PermitSubjectEquipment]] = relationship(
        back_populates="permit", cascade="all, delete-orphan"
    )
    gasTestPlan: Mapped[PermitGasTestPlan | None] = relationship(
        back_populates="permit", cascade="all, delete-orphan", uselist=False
    )
    gasTestReadings: Mapped[list[PermitGasTestReading]] = relationship(
        back_populates="permit", cascade="all, delete-orphan"
    )
    approvals: Mapped[list[PermitApproval]] = relationship(
        back_populates="permit", cascade="all, delete-orphan"
    )
    suspensions: Mapped[list[PermitSuspension]] = relationship(
        back_populates="permit", cascade="all, delete-orphan"
    )
    extensions: Mapped[list[PermitExtension]] = relationship(
        back_populates="permit", cascade="all, delete-orphan"
    )
    attachments: Mapped[list[PermitAttachment]] = relationship(
        back_populates="permit", cascade="all, delete-orphan"
    )
    actionEvidence: Mapped[list[PermitActionEvidence]] = relationship(
        back_populates="permit", cascade="all, delete-orphan"
    )


class PermitCrewMember(Base, IdMixin):
    __tablename__ = "PermitCrewMember"
    __table_args__ = (UniqueConstraint("permitId", "userId", name="uq_permit_crew"),)

    permitId: Mapped[str] = mapped_column(ForeignKey("Permit.id", ondelete="CASCADE"), nullable=False)
    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String, nullable=False, default="WORKER")
    addedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # ─── Validity snapshot at issuance (Commit 1 additions) ───
    trainingValidAtIssuance: Mapped[bool | None] = mapped_column(Boolean)
    trainingValidationNotes: Mapped[str | None] = mapped_column(String)
    medicalValidAtIssuance: Mapped[bool | None] = mapped_column(Boolean)
    contractorActiveAtIssuance: Mapped[bool | None] = mapped_column(Boolean)
    # PPE snapshot at crew add (PPE-01 Pass 2). Audit only — the activation
    # gate re-checks PPE live because issuance state moves fast.
    ppeValidAtIssuance: Mapped[bool | None] = mapped_column(Boolean)
    ppeValidationNotes: Mapped[str | None] = mapped_column(String)

    removedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removalReason: Mapped[str | None] = mapped_column(String)

    permit: Mapped[Permit] = relationship(back_populates="workCrew")


# ═══════════════════════════════════════════════════════════════════════
#  Permit child models — production-depth refactor, Commit 2
# ═══════════════════════════════════════════════════════════════════════


class PermitIsolation(Base, IdMixin):
    """One row per isolation point. Replaces the legacy `isolationsRequired`
    CSV/text field. Verified by Issuer pre-activation, restored by Receiver
    during permit return."""

    __tablename__ = "PermitIsolation"

    permitId: Mapped[str] = mapped_column(
        ForeignKey("Permit.id", ondelete="CASCADE"), nullable=False, index=True
    )
    isolationType: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    isolationPointTag: Mapped[str] = mapped_column(String, nullable=False)
    lotoTagNumber: Mapped[str | None] = mapped_column(String)
    isolationVerifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    isolationVerifiedById: Mapped[str | None] = mapped_column(String)
    restoredAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restoredById: Mapped[str | None] = mapped_column(String)

    permit: Mapped[Permit] = relationship(back_populates="isolations")


class PermitToolEquipment(Base, IdMixin):
    """Tools / equipment used during the permit. Each has its inspection
    currency snapshotted at issuance — adding a tool with overdue
    inspection blocks the permit form (enforced application-side)."""

    __tablename__ = "PermitToolEquipment"

    permitId: Mapped[str] = mapped_column(
        ForeignKey("Permit.id", ondelete="CASCADE"), nullable=False, index=True
    )
    equipmentId: Mapped[str | None] = mapped_column(ForeignKey("Equipment.id"))
    freeTextDescription: Mapped[str | None] = mapped_column(String)
    inspectionCurrentAtIssuance: Mapped[bool] = mapped_column(Boolean, default=False)

    permit: Mapped[Permit] = relationship(back_populates="toolsEquipment")


class PermitSubjectEquipment(Base, IdMixin):
    """Equipment being worked on (subject of the work). Distinct from
    `toolsEquipment` which is what the crew uses. Drives Equipment
    History updates in the post-closure rules engine (Commit 8)."""

    __tablename__ = "PermitSubjectEquipment"

    permitId: Mapped[str] = mapped_column(
        ForeignKey("Permit.id", ondelete="CASCADE"), nullable=False, index=True
    )
    equipmentId: Mapped[str] = mapped_column(ForeignKey("Equipment.id"), nullable=False, index=True)
    workNature: Mapped[str] = mapped_column(String, nullable=False)

    permit: Mapped[Permit] = relationship(back_populates="subjectEquipment")


class PermitGasTestPlan(Base, IdMixin):
    """Gas test plan (1-to-1 with Permit). Captured at permit issuance for
    permit types that require it (Confined Space always, Hot Work in fuel
    areas). Refresh tasks for active permits read from this row's
    refreshFrequencyMinutes (Commit 5)."""

    __tablename__ = "PermitGasTestPlan"

    permitId: Mapped[str] = mapped_column(
        ForeignKey("Permit.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    refreshFrequencyMinutes: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    parametersToTest: Mapped[list | None] = mapped_column(JSON)
    instrumentSerial: Mapped[str | None] = mapped_column(String)
    instrumentLastCalibrated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    permit: Mapped[Permit] = relationship(back_populates="gasTestPlan")


# ═══════════════════════════════════════════════════════════════════════
#  Permit child models — production-depth refactor, Commit 5
#  (active-phase: gas readings + approvals audit + suspensions + extensions)
# ═══════════════════════════════════════════════════════════════════════


class PermitGasTestReading(Base, IdMixin):
    """Per-reading log captured during the active permit. Each row is a
    snapshot of all parameters in `readings`. `isExceedance` is computed
    against `PermitGasTestPlan.parametersToTest`. When True, the gas-test
    service auto-suspends the permit and records the reason."""

    __tablename__ = "PermitGasTestReading"

    permitId: Mapped[str] = mapped_column(
        ForeignKey("Permit.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recordedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    recordedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    readings: Mapped[list | None] = mapped_column(JSON)
    isExceedance: Mapped[bool] = mapped_column(Boolean, default=False)
    exceedanceAction: Mapped[str | None] = mapped_column(String)
    instrumentSerial: Mapped[str | None] = mapped_column(String)
    isPreEntry: Mapped[bool] = mapped_column(Boolean, default=False)
    refreshDueBy: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    permit: Mapped[Permit] = relationship(back_populates="gasTestReadings")


class PermitApproval(Base, IdMixin):
    """Per-approval audit log. The legacy `*ApprovedAt` columns on Permit
    stay for fast reads, but every approval also creates a row here."""

    __tablename__ = "PermitApproval"

    permitId: Mapped[str] = mapped_column(
        ForeignKey("Permit.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step: Mapped[str] = mapped_column(String, nullable=False)
    approverId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    comments: Mapped[str | None] = mapped_column(Text)
    conditions: Mapped[str | None] = mapped_column(Text)
    decidedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    permit: Mapped[Permit] = relationship(back_populates="approvals")


class PermitSuspension(Base, IdMixin):
    """Each suspension/resumption cycle gets its own row. Multiple
    suspensions per permit allowed. The legacy single fields on Permit
    stay for back-compat reads."""

    __tablename__ = "PermitSuspension"

    permitId: Mapped[str] = mapped_column(
        ForeignKey("Permit.id", ondelete="CASCADE"), nullable=False, index=True
    )
    suspendedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    suspendedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    reasonDetail: Mapped[str | None] = mapped_column(Text)
    resumedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resumedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    resumptionConditions: Mapped[str | None] = mapped_column(Text)
    reFlraRequired: Mapped[bool] = mapped_column(Boolean, default=True)

    permit: Mapped[Permit] = relationship(back_populates="suspensions")


class PermitAttachment(Base, IdMixin):
    """Generic file attachment — drawings, MSDS, action-evidence photos,
    return/verification photos, rescue plans, TPI certs. Table was created
    by the original Prisma push (schema.prisma `PermitAttachment`); this
    mapping + the upload endpoints wire it up on the Python side.

    `actionEvidenceId` (added by apply-ptw-closed-loop-ddl.ts) links a photo
    to the specific lifecycle action it evidences; NULL for general permit
    attachments (drawings, MSDS…)."""

    __tablename__ = "PermitAttachment"

    permitId: Mapped[str] = mapped_column(
        ForeignKey("Permit.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actionEvidenceId: Mapped[str | None] = mapped_column(
        ForeignKey("PermitActionEvidence.id"), index=True
    )
    category: Mapped[str] = mapped_column(String, nullable=False)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    storagePath: Mapped[str] = mapped_column(String, nullable=False)
    fileSize: Mapped[int] = mapped_column(Integer, nullable=False)
    mimeType: Mapped[str] = mapped_column(String, nullable=False)
    caption: Mapped[str | None] = mapped_column(String)
    uploadedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    uploadedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    permit: Mapped[Permit] = relationship(back_populates="attachments")
    actionEvidence: Mapped[PermitActionEvidence | None] = relationship(
        back_populates="photos"
    )


class PermitActionEvidence(Base, IdMixin):
    """Field evidence captured at each permit lifecycle action — the
    closed-loop audit record: WHO did WHAT, WHERE (GPS), WHEN, with an
    onsite photo (via PermitAttachment.actionEvidenceId) and a drawn
    signature. One row per action per permit (approvals, accept,
    suspend/resume/extend, work-completed, handback, close, cancel…).

    The close-out report renders this table as the permit's evidence
    timeline. Registered with the tamper-evident audit hash-chain."""

    __tablename__ = "PermitActionEvidence"

    permitId: Mapped[str] = mapped_column(
        ForeignKey("Permit.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action: Mapped[PermitEvidenceAction] = mapped_column(
        Enum(PermitEvidenceAction, name="PermitEvidenceAction", native_enum=False),
        nullable=False,
    )
    actorId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    gpsLatitude: Mapped[float | None] = mapped_column(Float)
    gpsLongitude: Mapped[float | None] = mapped_column(Float)
    # Useful in audit disputes — captured when the device provides it.
    gpsAccuracyMeters: Mapped[float | None] = mapped_column(Float)
    capturedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Drawn signature (data-URL PNG, ~5-20 KB). Stored inline: small, immutable,
    # and must survive even if the storage bucket is repointed.
    signatureImageBase64: Mapped[str | None] = mapped_column(Text)
    # Free text the actor confirmed, e.g. "I confirm the site has been
    # inspected and is safe to hand back."
    declarationText: Mapped[str | None] = mapped_column(Text)
    comments: Mapped[str | None] = mapped_column(Text)

    permit: Mapped[Permit] = relationship(back_populates="actionEvidence")
    photos: Mapped[list[PermitAttachment]] = relationship(
        back_populates="actionEvidence"
    )


class PermitExtension(Base, IdMixin):
    """Validity extension. Original validTo stays on Permit; if approved
    the Permit.validTo is updated AND a row here is kept as audit."""

    __tablename__ = "PermitExtension"

    permitId: Mapped[str] = mapped_column(
        ForeignKey("Permit.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requestedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    requestedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    newValidTo: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approvedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    approverComments: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")

    permit: Mapped[Permit] = relationship(back_populates="extensions")
