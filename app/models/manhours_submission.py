"""Manhours submission — the IS 3786 monthly exposure return.

Separate module from `manhours.py` on purpose. That file holds the legacy flat
`Manhours` table (a single row of employeeHours + contractorHours). This is the
structured replacement the wizard writes: headcount and hours broken down by
category, statutory deductions, and the net exposure figure the KPIs divide by.

Why the distinction matters: the flat table's `employeeHours + contractorHours`
is a GROSS number. IS 3786 says the LTIFR/TRIR/severity denominator is exposure
hours NET of annual leave, sick leave, training and maternity. Dividing by the
gross figure silently understates every rate — the audit's "wrong denominator"
finding. `netExposureHours` below is the correct denominator, and it is frozen
into `kpiSnapshot` at lock time so a later reclassification of a source incident
cannot rewrite a KPI that has already been reported.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin

# The state machine the orchestrator drives. Note REJECTED is absent by
# design: a rejection returns the submission to DRAFT for correction rather
# than parking it in a terminal state, because the month still has to be
# reported.
SUBMISSION_STATUSES = (
    "DRAFT",
    "SUBMITTED",
    "UNDER_REVIEW",
    "APPROVED",
    "LOCKED",
    "UNLOCKED_FOR_REVISION",
)

# Statuses in which the wizard may still write. Everything else is read-only
# until Corporate HSE unlocks it.
EDITABLE_STATUSES = ("DRAFT", "UNLOCKED_FOR_REVISION")

CATEGORY_TYPES = ("PERMANENT", "CONTRACT", "TRAINEE")


class ManhoursSubmission(Base, IdMixin):
    __tablename__ = "ManhoursSubmission"
    __table_args__ = (
        # One return per plant per month — the natural key of the whole module.
        UniqueConstraint(
            "plantId", "reportingYear", "reportingMonth", name="uq_manhours_plant_period"
        ),
    )

    # MH-YYYY-PLANT-MM, assigned at the SUBMITTED transition. Stays null while
    # in DRAFT so a save-resume cycle can't collide on the unique index.
    submissionNumber: Mapped[str | None] = mapped_column(String, unique=True)

    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False, index=True)
    reportingYear: Mapped[int] = mapped_column(Integer, nullable=False)
    reportingMonth: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-12
    reportingPeriodStart: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reportingPeriodEnd: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # ── Aggregates, recomputed on every save ──
    totalManhoursPermanent: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    totalManhoursContract: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    totalManhoursTrainee: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    totalManhoursAll: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    totalEmployeeStrength: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    totalContractorStrength: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    totalDaysWorked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    totalShiftsWorked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── Deductions (IS 3786 — hours NOT counted toward exposure) ──
    hoursAnnualLeave: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    hoursSickLeave: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    hoursTraining: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    hoursMaternityLeave: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    hoursOther: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    hoursDeductionsTotal: Mapped[float] = mapped_column(Float, nullable=False, default=0)

    # totalManhoursAll − hoursDeductionsTotal. The KPI denominator.
    netExposureHours: Mapped[float] = mapped_column(Float, nullable=False, default=0)

    # Numerators + denominator + formula, frozen at LOCKED.
    kpiSnapshot: Mapped[dict | None] = mapped_column(JSON)

    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT", index=True)

    submittedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    submittedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submissionNotes: Mapped[str | None] = mapped_column(Text)

    reviewedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    reviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewerNotes: Mapped[str | None] = mapped_column(Text)
    # APPROVED | REJECTED | RETURNED_FOR_REVISION
    reviewDecision: Mapped[str | None] = mapped_column(String)

    lockedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    lockedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lockNotes: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    categories: Mapped[list["ManhoursEmployeeCategory"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    visitors: Mapped["ManhoursVisitorRecord | None"] = relationship(
        back_populates="submission", cascade="all, delete-orphan", uselist=False
    )
    unlockHistory: Mapped[list["ManhoursUnlockEvent"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    attachments: Mapped[list["ManhoursAttachment"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    comments: Mapped[list["ManhoursComment"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )


class ManhoursEmployeeCategory(Base, IdMixin):
    """Headcount + hours for one slice of the workforce.

    One relation discriminated by `categoryType` rather than three FK columns —
    the wizard and the aggregate engine both group on read, so a single table
    keeps that a GROUP BY instead of a union.
    """

    __tablename__ = "ManhoursEmployeeCategory"

    submissionId: Mapped[str] = mapped_column(
        ForeignKey("ManhoursSubmission.id", ondelete="CASCADE"), nullable=False, index=True
    )
    categoryType: Mapped[str] = mapped_column(String, nullable=False, index=True)

    departmentId: Mapped[str | None] = mapped_column(ForeignKey("Department.id"), index=True)
    # Loose FK to MasterItem(type=SHIFT) — shifts are semantically keyed by
    # `code` while MasterItem's PK is `id`, so this is deliberately unenforced
    # (same pattern as Incident.shiftId).
    shiftId: Mapped[str | None] = mapped_column(String)
    contractorCompanyId: Mapped[str | None] = mapped_column(
        ForeignKey("ContractorCompany.id"), index=True
    )

    averageHeadcount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    peakHeadcount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    endOfPeriodHeadcount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    regularHours: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    overtimeHours: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    totalHours: Mapped[float] = mapped_column(Float, nullable=False, default=0)

    notes: Mapped[str | None] = mapped_column(String)

    submission: Mapped[ManhoursSubmission] = relationship(back_populates="categories")


class ManhoursVisitorRecord(Base, IdMixin):
    """Visitor exposure. 1:1 because it is a single aggregate for the month,
    not a per-department breakdown."""

    __tablename__ = "ManhoursVisitorRecord"

    submissionId: Mapped[str] = mapped_column(
        ForeignKey("ManhoursSubmission.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    totalVisitorCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    totalVisitorHours: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    notableVisits: Mapped[str | None] = mapped_column(Text)

    submission: Mapped[ManhoursSubmission] = relationship(back_populates="visitors")


class ManhoursUnlockEvent(Base, IdMixin):
    """One row per unlock, paired with its re-lock.

    A locked return is a reported figure. Every reopening is therefore an
    audit event with a reason, and the diff captured at re-lock records what
    actually changed between the two locked states.
    """

    __tablename__ = "ManhoursUnlockEvent"

    submissionId: Mapped[str] = mapped_column(
        ForeignKey("ManhoursSubmission.id", ondelete="CASCADE"), nullable=False, index=True
    )
    unlockedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    unlockedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # { before: {...}, after: {...} } — written at re-lock.
    changeLog: Mapped[dict | None] = mapped_column(JSON)
    reLockedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reLockedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))

    submission: Mapped[ManhoursSubmission] = relationship(back_populates="unlockHistory")


class ManhoursAttachment(Base, IdMixin):
    """Source evidence for the return — attendance reports, payroll exports,
    contractor bills. What an inspector asks to see behind the numbers."""

    __tablename__ = "ManhoursAttachment"

    submissionId: Mapped[str] = mapped_column(
        ForeignKey("ManhoursSubmission.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # ATTENDANCE_REPORT | PAYROLL_EXPORT | HR_SYSTEM_EXPORT | CONTRACTOR_BILL |
    # STATUTORY_FORM | OTHER
    category: Mapped[str] = mapped_column(String, nullable=False)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    fileUrl: Mapped[str] = mapped_column(String, nullable=False)
    fileSize: Mapped[int] = mapped_column(Integer, nullable=False)
    mimeType: Mapped[str] = mapped_column(String, nullable=False)
    caption: Mapped[str | None] = mapped_column(String)
    uploadedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    uploadedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    submission: Mapped[ManhoursSubmission] = relationship(back_populates="attachments")


class ManhoursComment(Base, IdMixin):
    __tablename__ = "ManhoursComment"

    submissionId: Mapped[str] = mapped_column(
        ForeignKey("ManhoursSubmission.id", ondelete="CASCADE"), nullable=False, index=True
    )
    authorId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    submission: Mapped[ManhoursSubmission] = relationship(back_populates="comments")


__all__ = [
    "ManhoursSubmission",
    "ManhoursEmployeeCategory",
    "ManhoursVisitorRecord",
    "ManhoursUnlockEvent",
    "ManhoursAttachment",
    "ManhoursComment",
    "SUBMISSION_STATUSES",
    "EDITABLE_STATUSES",
    "CATEGORY_TYPES",
]
