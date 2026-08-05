"""observation.triaged_high → cluster check: >=3 same-category same-area
high/critical field reports inside 7 days fires "Cluster: {n}x {category} in
{area} this week"."""

from __future__ import annotations

from app.services.alerts import AlertDraft, ImpactedEntity, ImpactRule

CLUSTER_THRESHOLD = 3
CLUSTER_WINDOW_DAYS = 7


async def _resolve(event, ctx):  # noqa: ANN001
    p = event.payload or {}
    category = p.get("categoryL1")
    area_id = p.get("areaId")
    if not event.siteId:
        return []
    n = await ctx.count_high_submissions(event.siteId, area_id, category, CLUSTER_WINDOW_DAYS)
    if n < CLUSTER_THRESHOLD:
        return []
    area = await ctx.area_name(area_id) or "one area"
    cat_label = (category or "high-risk").replace("_", " ")
    return [
        AlertDraft(
            severity="critical" if p.get("riskLevel") == "CRITICAL" else "attention",
            title=f"Cluster: {n}× {cat_label} in {area} this week",
            body_text=(
                f"{n} field reports triaged HIGH+ for the same category in the same area within "
                f"{CLUSTER_WINDOW_DAYS} days — this is a pattern, not noise. Consider a stand-down "
                "inspection or targeted toolbox talk."
            ),
            body_template_key="observation_cluster",
            body_params={"count": n, "category": cat_label, "area": area, "windowDays": CLUSTER_WINDOW_DAYS},
            # dedupe per plant+area+category — repeat highs bump the counter
            dedupe_key=f"cluster:{event.siteId}:{area_id or 'any'}:{category or 'any'}",
            site_id=event.siteId,
            impacted=[
                ImpactedEntity(
                    type="CaptureSubmission",
                    id=event.entityId,
                    ref=event.entityRef or "",
                    label="latest report",
                    href=f"/field-reports/{event.entityId}",
                )
            ],
            deep_link="/field-reports",
            audience_roles=["SAFETY_OFFICER", "HSE_MANAGER", "PLANT_HEAD"],
        )
    ]


RULE = ImpactRule(key="observation_cluster", event_types=("observation.triaged_high",), resolve=_resolve)
