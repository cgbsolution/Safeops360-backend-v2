"""Fire Safety & Emergency Response engines (P1-4, extended by Fire & Life Safety).

  • equipment status engine — now driven by the config-resolved inspection
    frequency (`services/fire_frequency`) rather than a per-row integer, with
    open CRITICAL defects as a second input (spec §5.2)
  • CAMS-engine inspection integration (engagement sourceModule='FIRE'); on close,
    advance the equipment's inspection dates and flip status back to ACTIVE
  • drill MAJOR_GAP gate (a drill can't complete with an unaccounted-persons or
    MAJOR_GAP finding that has no CAPA)
  • crisis escalation (CRITICAL fire incident → ERM-P3 CrisisEvent) + the FSER
    provider the crisis workspace reads (assembly points, contacts, plan summary)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fire_safety import AssemblyPoint, FireDrill, FireDrillFinding, FireEmergencyPlan, FireEquipment

DUE_SOON_DAYS = 30

# Statuses a human sets deliberately, which no batch job may overwrite. Held in
# `statusOverride` rather than in `status` itself: the P1-4 engine inferred
# stickiness from the computed column, so a recompute could not tell "an operator
# decommissioned this" from "the engine last wrote DECOMMISSIONED", and there was
# nowhere to record who decided or why.
STICKY_STATUSES = ("OUT_OF_SERVICE", "DECOMMISSIONED")

# The shipped vocabulary is kept (ACTIVE ≡ the spec's COMPLIANT, DUE_INSPECTION ≡
# its DUE) so existing dashboard filters and the equipment register keep working.
# NON_COMPLIANT is genuinely new: it is what an asset is when it has been
# inspected on time and FAILED, which the old three-state engine could not say.
VALID_STATUSES = ("ACTIVE", "DUE_INSPECTION", "OVERDUE", "NON_COMPLIANT", *STICKY_STATUSES)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def compute_status(
    equipment: FireEquipment,
    now: datetime | None = None,
    *,
    has_open_critical_defect: bool = False,
) -> str:
    """Derived equipment status.

    Precedence, and the reasoning for it:

      1. A manual override wins over everything. Someone physically removed the
         asset from service; no derived state should contradict them.
      2. An open CRITICAL defect beats the schedule. An extinguisher inspected
         yesterday and found discharged is NON_COMPLIANT, not ACTIVE — reading it
         as compliant because its next inspection is 89 days away is precisely
         the reading that gets someone hurt.
      3. Never inspected → DUE_INSPECTION, never ACTIVE. An asset with no
         inspection history has not demonstrated anything.
      4. Otherwise: overdue / due-soon / active from the next-due date.

    Note what is deliberately absent: AMC lapse. Spec §4.4 makes it
    informational, so it is reported on the asset and never folded into status —
    letting a lapsed service contract mark an on-schedule asset non-compliant
    would make the overdue count mean two different things at once.
    """
    if equipment.statusOverride:
        return equipment.statusOverride
    if has_open_critical_defect:
        return "NON_COMPLIANT"
    now = now or _now()
    due = _aware(equipment.nextInspectionDueDate)
    if due is None:
        return "DUE_INSPECTION"  # never inspected → needs one
    if due < now:
        return "OVERDUE"
    if due <= now + timedelta(days=DUE_SOON_DAYS):
        return "DUE_INSPECTION"
    return "ACTIVE"


async def set_status_override(
    db: AsyncSession,
    equipment: FireEquipment,
    *,
    status: str | None,
    reason: str,
    actor_id: str | None,
) -> dict[str, Any]:
    """Apply or clear a manual status override, with a reason, audit-logged.

    Spec §5.2: status must not be manually overridable "without an audit-logged
    reason". `FireEquipment` is already in the tamper-evident hash chain via
    `register_audited`, so the column write is captured automatically — but the
    chain records *what changed*, not *why*, so the reason is recorded explicitly
    as its own event. Passing `status=None` clears the override and hands the
    asset back to the engine.
    """
    from app.services.audit_log import record_event

    if status is not None and status not in STICKY_STATUSES:
        raise ValueError(
            f"{status} is a derived status; only {' / '.join(STICKY_STATUSES)} may be set manually."
        )
    if not (reason or "").strip():
        raise ValueError("A reason is required to override or restore equipment status.")

    previous = equipment.statusOverride
    equipment.statusOverride = status
    equipment.statusOverrideReason = reason
    equipment.statusOverriddenBy = actor_id
    equipment.statusOverriddenAt = _now()
    if status == "OUT_OF_SERVICE":
        equipment.outOfServiceReason = reason
    equipment.status = compute_status(equipment)
    equipment.updatedBy = actor_id

    # `record_event` reads the actor from the request-scoped audit context, so
    # `actor_id` is not passed — it is already on the chain entry.
    await record_event(
        db,
        entity_type="FireEquipment",
        entity_id=equipment.id,
        entity_code=equipment.equipmentCode,
        plant_id=equipment.plantId,
        action="STATUS_OVERRIDE_CLEARED" if status is None else "STATUS_OVERRIDE_SET",
        before={"statusOverride": previous},
        after={"statusOverride": status, "status": equipment.status},
        reason=reason,
    )
    return {
        "equipmentId": equipment.id,
        "statusOverride": equipment.statusOverride,
        "status": equipment.status,
        "reason": reason,
    }


async def recompute_all_statuses(db: AsyncSession, plant_id: str | None = None) -> dict[str, Any]:
    """Recompute every active asset's status. The nightly batch job of spec §5.2.

    Three inputs, all resolved in bulk rather than per asset — this runs over the
    whole register, so an N+1 here is the difference between a job that finishes
    overnight and one that does not:

      • latest COMPLETED FIRE inspection per asset (advances lastInspectionDate)
      • the config-resolved frequency per asset (sets nextInspectionDueDate, and
        records WHICH rule was applied in `frequencyMasterId` so the due date is
        explicable, not just present)
      • open CRITICAL defects per asset (forces NON_COMPLIANT)

    Returns a per-status breakdown plus the unresolved-frequency count, because
    "1,400 assets fell back to the 30-day default" is an operational fact the
    caller needs and a bare `statusChanged` number hides.
    """
    from app.models.cams import CamsEngagement
    from app.services import fire_defects, fire_frequency

    q = select(FireEquipment).where(FireEquipment.isActive.is_(True)).where(FireEquipment.isDeleted.is_(False))
    if plant_id:
        q = q.where(FireEquipment.plantId == plant_id)
    equip = (await db.execute(q)).scalars().all()

    # latest closed FIRE inspection per equipment
    insp = (
        await db.execute(
            select(CamsEngagement)
            .where(CamsEngagement.sourceModule == "FIRE")
            .where(CamsEngagement.status.in_(("completed", "closed", "COMPLETED", "CLOSED")))
        )
    ).scalars().all()
    latest_by_eq: dict[str, datetime] = {}
    for e in insp:
        if not e.sourceEntityId:
            continue
        d = _aware(getattr(e, "conductedDate", None) or getattr(e, "plannedDate", None))
        if d and (e.sourceEntityId not in latest_by_eq or d > latest_by_eq[e.sourceEntityId]):
            latest_by_eq[e.sourceEntityId] = d

    frequencies = await fire_frequency.resolve_many(db, equip)
    critical_assets = await fire_defects.open_critical_defect_asset_ids(db, plant_id)

    changed = 0
    unresolved = 0
    by_status: dict[str, int] = {}
    for eq in equip:
        freq = frequencies.get(eq.id)
        if freq is not None:
            if not freq.resolved:
                unresolved += 1
            eq.frequencyMasterId = freq.masterId
            latest = latest_by_eq.get(eq.id)
            if latest and (eq.lastInspectionDate is None or latest > _aware(eq.lastInspectionDate)):
                eq.lastInspectionDate = latest
            # Recompute the due date from the CURRENT rule every night, not only
            # when a new inspection lands. Otherwise a frequency change in config
            # would not reach existing assets until each was next inspected —
            # which is the same "remap needs a code change" problem in a new hat.
            base = _aware(eq.lastInspectionDate)
            if base:
                eq.nextInspectionDueDate = base + timedelta(days=freq.days)

        new_status = compute_status(eq, has_open_critical_defect=eq.id in critical_assets)
        if new_status != eq.status:
            eq.status = new_status
            changed += 1
        by_status[new_status] = by_status.get(new_status, 0) + 1

    await db.flush()
    return {
        "evaluated": len(equip),
        "statusChanged": changed,
        "byStatus": by_status,
        "unresolvedFrequency": unresolved,
        "openCriticalDefectAssets": len(critical_assets),
    }


# ── Drill gate ──────────────────────────────────────────────────────────────
async def drill_completion_blockers(db: AsyncSession, drill: FireDrill) -> list[str]:
    """Reasons a drill cannot be marked COMPLETED: unaccounted persons, or a
    MAJOR_GAP finding with no CAPA raised."""
    blockers: list[str] = []
    if (drill.unaccountedPersons or 0) > 0:
        blockers.append(f"{drill.unaccountedPersons} unaccounted person(s) at muster — raise a CAPA and account for everyone.")
    findings = (await db.execute(select(FireDrillFinding).where(FireDrillFinding.drillId == drill.id))).scalars().all()
    for f in findings:
        if f.severity == "MAJOR_GAP" and not f.capaId:
            blockers.append(f"MAJOR_GAP finding '{f.description[:60]}' has no CAPA.")
    return blockers


# ── FSER provider (consumed by ERM-P3 crisis workspace) ─────────────────────
async def fser_panel(db: AsyncSession, plant_id: str) -> dict[str, Any]:
    """Fire & Emergency Site Response panel for a site — assembly points, the
    emergency plan summary, external contacts, command structure. This is the
    provider the ERM Phase-3 crisis workspace reads when a fire crisis activates."""
    aps = (
        await db.execute(select(AssemblyPoint).where(AssemblyPoint.plantId == plant_id).where(AssemblyPoint.isDeleted.is_(False)))
    ).scalars().all()
    plan = (
        await db.execute(
            select(FireEmergencyPlan).where(FireEmergencyPlan.plantId == plant_id)
            .where(FireEmergencyPlan.isDeleted.is_(False)).where(FireEmergencyPlan.status == "APPROVED")
            .order_by(FireEmergencyPlan.updatedAt.desc()).limit(1)
        )
    ).scalar_one_or_none()
    return {
        "plantId": plant_id,
        "available": bool(aps or plan),
        "assemblyPoints": [
            {"code": a.code, "name": a.name, "capacity": a.capacity,
             "wardenUserId": a.wardenUserId, "alternateWardenUserId": a.alternateWardenUserId,
             "lat": a.latitude, "lng": a.longitude}
            for a in aps
        ],
        "plan": None if not plan else {
            "planCode": plan.planCode, "title": plan.title, "fireTypes": plan.fireTypes,
            "commandStructure": plan.commandStructure, "externalContacts": plan.externalContacts,
            "criticalEquipmentShutdownSequence": plan.criticalEquipmentShutdownSequence,
        },
    }


# ── Crisis escalation ────────────────────────────────────────────────────────
async def escalate_incident_to_crisis(
    db: AsyncSession, incident_id: str, plant_id: str | None, actor_id: str | None,
    affected_equipment_ids: list[str], evacuation_ordered: bool, fire_service_called: bool,
) -> dict[str, Any]:
    """Create a FireIncidentLink and an ERM-P3 CrisisEvent for a CRITICAL fire
    incident, wiring the FSER panel as the crisis context."""
    from app.models.erm_p3 import CrisisEvent
    from app.models.fire_safety import FireIncidentLink

    now = _now()
    crisis_code = f"CRX-FIRE-{now.strftime('%Y%m%d%H%M%S')}"
    crisis = CrisisEvent(
        crisisCode=crisis_code,
        title=f"Fire emergency — incident {incident_id}",
        severityLevel=1,
        status="ACTIVATED",
        siteId=plant_id,
        activatedPlanIds=[],
        linkedIncidentId=incident_id,
        activatedBy=actor_id or "SYSTEM",
        activatedAt=now,
    )
    db.add(crisis)
    await db.flush()
    link = FireIncidentLink(
        incidentId=incident_id, plantId=plant_id, affectedEquipmentIds=affected_equipment_ids or [],
        crisisEventId=crisis.id, evacuationOrdered=evacuation_ordered, fireServiceCalled=fire_service_called,
        createdBy=actor_id,
    )
    db.add(link)
    await db.flush()
    return {"crisisEventId": crisis.id, "crisisCode": crisis_code, "fireIncidentLinkId": link.id}
