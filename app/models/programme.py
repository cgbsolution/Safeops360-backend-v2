"""Annual Audit Programme — ISO 19011 clause 5, ISO 45001/9001/14001 clause 9.2.2.

Designed in [docs/cams/08-audit-programme.md](../../../docs/cams/08-audit-programme.md).

This is not a scheduler with a year filter. The programme is the artefact a
certification body asks to see *before* it looks at a single audit, and CAMS had
no concept of it. (`cams/audits/programme-view.tsx` is a misnomer — it is the
audit register.)

**The load-bearing decision: a slot is not an engagement.** The slot holds the
PLAN — "Q2, North Works, Fire Safety + Electrical, ISO 45001, ~3 auditor-days,
lead TBD". The engagement holds what happened. The gap between them — timing
drift, scope variance, non-execution — is the programme's entire value as a
monitoring instrument. Collapsing them produces a calendar.

`ProgrammeSlot.engagementKind` + `engagementId` is a **polymorphic pointer**, not
a join, so the programme sits above BOTH engines (audits and inspections) rather
than covering half the estate. `app.services.programme.resolver` normalises the
two shapes; that resolver is a prerequisite of the WP-18 unification anyway, so
building it here means WP-18 inherits a tested abstraction with real callers.

Schema is owned here (SQLAlchemy) with a matching Prisma block; applied by
`scripts/add_programme_tables.py`, never `prisma db push`.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin

# ── Vocabularies ─────────────────────────────────────────────────────

CYCLE_STATES = ("DRAFT", "UNDER_REVIEW", "APPROVED", "ACTIVE", "CLOSED")
SLOT_STATES = (
    "PLANNED",
    "SCHEDULED",
    "IN_PROGRESS",
    "COMPLETED",
    "DEFERRED",
    "CANCELLED",
    "WAIVED",
)
SLOT_TERMINAL = ("COMPLETED", "CANCELLED", "WAIVED")
SCOPE_DIMENSIONS = ("DISCIPLINE", "STANDARD", "PROCESS", "SUPPLIER", "CLAUSE")
SLOT_ORIGINS = ("INTERNAL", "EXTERNAL", "UNPLANNED")
AMENDMENT_TYPES = ("DEFER", "CANCEL", "WAIVE", "SCOPE_CHANGE", "ADD_SLOT", "FREQUENCY_CHANGE")


class AuditProgramme(Base, IdMixin):
    """The standing programme — long-lived across cycles.

    Per docs/cams/08 §1 Decision 3 this is **per tenant + per standard set**, not
    per site: a 16-factory apparel group runs one ISO 45001 internal-audit
    programme spanning the estate, one SA8000 social-performance programme, and
    one buyer/certification programme. Sites enter as `ProgrammeScopeUnit` rows.
    A single-site client is still expressible — a scope-unit set of one site.
    """

    __tablename__ = "AuditProgramme"

    tenantId: Mapped[str | None] = mapped_column(String, index=True)
    programmeCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # ISO 19011 §5.2 — programme objectives are REQUIRED, not decorative. The
    # approval guard rejects an empty string.
    objectives: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scopeStatement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    standardRefs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    ownerUserId: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    revisionHistory: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Proportion of a scope unit's checkpoints that must be assessed for the
    # period to count as fully covered. Per-programme because a 1,500-checkpoint
    # engagement and a 30-question inspection do not deserve the same bar.
    fullCoverageThresholdPct: Mapped[float] = mapped_column(Float, nullable=False, default=80.0)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    cycles: Mapped[list["ProgrammeCycle"]] = relationship(
        back_populates="programme", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_AuditProgramme_tenant_status", "tenantId", "status"),)


class ProgrammeCycle(Base, IdMixin):
    """One period instance — FY27, or a 3-year certification cycle.

    **The approved cycle is an immutable snapshot.** Everything after approval is
    a logged `ProgrammeAmendment`, never an edit. That is what makes "why did this
    planned audit not happen?" answerable a year later.
    """

    __tablename__ = "ProgrammeCycle"

    programmeId: Mapped[str] = mapped_column(
        ForeignKey("AuditProgramme.id", ondelete="CASCADE"), nullable=False, index=True
    )
    programme: Mapped[AuditProgramme] = relationship(back_populates="cycles")

    cycleLabel: Mapped[str] = mapped_column(String, nullable=False)
    periodStart: Mapped[date] = mapped_column(Date, nullable=False)
    periodEnd: Mapped[date] = mapped_column(Date, nullable=False)
    # Number of sub-periods coverage is measured over (4 = quarters). Drives the
    # matrix columns and the required-frequency arithmetic.
    periodsPerCycle: Mapped[int] = mapped_column(Integer, nullable=False, default=4)

    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    submittedForReviewAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Recorded so four-eyes is enforceable on the pair that actually matters.
    # Owner ≠ approver was already guarded, but the person who *prepared and
    # submitted* the plan is the one with the incentive to wave it through, and
    # they were anonymous — a submitter could approve their own submission.
    submittedByUserId: Mapped[str | None] = mapped_column(String)
    approvedByUserId: Mapped[str | None] = mapped_column(String)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # APPROVED freezes the plan; ACTIVE means the cycle is running. Both states
    # exist in CYCLE_TRANSITIONS and only ACTIVE may close, so this is the step
    # that makes the lifecycle traversable end to end.
    activatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Frozen plan-of-record + its full-length SHA-256, same integrity discipline
    # as the report snapshot (docs/cams/09 §2.5).
    approvedSnapshot: Mapped[dict | None] = mapped_column(JSON)
    approvedSnapshotHash: Mapped[str | None] = mapped_column(String)
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    scopeUnits: Mapped[list["ProgrammeScopeUnit"]] = relationship(
        back_populates="cycle", cascade="all, delete-orphan"
    )
    slots: Mapped[list["ProgrammeSlot"]] = relationship(
        back_populates="cycle", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("programmeId", "cycleLabel", name="uq_ProgrammeCycle_label"),
        Index("ix_ProgrammeCycle_status", "status"),
        Index("ix_ProgrammeCycle_period", "periodStart", "periodEnd"),
    )


class ProgrammeScopeUnit(Base, IdMixin):
    """The atomic covered thing — the row the coverage matrix is built from.

    `dimension` is an enum from day one so clause-level coverage is an ADDITIVE
    upgrade when WP-20's ClauseRef catalogue lands: no schema change, no matrix
    rewrite. `CLAUSE` is defined and rejected at the API until then, because
    `standard` / `requirementReference` are free text today (populated on 2,502 /
    1,690 of 2,503 rows) — good data, unstructured, and not enough to assert
    "clause 8.1.2 was covered".
    """

    __tablename__ = "ProgrammeScopeUnit"

    cycleId: Mapped[str] = mapped_column(
        ForeignKey("ProgrammeCycle.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cycle: Mapped[ProgrammeCycle] = relationship(back_populates="scopeUnits")

    dimension: Mapped[str] = mapped_column(String, nullable=False, default="DISCIPLINE")
    # Null siteId = estate-wide scope unit.
    siteId: Mapped[str | None] = mapped_column(String, index=True)
    dimensionKey: Mapped[str] = mapped_column(String, nullable=False)
    # Snapshotted label so a master rename does not silently rewrite history.
    dimensionLabel: Mapped[str] = mapped_column(String, nullable=False, default="")

    requiredPerCycle: Mapped[int | None] = mapped_column(Integer)
    riskWeight: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # A waiver is the ONLY legitimate alternative to a frequency at approval.
    waiverReason: Mapped[str | None] = mapped_column(Text)
    waivedByUserId: Mapped[str | None] = mapped_column(String)
    waivedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "cycleId", "dimension", "siteId", "dimensionKey", name="uq_ProgrammeScopeUnit_key"
        ),
        Index("ix_ProgrammeScopeUnit_cycle_dim", "cycleId", "dimension"),
    )


class ProgrammeSlot(Base, IdMixin):
    """A PLANNED engagement. Not an engagement.

    `windowStart`/`windowEnd` is a **window**, never a date — a programme that
    commits to 2027-06-14 is lying, and the variance query needs a window to
    measure drift against.

    `origin`:
      INTERNAL  — our own planned audit
      EXTERNAL  — brand audit, SMETA/Sedex, certification surveillance. No
                  internal lead, but auditor-days ARE populated because the
                  AUDITEE side consumes real capacity. Excluding these is how a
                  plan says "3 audits in Q3" while the site absorbs seven.
      UNPLANNED — an engagement created outside the programme. Auto-created so
                  no completed engagement can fail to appear in coverage.
    """

    __tablename__ = "ProgrammeSlot"

    cycleId: Mapped[str] = mapped_column(
        ForeignKey("ProgrammeCycle.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cycle: Mapped[ProgrammeCycle] = relationship(back_populates="slots")

    slotCode: Mapped[str] = mapped_column(String, nullable=False)
    windowStart: Mapped[date] = mapped_column(Date, nullable=False)
    windowEnd: Mapped[date] = mapped_column(Date, nullable=False)
    periodIndex: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    origin: Mapped[str] = mapped_column(String, nullable=False, default="INTERNAL")
    externalBody: Mapped[str | None] = mapped_column(String)

    # Polymorphic pointer to the materialised engagement (docs/cams/08 §1).
    engagementKind: Mapped[str | None] = mapped_column(String)  # AUDIT | INSPECTION
    engagementId: Mapped[str | None] = mapped_column(String, index=True)

    engagementTypeRef: Mapped[str | None] = mapped_column(String)
    intendedLeadUserId: Mapped[str | None] = mapped_column(String, index=True)
    ownerUserId: Mapped[str | None] = mapped_column(String)
    estimatedAuditorDays: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    actualAuditorDays: Mapped[float | None] = mapped_column(Float)

    # docs/cams/09 §2.4 — declared here so materialisation pre-fills it.
    samplingApproach: Mapped[str] = mapped_column(String, nullable=False, default="FULL")
    samplingJustification: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String, nullable=False, default="PLANNED")
    # Maintained by the service on every amendment write. The DB CHECK
    # constraint (see scripts/add_programme_tables.py) uses it to enforce
    # "no slot leaves PLANNED without an engagement or an amendment" at the
    # storage layer, because service-layer-only guards get bypassed by scripts.
    amendmentCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    notes: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("cycleId", "slotCode", name="uq_ProgrammeSlot_code"),
        Index("ix_ProgrammeSlot_cycle_status", "cycleId", "status"),
        Index("ix_ProgrammeSlot_window", "windowStart", "windowEnd"),
        Index("ix_ProgrammeSlot_engagement", "engagementKind", "engagementId"),
    )


class SlotScopeUnit(Base, IdMixin):
    """Join: one slot covers N scope units.

    A single audit typically covers several disciplines at one site, and a scope
    unit is typically hit by several slots across a cycle. Many-to-many is the
    honest shape; denormalising either side breaks the coverage arithmetic.
    """

    __tablename__ = "SlotScopeUnit"

    slotId: Mapped[str] = mapped_column(
        ForeignKey("ProgrammeSlot.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scopeUnitId: Mapped[str] = mapped_column(
        ForeignKey("ProgrammeScopeUnit.id", ondelete="CASCADE"), nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint("slotId", "scopeUnitId", name="uq_SlotScopeUnit"),
    )


class ProgrammeReview(Base, IdMixin):
    """The ISO 19011 §5.6 periodic review OF THE PROGRAMME ITSELF.

    First-class, not a notes field, and a guard on cycle closure — most tools
    stop at "monitor" and never model "review and improve", which is exactly the
    clause an auditor asks about.

    `programmeFindings` are findings about the *programme* (coverage was missed,
    frequencies were wrong, resourcing was short) — not audit findings.
    """

    __tablename__ = "ProgrammeReview"

    cycleId: Mapped[str] = mapped_column(
        ForeignKey("ProgrammeCycle.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reviewDate: Mapped[date] = mapped_column(Date, nullable=False)
    participantUserIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    externalParticipants: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    programmeFindings: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decisions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    effectivenessAssessment: Mapped[str | None] = mapped_column(Text)
    # The amendments this review DECIDED, as opposed to every amendment that
    # happened to occur in the cycle. ISO 19011 §5.6 is a review that changes
    # the programme; a review with no traceable consequence is minutes, not a
    # review. Ids rather than an FK to match `ProgrammeAmendment.slotId`, which
    # is also a plain reference.
    resultingAmendmentIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    reviewedByUserId: Mapped[str] = mapped_column(String, nullable=False)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_ProgrammeReview_cycle_date", "cycleId", "reviewDate"),)


class ProgrammeAmendment(Base, IdMixin):
    """Every deferral, cancellation, waiver or scope change AFTER approval.

    A certification body will ask why a planned audit did not happen. This row is
    the answer, and the slot state machine cannot advance without it.
    """

    __tablename__ = "ProgrammeAmendment"

    cycleId: Mapped[str] = mapped_column(
        ForeignKey("ProgrammeCycle.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Null for cycle-level amendments (adding a scope unit, changing a frequency).
    slotId: Mapped[str | None] = mapped_column(String, index=True)
    scopeUnitId: Mapped[str | None] = mapped_column(String)

    amendmentType: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    beforeValue: Mapped[dict | None] = mapped_column(JSON)
    afterValue: Mapped[dict | None] = mapped_column(JSON)
    approvedByUserId: Mapped[str] = mapped_column(String, nullable=False)
    approvedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    raisedByUserId: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_ProgrammeAmendment_cycle_type", "cycleId", "amendmentType"),
    )


class ProgrammeRecommendation(Base, IdMixin):
    """A risk-based frequency recommendation, WITH ITS INPUTS (docs/cams/08 §5).

    Persisting the inputs — not just the output — is what makes the
    recommendation defensible: the UI renders the arithmetic, and a reviewer can
    disagree with a number rather than with a black box.

    **Recommends, never applies.** `acceptedByUserId` is the gate; nothing mutates
    `ProgrammeScopeUnit.requiredPerCycle` without it. A programme that rewrites
    itself is not auditable.
    """

    __tablename__ = "ProgrammeRecommendation"

    cycleId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    scopeUnitId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    currentFrequency: Mapped[int | None] = mapped_column(Integer)
    recommendedFrequency: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    band: Mapped[str] = mapped_column(String, nullable=False)  # INCREASE | HOLD | REDUCE
    # [{input, rawValue, weight, contribution, available}] — the arithmetic.
    inputs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    unavailableInputs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    narrative: Mapped[str] = mapped_column(Text, nullable=False, default="")

    computedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    acceptedByUserId: Mapped[str | None] = mapped_column(String)
    acceptedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acceptedFrequency: Mapped[int | None] = mapped_column(Integer)
    rejectedByUserId: Mapped[str | None] = mapped_column(String)
    rejectedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejectionReason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_ProgrammeRecommendation_cycle", "cycleId", "acceptedAt"),
    )


class DisciplineHazardMap(Base, IdMixin):
    """Maps an incident/near-miss category to the audit discipline that governs it.

    This is the join that did NOT exist and that the cross-module frequency
    signal depends on (docs/cams/08 §5.1). `Incident` carries plant, area and its
    own category taxonomy; `AuditCheckpointResponse.categoryId` is the audit
    discipline taxonomy; nothing connected them.

    Hand-authored, small, tenant-overridable and visible in admin — the map IS
    the rationale, so it has to be inspectable rather than buried in code.
    """

    __tablename__ = "DisciplineHazardMap"

    plantId: Mapped[str | None] = mapped_column(String, index=True)
    disciplineCode: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Source taxonomy value, e.g. an Incident category or hazard category.
    hazardCategory: Mapped[str] = mapped_column(String, nullable=False, index=True)
    sourceModule: Mapped[str] = mapped_column(String, nullable=False, default="INCIDENT")
    # 0.0-1.0 — a partial association contributes proportionally rather than
    # forcing a binary mapping decision on ambiguous categories.
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    createdBy: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint(
            "plantId", "disciplineCode", "hazardCategory", "sourceModule",
            name="uq_DisciplineHazardMap",
        ),
        Index("ix_DisciplineHazardMap_lookup", "hazardCategory", "isActive"),
    )


__all__ = [
    "AuditProgramme",
    "ProgrammeCycle",
    "ProgrammeScopeUnit",
    "ProgrammeSlot",
    "SlotScopeUnit",
    "ProgrammeReview",
    "ProgrammeAmendment",
    "ProgrammeRecommendation",
    "DisciplineHazardMap",
    "CYCLE_STATES",
    "SLOT_STATES",
    "SLOT_TERMINAL",
    "SCOPE_DIMENSIONS",
    "SLOT_ORIGINS",
    "AMENDMENT_TYPES",
]
