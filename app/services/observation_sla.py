"""Target-closure-date SLA policy for Safety Observations.

Replaces the free-text `Target Closure Date` the reportee used to type with a
severity × category-group matrix, an audit trail, and a reason-required
override.

── How categoryGroup is resolved ────────────────────────────────────────────
From the `ObservationCategoryGroup` table — configuration, not code.

The first build derived the group from the act/condition axis alone: an act is
behavioural, a condition is physical. That is defensible, and it is why "PPE not
worn" (STOP-3 on the ACT axis) resolved to Behavioral. It was not a mapping
bug — but it took the decision away from the policy owner, who could not
override it, and it could not express "not decided yet" at all.

The mapping is now seeded per the published DuPont grouping (STOP-1
behavioural; STOP-3…STOP-6 physical) and editable in Settings → Configuration →
Observation SLA Matrix.

Two properties worth keeping:

* **STOP-2 (Positions of People) is seeded PENDING_DECISION, not guessed.** It
  resolves no SLA at all, so the form falls back to manual entry with the same
  inline warning a missing config row produces. Nothing picks a band for it.

* **Axis derivation survives as the last-resort fallback.** `SAFE_ACT` and
  `SAFE_CONDITION` carry NO categoryCode — `validate_selection` returns
  (None, None, None) for them — so a purely category-keyed lookup would have
  nothing to match and would push every safe observation to manual entry. The
  axis fallback keeps them auto-calculating, and also covers any category added
  to the taxonomy before someone configures it here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.observation import Observation
from app.models.observation_sla import (
    AXIS_ANY,
    CATEGORY_GROUP_BEHAVIORAL,
    CATEGORY_GROUP_PENDING,
    CATEGORY_GROUP_PHYSICAL,
    SOURCE_AUTO_SLA,
    SOURCE_MANUAL_NO_POLICY,
    ObservationCategoryGroup,
    ObservationSlaConfig,
    ObservationTargetDateHistory,
)
from app.services import observation_taxonomy as tax

# Minimum length of a manual-override justification. Enforced HERE (server
# side) as well as in the form — spec §7 explicitly tests the API directly.
MIN_OVERRIDE_REASON_CHARS = 10


def category_group_for_axis(axis: str | None) -> str | None:
    """Last-resort derivation: ACT → BEHAVIORAL, CONDITION → PHYSICAL.

    Only reached when the category has no `ObservationCategoryGroup` row —
    which is always the case for SAFE_ACT / SAFE_CONDITION, since safe
    observations carry no STOP category at all.
    """
    if axis == "ACT":
        return CATEGORY_GROUP_BEHAVIORAL
    if axis == "CONDITION":
        return CATEGORY_GROUP_PHYSICAL
    return None


def category_group_for_type(obs_type: object) -> str | None:
    """The fallback group for an observation type. Works for all four
    ObservationType values — the safe ones have no STOP category but still have
    an axis."""
    return category_group_for_axis(tax.axis_for_type(obs_type))


async def resolve_category_group(
    db: AsyncSession,
    *,
    category_code: str | None,
    axis: str | None,
    obs_type: object = None,
) -> tuple[str | None, str]:
    """The configured SLA band for a category, and how it was decided.

    Returns `(group, source)` where source is one of:
      • ``category_axis``  — an exact (categoryCode, axis) row
      • ``category_any``   — a (categoryCode, "ANY") row
      • ``axis_fallback``  — no row for this category; derived from the axis
      • ``pending``        — mapped to PENDING_DECISION; group is None so the
                             caller resolves NO SLA and falls back to manual

    `pending` returns None for the group deliberately: a caller that forgets to
    check `source` still cannot accidentally apply a policy to an undecided
    category, because there is no group to look one up with.
    """
    fallback_axis = axis or tax.axis_for_type(obs_type)

    if category_code:
        rows = (
            await db.execute(
                select(ObservationCategoryGroup)
                .where(ObservationCategoryGroup.categoryCode == category_code)
                .where(ObservationCategoryGroup.isActive.is_(True))
            )
        ).scalars().all()
        # Exact axis beats ANY, so a category can be split per axis later
        # without a schema change if that is what the policy owner decides.
        match = next((r for r in rows if fallback_axis and r.axis == fallback_axis), None)
        source = "category_axis"
        if match is None:
            match = next((r for r in rows if r.axis == AXIS_ANY), None)
            source = "category_any"
        if match is not None:
            if match.categoryGroup == CATEGORY_GROUP_PENDING:
                return None, "pending"
            return match.categoryGroup, source

    return category_group_for_axis(fallback_axis), "axis_fallback"


def _severity_str(severity: object) -> str:
    """Severity enum member or plain string → its uppercase value.
    `str(Severity.HIGH)` yields 'Severity.HIGH', so .value must be used."""
    val = getattr(severity, "value", None) or getattr(severity, "name", None) or severity
    return str(val).upper()


async def resolve_sla_row(
    db: AsyncSession, *, plant_id: str | None, severity: object, category_group: str | None
) -> ObservationSlaConfig | None:
    """The active SLA row for this combination, plant row winning over global.

    Returns None when nothing matches — a missing or deactivated row is a
    supported state, not an error: §2.1 requires the form to fall back to manual
    entry rather than block submission on unconfigured policy.
    """
    if not category_group:
        return None
    sev = _severity_str(severity)
    rows = (
        await db.execute(
            select(ObservationSlaConfig)
            .where(ObservationSlaConfig.severity == sev)
            .where(ObservationSlaConfig.categoryGroup == category_group)
            .where(ObservationSlaConfig.isActive.is_(True))
        )
    ).scalars().all()
    # Plant-specific beats global; same precedence as TrainingRuleConfig.
    for r in rows:
        if plant_id is not None and r.plantId == plant_id:
            return r
    for r in rows:
        if r.plantId is None:
            return r
    return None


def applied_snapshot(row: ObservationSlaConfig) -> dict:
    """The frozen policy record stamped onto the observation. Frozen so editing
    the matrix later cannot retroactively restate what an existing record was
    held to (spec §7, first checklist item)."""
    return {
        "configId": row.id,
        "severity": row.severity,
        "categoryGroup": row.categoryGroup,
        "slaDays": row.slaDays,
        "scope": "PLANT" if row.plantId else "GLOBAL",
        "plantId": row.plantId,
    }


def compute_target_date(observation_date: datetime, sla_days: int) -> datetime:
    """observationDate + slaDays, calendar days.

    Calendar days is the spec's stated v1 default (open question 2). Working
    days would need a holiday calendar per plant, which this schema does not
    have — inventing one would produce dates no site could reconcile against
    its own shift calendar.
    """
    return observation_date + timedelta(days=sla_days)


async def preview(
    db: AsyncSession,
    *,
    plant_id: str | None,
    obs_type: object,
    severity: object,
    observation_date: datetime,
    category_code: str | None = None,
) -> dict:
    """What the form shows before submission. Returns the same shape whether or
    not a policy matched, so the client renders either the read-only auto date
    or the manual-entry fallback from one response.

    `reason` distinguishes the two ways a policy can be absent — an undecided
    category mapping versus a genuinely missing matrix row — so the warning can
    say which it is instead of blaming config that is actually fine.
    """
    group, group_source = await resolve_category_group(
        db, category_code=category_code, axis=None, obs_type=obs_type
    )
    row = (
        None
        if group is None
        else await resolve_sla_row(
            db, plant_id=plant_id, severity=severity, category_group=group
        )
    )
    if row is None:
        return {
            "matched": False,
            "categoryGroup": group,
            "categoryGroupSource": group_source,
            "reason": "PENDING_DECISION" if group_source == "pending" else "NO_POLICY",
            "severity": _severity_str(severity),
            "slaDays": None,
            "targetDate": None,
            "label": None,
        }
    target = compute_target_date(observation_date, row.slaDays)
    return {
        "matched": True,
        "categoryGroup": group,
        "categoryGroupSource": group_source,
        "reason": None,
        "severity": row.severity,
        "slaDays": row.slaDays,
        "targetDate": target,
        "scope": "PLANT" if row.plantId else "GLOBAL",
        "label": (
            f"Auto-set per SLA policy ({row.severity.title()} / "
            f"{row.categoryGroup.title()} → {row.slaDays} days)"
        ),
    }


def record_history(
    db: AsyncSession,
    *,
    observation_id: str,
    target_date: datetime | None,
    source: str,
    reason: str | None = None,
    sla_config: dict | None = None,
    changed_by_id: str | None = None,
) -> ObservationTargetDateHistory:
    """Append one row to the closure-date trail. Staged in the caller's session
    so it commits atomically with the change it describes."""
    row = ObservationTargetDateHistory(
        observationId=observation_id,
        targetDate=target_date,
        source=source,
        reason=reason,
        slaConfigApplied=sla_config,
        changedById=changed_by_id,
    )
    db.add(row)
    return row


async def apply_on_create(
    db: AsyncSession,
    obs: Observation,
    *,
    submitted_target_date: datetime | None,
    actor_id: str | None,
) -> None:
    """Set `targetDate` + provenance at submission and open the history trail.

    Policy wins over whatever the client sent: the field is read-only in the UI
    when a policy exists, and trusting a client-supplied date here would make
    the "read-only" purely cosmetic. When no policy matches, the client's own
    date is kept (free-text fallback, §2.1) — including None.
    """
    group, group_source = await resolve_category_group(
        db, category_code=obs.categoryCode, axis=obs.taxonomyAxis, obs_type=obs.type
    )
    row = (
        None
        if group is None
        else await resolve_sla_row(
            db, plant_id=obs.plantId, severity=obs.severity, category_group=group
        )
    )

    if row is None:
        obs.targetDate = submitted_target_date
        obs.targetDateSource = SOURCE_MANUAL_NO_POLICY
        obs.targetDateSlaConfig = None
        record_history(
            db,
            observation_id=obs.id,
            target_date=submitted_target_date,
            source=SOURCE_MANUAL_NO_POLICY,
            reason=(
                f"Category '{obs.categoryCode}' is not yet assigned a Behavioural / "
                "Physical group — closure date set manually pending that decision."
                if group_source == "pending"
                else "No active SLA policy for this severity / category group."
            ),
            changed_by_id=actor_id,
        )
        return

    snapshot = applied_snapshot(row)
    # Record HOW the group was chosen, so an audit can tell a configured
    # mapping from the axis fallback without re-deriving it.
    snapshot["categoryCode"] = obs.categoryCode
    snapshot["categoryGroupSource"] = group_source
    obs.targetDate = compute_target_date(obs.date, row.slaDays)
    obs.targetDateSource = SOURCE_AUTO_SLA
    obs.targetDateSlaConfig = snapshot
    record_history(
        db,
        observation_id=obs.id,
        target_date=obs.targetDate,
        source=SOURCE_AUTO_SLA,
        sla_config=snapshot,
        changed_by_id=actor_id,
    )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "MIN_OVERRIDE_REASON_CHARS",
    "category_group_for_axis",
    "category_group_for_type",
    "resolve_category_group",
    "resolve_sla_row",
    "applied_snapshot",
    "compute_target_date",
    "preview",
    "record_history",
    "apply_on_create",
    "now_utc",
]
