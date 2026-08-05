"""Executive Sentinel scanner (Daily Brief upgrade, spec §1.1 / §2).

Runs the existing deterministic AI Insights engine (``app.services.insights.
compute``) across every module and plant, keeps the PREDICTIVE / leading-
indicator insights the reactive event rules don't already produce
(predictive_risk / cluster / anomaly), scores each with the Brief Priority
Score, and MATERIALISES the qualifying ones as ``Alert`` rows. They then flow
through the exact same feed / ack / mute / audit / digest / push machinery as
the event-driven cards — the 'unify into one scored feed' decision.

Consumes the insight engine; recomputes nothing (spec §1.1). Fully
deterministic — no network call anywhere. Registered as the scheduler job
``sentinel_scan``; also runnable on-demand via ``/api/jobs/sentinel_scan/run``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import Alert
from app.models.plant import Plant
from app.services.insights import SUPPORTED_MODULES, compute
from app.services.sentinel.score import score_insight

# The insight kinds the Sentinel surfaces — leading indicators the reactive event
# rules (rca.*/ptw.*/capa.overdue/observation.triaged_high/hira.control_failed)
# do NOT already produce, so there is no double-counting in the unified feed.
_MATERIALISE_KINDS = {"predictive_risk", "cluster", "anomaly"}

# module key → the list screen a card deep-links to (all routes verified present).
MODULE_ROUTES = {
    "incident": "/incidents",
    "nearmiss": "/near-miss",
    "observation": "/observations",
    "hira": "/hira",
    "eai": "/eai",
    "combined-risk": "/risk-register",
    "capa": "/capa",
    "moc": "/moc",
}
_MODULE_LABEL = {
    "incident": "Incident",
    "nearmiss": "Near Miss",
    "observation": "Observation",
    "hira": "HIRA",
    "eai": "Environmental",
    "combined-risk": "Risk Register",
    "capa": "CAPA",
    "moc": "MOC",
}
# Roles that receive an in-platform push when a NEW critical sentinel card appears.
_PUSH_ROLES = ("HSE_MANAGER", "PLANT_HEAD", "CORPORATE_HSE", "SAFETY_OFFICER")
_CROSS_SITE_ROLES = {"CORPORATE_HSE"}  # get every site's critical regardless of home plant


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dedupe_key(plant_id: str, module: str, insight_id: str) -> str:
    # plant is part of the key: the same insight id (e.g. the global
    # 'nearmiss:predictive:critical-uninvestigated') is computed per-plant, so
    # two plants must not collide onto one card.
    return f"insight:{plant_id}:{module}:{insight_id}"


def _impacted(module: str, refs: list[str]) -> list[dict[str, str]]:
    route = MODULE_ROUTES.get(module, "/dashboard/daily")
    label = _MODULE_LABEL.get(module, module)
    # Insights carry human record numbers (refs), not row ids — each pill deep-
    # links to the module list where that record is shown; the ref is the visible
    # trace back to the real source record (spec §1.3 card anatomy).
    return [
        {"type": label, "id": r, "ref": r, "label": f"{label} {r}", "href": route}
        for r in refs[:8]
    ]


async def run_sentinel_scan(db: AsyncSession, *, push: bool = True) -> dict:
    plants = (await db.execute(select(Plant.id, Plant.name))).all()
    scanned = materialised = new_critical = pushed = 0
    for plant_id, _plant_name in plants:
        for module in SUPPORTED_MODULES:
            try:
                resp = await compute(db, module, plant=plant_id)
            except Exception as e:  # noqa: BLE001 — one bad module must not wedge the scan
                print(f"[sentinel] compute failed {module}/{plant_id}: {e}", file=sys.stderr)
                continue
            if resp.suppressed:
                continue
            for ins in resp.bar:
                if ins.kind not in _MATERIALISE_KINDS:
                    continue
                scanned += 1
                scored = score_insight(ins)
                alert, is_new = await _upsert(db, plant_id, module, ins, scored)
                materialised += 1
                if is_new and alert.severity == "critical":
                    new_critical += 1
                    if push:
                        pushed += await _push(db, alert)
    await db.commit()
    return {
        "plants": len(plants),
        "scanned": scanned,
        "materialised": materialised,
        "newCritical": new_critical,
        "pushed": pushed,
    }


async def _upsert(
    db: AsyncSession, plant_id: str, module: str, insight: Any, scored: dict[str, Any]
) -> tuple[Alert, bool]:
    """Non-incrementing upsert keyed on dedupeKey. Unlike the event resolver's
    ``materialise`` (which counts occurrences), a persisting insight is ONE
    finding — refresh its fields but never inflate ``count``, and only bump
    ``updatedAt`` on a MATERIAL change so the feed doesn't churn every scan."""
    key = _dedupe_key(plant_id, module, insight.id)
    existing = (
        await db.execute(
            select(Alert)
            .where(Alert.dedupeKey == key)
            .where(Alert.isDeleted.is_(False))
            .order_by(Alert.createdAt.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    route = MODULE_ROUTES.get(module, "/dashboard/daily")
    impacted = _impacted(module, insight.recordRefs)
    severity = scored["alertSeverity"]
    count = max(1, scored["clusterSize"])
    body_params: dict[str, Any] = {
        "source": "sentinel",
        "module": module,
        "kind": insight.kind,
        "confidence": scored["confidence"],
        "seriousPotential": scored["seriousPotential"],
        "overdueDays": scored["overdueDays"],
        "clusterSize": scored["clusterSize"],
        "tier": scored["tier"],
        "earlySignal": scored["earlySignal"],
        "score": scored["score"],
        "scoreComponents": scored["components"],
        "suggestedAction": insight.suggestedAction,
        "escalated": scored["escalated"],
    }

    if existing is not None:
        changed = (
            existing.title != insight.headline
            or existing.severity != severity
            or existing.bodyText != insight.evidence
        )
        # Preserve an escalation flag already set on a prior scan (don't reset the
        # escalation state — it feeds the score's freshness component).
        if (existing.bodyParams or {}).get("escalated"):
            body_params["escalated"] = True
            body_params["scoreComponents"] = {**scored["components"], "freshness": 0}
            body_params["score"] = sum(body_params["scoreComponents"].values())
        existing.title = insight.headline
        existing.bodyText = insight.evidence
        existing.severity = severity
        existing.impactedEntities = impacted
        existing.deepLink = route
        existing.bodyParams = body_params
        existing.count = count
        if existing.status in ("resolved", "muted") and severity == "critical":
            existing.status = "new"  # a re-confirmed critical resurfaces
            changed = True
        if changed:
            existing.updatedAt = _now()
        return existing, False

    alert = Alert(
        siteId=plant_id,
        severity=severity,
        title=insight.headline,
        bodyTemplateKey=None,
        bodyParams=body_params,
        bodyText=insight.evidence,
        sourceEventType="insight",
        sourceEntityType=module,
        sourceEntityId=insight.id,
        impactedEntities=impacted,
        deepLink=route,
        dedupeKey=key,
        count=count,
        audienceRoles=list(_PUSH_ROLES),
        audienceSiteIds=[plant_id],
    )
    db.add(alert)
    await db.flush()  # assign id before any Notification references it
    return alert, True


async def _push(db: AsyncSession, alert: Alert) -> int:
    """In-platform critical push (spec §4) — a Notification row per entitled user,
    zero external network dependency (works airgapped). Site staff for the card's
    plant + all cross-site (corporate) role holders."""
    from app.models.notification import Notification
    from app.models.user import Role, User, UserRole

    rows = (
        await db.execute(
            select(User, Role.code)
            .join(UserRole, UserRole.userId == User.id)
            .join(Role, Role.id == UserRole.roleId)
            .where(Role.code.in_(_PUSH_ROLES))
        )
    ).all()
    by_user: dict[str, tuple[Any, set[str]]] = {}
    for u, code in rows:
        if u.id not in by_user:
            by_user[u.id] = (u, set())
        by_user[u.id][1].add(code)

    sent = 0
    for u, codes in by_user.values():
        cross_site = bool(codes & _CROSS_SITE_ROLES)
        if not (cross_site or u.plantId == alert.siteId or u.plantId is None):
            continue
        db.add(
            Notification(
                userId=u.id,
                type="SENTINEL_CRITICAL",
                severity="CRITICAL",
                title=alert.title,
                body=alert.bodyText,
                entityType="Alert",
                entityId=alert.id,
                linkUrl="/dashboard/daily",
            )
        )
        sent += 1

    # Record the escalation on the card so the score's freshness weight drops and
    # a later scan doesn't re-push (reassign — in-place JSON mutation isn't tracked).
    alert.bodyParams = {**(alert.bodyParams or {}), "escalated": True}
    return sent
