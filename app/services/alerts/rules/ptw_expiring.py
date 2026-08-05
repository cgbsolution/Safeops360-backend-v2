"""ptw.expiring (T-24h scan) → "PTW-{ref} expires in {h}h — work status unconfirmed"."""

from __future__ import annotations

from app.services.alerts import AlertDraft, ImpactedEntity, ImpactRule


async def _resolve(event, ctx):  # noqa: ANN001
    permit = await ctx.permit(event.entityId)
    if permit is None or permit.status != "ACTIVE":
        return []  # already returned/closed/expired between scan and resolve
    hours = (event.payload or {}).get("hoursLeft")
    area = await ctx.area_name(permit.areaId)
    where = f" ({area})" if area else ""
    return [
        AlertDraft(
            severity="attention",
            title=f"{permit.number} expires in {hours}h — work status unconfirmed",
            body_text=f"{permit.type} permit{where} has not been returned or extended. "
            "Confirm the job is complete or extend before it lapses into INCOMPLETE_RETURN.",
            body_template_key="ptw_expiring",
            body_params={"permitRef": permit.number, "hoursLeft": hours, "area": area},
            dedupe_key=f"ptw.expiring:{permit.id}",
            site_id=event.siteId or permit.plantId,
            impacted=[ImpactedEntity(type="PTW", id=permit.id, ref=permit.number, label=f"{permit.type} permit", href=f"/ptw/{permit.id}")],
            deep_link=f"/ptw/{permit.id}",
            audience_roles=["SAFETY_OFFICER", "PERMIT_ISSUER", "HSE_MANAGER"],
            expires_at=permit.validTo,
        )
    ]


RULE = ImpactRule(key="ptw_expiring", event_types=("ptw.expiring",), resolve=_resolve)
