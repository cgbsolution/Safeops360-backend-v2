"""Fire Safety & Emergency Response engines (P1-4).

  • equipment status engine (ACTIVE / DUE_INSPECTION / OVERDUE from next-due date)
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def compute_status(equipment: FireEquipment, now: datetime | None = None) -> str:
    """Derived equipment status. Manual OUT_OF_SERVICE / DECOMMISSIONED are sticky."""
    if equipment.status in ("OUT_OF_SERVICE", "DECOMMISSIONED"):
        return equipment.status
    now = now or _now()
    due = _aware(equipment.nextInspectionDueDate)
    if due is None:
        return "DUE_INSPECTION"  # never inspected → needs one
    if due < now:
        return "OVERDUE"
    if due <= now + timedelta(days=DUE_SOON_DAYS):
        return "DUE_INSPECTION"
    return "ACTIVE"


async def recompute_all_statuses(db: AsyncSession, plant_id: str | None = None) -> dict[str, Any]:
    """Recompute every active equipment's status from its next-due date (on-demand,
    scheduler substitute). Also pulls the latest CLOSED FIRE inspection engagement
    per equipment and advances its inspection dates."""
    from app.models.cams import CamsEngagement

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
    changed = 0
    for eq in equip:
        latest = latest_by_eq.get(eq.id)
        if latest and (eq.lastInspectionDate is None or latest > _aware(eq.lastInspectionDate)):
            eq.lastInspectionDate = latest
            eq.nextInspectionDueDate = latest + timedelta(days=eq.inspectionFrequencyDays)
        new_status = compute_status(eq)
        if new_status != eq.status:
            eq.status = new_status
            changed += 1
    await db.flush()
    return {"evaluated": len(equip), "statusChanged": changed}


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
