"""Regulatory-threshold evaluation and the auto-MOC-on-breach trigger (§4.3).

This is the module's core value proposition, and it is the exact place the build
spec warns about: wiring it to the platform's previous trigger pattern would
have shipped a second trigger that can fail with nobody finding out. So it runs
through `app.services.trigger_engine`, which provides the SAVEPOINT isolation,
the guaranteed non-empty failure reason, the HSE Manager notification and the
DomainEvent. What is left here is the domain logic:

**Edge-triggered, not level-triggered.** Every RECEIPT and TRANSFER recomputes
the site total for the affected rules, but an MOC is raised on the *transition*
BELOW/APPROACHING → BREACHED, recorded in `ChemicalThresholdState`. Firing on
every receipt while a site sits above a threshold would raise dozens of MOCs for
one regulatory fact, and the observable result of that is approvers who close
MOCs without reading them — a worse outcome than not firing at all.

**Aggregation is over the hazard class at the site, not the item.** MSIHC
thresholds are site inventories of a class of substance. Summing one batch, or
one chemical, would under-report exactly the situation the rule exists to catch:
five separate flammables that individually look fine.

**Unit normalisation is explicit and refuses to guess.** A rule in KG cannot be
compared to stock in L without a density, and this module does not hold
densities (that is SDS data, and the SDS is evidence here, not a parsed source —
§0). Mismatched units are reported as a SKIPPED evaluation naming the problem,
never silently coerced. A threshold engine that quietly treats 1 L as 1 KG is
worse than one that says it cannot tell.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chemical import (
    ChemicalInventoryItem,
    ChemicalMaster,
    ChemicalThresholdRule,
    ChemicalThresholdState,
    MocTriggerLog,
)
from app.services.trigger_engine import (
    TriggerOutcome,
    TriggerResult,
    TriggerRun,
    run_trigger_rules,
)

logger = logging.getLogger(__name__)

#: Mass units normalised to kilograms, volume units to litres. Cross-family
#: conversion is deliberately absent — see the module docstring.
_MASS_TO_KG = {"KG": 1.0, "G": 0.001, "MG": 1e-6, "T": 1000.0, "TONNE": 1000.0, "MT": 1000.0}
_VOLUME_TO_L = {"L": 1.0, "ML": 0.001, "KL": 1000.0, "M3": 1000.0}


def _canonical(quantity: float, unit: str) -> tuple[float, str] | None:
    u = (unit or "").strip().upper()
    if u in _MASS_TO_KG:
        return quantity * _MASS_TO_KG[u], "KG"
    if u in _VOLUME_TO_L:
        return quantity * _VOLUME_TO_L[u], "L"
    return None


def derive_status(quantity: float, threshold: float, approach_ratio: float) -> str:
    """BELOW | APPROACHING | BREACHED.

    Split out as a pure function so the band boundaries are unit-testable
    without a database — the previous generation of triggers had no tests at
    all, and the boundary (`>=`, not `>`) is exactly the kind of detail that
    silently shifts. At-threshold IS a breach: MSIHC thresholds are "quantities
    equal to or exceeding", and rounding a site under the limit is not this
    module's call to make.
    """
    if threshold <= 0:
        return "BELOW"
    if quantity >= threshold:
        return "BREACHED"
    if quantity >= threshold * approach_ratio:
        return "APPROACHING"
    return "BELOW"


@dataclass
class ThresholdEvaluation:
    """One rule's standing at one site, after this evaluation."""

    rule: ChemicalThresholdRule
    plant_id: str
    observed_quantity: float
    threshold_quantity: float
    unit: str
    previous_status: str
    status: str  # BELOW | APPROACHING | BREACHED
    #: True only on the transition into BREACHED — the edge that raises an MOC.
    newly_breached: bool
    #: Set when the rule could not be evaluated (e.g. unit mismatch). The
    #: evaluation is reported, not dropped.
    skipped_reason: str | None = None
    contributing_chemicals: list[dict[str, Any]] = field(default_factory=list)


# ── aggregation ───────────────────────────────────────────────────────────────
async def _applicable_rules(
    db: AsyncSession,
    *,
    tenant_id: str,
    region: str,
    hazard_classes: Sequence[str],
    chemical_id: str | None,
) -> list[ChemicalThresholdRule]:
    """Rules that could bind for this chemical. Tenant rows shadow platform
    defaults with the same scheduleReference — otherwise a tenant that tightens
    a limit ends up evaluated against both its own rule and the default."""
    stmt = (
        select(ChemicalThresholdRule)
        .where(ChemicalThresholdRule.isActive.is_(True))
        .where(ChemicalThresholdRule.isDeleted.is_(False))
        .where(ChemicalThresholdRule.region == region)
        .where(
            or_(
                ChemicalThresholdRule.tenantId.is_(None),
                ChemicalThresholdRule.tenantId == tenant_id,
            )
        )
    )
    scope = [ChemicalThresholdRule.hazardClass.in_(list(hazard_classes))] if hazard_classes else []
    if chemical_id:
        scope.append(ChemicalThresholdRule.chemicalId == chemical_id)
    if scope:
        stmt = stmt.where(or_(*scope))
    rows = (await db.execute(stmt)).scalars().all()

    by_ref: dict[str, ChemicalThresholdRule] = {}
    for r in rows:
        key = f"{r.scheduleReference}|{r.hazardClass or r.chemicalId}"
        incumbent = by_ref.get(key)
        if incumbent is None or (r.tenantId and not incumbent.tenantId):
            by_ref[key] = r
    return list(by_ref.values())


async def _site_quantity_for_rule(
    db: AsyncSession,
    *,
    tenant_id: str,
    plant_id: str,
    rule: ChemicalThresholdRule,
) -> tuple[float, str, list[dict[str, Any]], str | None]:
    """Total on-hand quantity at `plant_id` covered by `rule`.

    Returns (quantity_in_rule_units, unit, contributing_chemicals, skip_reason).
    """
    target = _canonical(rule.thresholdQuantity, rule.unit)
    if target is None:
        return 0.0, rule.unit, [], (
            f"Rule unit '{rule.unit}' is not a recognised mass or volume unit; "
            f"cannot evaluate '{rule.scheduleReference}'."
        )
    _, target_family = target

    stmt = (
        select(
            ChemicalMaster.id,
            ChemicalMaster.name,
            ChemicalInventoryItem.unit,
            func.sum(ChemicalInventoryItem.quantityLedger),
        )
        .join(ChemicalMaster, ChemicalMaster.id == ChemicalInventoryItem.chemicalId)
        .where(ChemicalInventoryItem.tenantId == tenant_id)
        .where(ChemicalInventoryItem.plantId == plant_id)
        .where(ChemicalInventoryItem.isDeleted.is_(False))
        .where(ChemicalInventoryItem.quantityLedger > 0)
        .group_by(ChemicalMaster.id, ChemicalMaster.name, ChemicalInventoryItem.unit)
    )
    if rule.chemicalId:
        stmt = stmt.where(ChemicalMaster.id == rule.chemicalId)
    elif rule.hazardClass:
        # JSONB containment: `hazardClasses @> '["FLAMMABLE"]'`. Index-friendly
        # and, unlike a LIKE over the serialised array, immune to a class name
        # that is a prefix of another.
        stmt = stmt.where(ChemicalMaster.hazardClasses.contains([rule.hazardClass]))
    else:
        return 0.0, rule.unit, [], "Rule names neither a hazard class nor a chemical."

    rows = (await db.execute(stmt)).all()

    total_canonical = 0.0
    contributing: list[dict[str, Any]] = []
    incompatible_units: set[str] = set()
    for chem_id, chem_name, item_unit, qty in rows:
        conv = _canonical(float(qty or 0.0), item_unit)
        if conv is None or conv[1] != target_family:
            # Recorded, not silently dropped: an operator must be able to see
            # that 400 L of something is missing from a KG threshold total.
            incompatible_units.add(item_unit or "?")
            continue
        total_canonical += conv[0]
        contributing.append(
            {"chemicalId": chem_id, "chemicalName": chem_name, "quantity": float(qty), "unit": item_unit}
        )

    # Express the total back in the rule's own unit so the dashboard compares
    # like with like without the reader doing arithmetic.
    per_rule_unit = _canonical(1.0, rule.unit)
    total_in_rule_unit = total_canonical / per_rule_unit[0] if per_rule_unit and per_rule_unit[0] else total_canonical

    skip_reason = None
    if incompatible_units:
        skip_reason = (
            f"Stock held in {', '.join(sorted(incompatible_units))} could not be compared "
            f"against a threshold in {rule.unit} (no density on file — SDS values are "
            f"evidence, not parsed data). Those quantities are EXCLUDED from this total."
        )
    return total_in_rule_unit, rule.unit, contributing, skip_reason


# ── evaluation ────────────────────────────────────────────────────────────────
async def evaluate_thresholds(
    db: AsyncSession,
    *,
    tenant_id: str,
    plant_id: str,
    chemical_id: str | None = None,
    region: str = "IN",
    persist_state: bool = True,
) -> list[ThresholdEvaluation]:
    """Recompute standing for every rule that could bind at this site.

    `chemical_id` narrows rule selection to the substance that just moved (the
    hot path, called on every receipt). Omit it for the full-site sweep the
    dashboard and the nightly job use.
    """
    hazard_classes: list[str] = []
    if chemical_id:
        chem = await db.get(ChemicalMaster, chemical_id)
        if chem is None:
            return []
        hazard_classes = [str(c) for c in (chem.hazardClasses or [])]
    else:
        rows = (
            await db.execute(
                select(ChemicalMaster.hazardClasses)
                .join(ChemicalInventoryItem, ChemicalInventoryItem.chemicalId == ChemicalMaster.id)
                .where(ChemicalInventoryItem.plantId == plant_id)
                .where(ChemicalInventoryItem.quantityLedger > 0)
                .where(ChemicalInventoryItem.isDeleted.is_(False))
                .distinct()
            )
        ).scalars().all()
        hazard_classes = sorted({str(c) for arr in rows for c in (arr or [])})

    rules = await _applicable_rules(
        db,
        tenant_id=tenant_id,
        region=region,
        hazard_classes=hazard_classes,
        chemical_id=chemical_id,
    )

    now = datetime.now(timezone.utc)
    evaluations: list[ThresholdEvaluation] = []

    for rule in rules:
        qty, unit, contributing, skip = await _site_quantity_for_rule(
            db, tenant_id=tenant_id, plant_id=plant_id, rule=rule
        )

        state = (
            await db.execute(
                select(ChemicalThresholdState)
                .where(ChemicalThresholdState.tenantId == tenant_id)
                .where(ChemicalThresholdState.plantId == plant_id)
                .where(ChemicalThresholdState.ruleId == rule.id)
            )
        ).scalar_one_or_none()
        previous = state.status if state else "BELOW"

        status = derive_status(qty, rule.thresholdQuantity, rule.approachRatio)
        newly_breached = status == "BREACHED" and previous != "BREACHED"

        if persist_state:
            if state is None:
                state = ChemicalThresholdState(
                    tenantId=tenant_id, plantId=plant_id, ruleId=rule.id
                )
                db.add(state)
            state.status = status
            state.currentQuantity = qty
            state.thresholdQuantity = rule.thresholdQuantity
            state.unit = unit
            state.lastEvaluatedAt = now
            if newly_breached:
                state.lastBreachedAt = now
            if previous == "BREACHED" and status != "BREACHED":
                state.lastClearedAt = now
                # The obligation episode is over; a later re-breach is a new
                # regulatory event and must raise its own MOC.
                state.activeMocId = None

        evaluations.append(
            ThresholdEvaluation(
                rule=rule,
                plant_id=plant_id,
                observed_quantity=qty,
                threshold_quantity=rule.thresholdQuantity,
                unit=unit,
                previous_status=previous,
                status=status,
                newly_breached=newly_breached,
                skipped_reason=skip,
                contributing_chemicals=contributing,
            )
        )

    if persist_state:
        await db.flush()
    return evaluations


# ── the trigger ───────────────────────────────────────────────────────────────
def _breach_rule(ev: ThresholdEvaluation, *, tenant_id: str, actor_user_id: str):
    """Build the per-evaluation trigger rule handed to the shared engine.

    One rule object per breached threshold rather than one rule for all of them:
    a failure to raise the MOC for MSIHC Schedule 3 must not prevent the
    Schedule 2 MOC from being raised, and the engine's SAVEPOINT isolation only
    helps if the unit of isolation is the individual obligation.
    """

    async def _rule(db: AsyncSession, subject: Any) -> TriggerResult:
        from app.models.chemical import ChemicalThresholdState as _State
        from app.services.moc_autocreate import create_auto_moc

        rule = ev.rule
        label = f"Threshold breach — {rule.scheduleReference}"

        if not rule.autoMocOnBreach:
            return TriggerResult(
                rule_name=label,
                rule_id=f"rule_threshold_{rule.id}",
                outcome=TriggerOutcome.SKIPPED,
                reason=(
                    f"Threshold breached ({ev.observed_quantity:.2f} {ev.unit} vs "
                    f"{ev.threshold_quantity:.2f} {ev.unit}) but autoMocOnBreach is off "
                    f"for this rule. Obligation: {rule.triggerObligation}."
                ),
            )

        state = (
            await db.execute(
                select(_State)
                .where(_State.tenantId == tenant_id)
                .where(_State.plantId == ev.plant_id)
                .where(_State.ruleId == rule.id)
            )
        ).scalar_one_or_none()
        if state is not None and state.activeMocId:
            return TriggerResult(
                rule_name=label,
                rule_id=f"rule_threshold_{rule.id}",
                outcome=TriggerOutcome.SKIPPED,
                reason=f"MOC {state.activeMocId} is already open for this breach episode.",
            )

        contributors = ", ".join(
            f"{c['chemicalName']} ({c['quantity']:.1f} {c['unit']})"
            for c in ev.contributing_chemicals[:8]
        ) or "no itemised contributors recorded"

        # MocAutoCreateError is deliberately NOT caught here. The shared engine
        # converts it into a FAILED MocTriggerLog row with a non-empty reason,
        # notifies the HSE Manager and keeps the stack trace — all of which a
        # local `except` would throw away in exchange for a tidier-looking
        # function.
        cr = await create_auto_moc(
            db,
            plant_id=ev.plant_id,
            title=f"Regulatory threshold breached — {rule.scheduleReference}",
            description=(
                f"Site inventory has crossed the {rule.scheduleReference} threshold.\n\n"
                f"Observed: {ev.observed_quantity:.2f} {ev.unit}\n"
                f"Threshold: {ev.threshold_quantity:.2f} {ev.unit}\n"
                f"Scope: {rule.hazardClass or rule.chemicalId}\n"
                f"Triggered obligation: {rule.triggerObligation}\n\n"
                f"Contributing stock: {contributors}\n\n"
                f"This change request was raised automatically by the Chemical "
                f"Management module. Assess and discharge the obligation above, "
                f"then record the outcome against the Statutory Register."
                + (f"\n\nEvaluation caveat: {ev.skipped_reason}" if ev.skipped_reason else "")
            ),
            category="chemical_inventory",
            classification="major",
            origin="auto_trigger",
            origin_source_type="ChemicalThresholdRule",
            origin_source_id=rule.id,
            initiated_by_user_id=actor_user_id,
            business_justification=(
                f"Statutory obligation {rule.triggerObligation} is engaged by "
                f"{rule.scheduleReference}."
            ),
            hazard_categories=[rule.hazardClass] if rule.hazardClass else None,
            reviewers=[{"role": "HSE_MANAGER", "isRequired": True}],
            rationale=f"Auto-raised on {rule.scheduleReference} threshold breach",
        )

        if state is not None:
            state.activeMocId = cr.id

        return TriggerResult(
            rule_name=label,
            rule_id=f"rule_threshold_{rule.id}",
            outcome=TriggerOutcome.FIRED,
            reason=(
                f"Raised {cr.number}: {ev.observed_quantity:.2f} {ev.unit} exceeds the "
                f"{rule.scheduleReference} threshold of {ev.threshold_quantity:.2f} {ev.unit}. "
                f"Obligation: {rule.triggerObligation}."
            ),
            spawned_record_type="MOC",
            spawned_record_id=cr.id,
            data={
                "mocNumber": cr.number,
                "ruleId": rule.id,
                "scheduleReference": rule.scheduleReference,
                "observedQuantity": ev.observed_quantity,
                "thresholdQuantity": ev.threshold_quantity,
                "unit": ev.unit,
                "triggerObligation": rule.triggerObligation,
            },
        )

    _rule.trigger_name = f"Threshold breach — {ev.rule.scheduleReference}"  # type: ignore[attr-defined]
    return _rule


def _moc_trigger_log_sink(
    *, tenant_id: str, plant_id: str, trigger_type: str, evaluations: Sequence[ThresholdEvaluation]
):
    """Persist engine results as MocTriggerLog rows (spec §3).

    The engine already guarantees a non-empty `failure_reason` on FAILED, and
    the table has a CHECK that says the same thing. The belt-and-braces here is
    intentional: the spec singles this field out because an empty failure reason
    is how the previous generation of triggers managed to fail invisibly."""

    by_rule = {f"rule_threshold_{ev.rule.id}": ev for ev in evaluations}

    async def _sink(db: AsyncSession, subject: Any, results: list[TriggerResult]) -> None:
        for r in results:
            ev = by_rule.get(r.rule_id or "")
            failure = r.failure_reason
            if r.outcome is TriggerOutcome.FAILED and not (failure or "").strip():
                failure = "Rule failed without reporting a reason (engine invariant violated)."
            db.add(
                MocTriggerLog(
                    tenantId=tenant_id,
                    plantId=plant_id,
                    triggerType=trigger_type,
                    sourceEntityType="ChemicalThresholdRule",
                    sourceEntityId=(ev.rule.id if ev else (r.rule_id or "unknown")),
                    mocId=r.spawned_record_id,
                    mocNumber=(r.data or {}).get("mocNumber"),
                    status=r.outcome.value,
                    reason=r.reason,
                    failureReason=failure,
                    stackTrace=r.stack,
                    ruleId=(ev.rule.id if ev else None),
                    scheduleReference=(ev.rule.scheduleReference if ev else None),
                    observedQuantity=(ev.observed_quantity if ev else None),
                    thresholdQuantity=(ev.threshold_quantity if ev else None),
                    unit=(ev.unit if ev else None),
                )
            )
        await db.flush()

    return _sink


async def evaluate_and_trigger(
    db: AsyncSession,
    *,
    tenant_id: str,
    plant_id: str,
    chemical_id: str | None,
    actor_user_id: str,
    trigger_type: str = "THRESHOLD_BREACH",
    region: str = "IN",
) -> tuple[list[ThresholdEvaluation], TriggerRun | None]:
    """The §4.3 entry point: recompute site totals, raise an MOC on each new
    breach, and record an explicit FIRED/FAILED/SKIPPED row for every one.

    Called from the ledger service on RECEIPT and TRANSFER. Returns the full
    evaluation set (so the caller can surface APPROACHING sites too) and the
    trigger run, or None when nothing newly breached.
    """
    evaluations = await evaluate_thresholds(
        db, tenant_id=tenant_id, plant_id=plant_id, chemical_id=chemical_id, region=region
    )

    # Report evaluations that could not be computed even when nothing breached —
    # "we could not tell" must not look like "we checked and it was fine".
    for ev in evaluations:
        if ev.skipped_reason and not ev.newly_breached:
            logger.warning(
                "[chemical_threshold] plant=%s rule=%s not fully evaluable: %s",
                plant_id, ev.rule.scheduleReference, ev.skipped_reason,
            )

    breaches = [ev for ev in evaluations if ev.newly_breached]
    if not breaches:
        return evaluations, None

    run = await run_trigger_rules(
        db,
        [_breach_rule(ev, tenant_id=tenant_id, actor_user_id=actor_user_id) for ev in breaches],
        None,
        source_kind="ChemicalThresholdBreach",
        source_id=plant_id,
        sink=_moc_trigger_log_sink(
            tenant_id=tenant_id,
            plant_id=plant_id,
            trigger_type=trigger_type,
            evaluations=breaches,
        ),
        site_id=plant_id,
        failure_audience_roles=("HSE_MANAGER", "PLANT_HEAD"),
    )

    _emit_brief_events(db, plant_id=plant_id, evaluations=evaluations, run=run)
    return evaluations, run


def _emit_brief_events(
    db: AsyncSession,
    *,
    plant_id: str,
    evaluations: Sequence[ThresholdEvaluation],
    run: TriggerRun,
) -> None:
    """Domain events for the Daily Brief cards (app/services/alerts/rules/
    chemical_signals.py). Synchronous — `emit` is a session.add; the caller's
    commit publishes them alongside the ledger row that caused them."""
    from app.services.events import emit

    by_rule = {f"rule_threshold_{ev.rule.id}": ev for ev in evaluations}

    for r in run.results:
        ev = by_rule.get(r.rule_id or "")
        if ev is None:
            continue
        rule = ev.rule
        if r.outcome is TriggerOutcome.FAILED:
            emit(
                db,
                event_type="chemical.trigger_failed",
                entity_type="ChemicalThresholdRule",
                entity_id=rule.id,
                entity_ref=rule.scheduleReference,
                site_id=plant_id,
                payload={
                    "ruleName": r.rule_name,
                    "failureReason": r.failure_reason,
                    "scheduleReference": rule.scheduleReference,
                },
            )
        elif r.outcome is TriggerOutcome.FIRED:
            emit(
                db,
                event_type="chemical.threshold_breached",
                entity_type="ChemicalThresholdRule",
                entity_id=rule.id,
                entity_ref=rule.scheduleReference,
                site_id=plant_id,
                payload={
                    "ruleId": rule.id,
                    "scheduleReference": rule.scheduleReference,
                    "observedQuantity": ev.observed_quantity,
                    "thresholdQuantity": ev.threshold_quantity,
                    "unit": ev.unit,
                    "triggerObligation": rule.triggerObligation,
                    "mocId": r.spawned_record_id,
                    "mocNumber": (r.data or {}).get("mocNumber"),
                },
            )

    # Approaching is the preventive card, and it is emitted on the transition
    # only — a site that sits at 85% for a month should generate one card, not
    # thirty.
    for ev in evaluations:
        if ev.status == "APPROACHING" and ev.previous_status != "APPROACHING":
            emit(
                db,
                event_type="chemical.threshold_approaching",
                entity_type="ChemicalThresholdRule",
                entity_id=ev.rule.id,
                entity_ref=ev.rule.scheduleReference,
                site_id=plant_id,
                payload={
                    "ruleId": ev.rule.id,
                    "scheduleReference": ev.rule.scheduleReference,
                    "percentOfThreshold": (
                        100 * ev.observed_quantity / ev.threshold_quantity
                        if ev.threshold_quantity else None
                    ),
                    "unit": ev.unit,
                },
            )


__all__ = [
    "ThresholdEvaluation",
    "derive_status",
    "evaluate_thresholds",
    "evaluate_and_trigger",
]
