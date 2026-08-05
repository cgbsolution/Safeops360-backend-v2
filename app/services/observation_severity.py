"""Deterministic severity suggestion for Safety Observations.

Rules and a lookup table. No LLM, no external call — the same airgap constraint
the Insight Engine is built under, and the reason this can be re-run over a
historical record and produce the identical answer.

    (axis, category, subCategory) ──► SeverityMatrixRule.baseSeverity
                                             │
                       AreaHazardTier(plant, area) ──► tier
                                             │
                                      apply_tier_modifier
                                             ▼
                                     suggested severity

Three properties the rest of the module is built to protect:

* **A missing rule is a supported state, never an error.** `resolve` returns
  `suggested=None` and the form falls back to exactly today's behaviour: a plain
  manual dropdown, no suggestion label, no override reason required. A taxonomy
  pair added before anyone writes a rule for it must not block reporting.

* **The server re-resolves at submit; it never trusts the client's suggestion.**
  If the requirement to justify an override were keyed on a value the client
  sent, any client could dodge it by claiming the suggestion matched. The
  resolver is deterministic, so recomputing costs one indexed lookup and makes
  the gate real.

* **Agreement writes nothing.** Only divergence is logged. The denominator for
  an override *rate* is the count of observations carrying the same taxonomy
  pair, which `Observation` already holds exactly.
"""

from __future__ import annotations

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.observation import Observation
from app.models.observation_severity import (
    MIN_OVERRIDE_REASON_CHARS,
    OVERRIDE_SOURCE_OBSERVER_FORM,
    SEVERITY_LADDER,
    TIER_ELEVATED,
    TIER_HIGH_HAZARD,
    TIER_STANDARD,
    AreaHazardTier,
    SeverityMatrixRule,
    SeverityOverrideLog,
)
from app.services import observation_taxonomy as tax

# ── Tier modifier ───────────────────────────────────────────────────────────
# How many rungs of SEVERITY_LADDER an area's hazard tier adds, and the highest
# base severity the bump still applies to.
#
# ⚠ POLICY, NOT PHYSICS. These are the build spec's own stated starting values,
# and the spec flags them for confirmation with the policy owner before they are
# treated as final. They are isolated here — a single table, read by one
# function — so a ruling changes this constant and nothing else. Every
# suggestion response and every override-log row records `tierApplied` and
# `baseSeverity` separately, so a later ruling can be back-tested against the
# overrides already collected rather than guessed at again.
#
#   HighHazard : bump one rung at every level (CRITICAL is already the top and
#                clamps, which is the spec's "Critical stays Critical")
#   Elevated   : bump one rung, but only from LOW — the spec's "Elevated tier:
#                bump only Low->Medium". `max_base` is what expresses that.
#   Standard   : no change
_TIER_BUMP: dict[str, tuple[int, str | None]] = {
    TIER_HIGH_HAZARD: (1, None),          # (rungs, highest base severity it applies to)
    TIER_ELEVATED: (1, "LOW"),
    TIER_STANDARD: (0, None),
}


def severity_index(severity: str | None) -> int | None:
    """Rung of SEVERITY_LADDER, or None for anything unrecognised."""
    if not severity:
        return None
    try:
        return SEVERITY_LADDER.index(str(severity).strip().upper())
    except ValueError:
        return None


def _as_str(value: object) -> str:
    """Normalise an enum-or-string to its raw string value.
    `str(Severity.HIGH)` yields 'Severity.HIGH', so `.value` must be used."""
    return str(getattr(value, "value", None) or value or "")


def normalise_severity(value: object) -> str | None:
    """Severity enum member, or any casing of its name, → the canonical value."""
    token = _as_str(value).strip().upper()
    return token if token in SEVERITY_LADDER else None


def apply_tier_modifier(base_severity: str, hazard_tier: str | None) -> str:
    """Base severity raised by the area's hazard tier, clamped at CRITICAL.

    Unknown tiers are treated as Standard — a typo in config must not silently
    inflate every observation in an area.
    """
    base = normalise_severity(base_severity)
    if base is None:
        return base_severity
    rungs, max_base = _TIER_BUMP.get(hazard_tier or TIER_STANDARD, (0, None))
    if rungs == 0:
        return base
    if max_base is not None and severity_index(base) > severity_index(max_base):
        return base
    idx = min(severity_index(base) + rungs, len(SEVERITY_LADDER) - 1)
    return SEVERITY_LADDER[idx]


# ── Lookups ─────────────────────────────────────────────────────────────────


async def get_area_hazard_tier(
    db: AsyncSession, *, plant_id: str | None, area_id: str | None
) -> tuple[str, str]:
    """`(tier, source)` for a location.

    Precedence: the area's own row, then the plant-wide default (`areaId IS
    NULL`), then `Standard`. `source` says which applied — `area`, `plant` or
    `default` — so the form can explain a bump instead of just applying one.
    """
    if not plant_id:
        return TIER_STANDARD, "default"

    rows = (
        await db.execute(
            select(AreaHazardTier)
            .where(AreaHazardTier.plantId == plant_id)
            .where(AreaHazardTier.isActive.is_(True))
        )
    ).scalars().all()

    if area_id:
        for r in rows:
            if r.areaId == area_id:
                return r.hazardTier, "area"
    for r in rows:
        if r.areaId is None:
            return r.hazardTier, "plant"
    return TIER_STANDARD, "default"


async def find_matrix_rule(
    db: AsyncSession, *, axis: str | None, category: str | None, sub_category: str | None
) -> SeverityMatrixRule | None:
    if not axis or not category or not sub_category:
        return None
    return (
        await db.execute(
            select(SeverityMatrixRule)
            .where(SeverityMatrixRule.observationType == axis)
            .where(SeverityMatrixRule.category == category)
            .where(SeverityMatrixRule.subCategory == sub_category)
            .where(SeverityMatrixRule.isActive.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none()


# ── The resolver ────────────────────────────────────────────────────────────

NO_RULE_MESSAGE = "No matrix rule found — manual selection required"


async def resolve(
    db: AsyncSession,
    *,
    observation_type: object,
    category: str | None,
    sub_category: str | None,
    plant_id: str | None = None,
    area_id: str | None = None,
) -> dict:
    """The suggested severity for a classification, and how it was reached.

    `observation_type` accepts anything `normalise_axis` understands — a bare
    axis (`ACT`), a full `ObservationType` (`UNSAFE_ACT`), or the build spec's
    lowercase `unsafe_act`.

    Always returns the same shape. `suggested is None` is the supported
    "no opinion" answer, not a failure: the caller renders the plain manual
    dropdown and requires no justification for anything the observer picks.
    """
    axis = tax.normalise_axis(_as_str(observation_type) or None)
    category = (category or "").strip() or None
    sub_category = (sub_category or "").strip() or None

    rule = await find_matrix_rule(
        db, axis=axis, category=category, sub_category=sub_category
    )
    if rule is None:
        return {
            "suggested": None,
            "baseSeverity": None,
            "tierApplied": None,
            "tierSource": None,
            "tierUplifted": False,
            "rationale": NO_RULE_MESSAGE,
            "matrixRuleId": None,
            "observationType": axis,
            "categoryCode": category,
            "subCategoryCode": sub_category,
        }

    tier, tier_source = await get_area_hazard_tier(db, plant_id=plant_id, area_id=area_id)
    suggested = apply_tier_modifier(rule.baseSeverity, tier)

    return {
        "suggested": suggested,
        "baseSeverity": rule.baseSeverity,
        "tierApplied": tier,
        "tierSource": tier_source,
        # The form only mentions the area when the area actually changed the
        # answer — an unchanged Standard tier is noise in a helper line.
        "tierUplifted": suggested != rule.baseSeverity,
        "rationale": rule.rationale,
        "matrixRuleId": rule.id,
        "observationType": axis,
        "categoryCode": category,
        "subCategoryCode": sub_category,
    }


async def resolve_for_observation(db: AsyncSession, obs: Observation) -> dict:
    """`resolve` keyed off a persisted observation's own classification."""
    return await resolve(
        db,
        observation_type=obs.taxonomyAxis or obs.type,
        category=obs.categoryCode,
        sub_category=obs.subCategoryCode,
        plant_id=obs.plantId,
        area_id=obs.areaId,
    )


# ── Override gate + log ─────────────────────────────────────────────────────


class SeverityOverrideError(ValueError):
    """Raised when an override is submitted without a usable justification.
    The router turns this into a 400 — kept as a plain exception so the service
    stays importable from non-HTTP callers (seeds, migrations, tests)."""


def reason_is_usable(reason: str | None) -> bool:
    return bool(reason and reason.strip() and len(reason.strip()) >= MIN_OVERRIDE_REASON_CHARS)


def diverges(suggestion: dict, final_severity: object) -> bool:
    """True when a suggestion exists AND the submitted severity differs from it.

    No suggestion ⇒ never a divergence: with no rule seeded for this pair the
    observer has nothing to disagree with, and demanding a reason would be
    asking them to justify unconfigured policy.
    """
    suggested = suggestion.get("suggested")
    if not suggested:
        return False
    return normalise_severity(final_severity) != suggested


def require_reason(suggestion: dict, final_severity: object, reason: str | None) -> None:
    """Enforce the justification rule. No-op when nothing diverged."""
    if not diverges(suggestion, final_severity):
        return
    if reason_is_usable(reason):
        return
    raise SeverityOverrideError(
        f"Severity was changed from the suggested {suggestion['suggested'].title()} to "
        f"{normalise_severity(final_severity) or 'an unrecognised value'} — give a reason of "
        f"at least {MIN_OVERRIDE_REASON_CHARS} characters explaining why this observation "
        "differs from the suggestion."
    )


def log_override(
    db: AsyncSession,
    *,
    observation: Observation,
    suggestion: dict,
    final_severity: object,
    reason: str | None,
    actor_id: str | None,
    source: str = OVERRIDE_SOURCE_OBSERVER_FORM,
) -> SeverityOverrideLog | None:
    """Append one override row, or return None when nothing diverged.

    Staged in the caller's session so it commits atomically with the
    observation it describes — the row can never reference an id that was
    rolled back, and it can never be silently lost while the observation saves.
    Call AFTER the observation has been flushed, so `observation.id` exists.
    """
    if not diverges(suggestion, final_severity):
        return None

    row = SeverityOverrideLog(
        observationId=observation.id,
        suggestedSeverity=suggestion["suggested"],
        finalSeverity=normalise_severity(final_severity) or _as_str(final_severity),
        overrideReason=(reason or "").strip() or None,
        observationType=suggestion.get("observationType"),
        categoryCode=suggestion.get("categoryCode"),
        subCategoryCode=suggestion.get("subCategoryCode"),
        baseSeverity=suggestion.get("baseSeverity"),
        hazardTier=suggestion.get("tierApplied"),
        matrixRuleId=suggestion.get("matrixRuleId"),
        plantId=observation.plantId,
        areaId=observation.areaId,
        source=source,
        overriddenById=actor_id,
    )
    db.add(row)
    return row


async def existing_override(
    db: AsyncSession, *, observation_id: str, suggested: str, final: str
) -> SeverityOverrideLog | None:
    """A logged override on this record for exactly this (suggested, final) pair.

    Used by the edit path: re-saving an observation whose severity was already
    justified must not demand the same justification again. A *different* pair
    is a new decision and needs its own reason.
    """
    return (
        await db.execute(
            select(SeverityOverrideLog)
            .where(SeverityOverrideLog.observationId == observation_id)
            .where(SeverityOverrideLog.suggestedSeverity == suggested)
            .where(SeverityOverrideLog.finalSeverity == final)
            .limit(1)
        )
    ).scalars().first()


# ── Calibration reporting ───────────────────────────────────────────────────


def _direction(suggested: str, final: str) -> str:
    a, b = severity_index(suggested), severity_index(final)
    if a is None or b is None:
        return "unknown"
    if b > a:
        return "up"
    if b < a:
        return "down"
    return "same"


async def calibration_report(
    db: AsyncSession,
    *,
    plant_ids: list[str] | None,
    since=None,
    sources: tuple[str, ...] = (OVERRIDE_SOURCE_OBSERVER_FORM,),
    min_overrides: int = 1,
) -> list[dict]:
    """Override frequency per taxonomy pair — the feedback loop of §6.7.

    A pair overridden consistently in ONE direction is the signal worth acting
    on; a pair overridden in both directions usually means the sub-category
    itself spans two different exposures, which is a taxonomy problem rather
    than a matrix problem. Both are reported separately for that reason.

    `plant_ids is None` means all plants (the ALL_PLANTS scope). An empty list
    means no accessible plants and yields an empty report — never an unscoped
    one.
    """
    if plant_ids is not None and len(plant_ids) == 0:
        return []

    def _scope(stmt: Select, plant_col) -> Select:
        if plant_ids is not None:
            stmt = stmt.where(plant_col.in_(plant_ids))
        return stmt

    log_stmt = select(SeverityOverrideLog).where(SeverityOverrideLog.source.in_(sources))
    if since is not None:
        log_stmt = log_stmt.where(SeverityOverrideLog.createdAt >= since)
    logs = (await db.execute(_scope(log_stmt, SeverityOverrideLog.plantId))).scalars().all()

    # Denominator: observations carrying the same pair over the same window.
    # Counted from Observation rather than from confirmation rows, which is why
    # agreement needs no log entry at all.
    denom_stmt = select(
        Observation.taxonomyAxis,
        Observation.categoryCode,
        Observation.subCategoryCode,
        func.count().label("n"),
    ).where(Observation.categoryCode.is_not(None))
    if since is not None:
        denom_stmt = denom_stmt.where(Observation.createdAt >= since)
    denom_stmt = _scope(denom_stmt, Observation.plantId).group_by(
        Observation.taxonomyAxis, Observation.categoryCode, Observation.subCategoryCode
    )
    denominators = {
        (axis, cat, sub): n for axis, cat, sub, n in (await db.execute(denom_stmt)).all()
    }

    buckets: dict[tuple, dict] = {}
    for row in logs:
        key = (row.observationType, row.categoryCode, row.subCategoryCode)
        b = buckets.setdefault(
            key,
            {
                "observationType": row.observationType,
                "categoryCode": row.categoryCode,
                "subCategoryCode": row.subCategoryCode,
                "suggestedSeverity": row.suggestedSeverity,
                "overrides": 0,
                "up": 0,
                "down": 0,
                "observations": denominators.get(key, 0),
            },
        )
        b["overrides"] += 1
        direction = _direction(row.suggestedSeverity, row.finalSeverity)
        if direction in ("up", "down"):
            b[direction] += 1
        # The most recent rule's suggestion is the one worth reporting against.
        b["suggestedSeverity"] = row.suggestedSeverity

    out: list[dict] = []
    for b in buckets.values():
        if b["overrides"] < min_overrides:
            continue
        total = b["overrides"]
        # Denominator can lag the numerator when an observation was deleted or
        # its category later edited; never report a rate above 100%.
        seen = max(b["observations"], total)
        consistent = max(b["up"], b["down"])
        b["observations"] = seen
        b["overrideRatePct"] = round(100.0 * total / seen, 1) if seen else None
        b["dominantDirection"] = (
            "up" if b["up"] > b["down"] else "down" if b["down"] > b["up"] else "mixed"
        )
        b["directionConsistencyPct"] = round(100.0 * consistent / total, 1) if total else None
        out.append(b)

    # Worst offenders first: most overridden, then most one-directional.
    out.sort(key=lambda r: (r["overrides"], r["directionConsistencyPct"] or 0), reverse=True)
    return out


__all__ = [
    "MIN_OVERRIDE_REASON_CHARS",
    "NO_RULE_MESSAGE",
    "SeverityOverrideError",
    "apply_tier_modifier",
    "calibration_report",
    "diverges",
    "existing_override",
    "find_matrix_rule",
    "get_area_hazard_tier",
    "log_override",
    "normalise_severity",
    "reason_is_usable",
    "require_reason",
    "resolve",
    "resolve_for_observation",
    "severity_index",
]
