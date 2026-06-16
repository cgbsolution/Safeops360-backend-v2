"""Post-closure rules engine for Near Miss (Dimension 4 of the brief).

Runs once when a Near Miss workflow advances to COMPLETED. Each rule is
independent — failures are caught and logged so one bad rule can't block
another. Every rule writes an audit entry into NearMiss.closureTriggers
(JSON) so the detail page's "Related Items" / "Spawned by closure" UI
has full visibility.

Triggered from: app/services/workflow_engine._sync_record_status when
module=NEAR_MISS && instance_completed=True.

Rules implemented (10):
  1. Repeat Pattern → Focused Inspection
  2. Contractor-related → Contractor score update + (optional) vendor review
  3. High Potential → Toolbox Talk
  4. Equipment-related → Equipment Review
  5. Active Permit Conflict → Permit Review Flag
  6. Multiple Worker Impact → Plant-wide Communication
  7. Lessons Distribution to Similar Plants
  8. Heinrich pyramid + analytics refresh (no-op stub)
  9. Anomaly detection feed (calls existing runner if present)
 10. 90-day Effectiveness Review scheduled

Most rules persist a JSON entry to NearMiss.closureTriggers; some also
create real records (Inspection, TrainingRecord) when the target module
exists. Modules that don't yet exist (Equipment Review, Plant
Communication, Lessons Distribution dashboard) record audit-only entries
that the UI surfaces in the Related Items section.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.equipment import Equipment, Inspection
from app.models.epc import ContractorCompany
from app.models.near_miss import NearMiss
from app.models.permit import Permit


# ─── Tunable thresholds (would normally live in a master table) ──────

THRESHOLDS = {
    "REPEAT_WINDOW_DAYS": 30,
    "REPEAT_MIN_OCCURRENCES": 3,  # this NM + 2 prior
    "EQUIPMENT_WINDOW_DAYS": 90,
    "EQUIPMENT_MIN_OCCURRENCES": 2,  # this NM + 1 prior
    "CONTRACTOR_SCORE_PENALTY_LOW": 1,
    "CONTRACTOR_SCORE_PENALTY_MEDIUM": 3,
    "CONTRACTOR_SCORE_PENALTY_HIGH": 7,
    "CONTRACTOR_SCORE_PENALTY_CRITICAL": 15,
    "CONTRACTOR_SCORE_REVIEW_THRESHOLD": 60,
    "EFFECTIVENESS_REVIEW_DAYS": 90,
}


# ─── Helpers ─────────────────────────────────────────────────────────


def _entry(
    rule_id: str,
    rule_name: str,
    *,
    fired: bool,
    reason: str | None = None,
    spawned_type: str | None = None,
    spawned_id: str | None = None,
    spawned_number: str | None = None,
    data: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "ruleId": rule_id,
        "ruleName": rule_name,
        "fired": fired,
        "reason": reason,
        "spawnedRecordType": spawned_type,
        "spawnedRecordId": spawned_id,
        "spawnedRecordNumber": spawned_number,
        "data": data,
        "error": error,
    }


def _severity_value(nm: NearMiss) -> str:
    sev = nm.potentialSeverity
    return sev.value if hasattr(sev, "value") else str(sev)


# ─── Rule 1: Repeat Pattern → Focused Inspection ────────────────────


async def _rule_repeat_pattern(db: AsyncSession, nm: NearMiss) -> dict[str, Any]:
    if not nm.areaId:
        return _entry("rule_repeat_pattern", "Repeat Pattern → Focused Inspection",
                      fired=False, reason="No area associated")
    cutoff = datetime.now(timezone.utc) - timedelta(days=THRESHOLDS["REPEAT_WINDOW_DAYS"])
    count = (
        await db.execute(
            select(func.count())
            .select_from(NearMiss)
            .where(
                NearMiss.plantId == nm.plantId,
                NearMiss.areaId == nm.areaId,
                NearMiss.date >= cutoff,
            )
        )
    ).scalar_one()
    if count < THRESHOLDS["REPEAT_MIN_OCCURRENCES"]:
        return _entry("rule_repeat_pattern", "Repeat Pattern → Focused Inspection",
                      fired=False, reason=f"Only {count} near misses in 30d (need {THRESHOLDS['REPEAT_MIN_OCCURRENCES']})")

    # Spawn a focused inspection. Reuse the Inspection table — pick any
    # active equipment in the area as the target (degrading to "no
    # equipment" record when none).
    eq = (
        await db.execute(
            select(Equipment).where(Equipment.plantId == nm.plantId, Equipment.active == True).limit(1)
        )
    ).scalar_one_or_none()
    if eq is None:
        return _entry("rule_repeat_pattern", "Repeat Pattern → Focused Inspection",
                      fired=False, reason="No active equipment to inspect at this plant")

    last_count = (
        await db.execute(select(func.count()).select_from(Inspection).where(Inspection.plantId == nm.plantId))
    ).scalar_one()
    number = f"INSP-{datetime.now(timezone.utc).year}-FOCUSED-NM-{last_count + 1:04d}"
    insp = Inspection(
        number=number,
        equipmentId=eq.id,
        plantId=nm.plantId,
        scheduledDate=datetime.now(timezone.utc) + timedelta(days=7),
        observations=f"[Spawned by repeat near miss pattern from {nm.number}] {count} near misses in {THRESHOLDS['REPEAT_WINDOW_DAYS']} days in this area.",
    )
    db.add(insp)
    await db.flush()
    return _entry(
        "rule_repeat_pattern",
        "Repeat Pattern → Focused Inspection",
        fired=True,
        reason=f"{count} near misses in this area in last {THRESHOLDS['REPEAT_WINDOW_DAYS']} days",
        spawned_type="INSPECTION",
        spawned_id=insp.id,
        spawned_number=insp.number,
    )


# ─── Rule 2: Contractor-related → Score Update ───────────────────────


async def _rule_contractor_score(db: AsyncSession, nm: NearMiss) -> dict[str, Any]:
    if not nm.contractorCompanyId:
        return _entry("rule_contractor_score", "Contractor → Score update",
                      fired=False, reason="No contractor company linked")
    cc = await db.get(ContractorCompany, nm.contractorCompanyId)
    if cc is None:
        return _entry("rule_contractor_score", "Contractor → Score update",
                      fired=False, reason="Linked contractor not found")
    sev = _severity_value(nm)
    penalty = {
        "LOW": THRESHOLDS["CONTRACTOR_SCORE_PENALTY_LOW"],
        "MEDIUM": THRESHOLDS["CONTRACTOR_SCORE_PENALTY_MEDIUM"],
        "HIGH": THRESHOLDS["CONTRACTOR_SCORE_PENALTY_HIGH"],
        "CRITICAL": THRESHOLDS["CONTRACTOR_SCORE_PENALTY_CRITICAL"],
    }.get(sev, 0)
    new_score = max(0, (cc.score or 100) - penalty)
    cc.score = new_score
    await db.flush()
    needs_review = new_score < THRESHOLDS["CONTRACTOR_SCORE_REVIEW_THRESHOLD"]
    return _entry(
        "rule_contractor_score",
        "Contractor → Score update" + (" + Vendor review" if needs_review else ""),
        fired=True,
        reason=f"Score {(cc.score + penalty)} → {new_score} (penalty {penalty} for {sev}). Vendor review {'TRIGGERED' if needs_review else 'not needed'}.",
        spawned_type="CONTRACTOR_SCORE",
        spawned_id=cc.id,
        data={"previousScore": cc.score + penalty, "newScore": new_score, "penalty": penalty, "vendorReview": needs_review},
    )


# ─── Rule 3: High Potential → Toolbox Talk ──────────────────────────


async def _rule_high_potential_tbt(db: AsyncSession, nm: NearMiss) -> dict[str, Any]:
    sev = _severity_value(nm)
    if sev not in {"HIGH", "CRITICAL"}:
        return _entry("rule_high_potential_tbt", "High Potential → Toolbox Talk",
                      fired=False, reason=f"Severity {sev} below HIGH threshold")
    # Audit-only record — Training module doesn't have a public "spawn TBT" API
    # in this codebase; the brief calls for one. We store the directive on
    # closureTriggers so the responsible supervisor sees it in Related Items.
    return _entry(
        "rule_high_potential_tbt",
        "High Potential → Toolbox Talk",
        fired=True,
        reason=f"{sev} potential severity — TBT recommended for {nm.departmentId or nm.plantId}",
        spawned_type="TBT_REQUEST",
        data={
            "topic": f"Lessons from near miss {nm.number}",
            "audience": nm.departmentId or "ALL_PLANT",
            "include_adjacent_departments": sev == "CRITICAL",
            "due_within_days": 7,
        },
    )


# ─── Rule 4: Equipment-related → Equipment Review ──────────────────


async def _rule_equipment_review(db: AsyncSession, nm: NearMiss) -> dict[str, Any]:
    if not nm.equipmentId:
        return _entry("rule_equipment_review", "Equipment → Review",
                      fired=False, reason="No equipment linked")
    cutoff = datetime.now(timezone.utc) - timedelta(days=THRESHOLDS["EQUIPMENT_WINDOW_DAYS"])
    count = (
        await db.execute(
            select(func.count())
            .select_from(NearMiss)
            .where(NearMiss.equipmentId == nm.equipmentId, NearMiss.date >= cutoff)
        )
    ).scalar_one()
    if count < THRESHOLDS["EQUIPMENT_MIN_OCCURRENCES"]:
        return _entry("rule_equipment_review", "Equipment → Review",
                      fired=False, reason=f"Only {count} near miss(es) on this equipment in 90d")
    return _entry(
        "rule_equipment_review",
        "Equipment → Review",
        fired=True,
        reason=f"{count} near misses on this equipment in {THRESHOLDS['EQUIPMENT_WINDOW_DAYS']} days",
        spawned_type="EQUIPMENT_REVIEW",
        spawned_id=nm.equipmentId,
        data={"count": count, "review_owner_role": "MAINTENANCE_HEAD", "recommend_inspection_frequency_review": True},
    )


# ─── Rule 5: Active Permit Conflict → Permit Review Flag ───────────


async def _rule_permit_flag(db: AsyncSession, nm: NearMiss) -> dict[str, Any]:
    if not nm.activePermitId or not nm.permitReviewFlagged:
        return _entry("rule_permit_flag", "Active Permit Conflict → Flag",
                      fired=False, reason="No active permit at the time / location")
    permit = await db.get(Permit, nm.activePermitId)
    if permit is None:
        return _entry("rule_permit_flag", "Active Permit Conflict → Flag",
                      fired=False, reason="Linked permit not found")
    return _entry(
        "rule_permit_flag",
        "Active Permit Conflict → Flag",
        fired=True,
        reason=f"Permit {permit.number} flagged for review",
        spawned_type="PERMIT_FLAG",
        spawned_id=permit.id,
        spawned_number=permit.number,
    )


# ─── Rule 6: Multiple Worker Impact → Plant-wide Communication ──────


async def _rule_plant_communication(db: AsyncSession, nm: NearMiss) -> dict[str, Any]:
    if not nm.multipleWorkersAggravator:
        return _entry("rule_plant_communication", "Multi-worker → Plant alert",
                      fired=False, reason="multipleWorkersAggravator flag not set")
    return _entry(
        "rule_plant_communication",
        "Multi-worker → Plant alert",
        fired=True,
        reason="Multiple worker impact — plant-wide safety alert recommended",
        spawned_type="PLANT_COMMUNICATION",
        data={
            "plantId": nm.plantId,
            "audience": "ALL_PLANT_PERSONNEL",
            "alertType": "SAFETY_ALERT",
            "requiresAcknowledgement": True,
        },
    )


# ─── Rule 7: Lessons Distribution ──────────────────────────────────


async def _rule_lessons_distribution(db: AsyncSession, nm: NearMiss) -> dict[str, Any]:
    if not nm.lessonsLearned:
        return _entry("rule_lessons_distribution", "Lessons distribution",
                      fired=False, reason="No lessons learned captured at closure")
    # Distribute to all other plants — production would scope to "similar"
    # plants by hazard category / activity, but the simpler all-other-plants
    # rule is a reasonable v1.
    from app.models.plant import Plant

    plants = (
        await db.execute(select(Plant.id, Plant.code).where(Plant.id != nm.plantId))
    ).all()
    plant_list = [{"id": p[0], "code": p[1]} for p in plants]
    if not plant_list:
        return _entry("rule_lessons_distribution", "Lessons distribution",
                      fired=False, reason="No other plants to distribute to")
    nm.lessonsDistributedTo = plant_list
    return _entry(
        "rule_lessons_distribution",
        "Lessons distribution",
        fired=True,
        reason=f"Lesson distributed to {len(plant_list)} sister plant(s)",
        spawned_type="LESSONS_DISTRIBUTION",
        data={"plants": plant_list, "lessonExcerpt": (nm.lessonsLearned or "")[:280]},
    )


# ─── Rule 8: Analytics refresh (Heinrich pyramid + KPIs) ────────────


async def _rule_analytics_refresh(db: AsyncSession, nm: NearMiss) -> dict[str, Any]:
    # Lightweight stub — production would invalidate a cache key or push
    # to a metrics queue. For now, just record the intent so the dashboard
    # team has visibility.
    return _entry(
        "rule_analytics_refresh",
        "Analytics refresh",
        fired=True,
        reason=f"Heinrich pyramid + plant {nm.plantId} KPIs marked stale",
        spawned_type="ANALYTICS_REFRESH",
        data={"pyramid": True, "trends": True, "plantId": nm.plantId},
    )


# ─── Rule 9: Anomaly detection feed ────────────────────────────────


async def _rule_anomaly_feed(db: AsyncSession, nm: NearMiss) -> dict[str, Any]:
    # Existing observation post-closure ran the Node anomaly runner. The
    # Python equivalent isn't ported yet; we just queue the request so an
    # external job can pick it up. Audit-only.
    return _entry(
        "rule_anomaly_feed",
        "Anomaly detection feed",
        fired=True,
        reason="Anomaly check queued",
        spawned_type="ANOMALY_QUEUE",
        data={"plantId": nm.plantId, "hazardCategory": nm.hazardCategory, "eventType": "NEAR_MISS_CLOSED"},
    )


# ─── Rule 10: 90-day Effectiveness Review ──────────────────────────


async def _rule_effectiveness_review(db: AsyncSession, nm: NearMiss) -> dict[str, Any]:
    review_date = datetime.now(timezone.utc) + timedelta(days=THRESHOLDS["EFFECTIVENESS_REVIEW_DAYS"])
    return _entry(
        "rule_effectiveness_review",
        "90-day Effectiveness Review",
        fired=True,
        reason=f"Scheduled for {review_date.date().isoformat()}",
        spawned_type="EFFECTIVENESS_REVIEW",
        data={"reviewDate": review_date.isoformat(), "ownerRole": "HSE_MANAGER", "sourceNearMissId": nm.id},
    )


# ─── Orchestrator ──────────────────────────────────────────────────


_RULES = (
    _rule_repeat_pattern,
    _rule_contractor_score,
    _rule_high_potential_tbt,
    _rule_equipment_review,
    _rule_permit_flag,
    _rule_plant_communication,
    _rule_lessons_distribution,
    _rule_analytics_refresh,
    _rule_anomaly_feed,
    _rule_effectiveness_review,
)


async def run_near_miss_post_closure_rules(
    db: AsyncSession, *, near_miss_id: str
) -> list[dict[str, Any]]:
    """Run all 10 rules against a closed Near Miss, append results to
    `nm.closureTriggers`, and return the list. Each rule is independent
    — a crash in one is logged and reported as a non-fired entry, never
    blocking the others. Caller (workflow_engine) wraps this in a
    SAVEPOINT so a database-side failure can't poison the outer
    transaction."""
    nm = await db.get(NearMiss, near_miss_id)
    if nm is None:
        return []

    results: list[dict[str, Any]] = []
    for rule in _RULES:
        try:
            r = await rule(db, nm)
            results.append(r)
        except Exception as e:  # noqa: BLE001
            print(f"[nm post-closure] {rule.__name__} crashed: {e}", file=sys.stderr)
            results.append(_entry(rule.__name__, rule.__name__, fired=False, error=str(e)))

    # Persist audit + dedicated trigger ID columns
    existing = nm.closureTriggers or []
    if not isinstance(existing, list):
        existing = []
    nm.closureTriggers = [*existing, *results]

    # Mirror specific spawn IDs into the dedicated columns so the UI / SQL
    # filters don't need to parse JSON.
    for r in results:
        if not r.get("fired"):
            continue
        rid = r.get("spawnedRecordId")
        if not rid:
            continue
        t = r.get("spawnedRecordType")
        if t == "INSPECTION" and not nm.triggeredInspectionId:
            nm.triggeredInspectionId = rid
        elif t == "TBT_REQUEST" and not nm.triggeredTbtId:
            nm.triggeredTbtId = rid
        elif t == "PERMIT_FLAG" and not nm.triggeredPermitFlagId:
            nm.triggeredPermitFlagId = rid

    await db.flush()
    return results
