from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin
from app.models.observation import Severity


class NearMissStatus(str, enum.Enum):
    """Mirrors the Prisma `NearMissStatus` enum (the source of truth for the
    Postgres native enum type). Keep this exactly aligned with the Prisma
    schema — values declared here that are absent from the DB enum will
    produce InvalidTextRepresentationError at write time. The "promoted to
    incident" state is conveyed via the `promotedToIncident` boolean +
    `promotedIncidentId` FK on the NearMiss row, not by a status value."""

    REPORTED = "REPORTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    ACTION_ASSIGNED = "ACTION_ASSIGNED"
    CLOSED = "CLOSED"


class NearMiss(Base, IdMixin):
    """Mirror of the Prisma NearMiss model. Fields match column names
    one-for-one; only the columns Python actually reads / writes are
    declared (the workflow engine, the post-closure rules service, the
    detail-page READ endpoints). Future commits that wire up the new
    UI / agents will read more of these as they're touched."""

    __tablename__ = "NearMiss"

    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False)
    areaId: Mapped[str | None] = mapped_column(ForeignKey("Area.id"))
    reporterId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # Location / context (new)
    location: Mapped[str | None] = mapped_column(String)  # legacy free-text
    specificLocation: Mapped[str | None] = mapped_column(String)
    gpsLatitude: Mapped[float | None] = mapped_column(Float)
    gpsLongitude: Mapped[float | None] = mapped_column(Float)

    departmentId: Mapped[str | None] = mapped_column(ForeignKey("Department.id"))
    shiftId: Mapped[str | None] = mapped_column(String)  # FK by id to MasterItem(type=SHIFT)

    reporterType: Mapped[str | None] = mapped_column(String)
    isAnonymous: Mapped[bool] = mapped_column(Boolean, default=False)

    # Activity
    activityBeingPerformed: Mapped[str | None] = mapped_column(String)
    activityIsRoutine: Mapped[bool | None] = mapped_column(Boolean)
    activity: Mapped[str | None] = mapped_column(String)  # legacy free-text
    immediateAction: Mapped[str | None] = mapped_column(Text)

    # Equipment / contractor
    equipmentId: Mapped[str | None] = mapped_column(ForeignKey("Equipment.id"))
    contractorCompanyId: Mapped[str | None] = mapped_column(ForeignKey("ContractorCompany.id"))

    # Severity & consequence
    potentialSeverity: Mapped[Severity] = mapped_column(
        Enum(Severity, name="Severity", native_enum=False), nullable=False
    )
    potentialConsequences: Mapped[list | None] = mapped_column(JSON)
    potentialConsequence: Mapped[str | None] = mapped_column(String)  # legacy CSV
    multipleWorkersAggravator: Mapped[bool] = mapped_column(Boolean, default=False)

    # Hazard
    hazardCategory: Mapped[str | None] = mapped_column(String)
    energySource: Mapped[str | None] = mapped_column(String)

    # Risk matrix
    riskLikelihood: Mapped[int | None] = mapped_column(Integer)
    riskConsequence: Mapped[int | None] = mapped_column(Integer)
    riskScore: Mapped[int | None] = mapped_column(Integer)
    riskLevel: Mapped[str | None] = mapped_column(String)

    # Reporter root-cause hint + barriers
    initialRootCauseCategory: Mapped[str | None] = mapped_column(String)
    controlsThatFailed: Mapped[str | None] = mapped_column(Text)
    controlsThatWorked: Mapped[str | None] = mapped_column(Text)

    # Reporter recommendation
    recommendedActions: Mapped[str | None] = mapped_column(Text)
    suggestedActionOwnerId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))

    # Transitional single-CAPA fields (kept until parallel-CAPA workflow lands)
    rootCauseCategory: Mapped[str | None] = mapped_column(String)
    rootCauseDetail: Mapped[str | None] = mapped_column(Text)
    correctiveActions: Mapped[str | None] = mapped_column(Text)
    actionOwnerId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    targetDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Auto-detection flags
    isRepeat: Mapped[bool] = mapped_column(Boolean, default=False)
    activePermitId: Mapped[str | None] = mapped_column(ForeignKey("Permit.id"))
    permitReviewFlagged: Mapped[bool] = mapped_column(Boolean, default=False)

    # Auto-promotion
    autoPromoteToIncident: Mapped[bool] = mapped_column(Boolean, default=False)
    promotedToIncident: Mapped[bool] = mapped_column(Boolean, default=False)
    promotedIncidentId: Mapped[str | None] = mapped_column(ForeignKey("Incident.id"))
    promotedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Joint Review (parallel)
    reviewByHseManagerId: Mapped[str | None] = mapped_column(String)
    reviewByHseManagerAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewBySectionHeadId: Mapped[str | None] = mapped_column(String)
    reviewBySectionHeadAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewerNotes: Mapped[str | None] = mapped_column(Text)
    refinedRootCauseCategory: Mapped[str | None] = mapped_column(String)

    # Verification & effectiveness
    verificationMethod: Mapped[str | None] = mapped_column(String)
    verificationNotes: Mapped[str | None] = mapped_column(Text)
    verifiedById: Mapped[str | None] = mapped_column(String)
    verifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effectivenessRating: Mapped[int | None] = mapped_column(Integer)

    # Closure
    closingRemark: Mapped[str | None] = mapped_column(Text)
    closedById: Mapped[str | None] = mapped_column(String)
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Lessons + cross-module trigger outputs
    lessonsLearned: Mapped[str | None] = mapped_column(Text)
    lessonsDistributedTo: Mapped[list | None] = mapped_column(JSON)
    triggeredInspectionId: Mapped[str | None] = mapped_column(String)
    triggeredTbtId: Mapped[str | None] = mapped_column(String)
    triggeredCapaId: Mapped[str | None] = mapped_column(String)
    triggeredPermitFlagId: Mapped[str | None] = mapped_column(String)
    # Audit log of post-closure cross-module triggers (Dimension 4).
    # Shape mirrors Observation.closureTriggers — see
    # app/services/post_closure_rules_nm.py.
    closureTriggers: Mapped[list | None] = mapped_column(JSON)

    # SLA
    slaTargetAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    slaActualClosedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    slaPerformance: Mapped[str | None] = mapped_column(String)

    status: Mapped[NearMissStatus] = mapped_column(
        Enum(NearMissStatus, name="NearMissStatus", native_enum=False),
        nullable=False,
        default=NearMissStatus.REPORTED,
    )
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
