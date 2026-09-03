"""DuPont STOP observation taxonomy — axis derivation, lookup, validation.

The demo bug this fixes: Category/Sub-category were fed from one shared master
list regardless of act-vs-condition, so "Unsafe Condition: PPE non-compliance"
was a selectable (and meaningless) combination.

Two rules do the work here:

  1. The taxonomy is keyed on an ACT/CONDITION *axis*, derived from
     Observation.type. `type` conflates two orthogonal facts — the axis AND the
     safe/at-risk verdict — so the axis is extracted once, here, and dual-written
     to Observation.taxonomyAxis for the composite FK.

  2. Category eligibility per axis is DERIVED from the seed data: a category is
     offered only when ≥1 active sub-category exists for that (category, axis).
     "Reactions of People" / "Positions of People" fall out of the CONDITION
     list because zero CONDITION rows are seeded for them — there is no
     hardcoded exclusion list anywhere in this file, by design.

Every server-side write path (observations POST/PATCH, capture conversion,
culture-walk raise) funnels through `validate_selection`. Frontend filtering is
a convenience, never the enforcement.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.observation import (
    ObservationTaxonomy,
    ObservationType,
    TaxonomyAxis,
)

# ─── Axis derivation ──────────────────────────────────────────────────────────

_AXIS_FOR_TYPE: dict[str, str] = {
    ObservationType.SAFE_ACT.value: TaxonomyAxis.ACT.value,
    ObservationType.UNSAFE_ACT.value: TaxonomyAxis.ACT.value,
    ObservationType.SAFE_CONDITION.value: TaxonomyAxis.CONDITION.value,
    ObservationType.UNSAFE_CONDITION.value: TaxonomyAxis.CONDITION.value,
}

# Only at-risk observations carry the STOP taxonomy. The seeded sub-category
# labels are all deviation-phrased ("PPE not worn", "Machine guard defeated");
# forcing them onto a Safe Act would produce exactly the kind of nonsense
# combination this whole change exists to remove. Safe observations keep the
# legacy hazard-category dropdown and no sub-category.
AT_RISK_TYPES = frozenset({
    ObservationType.UNSAFE_ACT.value,
    ObservationType.UNSAFE_CONDITION.value,
})


def _as_str(value: object) -> str:
    """Normalise an enum-or-string to its raw string value."""
    return value.value if hasattr(value, "value") else str(value or "")


def axis_for_type(obs_type: object) -> str | None:
    """ACT / CONDITION for any of the four ObservationType values."""
    return _AXIS_FOR_TYPE.get(_as_str(obs_type))


def requires_taxonomy(obs_type: object) -> bool:
    return _as_str(obs_type) in AT_RISK_TYPES


def normalise_axis(raw: str | None) -> str | None:
    """Accept either a bare axis ("ACT") or a full observation type
    ("UNSAFE_ACT") and return the axis. Lets a caller pass whichever it has
    without every call site re-deriving."""
    if not raw:
        return None
    token = raw.strip().upper()
    if token in (TaxonomyAxis.ACT.value, TaxonomyAxis.CONDITION.value):
        return token
    return _AXIS_FOR_TYPE.get(token)


# ─── Legacy `category` dual-write ─────────────────────────────────────────────
# The legacy ObservationCategory enum was extended with the four new STOP codes
# (PPE + HOUSEKEEPING already existed), so an at-risk observation writes its STOP
# categoryCode straight into `category`. That keeps ~15 downstream consumers —
# insight rules, Daily Brief, BBS quality, list-view analytics, mobile — grouping
# by a column that still exists and still means something, with no rewrite.
_STOP_CATEGORY_CODES = frozenset({
    "REACTIONS_OF_PEOPLE",
    "POSITIONS_OF_PEOPLE",
    "PPE",
    "TOOLS_EQUIPMENT",
    "PROCEDURES",
    "HOUSEKEEPING",
})


def legacy_category_for(category_code: str | None) -> str | None:
    """The value to store in the legacy `category` enum column."""
    if category_code and category_code in _STOP_CATEGORY_CODES:
        return category_code
    return None


# ─── Reads ────────────────────────────────────────────────────────────────────


async def list_categories(db: AsyncSession, axis: str) -> list[dict]:
    """Distinct categories that have ≥1 active sub-category on this axis.

    This derivation IS the Act-only enforcement for STOP-1 / STOP-2.
    """
    stmt = (
        select(
            ObservationTaxonomy.categoryCode,
            ObservationTaxonomy.categoryLabel,
            ObservationTaxonomy.stopReferenceCode,
            ObservationTaxonomy.displayOrder,
        )
        .where(ObservationTaxonomy.observationType == axis)
        .where(ObservationTaxonomy.isActive.is_(True))
        .order_by(ObservationTaxonomy.displayOrder, ObservationTaxonomy.categoryCode)
    )
    rows = (await db.execute(stmt)).all()

    seen: dict[str, dict] = {}
    for code, label, stop_ref, order in rows:
        if code not in seen:
            seen[code] = {
                "categoryCode": code,
                "categoryLabel": label,
                "stopReferenceCode": stop_ref,
                "displayOrder": order,
            }
    return list(seen.values())


async def list_subcategories(db: AsyncSession, axis: str, category_code: str) -> list[dict]:
    stmt = (
        select(ObservationTaxonomy)
        .where(ObservationTaxonomy.observationType == axis)
        .where(ObservationTaxonomy.categoryCode == category_code)
        .where(ObservationTaxonomy.isActive.is_(True))
        .order_by(ObservationTaxonomy.displayOrder, ObservationTaxonomy.subCategoryCode)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "subCategoryCode": r.subCategoryCode,
            "subCategoryLabel": r.subCategoryLabel,
            "categoryCode": r.categoryCode,
            "categoryLabel": r.categoryLabel,
            "stopReferenceCode": r.stopReferenceCode,
            "displayOrder": r.displayOrder,
        }
        for r in rows
    ]


# ─── Validation ───────────────────────────────────────────────────────────────


async def validate_selection(
    db: AsyncSession,
    obs_type: object,
    category_code: str | None,
    sub_category_code: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Server-side gate for a (type, categoryCode, subCategoryCode) triple.

    Returns the normalised `(categoryCode, subCategoryCode, taxonomyAxis)` to
    persist. Raises HTTPException(400) on any mismatch — never relies on the
    client having filtered the dropdowns correctly.

    Safe observations pass through with all three set to None: they don't carry
    the STOP taxonomy, and quietly keeping a stale code on one would reintroduce
    exactly the mismatch this validates against.

    The sub-category is OPTIONAL. The category is what every downstream consumer
    groups by (legacy `category` dual-write, the SLA category-group matrix, the
    heat-map), so it stays mandatory on an at-risk type; the sub-category only
    refines it. When one IS supplied it is still validated against the
    (category, axis) pair — optional means "may be omitted", never "may be
    wrong".
    """
    axis = axis_for_type(obs_type)
    type_label = _as_str(obs_type)

    if not requires_taxonomy(obs_type):
        return None, None, None

    category_code = (category_code or "").strip() or None
    sub_category_code = (sub_category_code or "").strip() or None

    if not category_code:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{type_label} observations require a category from the STOP taxonomy.",
        )

    if sub_category_code is None:
        # Category-only. Still has to be a category that exists on this axis —
        # the composite FK to ObservationTaxonomy is MATCH SIMPLE, so a NULL
        # sub-category switches it off entirely and this check becomes the only
        # thing standing between "sub-category omitted" and "Reactions of People
        # recorded as a condition".
        eligible = {c["categoryCode"] for c in await list_categories(db, axis)}
        if category_code not in eligible:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Category '{category_code}' does not apply to a {type_label}. "
                f"Valid categories: {', '.join(sorted(eligible))}.",
            )
        return category_code, None, axis

    row = (
        await db.execute(
            select(ObservationTaxonomy)
            .where(ObservationTaxonomy.categoryCode == category_code)
            .where(ObservationTaxonomy.subCategoryCode == sub_category_code)
            .where(ObservationTaxonomy.observationType == axis)
            .where(ObservationTaxonomy.isActive.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None:
        return category_code, sub_category_code, axis

    # Nothing matched. Work out WHY so the 400 is actionable rather than a flat
    # "invalid combination" — a mis-scoped category and a mis-scoped
    # sub-category are different mistakes with different fixes.
    eligible = await list_categories(db, axis)
    eligible_codes = {c["categoryCode"] for c in eligible}
    if category_code not in eligible_codes:
        other_axis = (
            TaxonomyAxis.CONDITION.value if axis == TaxonomyAxis.ACT.value else TaxonomyAxis.ACT.value
        )
        exists_elsewhere = (
            await db.execute(
                select(ObservationTaxonomy.id)
                .where(ObservationTaxonomy.categoryCode == category_code)
                .where(ObservationTaxonomy.observationType == other_axis)
                .where(ObservationTaxonomy.isActive.is_(True))
                .limit(1)
            )
        ).scalar_one_or_none() is not None
        if exists_elsewhere:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Category '{category_code}' does not apply to a {type_label} — it is "
                f"only observable as {other_axis.lower()}. Pick from: "
                f"{', '.join(sorted(eligible_codes))}.",
            )
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown category '{category_code}'. Valid categories for a {type_label}: "
            f"{', '.join(sorted(eligible_codes))}.",
        )

    valid_subs = await list_subcategories(db, axis, category_code)
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        f"Sub-category '{sub_category_code}' is not valid for {category_code} on a "
        f"{type_label}. Valid sub-categories: "
        f"{', '.join(s['subCategoryCode'] for s in valid_subs) or '(none seeded)'}.",
    )


# ─── Guided-capture hazard mapping ────────────────────────────────────────────
# The /capture PWA classifies against its own hazard taxonomy (CaptureTaxonomy
# L1 codes) and the triager converts a submission into an Observation. That
# conversion always produces an at-risk type, so it must resolve to a real STOP
# pair. Mapped per axis because the same hazard reads differently as an act vs a
# condition (working_at_height as an act = a fall exposure; as a condition = the
# access equipment). A triager who disagrees can pass an explicit override on
# the convert call — see routers/capture.py.

HAZARD_TO_STOP: dict[str, dict[str, tuple[str, str]]] = {
    "slip_trip_fall": {
        "ACT": ("POSITIONS_OF_PEOPLE", "PP_FALLING_SAME_LEVEL"),
        "CONDITION": ("HOUSEKEEPING", "HK_DAMAGED_FLOORING"),
    },
    "fire": {
        "ACT": ("PROCEDURES", "PR_PERMIT_LOTO_BYPASSED"),
        "CONDITION": ("HOUSEKEEPING", "HK_BLOCKED_EGRESS"),
    },
    "electrical": {
        "ACT": ("POSITIONS_OF_PEOPLE", "PP_CONTACT_ELECTRICAL"),
        "CONDITION": ("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION"),
    },
    "chemical": {
        "ACT": ("PPE", "PPE_NOT_WORN"),
        "CONDITION": ("HOUSEKEEPING", "HK_SPILL_NOT_CLEANED"),
    },
    "machine_guarding": {
        "ACT": ("TOOLS_EQUIPMENT", "TE_USED_INCORRECTLY"),
        "CONDITION": ("TOOLS_EQUIPMENT", "TE_GUARD_MISSING"),
    },
    "housekeeping": {
        "ACT": ("HOUSEKEEPING", "HK_NOT_CLEAN_AS_YOU_GO"),
        "CONDITION": ("HOUSEKEEPING", "HK_CLUTTER_DEBRIS"),
    },
    "ppe": {
        "ACT": ("PPE", "PPE_NOT_WORN"),
        "CONDITION": ("PPE", "PPE_NOT_AVAILABLE"),
    },
    "ergonomics": {
        "ACT": ("POSITIONS_OF_PEOPLE", "PP_OVEREXERTION"),
        "CONDITION": ("PROCEDURES", "PR_INADEQUATE_OR_OUTDATED"),
    },
    "vehicle_forklift": {
        "ACT": ("PROCEDURES", "PR_NOT_FOLLOWED"),
        "CONDITION": ("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION"),
    },
    "working_at_height": {
        "ACT": ("POSITIONS_OF_PEOPLE", "PP_FALLING_DIFFERENT_LEVEL"),
        "CONDITION": ("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION"),
    },
    "confined_space": {
        "ACT": ("PROCEDURES", "PR_PERMIT_LOTO_BYPASSED"),
        "CONDITION": ("PROCEDURES", "PR_NOT_AVAILABLE"),
    },
    "material_handling": {
        "ACT": ("POSITIONS_OF_PEOPLE", "PP_OVEREXERTION"),
        "CONDITION": ("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION"),
    },
}

# Where the hazard code is missing or unrecognised. Deliberately the vaguest
# real entry on each axis rather than an invented "OTHER" bucket — the triager
# sees a concrete claim they can correct, not a silent dumping ground.
HAZARD_FALLBACK: dict[str, tuple[str, str]] = {
    "ACT": ("PROCEDURES", "PR_NOT_FOLLOWED"),
    "CONDITION": ("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION"),
}


def stop_pair_for_hazard(hazard_l1_code: str | None, axis: str) -> tuple[str, str]:
    """(categoryCode, subCategoryCode) for a capture hazard on the given axis."""
    entry = HAZARD_TO_STOP.get((hazard_l1_code or "").strip().lower())
    if entry and axis in entry:
        return entry[axis]
    return HAZARD_FALLBACK.get(axis, HAZARD_FALLBACK["CONDITION"])


# Legacy hazard category → STOP pair, for the paths that still speak the old
# vocabulary (leadership-walk "raise observation"). Only the two categories that
# mean the same thing in both vocabularies are mapped; everything else takes the
# axis fallback, same as an unrecognised capture hazard. This is the ONLY place
# a legacy category is auto-converted at write time — the backfill migration
# deliberately refuses to do the equivalent for historical records, because
# there a wrong guess becomes permanent register data rather than a value the
# raiser is about to see on screen.
_LEGACY_TO_STOP: dict[str, dict[str, tuple[str, str]]] = {
    "PPE": {
        "ACT": ("PPE", "PPE_NOT_WORN"),
        "CONDITION": ("PPE", "PPE_NOT_AVAILABLE"),
    },
    "HOUSEKEEPING": {
        "ACT": ("HOUSEKEEPING", "HK_NOT_CLEAN_AS_YOU_GO"),
        "CONDITION": ("HOUSEKEEPING", "HK_CLUTTER_DEBRIS"),
    },
}


def stop_pair_for_legacy_category(legacy_category: str | None, axis: str) -> tuple[str, str]:
    entry = _LEGACY_TO_STOP.get((legacy_category or "").strip().upper())
    if entry and axis in entry:
        return entry[axis]
    return HAZARD_FALLBACK.get(axis, HAZARD_FALLBACK["CONDITION"])


__all__ = [
    "AT_RISK_TYPES",
    "HAZARD_FALLBACK",
    "HAZARD_TO_STOP",
    "axis_for_type",
    "legacy_category_for",
    "list_categories",
    "list_subcategories",
    "normalise_axis",
    "requires_taxonomy",
    "stop_pair_for_hazard",
    "stop_pair_for_legacy_category",
    "validate_selection",
]
