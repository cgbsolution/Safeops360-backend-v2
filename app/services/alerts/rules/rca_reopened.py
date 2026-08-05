"""rca.reopened → CRITICAL: permits in the origin area rely on its controls.

No first-class RCA→PTW link exists in the schema (DECISIONS.md D8) — the
honest impact set is the ACTIVE permits in the same plant+area as the RCA's
origin event, plus its open CAPAs.
"""

from __future__ import annotations

from app.services.alerts import AlertDraft, ImpactedEntity, ImpactRule


async def _resolve(event, ctx):  # noqa: ANN001
    ref = event.entityRef or "RCA"
    plant_id, area_id = await ctx.rca_origin_area(event.entityId)
    permits = await ctx.active_permits(plant_id, area_id) if plant_id else []
    capas = [c for c in await ctx.capas_for_source("ENTERPRISE_RCA", event.entityId) if c.open]
    area = await ctx.area_name(area_id)

    impacted = [
        ImpactedEntity(type="PTW", id=p.id, ref=p.number, label=f"{p.type} permit", href=f"/ptw/{p.id}")
        for p in permits[:8]
    ] + [
        ImpactedEntity(type="CAPA", id=c.id, ref=c.number, label=c.title or c.number, href=f"/capa/{c.id}")
        for c in capas[:4]
    ]
    where = f" in {area}" if area else ""
    body = (
        f"{len(permits)} active permit{'s' if len(permits) != 1 else ''}{where} rely on controls this analysis "
        f"validated — review required before the next shift."
        if permits
        else "Its corrective actions may rest on invalidated causes — review required."
    )
    return [
        AlertDraft(
            severity="critical",
            title=f"{ref} reopened → {len(permits)} permit{'s' if len(permits) != 1 else ''} rely on its controls",
            body_text=body,
            body_template_key="rca_reopened",
            body_params={"rcaRef": ref, "permitCount": len(permits), "area": area},
            dedupe_key=f"rca.reopened:{event.entityId}",
            site_id=event.siteId or plant_id,
            impacted=impacted,
            deep_link=f"/erm/rca/{event.entityId}",
            audience_roles=["SAFETY_OFFICER", "HSE_MANAGER", "PLANT_HEAD", "CORPORATE_HSE"],
        )
    ]


RULE = ImpactRule(key="rca_reopened", event_types=("rca.reopened",), resolve=_resolve)
