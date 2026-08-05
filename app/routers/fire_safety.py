"""Fire Safety & Emergency Response API (P1-4).

Equipment lifecycle, assembly points, emergency plans, drills (with the MAJOR_GAP
completion gate), the CAMS-engine inspection trigger (sourceModule='FIRE'), crisis
escalation and the FSER panel. Plant-scoped via QueryScope.

NB: gated by the FIRE licence module in the model (registry/editions); the router is
mounted always-on in dev because the unsigned dev licence predates the FIRE code —
add "fire_safety": "FIRE" to ROUTER_MODULE once a FIRE-inclusive licence is issued.
RBAC uses the HSE permissions (INCIDENT.READ/UPDATE) until dedicated FIRE.* grants
are seeded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.db import get_db
from app.models.cams import CamsEngagement
from app.models.fire_safety import (
    AssemblyPoint, FireDrill, FireDrillFinding, FireEmergencyPlan, FireEquipment, FireIncidentLink,
)
from app.models.user import User
from app.services import fire_safety as svc
from app.services.access_scope import build_query_scope
from app.services.permissions import can

router = APIRouter(prefix="/api/fire", tags=["fire-safety"])

_READ = "INCIDENT.READ"
_WRITE = "INCIDENT.UPDATE"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _require(db: AsyncSession, user: User, perm: str, plant_id: str | None = None) -> None:
    from app.services.permissions import PermissionContext
    res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


def _eq(e: FireEquipment) -> dict[str, Any]:
    return {
        "id": e.id, "equipmentCode": e.equipmentCode, "type": e.type, "make": e.make, "model": e.model,
        "serialNo": e.serialNo, "location": e.location, "buildingId": e.buildingId, "plantId": e.plantId,
        "latitude": e.latitude, "longitude": e.longitude, "floorLevel": e.floorLevel,
        "lastInspectionDate": e.lastInspectionDate.isoformat() if e.lastInspectionDate else None,
        "nextInspectionDueDate": e.nextInspectionDueDate.isoformat() if e.nextInspectionDueDate else None,
        "inspectionFrequencyDays": e.inspectionFrequencyDays, "status": e.status, "capacitySpec": e.capacitySpec,
        "maintenanceContractor": e.maintenanceContractor, "qrCode": e.qrCode, "isActive": e.isActive,
        "outOfServiceReason": e.outOfServiceReason,
    }


# ── Dashboard (FS-01) ────────────────────────────────────────────────────────
@router.get("/dashboard")
async def dashboard(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, _READ)
    scope = await build_query_scope(db, user.id, _READ)
    eq = (await db.execute(scope.apply(select(FireEquipment).where(FireEquipment.isDeleted.is_(False)).where(FireEquipment.isActive.is_(True)), FireEquipment))).scalars().all()
    by_status: dict[str, int] = {}
    for e in eq:
        by_status[e.status] = by_status.get(e.status, 0) + 1
    now = _now()
    soon = now + timedelta(days=svc.DUE_SOON_DAYS)
    due_month = sum(1 for e in eq if e.nextInspectionDueDate and svc._aware(e.nextInspectionDueDate) <= soon and svc._aware(e.nextInspectionDueDate) >= now)
    overdue = by_status.get("OVERDUE", 0)
    drills = (await db.execute(scope.apply(select(FireDrill).where(FireDrill.isDeleted.is_(False)), FireDrill))).scalars().all()
    year_start = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    drills_done = sum(1 for d in drills if d.status == "COMPLETED" and d.conductedDate and svc._aware(d.conductedDate) >= year_start)
    drills_due = sum(1 for d in drills if d.status == "PLANNED")
    plans = (await db.execute(scope.apply(select(FireEmergencyPlan).where(FireEmergencyPlan.isDeleted.is_(False)), FireEmergencyPlan))).scalars().all()
    plans_review_due = sum(1 for p in plans if p.nextReviewDate and svc._aware(p.nextReviewDate) < now)
    return {
        "totalEquipment": len(eq), "byStatus": by_status, "dueThisMonth": due_month, "overdue": overdue,
        "drillsCompletedThisYear": drills_done, "drillsDue": drills_due, "plansReviewDue": plans_review_due,
        "overdueItems": [_eq(e) for e in eq if e.status == "OVERDUE"][:25],
    }


# ── Equipment register (FS-02/03) ────────────────────────────────────────────
@router.get("/equipment")
async def list_equipment(
    estatus: str | None = Query(None, alias="status"), etype: str | None = Query(None, alias="type"),
    buildingId: str | None = Query(None), dueOnly: bool = Query(False),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, _READ)
    scope = await build_query_scope(db, user.id, _READ)
    stmt = scope.apply(select(FireEquipment).where(FireEquipment.isDeleted.is_(False)), FireEquipment)
    if estatus:
        stmt = stmt.where(FireEquipment.status == estatus)
    if etype:
        stmt = stmt.where(FireEquipment.type == etype)
    if buildingId:
        stmt = stmt.where(FireEquipment.buildingId == buildingId)
    if dueOnly:
        stmt = stmt.where(FireEquipment.status.in_(("DUE_INSPECTION", "OVERDUE")))
    rows = (await db.execute(stmt.order_by(FireEquipment.nextInspectionDueDate.asc().nulls_first()))).scalars().all()
    return {"items": [_eq(e) for e in rows], "total": len(rows)}


class EquipmentCreate(BaseModel):
    type: str
    location: str = Field(min_length=2)
    plantId: str
    buildingId: str | None = None
    make: str | None = None
    model: str | None = None
    serialNo: str | None = None
    capacitySpec: str | None = None
    inspectionFrequencyDays: int = 30
    latitude: float | None = None
    longitude: float | None = None
    floorLevel: int | None = None
    maintenanceContractor: str | None = None
    installationDate: datetime | None = None


async def _next_code(db: AsyncSession, plant_id: str) -> str:
    n = (await db.execute(select(func.count()).select_from(FireEquipment).where(FireEquipment.plantId == plant_id))).scalar() or 0
    return f"FE-{plant_id[:4].upper()}-{n + 1:04d}"


@router.post("/equipment", status_code=201)
async def create_equipment(body: EquipmentCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, _WRITE, plant_id=body.plantId)
    code = await _next_code(db, body.plantId)
    e = FireEquipment(
        equipmentCode=code, type=body.type, location=body.location, plantId=body.plantId, buildingId=body.buildingId,
        make=body.make, model=body.model, serialNo=body.serialNo, capacitySpec=body.capacitySpec,
        inspectionFrequencyDays=body.inspectionFrequencyDays, latitude=body.latitude, longitude=body.longitude,
        floorLevel=body.floorLevel, maintenanceContractor=body.maintenanceContractor, installationDate=body.installationDate,
        qrCode=f"SAFEOPS-FIRE-{code}", createdBy=user.id,
    )
    e.status = svc.compute_status(e)
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return _eq(e)


@router.get("/equipment/{eid}")
async def get_equipment(eid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    e = await db.get(FireEquipment, eid)
    if not e or e.isDeleted:
        raise HTTPException(404, "Equipment not found")
    await _require(db, user, _READ, plant_id=e.plantId)
    # inspection history = CAMS engagements (single engine), sourceModule=FIRE
    insp = (
        await db.execute(
            select(CamsEngagement).where(CamsEngagement.sourceModule == "FIRE").where(CamsEngagement.sourceEntityId == eid)
            .order_by(CamsEngagement.plannedDate.desc())
        )
    ).scalars().all()
    history = [
        {"id": i.id, "engagementCode": i.engagementCode, "title": i.title, "status": i.status,
         "plannedDate": i.plannedDate.isoformat() if i.plannedDate else None, "scorePercent": i.scorePercent}
        for i in insp
    ]
    return {**_eq(e), "inspectionHistory": history}


class OutOfServiceBody(BaseModel):
    reason: str = Field(min_length=5)


@router.post("/equipment/{eid}/out-of-service")
async def out_of_service(eid: str, body: OutOfServiceBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    e = await db.get(FireEquipment, eid)
    if not e or e.isDeleted:
        raise HTTPException(404, "Equipment not found")
    await _require(db, user, _WRITE, plant_id=e.plantId)
    e.status = "OUT_OF_SERVICE"
    e.outOfServiceReason = body.reason
    e.updatedBy = user.id
    await db.commit()
    await db.refresh(e)
    return _eq(e)


@router.post("/equipment/{eid}/trigger-inspection", status_code=201)
async def trigger_inspection(eid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Create a CAMS inspection engagement for this equipment (single engine —
    sourceModule='FIRE', sourceEntityId=equipment.id). No parallel checklist store."""
    e = await db.get(FireEquipment, eid)
    if not e or e.isDeleted:
        raise HTTPException(404, "Equipment not found")
    await _require(db, user, _WRITE, plant_id=e.plantId)
    n = (await db.execute(select(func.count()).select_from(CamsEngagement).where(CamsEngagement.sourceModule == "FIRE"))).scalar() or 0
    eng = CamsEngagement(
        engagementCode=f"FIRE-INSP-{_now().year}-{n + 1:04d}",
        title=f"Fire equipment inspection — {e.equipmentCode} ({e.type})",
        engagementType="inspection", siteId=e.plantId, leadAuditorId=user.id,
        plannedDate=_now(), status="PLANNED", sourceModule="FIRE", sourceEntityId=e.id,
    )
    db.add(eng)
    await db.commit()
    await db.refresh(eng)
    return {"ok": True, "engagementId": eng.id, "engagementCode": eng.engagementCode}


@router.post("/recompute-status")
async def recompute(plantId: str | None = Query(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, _WRITE)
    res = await svc.recompute_all_statuses(db, plant_id=plantId)
    await db.commit()
    return res


@router.get("/equipment-due")
async def equipment_due(days: int = Query(30, ge=1, le=365), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """FS-09 — 'all equipment due within N days' (regulator-ready)."""
    await _require(db, user, _READ)
    scope = await build_query_scope(db, user.id, _READ)
    horizon = _now() + timedelta(days=days)
    stmt = scope.apply(select(FireEquipment).where(FireEquipment.isDeleted.is_(False)).where(FireEquipment.isActive.is_(True)).where(FireEquipment.nextInspectionDueDate <= horizon), FireEquipment)
    rows = (await db.execute(stmt.order_by(FireEquipment.nextInspectionDueDate.asc()))).scalars().all()
    return {"items": [_eq(e) for e in rows], "total": len(rows), "windowDays": days}


# ── Assembly points ──────────────────────────────────────────────────────────
@router.get("/assembly-points")
async def list_aps(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, _READ)
    scope = await build_query_scope(db, user.id, _READ)
    rows = (await db.execute(scope.apply(select(AssemblyPoint).where(AssemblyPoint.isDeleted.is_(False)), AssemblyPoint))).scalars().all()
    return {"items": [{"id": a.id, "code": a.code, "name": a.name, "plantId": a.plantId, "capacity": a.capacity,
                       "wardenUserId": a.wardenUserId, "alternateWardenUserId": a.alternateWardenUserId,
                       "buildingIds": a.buildingIds, "latitude": a.latitude, "longitude": a.longitude} for a in rows]}


# ── Plans ────────────────────────────────────────────────────────────────────
@router.get("/plans")
async def list_plans(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, _READ)
    scope = await build_query_scope(db, user.id, _READ)
    rows = (await db.execute(scope.apply(select(FireEmergencyPlan).where(FireEmergencyPlan.isDeleted.is_(False)), FireEmergencyPlan))).scalars().all()
    return {"items": [{"id": p.id, "planCode": p.planCode, "title": p.title, "plantId": p.plantId, "status": p.status,
                       "fireTypes": p.fireTypes, "assemblyPointIds": p.assemblyPointIds, "externalContacts": p.externalContacts,
                       "commandStructure": p.commandStructure, "nextReviewDate": p.nextReviewDate.isoformat() if p.nextReviewDate else None} for p in rows]}


# ── Drills (FS-07) ───────────────────────────────────────────────────────────
@router.get("/drills")
async def list_drills(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, _READ)
    scope = await build_query_scope(db, user.id, _READ)
    # Newest-created first — platform-wide register convention.
    rows = (
        await db.execute(
            scope.apply(select(FireDrill).where(FireDrill.isDeleted.is_(False)), FireDrill).order_by(
                FireDrill.createdAt.desc(), FireDrill.id.desc()
            ),
        )
    ).scalars().all()
    out = []
    for d in rows:
        out.append({"id": d.id, "drillCode": d.drillCode, "plantId": d.plantId, "drillType": d.drillType,
                    "status": d.status, "outcome": d.outcome,
                    "scheduledDate": d.scheduledDate.isoformat() if d.scheduledDate else None,
                    "conductedDate": d.conductedDate.isoformat() if d.conductedDate else None,
                    "evacuationTimeMinutes": d.evacuationTimeMinutes, "evacuationTargetMinutes": d.evacuationTargetMinutes,
                    "unaccountedPersons": d.unaccountedPersons, "isAnnualMandatory": d.isAnnualMandatory})
    return {"items": out, "total": len(out)}


class DrillCompleteBody(BaseModel):
    conductedDate: datetime | None = None
    outcome: str = "SATISFACTORY"
    participantCount: int | None = None
    evacuationTimeMinutes: float | None = None
    evacuationTargetMinutes: float | None = None
    assemblyPointVerified: bool = True
    unaccountedPersons: int = 0
    reportRichText: str | None = None


@router.post("/drills/{did}/complete")
async def complete_drill(did: str, body: DrillCompleteBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d = await db.get(FireDrill, did)
    if not d or d.isDeleted:
        raise HTTPException(404, "Drill not found")
    await _require(db, user, _WRITE, plant_id=d.plantId)
    # apply the conduct data first so the gate sees the final values
    d.unaccountedPersons = body.unaccountedPersons
    d.assemblyPointVerified = body.assemblyPointVerified
    d.participantCount = body.participantCount
    d.evacuationTimeMinutes = body.evacuationTimeMinutes
    d.evacuationTargetMinutes = body.evacuationTargetMinutes
    d.reportRichText = body.reportRichText
    blockers = await svc.drill_completion_blockers(db, d)
    if blockers:
        raise HTTPException(400, "Cannot complete drill: " + " ".join(blockers))
    d.status = "COMPLETED"
    d.outcome = body.outcome
    d.conductedDate = body.conductedDate or _now()
    d.updatedBy = user.id
    await db.commit()
    return {"ok": True, "drillId": d.id, "status": d.status, "outcome": d.outcome}


# ── Crisis escalation + FSER ─────────────────────────────────────────────────
class EscalateBody(BaseModel):
    plantId: str | None = None
    affectedEquipmentIds: list[str] = []
    evacuationOrdered: bool = True
    fireServiceCalled: bool = True


@router.post("/incidents/{incident_id}/escalate-crisis", status_code=201)
async def escalate_crisis(incident_id: str, body: EscalateBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, _WRITE, plant_id=body.plantId)
    res = await svc.escalate_incident_to_crisis(
        db, incident_id, body.plantId, user.id, body.affectedEquipmentIds, body.evacuationOrdered, body.fireServiceCalled,
    )
    await db.commit()
    return res


@router.get("/fser/{plant_id}")
async def fser(plant_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, _READ, plant_id=plant_id)
    return await svc.fser_panel(db, plant_id)
