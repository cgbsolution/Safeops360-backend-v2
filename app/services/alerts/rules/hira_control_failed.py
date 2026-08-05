"""hira.control_failed → CRITICAL: "Control '{name}' failed in audit → {n}
active PTWs cite it".

There is no PTW→HIRA-control FK on the platform (DECISIONS.md D8): when the
emitter knows the influenced permit types (HiraEntry.influencesPtwPermitTypes)
they arrive in the payload and narrow the impact set; otherwise every active
permit at the plant is flagged for review — deliberately conservative for a
failed control.
"""

from __future__ import annotations

from app.services.alerts import AlertDraft, ImpactedEntity, ImpactRule


async def _resolve(event, ctx):  # noqa: ANN001
    p = event.payload or {}
    control_name = p.get("controlName") or event.entityRef or "control"
    permit_types = [str(t).upper() for t in (p.get("influencedPermitTypes") or [])]
    if not event.siteId:
        return []
    permits = await ctx.active_permits(event.siteId)
    if permit_types:
        permits = [pm for pm in permits if pm.type.upper() in permit_types]
    return [
        AlertDraft(
            severity="critical",
            title=f"Control '{control_name}' failed in audit → {len(permits)} active PTW{'s' if len(permits) != 1 else ''} cite it",
            body_text=(
                "A control that live permits depend on failed its audit check. "
                "Re-validate the affected permits before work continues."
                if permits
                else "No active permits currently depend on it — re-validate before the next issuance."
            ),
            body_template_key="hira_control_failed",
            body_params={"controlName": control_name, "permitCount": len(permits), "auditRef": p.get("auditRef")},
            dedupe_key=f"hira.control_failed:{event.entityId}",
            site_id=event.siteId,
            impacted=[
                ImpactedEntity(type="PTW", id=pm.id, ref=pm.number, label=f"{pm.type} permit", href=f"/ptw/{pm.id}")
                for pm in permits[:8]
            ],
            deep_link=p.get("hiraHref") or "/hira",
            audience_roles=["SAFETY_OFFICER", "HSE_MANAGER", "PLANT_HEAD", "CORPORATE_HSE"],
        )
    ]


RULE = ImpactRule(key="hira_control_failed", event_types=("hira.control_failed",), resolve=_resolve)
