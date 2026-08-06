"""Daily Brief cards for the Chemical / Hazmat module (spec §6, §7 #8).

Four cards, in the order they matter operationally:

  1. **A failed MOC trigger** — critical. An automatic obligation did not get
     raised and a human has to do it by hand. This card is the visible half of
     the reliability fix; the MocTriggerLog row is the durable half. Neither
     alone is enough: a row nobody reads is the state the platform was already
     in, and a card with no row behind it cannot be audited.
  2. **A breached threshold** — critical. The site has crossed a statutory
     quantity and owes an obligation.
  3. **An approaching threshold** — attention, and the one that is actually
     preventive. A breach card tells you about a problem; an approaching card
     is the last point at which it is avoidable.
  4. **An unreviewed co-storage override** — attention. Someone accepted an
     incompatible-storage warning; until it is reviewed, that is an accepted
     risk with no owner.

Overdue SDS reviews are a fifth signal, but they are a *state* rather than an
event and are already served by `GET /api/chemicals/dashboard`, which the brief
composes directly. Emitting a domain event per overdue sheet per night would
produce a card per chemical per day — the exact noise pattern that trains people
to dismiss the brief.

Rules are pure functions over the event payload plus the narrow RuleContext, so
they unit-test with a fake context and no DB (house style — test_alert_rules.py).
"""

from __future__ import annotations

from app.services.alerts import AlertDraft, ImpactedEntity, ImpactRule

_HSE_AUDIENCE = ["SAFETY_OFFICER", "HSE_MANAGER", "PLANT_HEAD", "CORPORATE_HSE"]
_STORE_AUDIENCE = ["SAFETY_OFFICER", "HSE_MANAGER", "STORE_MANAGER", "PLANT_HEAD"]


# ── 1. failed MOC trigger ─────────────────────────────────────────────────────
async def _resolve_trigger_failed(event, ctx):  # noqa: ANN001
    p = event.payload or {}
    rule_name = p.get("ruleName") or "Automatic MOC trigger"
    reason = p.get("failureReason") or "no reason recorded"
    return [
        AlertDraft(
            severity="critical",
            title=f"{rule_name} FAILED — the MOC was not raised",
            body_text=(
                "An automatic change request could not be created. The inventory "
                "movement was saved, but the regulatory obligation it triggers has "
                "NOT been raised and needs to be created manually. "
                f"Reason: {reason}"
            ),
            body_template_key="chemical_trigger_failed",
            body_params={
                "ruleName": rule_name,
                "failureReason": reason,
                "scheduleReference": p.get("scheduleReference"),
            },
            # Per source entity, not per occurrence: a rule failing on every
            # receipt should produce one card with a count, not forty cards.
            dedupe_key=f"chemical.trigger_failed:{event.entityId}",
            site_id=event.siteId,
            impacted=[],
            deep_link="/chemicals/trigger-log?status=FAILED",
            audience_roles=_HSE_AUDIENCE,
        )
    ]


RULE_TRIGGER_FAILED = ImpactRule(
    key="chemical_trigger_failed",
    event_types=("chemical.trigger_failed",),
    resolve=_resolve_trigger_failed,
)


# ── 2. threshold breached ─────────────────────────────────────────────────────
async def _resolve_threshold_breached(event, ctx):  # noqa: ANN001
    p = event.payload or {}
    schedule = p.get("scheduleReference") or "a regulatory threshold"
    observed = p.get("observedQuantity")
    threshold = p.get("thresholdQuantity")
    unit = p.get("unit") or ""
    obligation = p.get("triggerObligation")
    moc_id = p.get("mocId")
    moc_number = p.get("mocNumber")

    qty_line = (
        f"{observed:,.0f} {unit} against a limit of {threshold:,.0f} {unit}"
        if isinstance(observed, (int, float)) and isinstance(threshold, (int, float))
        else "the site inventory has crossed the limit"
    )
    return [
        AlertDraft(
            severity="critical",
            title=f"Threshold breached — {schedule}",
            body_text=(
                f"Site inventory is {qty_line}. "
                + (f"Obligation engaged: {obligation.replace('_', ' ').lower()}. " if obligation else "")
                + (f"{moc_number} was raised automatically." if moc_number else
                   "No MOC was raised — check the trigger log.")
            ),
            body_template_key="chemical_threshold_breached",
            body_params={
                "scheduleReference": schedule,
                "observedQuantity": observed,
                "thresholdQuantity": threshold,
                "unit": unit,
                "triggerObligation": obligation,
                "mocNumber": moc_number,
            },
            dedupe_key=f"chemical.threshold_breached:{event.entityId}:{p.get('ruleId')}",
            site_id=event.siteId,
            impacted=(
                [ImpactedEntity(type="MOC", id=moc_id, ref=moc_number or moc_id,
                                label="Auto-raised change request", href=f"/moc/{moc_id}")]
                if moc_id else []
            ),
            deep_link="/chemicals/thresholds",
            audience_roles=_HSE_AUDIENCE,
        )
    ]


RULE_THRESHOLD_BREACHED = ImpactRule(
    key="chemical_threshold_breached",
    event_types=("chemical.threshold_breached",),
    resolve=_resolve_threshold_breached,
)


# ── 3. threshold approaching ──────────────────────────────────────────────────
async def _resolve_threshold_approaching(event, ctx):  # noqa: ANN001
    p = event.payload or {}
    schedule = p.get("scheduleReference") or "a regulatory threshold"
    pct = p.get("percentOfThreshold")
    return [
        AlertDraft(
            # `attention`, not `critical`: nothing is non-compliant yet, and
            # crying critical before the line is crossed is how a brief loses
            # the reader it needs when the line IS crossed.
            severity="attention",
            title=(
                f"Approaching {schedule}"
                + (f" — {pct:.0f}% of limit" if isinstance(pct, (int, float)) else "")
            ),
            body_text=(
                "Site inventory is close to a statutory threshold. Acting now — "
                "redistributing stock or scheduling disposal — avoids the "
                "obligation entirely; once the limit is crossed it must be "
                "discharged."
            ),
            body_template_key="chemical_threshold_approaching",
            body_params={
                "scheduleReference": schedule,
                "percentOfThreshold": pct,
                "unit": p.get("unit"),
            },
            dedupe_key=f"chemical.threshold_approaching:{event.entityId}:{p.get('ruleId')}",
            site_id=event.siteId,
            deep_link="/chemicals/thresholds",
            audience_roles=_STORE_AUDIENCE,
        )
    ]


RULE_THRESHOLD_APPROACHING = ImpactRule(
    key="chemical_threshold_approaching",
    event_types=("chemical.threshold_approaching",),
    resolve=_resolve_threshold_approaching,
)


# ── 4. unreviewed co-storage override ─────────────────────────────────────────
async def _resolve_storage_override(event, ctx):  # noqa: ANN001
    p = event.payload or {}
    this_chem = p.get("chemicalName") or "a chemical"
    other_chem = p.get("conflictingChemicalName") or "an incompatible chemical"
    location = p.get("storageLocationName") or "a storage location"
    return [
        AlertDraft(
            severity="attention",
            title=f"Co-storage override pending review — {this_chem} with {other_chem}",
            body_text=(
                f"An incompatible-storage warning was overridden at {location}. "
                f"Reason given: {p.get('overrideReason') or 'none recorded'}. "
                f"Until it is reviewed this is an accepted risk with no owner."
            ),
            body_template_key="chemical_storage_override",
            body_params={
                "chemicalName": this_chem,
                "conflictingChemicalName": other_chem,
                "storageLocationName": location,
                "overrideReason": p.get("overrideReason"),
            },
            dedupe_key=f"chemical.storage_override:{event.entityId}",
            site_id=event.siteId,
            deep_link="/chemicals/storage",
            audience_roles=_STORE_AUDIENCE,
        )
    ]


RULE_STORAGE_OVERRIDE = ImpactRule(
    key="chemical_storage_override",
    event_types=("chemical.storage_override",),
    resolve=_resolve_storage_override,
)


ALL_RULES = (
    RULE_TRIGGER_FAILED,
    RULE_THRESHOLD_BREACHED,
    RULE_THRESHOLD_APPROACHING,
    RULE_STORAGE_OVERRIDE,
)
