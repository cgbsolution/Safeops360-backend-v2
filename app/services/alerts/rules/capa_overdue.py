"""capa.overdue (daily scan) → "CAPA-{ref} overdue {d} days → linked to {source}".

Severity inherits from the source record (spec): a CAPA hanging off a
fatal-potential RCA/incident escalates to CRITICAL; the rest are ATTENTION.
CAPA overdue is a query-time predicate on the platform (no persisted state) —
the scan job emits one event per overdue CAPA per day and the alert dedupe
window collapses repeats into a counter.
"""

from __future__ import annotations

from app.services.alerts import AlertDraft, ImpactedEntity, ImpactRule


async def _resolve(event, ctx):  # noqa: ANN001
    p = event.payload or {}
    days = int(p.get("daysOverdue") or 0)
    source_ref = p.get("sourceRef")
    source_href = p.get("sourceHref")
    source_severity = (p.get("sourceSeverity") or "").upper()
    ref = event.entityRef or "CAPA"

    critical = source_severity in ("CRITICAL", "FATALITY", "HIGH_FATAL_POTENTIAL")
    linked = f" → linked to {source_ref}" if source_ref else ""
    tail = " (fatal-potential source)" if critical else ""

    impacted = [ImpactedEntity(type="CAPA", id=event.entityId, ref=ref, label=p.get("title") or ref, href=f"/capa/{event.entityId}")]
    if source_ref and source_href:
        impacted.append(ImpactedEntity(type=p.get("sourceType") or "Source", id=p.get("sourceId") or "", ref=source_ref, label=source_ref, href=source_href))

    return [
        AlertDraft(
            severity="critical" if critical else "attention",
            title=f"{ref} overdue {days} day{'s' if days != 1 else ''}{linked}{tail}",
            body_text=f"Owner: {p.get('ownerName') or 'unassigned'}. Escalation chain notified — closure target was {p.get('dueDate') or 'unset'}.",
            body_template_key="capa_overdue",
            body_params={"capaRef": ref, "daysOverdue": days, "sourceRef": source_ref, "sourceSeverity": source_severity},
            dedupe_key=f"capa.overdue:{event.entityId}",
            site_id=event.siteId,
            impacted=impacted,
            deep_link=f"/capa/{event.entityId}",
            audience_roles=["SAFETY_OFFICER", "HSE_MANAGER", "PLANT_HEAD", "CORPORATE_HSE"],
        )
    ]


RULE = ImpactRule(key="capa_overdue", event_types=("capa.overdue",), resolve=_resolve)
