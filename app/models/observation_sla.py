"""Safety Observation — SLA-based closure dates, worker involvement, and the
high-severity deroster workflow.

Three related concerns, one module because they share a single trigger surface
(severity + type + category at submission) and one audit story.

House conventions followed here (see models/training_engine.py, models/cams.py):
camelCase columns to match the Prisma-owned schema, cross-module references as
plain FK-by-value `String` columns where the target lives in another module's
table, and `status` / `partyType` as String rather than Enum so a new value is
a seed change, not a Postgres type migration.

Two deliberate departures from the build spec's shape:

1. **The audit trails are tables, not JSON arrays.** The spec models
   `DerosterRecord.auditLog[]` and `targetClosureDate.history[]` as embedded
   arrays. In-place mutation of a JSON column silently no-ops a SQLAlchemy
   commit unless the attribute is reassigned — a footgun this codebase has
   already been bitten by (see the CAMS clause-citation work). An append-only
   child table cannot lose a write, is queryable for the escalation scan, and
   matches `IndependenceEvent`.

2. **`Observation.targetDate` stays a flat column.** The spec nests it as
   `targetClosureDate.date`, which would be a breaking shape change for every
   report, dashboard and mobile screen that reads the field today. The SLA
   provenance is carried in sidecar columns on `Observation` instead, so
   nothing downstream has to change to keep working.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin

# ── vocabularies ────────────────────────────────────────────────────────────
# Kept as plain module constants (not Enum columns) so the seed owns the values.

CATEGORY_GROUP_BEHAVIORAL = "BEHAVIORAL"
CATEGORY_GROUP_PHYSICAL = "PHYSICAL"
# Explicit "nobody has decided this yet" sentinel. NOT a third SLA band — a
# category mapped to it has no closure policy, so the form falls back to manual
# entry with the same inline warning a missing config row produces. It exists so
# an undecided mapping cannot be silently resolved to Behavioural or Physical.
CATEGORY_GROUP_PENDING = "PENDING_DECISION"
CATEGORY_GROUPS = (CATEGORY_GROUP_BEHAVIORAL, CATEGORY_GROUP_PHYSICAL)
# What may be stored in ObservationCategoryGroup.categoryGroup.
CATEGORY_GROUP_VALUES = (*CATEGORY_GROUPS, CATEGORY_GROUP_PENDING)

# ObservationCategoryGroup.axis — "ANY" means the mapping holds on both the act
# and the condition axis, which is how the DuPont categories are seeded. A
# per-axis row overrides it, so a category CAN be split later without a schema
# change if that is what the policy owner decides.
AXIS_ANY = "ANY"

PARTY_USER = "USER"
PARTY_CONTRACTOR_WORKER = "CONTRACTOR_WORKER"
# A person the observer typed in by hand because neither people table has them:
# an agency hand on their first shift, a visiting vendor engineer, a driver.
# Refusing the observation until someone is onboarded loses the observation,
# which is the one outcome worse than an unlinked name. A MANUAL row carries no
# userId and no contractorWorkerId — see ObservationWorkerInvolved.
PARTY_MANUAL = "MANUAL"
PARTY_TYPES = (PARTY_USER, PARTY_CONTRACTOR_WORKER, PARTY_MANUAL)

# targetDate provenance
SOURCE_AUTO_SLA = "auto_sla"
SOURCE_MANUAL_OVERRIDE = "manual_override"
SOURCE_SECTION_HEAD_REASSIGNED = "section_head_reassigned"
SOURCE_MANUAL_NO_POLICY = "manual_no_policy"  # no SLA row matched — free-text fallback
SOURCE_LEGACY = "legacy"  # pre-existing rows; never written by this code

# Deroster lifecycle
DEROSTER_PENDING = "pending_review"
DEROSTER_CONFIRMED = "confirmed"
DEROSTER_OVERRULED = "overruled"
DEROSTER_REINSTATED = "reinstated"
# Terminal-for-review: anything past pending_review rejects a second decision.
DEROSTER_DECIDED = (DEROSTER_CONFIRMED, DEROSTER_OVERRULED, DEROSTER_REINSTATED)

# Roster gate (User.rosterStatus / ContractorWorker.rosterStatus)
ROSTER_ACTIVE = "active"
ROSTER_PENDING_REVIEW = "pending_safety_review"
ROSTER_DEROSTERED = "derostered"
# The single source of truth for "may this person be put on new work?".
ROSTER_BLOCKED = (ROSTER_PENDING_REVIEW, ROSTER_DEROSTERED)

DEFAULT_REVIEW_SLA_HOURS = 4


# ── ObservationSlaConfig — the severity × categoryGroup closure matrix ───────
class ObservationSlaConfig(Base, IdMixin):
    """Target-closure-date policy. `plantId IS NULL` is the global default;
    a plant row overrides it for that plant only — the same precedence
    `TrainingRuleConfig` uses. There is no Tenant table in this schema, so
    "tenant-scoped" in the spec resolves to plant-scoped-with-global-fallback.
    """

    __tablename__ = "ObservationSlaConfig"

    plantId: Mapped[str | None] = mapped_column(String, index=True)  # null = global

    severity: Mapped[str] = mapped_column(String, nullable=False, index=True)  # LOW|MEDIUM|HIGH|CRITICAL
    categoryGroup: Mapped[str] = mapped_column(String, nullable=False, index=True)  # BEHAVIORAL|PHYSICAL
    slaDays: Mapped[int] = mapped_column(Integer, nullable=False)

    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
    updatedById: Mapped[str | None] = mapped_column(String)


# ── ObservationCategoryGroup — STOP category → Behavioural | Physical ───────
class ObservationCategoryGroup(Base, IdMixin):
    """Which SLA band a DuPont STOP category falls into.

    Configuration, not code. The original build derived this from the
    act/condition axis alone (an act is behavioural, a condition is physical),
    which is defensible but took the decision away from the policy owner: it
    made "PPE not worn" behavioural and "PPE damaged" physical with no way to
    say otherwise. This table makes the mapping explicit and editable, and adds
    a third thing the axis could not express — "not decided yet"
    (CATEGORY_GROUP_PENDING).

    Resolution order in services/observation_sla.resolve_category_group:
      1. exact (categoryCode, axis)
      2. (categoryCode, "ANY")
      3. axis derivation — the fallback for SAFE_ACT / SAFE_CONDITION and
         legacy rows, which carry NO categoryCode at all (validate_selection
         returns (None, None, None) for the safe types), and for any category
         somebody adds to the taxonomy before configuring it here.

    Step 3 is what keeps every safe observation on an auto-calculated closure
    date — a purely category-keyed lookup would have nothing to match and would
    push all of them to manual entry.
    """

    __tablename__ = "ObservationCategoryGroup"

    categoryCode: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # "ACT" | "CONDITION" | "ANY" (see AXIS_ANY).
    axis: Mapped[str] = mapped_column(String, nullable=False, default=AXIS_ANY)
    # BEHAVIORAL | PHYSICAL | PENDING_DECISION
    categoryGroup: Mapped[str] = mapped_column(String, nullable=False)
    # Why this mapping is what it is — shown on the admin screen so an
    # undecided row explains itself rather than looking like a gap.
    notes: Mapped[str | None] = mapped_column(Text)

    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
    updatedById: Mapped[str | None] = mapped_column(String)


# ── ObservationDerosterConfig — review SLA + escalation contact ──────────────
class ObservationDerosterConfig(Base, IdMixin):
    """One row per plant (plus a global default). Separate from the SLA matrix
    because it is a singleton per scope rather than a severity × group grid —
    the admin screen renders it as the two fields under the table, per spec §2.4.

    Spec open question 4 (escalation contact) is resolved as *configuration*
    rather than a hardcoded "HSE Manager's manager": `escalationContactUserId`
    wins if set, otherwise holders of `escalationRoleCode`. Hardcoding a
    reporting line the schema does not model would have been a guess.
    """

    __tablename__ = "ObservationDerosterConfig"

    plantId: Mapped[str | None] = mapped_column(String, index=True)  # null = global

    reviewSlaHours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_REVIEW_SLA_HOURS
    )
    escalationContactUserId: Mapped[str | None] = mapped_column(String)
    escalationRoleCode: Mapped[str] = mapped_column(String, nullable=False, default="HSE_MANAGER")

    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
    updatedById: Mapped[str | None] = mapped_column(String)


# ── ObservationWorkerInvolved — who committed the act ────────────────────────
class ObservationWorkerInvolved(Base, IdMixin):
    """The named worker(s) on an observation.

    Polymorphic by necessity, not by preference: this platform has two disjoint
    people populations and no join between them. `User` is the employee
    directory (and the only thing the Training & Competency engine can assign
    to — `TrainingAssignment.personUserId` is a User). `ContractorWorker` is the
    EPC workforce and is documented as deliberately self-contained with no
    `userAccountId` FK. An unsafe act can be committed by either, so exactly one
    of `userId` / `contractorWorkerId` is populated per row — or neither, on a
    `MANUAL` row, where the observer typed a name and works ID for someone who
    is in neither directory (agency hand, visiting vendor, driver). A MANUAL row
    is a record, not a link: it cannot be soft-locked or assigned training,
    because there is nothing to lock or assign to.

    This is also the child table `training_engine.classify.build_classification`
    was waiting for — its comment reads "Observation has no person-involved
    child table … leave involvedUserIds empty". Once populated, the existing
    severity rule assigns training to the named worker with no new rule code.

    Name/role/employer are snapshotted at observation time so the record still
    reads correctly after a transfer, rename or demobilisation.
    """

    __tablename__ = "ObservationWorkerInvolved"

    observationId: Mapped[str] = mapped_column(
        ForeignKey("Observation.id", ondelete="CASCADE"), nullable=False, index=True
    )

    partyType: Mapped[str] = mapped_column(String, nullable=False, index=True)
    userId: Mapped[str | None] = mapped_column(ForeignKey("User.id"), index=True)
    contractorWorkerId: Mapped[str | None] = mapped_column(
        ForeignKey("ContractorWorker.id"), index=True
    )

    # Snapshot at time of observation (spec §1.2 "denormalized snapshot").
    nameSnapshot: Mapped[str] = mapped_column(String, nullable=False)
    roleSnapshot: Mapped[str | None] = mapped_column(String)
    employerSnapshot: Mapped[str | None] = mapped_column(String)
    # Employee / works / gate-pass number. The only handle a MANUAL row has for
    # matching the person later, so it is captured alongside the name rather
    # than folded into it; also snapshotted for linked rows when the source
    # record carries one.
    codeSnapshot: Mapped[str | None] = mapped_column(String)

    addedById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── ObservationDeroster — the soft-lock and its review ───────────────────────
class ObservationDeroster(Base, IdMixin):
    """One record per involved worker on a qualifying observation. Independent
    lifecycle per worker — a two-worker observation yields two rows that are
    confirmed or overruled separately (spec §7).

    `pending_review` is a soft-lock, not a punitive record. Nothing in this
    table is surfaced to the worker or to general reporting until the status is
    `confirmed`; see `services/observation_deroster.py`.
    """

    __tablename__ = "ObservationDeroster"

    observationId: Mapped[str] = mapped_column(
        ForeignKey("Observation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 1:1 with the involved-worker row. Unique index applied in the DDL — it is
    # what makes the trigger idempotent under a double submit.
    workerInvolvedId: Mapped[str] = mapped_column(
        ForeignKey("ObservationWorkerInvolved.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Denormalised from the worker row so the roster-gate and escalation queries
    # never need the join.
    partyType: Mapped[str] = mapped_column(String, nullable=False, index=True)
    userId: Mapped[str | None] = mapped_column(String, index=True)
    contractorWorkerId: Mapped[str | None] = mapped_column(String, index=True)
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    status: Mapped[str] = mapped_column(
        String, nullable=False, default=DEROSTER_PENDING, index=True
    )

    flaggedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    flaggedReason: Mapped[str] = mapped_column(String, nullable=False)
    reviewSlaHours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_REVIEW_SLA_HOURS
    )
    reviewDueAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    reviewedById: Mapped[str | None] = mapped_column(String)
    reviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewDecisionReason: Mapped[str | None] = mapped_column(Text)

    # ── corrective action before reinstatement (spec §2.6) ──
    # Employees: the TrainingAssignment minted by the Training & Competency
    # engine. Contractor workers cannot hold one (personUserId is a User FK), so
    # their gate is an EPC competency/training-certificate entry instead — the
    # same evidence gate checks (c) and (d) already validate. `competencyId` is
    # recorded for both so the two paths are auditable the same way.
    correctiveActionTrainingId: Mapped[str | None] = mapped_column(String, index=True)
    correctiveActionCompetencyId: Mapped[str | None] = mapped_column(String)
    correctiveActionNote: Mapped[str | None] = mapped_column(Text)

    # Timeout escalation. Non-null `escalatedAt` is what makes the scan fire
    # exactly once (spec §7) — it never decides the review.
    escalatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    escalatedToId: Mapped[str | None] = mapped_column(String)

    reinstatedById: Mapped[str | None] = mapped_column(String)
    reinstatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reinstatementNote: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


# ── ObservationDerosterEvent — append-only audit trail ───────────────────────
class ObservationDerosterEvent(Base, IdMixin):
    """Every state transition, appended. Never updated, never deleted.

    Mirrors `IndependenceEvent`: an enforcement log you can hand to an auditor
    without having to trust that a JSON array was reassigned correctly on every
    write path.
    """

    __tablename__ = "ObservationDerosterEvent"

    derosterId: Mapped[str] = mapped_column(
        ForeignKey("ObservationDeroster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    observationId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    action: Mapped[str] = mapped_column(String, nullable=False)  # flagged|confirmed|overruled|escalated|reinstated|training_linked
    fromStatus: Mapped[str | None] = mapped_column(String)
    toStatus: Mapped[str | None] = mapped_column(String)

    actorId: Mapped[str | None] = mapped_column(String)  # null = SYSTEM (scheduler)
    notes: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict | None] = mapped_column(JSON)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# ── ObservationTargetDateHistory — append-only closure-date trail ────────────
class ObservationTargetDateHistory(Base, IdMixin):
    """Every value `Observation.targetDate` has held, including the initial
    auto-calculated one. Append-only, same reasoning as the deroster events."""

    __tablename__ = "ObservationTargetDateHistory"

    observationId: Mapped[str] = mapped_column(
        ForeignKey("Observation.id", ondelete="CASCADE"), nullable=False, index=True
    )

    targetDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    # What the policy said at the time, so a later config edit can't rewrite
    # history: {severity, categoryGroup, slaDays, configId, scope}.
    slaConfigApplied: Mapped[dict | None] = mapped_column(JSON)

    changedById: Mapped[str | None] = mapped_column(String)
    changedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


__all__ = [
    "ObservationSlaConfig",
    "ObservationCategoryGroup",
    "ObservationDerosterConfig",
    "ObservationWorkerInvolved",
    "ObservationDeroster",
    "ObservationDerosterEvent",
    "ObservationTargetDateHistory",
    "CATEGORY_GROUP_BEHAVIORAL",
    "CATEGORY_GROUP_PHYSICAL",
    "CATEGORY_GROUP_PENDING",
    "CATEGORY_GROUPS",
    "CATEGORY_GROUP_VALUES",
    "AXIS_ANY",
    "PARTY_USER",
    "PARTY_CONTRACTOR_WORKER",
    "PARTY_TYPES",
    "PARTY_MANUAL",
    "SOURCE_AUTO_SLA",
    "SOURCE_MANUAL_OVERRIDE",
    "SOURCE_SECTION_HEAD_REASSIGNED",
    "SOURCE_MANUAL_NO_POLICY",
    "SOURCE_LEGACY",
    "DEROSTER_PENDING",
    "DEROSTER_CONFIRMED",
    "DEROSTER_OVERRULED",
    "DEROSTER_REINSTATED",
    "DEROSTER_DECIDED",
    "ROSTER_ACTIVE",
    "ROSTER_PENDING_REVIEW",
    "ROSTER_DEROSTERED",
    "ROSTER_BLOCKED",
    "DEFAULT_REVIEW_SLA_HOURS",
]
