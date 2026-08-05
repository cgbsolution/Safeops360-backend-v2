from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin
from app.models.user import User


class ObservationType(str, enum.Enum):
    SAFE_ACT = "SAFE_ACT"
    UNSAFE_ACT = "UNSAFE_ACT"
    SAFE_CONDITION = "SAFE_CONDITION"
    UNSAFE_CONDITION = "UNSAFE_CONDITION"


class ObservationCategory(str, enum.Enum):
    """Categories accepted by the read path.

    Some legacy / seeded rows were written with names that the original
    enum didn't list (OTHERS, EMERGENCY_PREP, …). Reads via the ORM
    eagerly enum-coerce the value, so any DB string that isn't in this
    enum throws LookupError and 500s the endpoint. We accept the drift
    here (additive, non-breaking) and document the canonical name. New
    writes should still use OTHER / EMERGENCY.

    The last block is the DuPont STOP taxonomy (see ObservationTaxonomy).
    At-risk observations write their STOP `categoryCode` straight into this
    column too, so every existing group-by-category dashboard keeps working
    without a rewrite. PPE and HOUSEKEEPING are shared between both lists.
    """

    PPE = "PPE"
    HOUSEKEEPING = "HOUSEKEEPING"
    WORK_AT_HEIGHT = "WORK_AT_HEIGHT"
    HOT_WORK = "HOT_WORK"
    MOBILE_EQUIPMENT = "MOBILE_EQUIPMENT"
    ELECTRICAL = "ELECTRICAL"
    MATERIAL_HANDLING = "MATERIAL_HANDLING"
    CONFINED_SPACE = "CONFINED_SPACE"
    CHEMICAL_HANDLING = "CHEMICAL_HANDLING"
    EMERGENCY = "EMERGENCY"
    EMERGENCY_PREP = "EMERGENCY_PREP"  # legacy alias of EMERGENCY
    OTHER = "OTHER"
    OTHERS = "OTHERS"  # legacy alias of OTHER
    ENVIRONMENT = "ENVIRONMENT"
    ERGONOMICS = "ERGONOMICS"
    BEHAVIOUR = "BEHAVIOUR"
    PROCESS_SAFETY = "PROCESS_SAFETY"
    LIFTING = "LIFTING"

    # ─── DuPont STOP categories (STOP-1 … STOP-6) ───
    REACTIONS_OF_PEOPLE = "REACTIONS_OF_PEOPLE"  # STOP-1, ACT only
    POSITIONS_OF_PEOPLE = "POSITIONS_OF_PEOPLE"  # STOP-2, ACT only
    TOOLS_EQUIPMENT = "TOOLS_EQUIPMENT"          # STOP-4
    PROCEDURES = "PROCEDURES"                    # STOP-5


class TaxonomyAxis(str, enum.Enum):
    """The axis the STOP taxonomy is keyed on.

    Deliberately NOT the same thing as ObservationType. `type` carries two
    orthogonal facts — the act/condition axis AND the safe/at-risk verdict.
    The taxonomy only varies along the first, so it's stored separately
    (and dual-written from `type`) rather than re-derived at every read.
    """

    ACT = "ACT"
    CONDITION = "CONDITION"


class Severity(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ObservationStatus(str, enum.Enum):
    OPEN = "OPEN"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    CLOSED = "CLOSED"


class Observation(Base, IdMixin):
    __tablename__ = "Observation"

    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    type: Mapped[ObservationType] = mapped_column(Enum(ObservationType, name="ObservationType", native_enum=False), nullable=False)
    category: Mapped[ObservationCategory] = mapped_column(
        Enum(ObservationCategory, name="ObservationCategory", native_enum=False), nullable=False
    )
    severity: Mapped[Severity] = mapped_column(Enum(Severity, name="Severity", native_enum=False), nullable=False, default=Severity.LOW)

    # ─── DuPont STOP taxonomy (ObservationTaxonomy) ──────────────────────
    # Required for at-risk types (UNSAFE_ACT / UNSAFE_CONDITION); NULL on safe
    # observations and on legacy rows the migration couldn't confidently map.
    # `taxonomyAxis` is dual-written from `type` so the composite FK to
    # ObservationTaxonomy(categoryCode, subCategoryCode, observationType) can
    # enforce "sub-category belongs to this axis" at the DB level. Plain String
    # (not Enum) — the FK does the constraining and it keeps the column
    # comparable to ObservationTaxonomy.observationType without a cast.
    categoryCode: Mapped[str | None] = mapped_column(String, index=True)
    subCategoryCode: Mapped[str | None] = mapped_column(String, index=True)
    taxonomyAxis: Mapped[str | None] = mapped_column(String, index=True)

    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False)
    areaId: Mapped[str | None] = mapped_column(ForeignKey("Area.id"))
    # The Prisma schema has no `location` or `correctiveAction` column on
    # Observation — those live on NearMiss / Incident. Don't add them here
    # or INSERT will fail with "column does not exist".

    observerId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    responsiblePersonId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    # Contractor traceability — when the unsafe act/condition involves a
    # contractor. Header-level link (mirrors NearMiss.contractorCompanyId);
    # nullable, added by apply-contractor-links-ddl.ts before backend restart.
    contractorCompanyId: Mapped[str | None] = mapped_column(ForeignKey("ContractorCompany.id"), index=True)

    description: Mapped[str] = mapped_column(Text, nullable=False)
    # P3-1 BBS — quality score (0..3 specificity) + optional ABC (antecedent→
    # behaviour→consequence) fields + auto-prompted CAPA link.
    qualityScore: Mapped[int | None] = mapped_column(Integer)
    antecedent: Mapped[str | None] = mapped_column(Text)
    behaviourObserved: Mapped[str | None] = mapped_column(Text)
    consequence: Mapped[str | None] = mapped_column(Text)
    capaId: Mapped[str | None] = mapped_column(String)
    capaPromptDeclined: Mapped[bool | None] = mapped_column(Boolean)
    immediateAction: Mapped[str | None] = mapped_column(Text)
    # The target closure date. DELIBERATELY still a flat column: the SLA build
    # spec nested it as `targetClosureDate.date`, which would have been a
    # breaking shape change for every report, dashboard and mobile screen that
    # reads it today. The provenance lives in the three sidecar columns below
    # and the full trail in ObservationTargetDateHistory, so nothing downstream
    # had to change. See models/observation_sla.py.
    targetDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # auto_sla | manual_override | section_head_reassigned | manual_no_policy.
    # NULL on rows written before the SLA layer existed — read as "legacy".
    targetDateSource: Mapped[str | None] = mapped_column(String)
    # Frozen copy of the policy that produced the date: {severity, categoryGroup,
    # slaDays, configId, scope}. Frozen so a later edit to the SLA matrix cannot
    # retroactively change what an already-submitted record was held to.
    # Written whole, never mutated in place (JSON in-place edits no-op the commit).
    targetDateSlaConfig: Mapped[dict | None] = mapped_column(JSON)
    targetDateOverrideReason: Mapped[str | None] = mapped_column(Text)
    closingRemark: Mapped[str | None] = mapped_column(Text)
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[ObservationStatus] = mapped_column(
        Enum(ObservationStatus, name="ObservationStatus", native_enum=False),
        nullable=False,
        default=ObservationStatus.OPEN,
    )

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # MUST be `default=func.now()` (NOT `server_default`). Prisma's
    # `@updatedAt` is client-managed — there is no DB-level DEFAULT on
    # this column, so a `server_default` would have SQLAlchemy omit
    # updatedAt from the INSERT and Postgres rejects it (NOT NULL, no
    # default). With `default=func.now()` SQLAlchemy emits NOW() inline.
    # The post-flush `db.refresh(obs)` call in the route loads the
    # computed value into the in-memory object so model_validate doesn't
    # trip MissingGreenlet.
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        onupdate=func.now(),
    )

    # Audit log of post-closure cross-module triggers (Dimension 4) +
    # AI agent outputs (LessonsDistributionAgent, TriageAgent, etc.).
    # Shape: list of `{ ruleId, ruleName, fired, reason?, error?, data? }`.
    # Triage entries (run on submission, not closure) are stored here too
    # under ruleId="rule_triage_on_submit" — Prisma's JSON column doesn't
    # need a separate aiTriage field.
    closureTriggers: Mapped[list | None] = mapped_column(JSON)


class ObservationTaxonomy(Base, IdMixin):
    """DuPont STOP category → sub-category master, split by act/condition axis.

    Category eligibility per axis is DERIVED, not flagged: a category is
    offered for an axis only when at least one active row exists for that
    (categoryCode, observationType) pair. "Reactions of People" and
    "Positions of People" therefore drop out of the CONDITION list purely
    because no CONDITION rows are seeded for them — there is no exclusion
    list to keep in sync.
    """

    __tablename__ = "ObservationTaxonomy"

    categoryCode: Mapped[str] = mapped_column(String, nullable=False, index=True)
    categoryLabel: Mapped[str] = mapped_column(String, nullable=False)
    # "ACT" | "CONDITION" — see TaxonomyAxis. Stored as String so the
    # composite FK from Observation.taxonomyAxis lines up without a cast.
    observationType: Mapped[str] = mapped_column(String, nullable=False, index=True)
    subCategoryCode: Mapped[str] = mapped_column(String, nullable=False, index=True)
    subCategoryLabel: Mapped[str] = mapped_column(String, nullable=False)
    # Traceability back to the published standard, e.g. "STOP-3".
    stopReferenceCode: Mapped[str] = mapped_column(String, nullable=False)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    displayOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


class UnmappedLegacyObservation(Base, IdMixin):
    """Review queue for legacy observations the taxonomy migration would have
    had to guess at. Written by prisma/migrate-observation-taxonomy.ts instead
    of forcing a bad mapping onto the record itself."""

    __tablename__ = "UnmappedLegacyObservation"

    observationId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    observationNumber: Mapped[str] = mapped_column(String, nullable=False)
    observationType: Mapped[str] = mapped_column(String, nullable=False)
    legacyCategory: Mapped[str | None] = mapped_column(String)
    # NO_CONFIDENT_CATEGORY_MATCH | SUBCATEGORY_REQUIRES_REVIEW
    reason: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # What the migration WAS able to establish (category only, usually).
    suggestedCategoryCode: Mapped[str | None] = mapped_column(String)
    suggestedAxis: Mapped[str | None] = mapped_column(String)
    resolvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolvedById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# Same shape as IncidentAttachment — see that model for upload lifecycle.
class ObservationAttachment(Base, IdMixin):
    __tablename__ = "ObservationAttachment"

    observationId: Mapped[str] = mapped_column(
        ForeignKey("Observation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # INITIAL_PHOTO | ACTION_EVIDENCE | VERIFICATION_PHOTO | DOCUMENT
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    storagePath: Mapped[str] = mapped_column(String, nullable=False)
    fileSize: Mapped[int] = mapped_column(Integer, nullable=False)
    mimeType: Mapped[str] = mapped_column(String, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)
    exifData: Mapped[dict | None] = mapped_column(JSON)
    uploadedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    uploadedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    uploadedBy: Mapped[User] = relationship(foreign_keys=[uploadedById], lazy="joined")
