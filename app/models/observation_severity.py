"""Deterministic severity suggestion for Safety Observations.

Severity was a free dropdown independent of the STOP taxonomy, so the same
hazard classification was rated Low by one observer and High by the next — which
makes the category heat-map and every severity trend unreadable. This module
adds a seeded lookup (taxonomy pair → base severity), an area hazard-tier
modifier, and an append-only log of every time an observer disagreed with the
suggestion.

Rules + lookup tables only. No LLM call, no external service — same airgap
constraint the Insight Engine is built under.

House conventions followed here (models/observation_sla.py, models/cams.py):
camelCase columns to match the Prisma-owned schema, cross-module references as
plain FK-by-value `String` columns, and vocabularies as module constants on
`String` columns rather than Postgres enums so a new value is a seed change
instead of a type migration.

Three deliberate departures from the build spec's shape:

1. **`observationType` on the matrix holds the ACT / CONDITION *axis*, not
   `unsafe_act` / `unsafe_condition`.** That is the key `ObservationTaxonomy`
   is already partitioned on (see models/observation.py — `Observation.type`
   conflates the axis with the safe/at-risk verdict, so the axis is stored
   separately). Using the same key means a matrix rule joins straight to the
   sub-category row it rates, and `services.observation_taxonomy.normalise_axis`
   still accepts the spec's `unsafe_act` spelling at the API edge.

2. **`AreaHazardTier` was created rather than reused.** The spec asks to check
   for an existing per-area risk classification first. There is none: `Area`
   carries only `name` / `plantId` / `ownerUserId`, and HIRA's `riskLevel` sits
   on `HiraEntry` — one row per hazard *scenario*, not a classification of the
   area itself. The seed derives the initial tier from those HIRA entries so the
   table starts from real risk data instead of a guess, but the tier is then
   owned here and editable.

3. **`SeverityOverrideLog` denormalises the taxonomy pair and the resolved
   inputs.** The spec's table references only the observation. But an
   observation's category is editable after the fact, so grouping the
   calibration report through it would silently re-attribute historical
   overrides. The columns below record what the engine was actually asked, at
   the moment it was asked.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin

# ── vocabularies ────────────────────────────────────────────────────────────

# Severity, lowest → highest. The ordering IS the tier modifier: a bump is an
# index step in this tuple, clamped at both ends. Matches
# models/observation.Severity; kept as a plain tuple so the service can do
# ordinal arithmetic without importing the enum into every call site.
SEVERITY_LADDER = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

TIER_STANDARD = "Standard"
TIER_ELEVATED = "Elevated"
TIER_HIGH_HAZARD = "HighHazard"
HAZARD_TIERS = (TIER_STANDARD, TIER_ELEVATED, TIER_HIGH_HAZARD)

# Where a SeverityOverrideLog row came from. The observer form is the only
# source that carries a human justification the observer typed; the others are
# recorded so the calibration report can exclude them rather than read a
# triager's or an editor's decision as observer disagreement.
OVERRIDE_SOURCE_OBSERVER_FORM = "OBSERVER_FORM"
OVERRIDE_SOURCE_CAPTURE_CONVERSION = "CAPTURE_CONVERSION"
OVERRIDE_SOURCE_EDIT = "EDIT"
OVERRIDE_SOURCES = (
    OVERRIDE_SOURCE_OBSERVER_FORM,
    OVERRIDE_SOURCE_CAPTURE_CONVERSION,
    OVERRIDE_SOURCE_EDIT,
)

# Minimum length of an override justification. Enforced server-side as well as
# in the form — spec §7 tests the requirement, and a form-only rule is not one.
MIN_OVERRIDE_REASON_CHARS = 10


# ── SeverityMatrixRule — the taxonomy pair → base severity lookup ────────────
class SeverityMatrixRule(Base, IdMixin):
    """Base severity for one (axis, category, sub-category) triple.

    This is the whole determinism story: identical hazard classifications get
    identical suggestions, everywhere, with no model in the loop. `rationale` is
    surfaced in the form so the observer is told *why* the suggestion is what it
    is and can argue with the reasoning rather than just the number.

    `isActive` retires a rule without deleting it, so a historical override log
    that points at `matrixRuleId` still resolves. The unique index is therefore
    partial (`WHERE isActive`) — a retired rule and its replacement coexist.
    """

    __tablename__ = "SeverityMatrixRule"

    # "ACT" | "CONDITION" — the taxonomy axis, matching
    # ObservationTaxonomy.observationType. NOT the four-value ObservationType.
    observationType: Mapped[str] = mapped_column(String, nullable=False, index=True)
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    subCategory: Mapped[str] = mapped_column(String, nullable=False, index=True)

    baseSeverity: Mapped[str] = mapped_column(String, nullable=False)
    # Shown next to the suggestion in the form. One sentence, observer-facing.
    rationale: Mapped[str] = mapped_column(Text, nullable=False)

    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
    updatedById: Mapped[str | None] = mapped_column(String)


# ── AreaHazardTier — the per-area modifier on the base severity ──────────────
class AreaHazardTier(Base, IdMixin):
    """How much worse the same deviation is in this area.

    `areaId IS NULL` is the plant-wide default; an area row overrides it. Same
    precedence `ObservationSlaConfig` uses for plant-vs-global, one level down.
    Nothing is required: an area with no row and a plant with no default
    resolve to `Standard`, i.e. the base severity passes through untouched.

    Created rather than reused — see the module docstring. The seed derives the
    first value per area from HIRA entry risk levels so the tiers start from
    assessed risk, but an edit here is not written back to HIRA: a HIRA study is
    a point-in-time assessment of specific scenarios, and this is a standing
    property of the place.
    """

    __tablename__ = "AreaHazardTier"

    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False, index=True)
    # NULL = the default for every area of this plant that has no row of its own.
    areaId: Mapped[str | None] = mapped_column(ForeignKey("Area.id"), index=True)

    hazardTier: Mapped[str] = mapped_column(String, nullable=False)
    # Why this area carries this tier — shown on the admin screen and in the
    # suggestion payload, so an Elevated rating is never an unexplained bump.
    notes: Mapped[str | None] = mapped_column(Text)
    # "hira_derived" | "manual". Distinguishes a tier the seed inferred from
    # HIRA from one a person set, so re-deriving can leave manual rows alone.
    source: Mapped[str] = mapped_column(String, nullable=False, default="manual")

    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
    updatedById: Mapped[str | None] = mapped_column(String)


# ── SeverityOverrideLog — the calibration signal ─────────────────────────────
class SeverityOverrideLog(Base, IdMixin):
    """One row per submission where the observer's severity ≠ the suggestion.

    Append-only, never updated. This is the feedback loop the whole feature
    exists for: a sub-category overridden upward by nine observers out of ten
    is telling you the matrix rule is wrong, not that ten observers are.

    A matching severity writes NO row. There is no analytical value in a
    confirmation row here — the denominator for an override *rate* is the count
    of observations carrying the same taxonomy pair, which `Observation` already
    holds exactly, so logging agreement would only add volume.

    The taxonomy pair and the resolver inputs are stored on the row rather than
    read back through the observation, because `Observation.categoryCode` and
    `severity` are both editable afterwards — a report that joined through them
    would quietly restate history.
    """

    __tablename__ = "SeverityOverrideLog"

    observationId: Mapped[str] = mapped_column(
        ForeignKey("Observation.id", ondelete="CASCADE"), nullable=False, index=True
    )

    suggestedSeverity: Mapped[str] = mapped_column(String, nullable=False, index=True)
    finalSeverity: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Required by the API when the source is the observer form. Nullable in the
    # schema because a NOT NULL here would be a lie the moment a future path
    # needs to record a system-set divergence.
    overrideReason: Mapped[str | None] = mapped_column(Text)

    # ── what the engine was asked, frozen ──
    observationType: Mapped[str | None] = mapped_column(String, index=True)  # ACT | CONDITION
    categoryCode: Mapped[str | None] = mapped_column(String, index=True)
    subCategoryCode: Mapped[str | None] = mapped_column(String, index=True)
    # The rule's own severity, before the area tier was applied. With
    # `hazardTier` this makes it possible to tell "the rule is wrong" from
    # "the tier is wrong" without re-running the resolver against today's config.
    baseSeverity: Mapped[str | None] = mapped_column(String)
    hazardTier: Mapped[str | None] = mapped_column(String)
    matrixRuleId: Mapped[str | None] = mapped_column(String, index=True)
    plantId: Mapped[str | None] = mapped_column(String, index=True)
    areaId: Mapped[str | None] = mapped_column(String)

    # OBSERVER_FORM | CAPTURE_CONVERSION | EDIT — see OVERRIDE_SOURCES.
    source: Mapped[str] = mapped_column(
        String, nullable=False, default=OVERRIDE_SOURCE_OBSERVER_FORM, index=True
    )

    overriddenById: Mapped[str | None] = mapped_column(ForeignKey("User.id"), index=True)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


__all__ = [
    "AreaHazardTier",
    "SeverityMatrixRule",
    "SeverityOverrideLog",
    "SEVERITY_LADDER",
    "TIER_STANDARD",
    "TIER_ELEVATED",
    "TIER_HIGH_HAZARD",
    "HAZARD_TIERS",
    "OVERRIDE_SOURCE_OBSERVER_FORM",
    "OVERRIDE_SOURCE_CAPTURE_CONVERSION",
    "OVERRIDE_SOURCE_EDIT",
    "OVERRIDE_SOURCES",
    "MIN_OVERRIDE_REASON_CHARS",
]
