"""rca.completed → "RCA closed → n corrective actions now active, earliest due …"."""

from __future__ import annotations

from app.services.alerts import AlertDraft, ImpactedEntity, ImpactRule


async def _resolve(event, ctx):  # noqa: ANN001
    capas = [c for c in await ctx.capas_for_source("ENTERPRISE_RCA", event.entityId) if c.open]
    if not capas:
        return []
    due_dates = sorted([c.dueAt for c in capas if c.dueAt is not None])
    earliest = due_dates[0].date().isoformat() if due_dates else "not set"
    ref = event.entityRef or "RCA"
    return [
        AlertDraft(
            severity="attention",
            title=f"{ref} closed → {len(capas)} corrective action{'s' if len(capas) != 1 else ''} now active",
            body_text=f"Earliest closure due {earliest}. Owners are on the hook from today — verify the plans are staffed.",
            body_template_key="rca_completed",
            body_params={"rcaRef": ref, "capaCount": len(capas), "earliestDue": earliest},
            dedupe_key=f"rca.completed:{event.entityId}",
            site_id=event.siteId,
            impacted=[
                ImpactedEntity(type="CAPA", id=c.id, ref=c.number, label=c.title or c.number, href=f"/capa/{c.id}")
                for c in capas[:8]
            ],
            deep_link=f"/erm/rca/{event.entityId}",
            audience_roles=["SAFETY_OFFICER", "HSE_MANAGER", "PLANT_HEAD", "CORPORATE_HSE"],
        )
    ]


RULE = ImpactRule(key="rca_completed", event_types=("rca.completed",), resolve=_resolve)
