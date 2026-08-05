"""ptw.suspended / ptw.resumed / ptw.modified / ptw.rejected →
"PTW-{ref} {action} → affects {area}, {n} overlapping permits flagged".

Suspension is CRITICAL (adjacent crews may be relying on its isolations);
modification/resumption is ATTENTION. There is no REVOKED status on the
platform — rejection covers it (DECISIONS.md D8).
"""

from __future__ import annotations

from app.services.alerts import AlertDraft, ImpactedEntity, ImpactRule

_ACTION_LABEL = {
    "ptw.suspended": "suspended",
    "ptw.resumed": "resumed",
    "ptw.modified": "modified",
    "ptw.rejected": "rejected",
}


async def _resolve(event, ctx):  # noqa: ANN001
    permit = await ctx.permit(event.entityId)
    if permit is None:
        return []
    action = _ACTION_LABEL.get(event.eventType, "changed")
    overlapping = await ctx.active_permits(permit.plantId, permit.areaId, exclude_id=permit.id)
    area = await ctx.area_name(permit.areaId)
    where = area or "its work area"
    reason = (event.payload or {}).get("reason")

    severity = "critical" if event.eventType == "ptw.suspended" else "attention"
    n = len(overlapping)
    body_bits = [f"Affects {where}."]
    if n:
        body_bits.append(f"{n} overlapping active permit{'s' if n != 1 else ''} in the same area flagged for review.")
    if reason:
        body_bits.append(f"Reason: {reason}.")
    if event.eventType == "ptw.suspended" and n == 0:
        body_bits.append("No overlapping permits — confirm isolations are safe to hold.")

    return [
        AlertDraft(
            severity=severity,
            title=f"{permit.number} {action} → {n} overlapping permit{'s' if n != 1 else ''} flagged",
            body_text=" ".join(body_bits),
            body_template_key="ptw_changed",
            body_params={"permitRef": permit.number, "action": action, "area": where, "overlapCount": n, "reason": reason},
            dedupe_key=f"{event.eventType}:{permit.id}",
            site_id=event.siteId or permit.plantId,
            impacted=[
                ImpactedEntity(type="PTW", id=p.id, ref=p.number, label=f"{p.type} permit", href=f"/ptw/{p.id}")
                for p in overlapping[:8]
            ],
            deep_link=f"/ptw/{permit.id}",
            audience_roles=["SAFETY_OFFICER", "HSE_MANAGER", "PLANT_HEAD"],
        )
    ]


RULE = ImpactRule(
    key="ptw_changed",
    event_types=("ptw.suspended", "ptw.resumed", "ptw.modified", "ptw.rejected"),
    resolve=_resolve,
)
