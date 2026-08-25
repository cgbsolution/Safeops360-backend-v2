"""Fire Safety & Emergency Response API (P1-4).

Equipment lifecycle, assembly points, emergency plans, drills (with the MAJOR_GAP
completion gate), the CAMS-engine inspection trigger (sourceModule='FIRE'), crisis
escalation and the FSER panel. Plant-scoped via QueryScope.

NB: gated by the FIRE licence module in the model (registry/editions); the router is
mounted always-on in dev because the unsigned dev licence predates the FIRE code —
add "fire_safety": "FIRE" to ROUTER_MODULE once a FIRE-inclusive licence is issued.

RBAC uses the dedicated FIRE.* grants (see services/fire_permissions.py and
prisma/seed-rbac.ts). The earlier INCIDENT.READ/UPDATE bootstrap is retired; it
had two live defects — auditors could not read the register they inspect, and
contractors could — both of which the FIRE grants close.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.db import get_db
from app.models.cams import CamsEngagement, CamsFinding
from app.models.fire_safety import (
    AssemblyPoint, FireAmcContract, FireAssetCertificate, FireDrill, FireDrillFinding,
    FireEmergencyPlan, FireEquipment, FireFalseAlarmLog, FireIncidentLink, FireZone,
    InspectionFrequencyMaster,
)
from app.models.user import User
from app.services import fire_certificates as certsvc
from app.services import fire_checklist_pdf as firepdf
from app.services import fire_checklist_xlsx as firexlsx
from app.services import fire_defects as defectsvc
from app.services import fire_frequency as freqsvc
from app.services import fire_permissions as perm
from app.services import fire_safety as svc
from app.services.access_scope import build_query_scope
from app.services.permissions import can

router = APIRouter(prefix="/api/fire", tags=["fire-safety"])

# Dedicated FIRE.* grants now exist, so the INCIDENT.* bootstrap this router
# documented is retired. It was not merely untidy — it had two live defects that
# the docstring called out and nothing fixed:
#
#   * AUDITOR and LEAD_AUDITOR hold no INCIDENT grant, so the roles whose job is
#     to inspect this register could not open it.
#   * WORKER and CONTRACTOR_WORKMAN hold INCIDENT.READ at OWN_RECORDS, which
#     get_accessible_plants_for widens to the whole plant — so a contractor could
#     read the entire fire estate.
#
# Both are closed by the FIRE grants in prisma/seed-rbac.ts (auditors get
# READ+EXPORT; workers and contractors get nothing). `fire_permissions.require`
# falls back to the old codes only while FIRE.* is un-seeded, so this switch does
# not lock out a deployment that has not reseeded yet.
_READ = perm.READ
_WRITE = perm.UPDATE
_require = perm.require


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
        "zoneId": e.zoneId, "assetSubtype": e.assetSubtype, "amcContractId": e.amcContractId,
        "frequencyMasterId": e.frequencyMasterId, "frequencyOverrideReason": e.frequencyOverrideReason,
        "statusOverride": e.statusOverride, "statusOverrideReason": e.statusOverrideReason,
        "statusOverriddenBy": e.statusOverriddenBy,
        "statusOverriddenAt": e.statusOverriddenAt.isoformat() if e.statusOverriddenAt else None,
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
    # Fire & Life Safety fields. Absent from the P1-4 body, so a client sending
    # them had them silently dropped by Pydantic's default extra-ignore — the
    # asset saved, unzoned, with no error to explain why.
    zoneId: str | None = None
    assetSubtype: str | None = None


async def _next_code(db: AsyncSession, plant_id: str) -> str:
    n = (await db.execute(select(func.count()).select_from(FireEquipment).where(FireEquipment.plantId == plant_id))).scalar() or 0
    return f"FE-{plant_id[:4].upper()}-{n + 1:04d}"


@router.post("/equipment", status_code=201)
async def create_equipment(body: EquipmentCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, _WRITE, plant_id=body.plantId)
    # A zone from another plant would break the hot-work guard silently: the
    # asset would count toward a zone it is nowhere near.
    if body.zoneId:
        z = await db.get(FireZone, body.zoneId)
        if not z or z.isDeleted:
            raise HTTPException(404, "Zone not found")
        if z.plantId != body.plantId:
            raise HTTPException(400, f"Zone {z.zoneCode} belongs to a different plant.")
    code = await _next_code(db, body.plantId)
    e = FireEquipment(
        equipmentCode=code, type=body.type, location=body.location, plantId=body.plantId, buildingId=body.buildingId,
        make=body.make, model=body.model, serialNo=body.serialNo, capacitySpec=body.capacitySpec,
        inspectionFrequencyDays=body.inspectionFrequencyDays, latitude=body.latitude, longitude=body.longitude,
        floorLevel=body.floorLevel, maintenanceContractor=body.maintenanceContractor, installationDate=body.installationDate,
        zoneId=body.zoneId, assetSubtype=body.assetSubtype,
        qrCode=f"SAFEOPS-FIRE-{code}", createdBy=user.id,
    )
    # Record which frequency rule governs this asset at create time, so the
    # register can show "quarterly per NBC 2016" before the first nightly run.
    # nextInspectionDueDate stays NULL deliberately — a brand-new asset has never
    # been inspected, and compute_status reads that as DUE_INSPECTION rather than
    # inventing a due date from an inspection that never happened.
    #
    # Non-fatal: InspectionFrequencyMaster is created by apply-firelifesafety-ddl,
    # and registering an asset must not depend on that config table existing.
    # Losing the provenance stamp costs a label on the detail screen; losing the
    # asset costs the inspection.
    freq = None
    try:
        freq = await freqsvc.resolve_for_equipment(db, e)
        e.frequencyMasterId = freq.masterId
    except Exception:  # noqa: BLE001 — see above; the nightly recompute backfills it
        await db.rollback()
    e.status = svc.compute_status(e)
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return {**_eq(e), "frequency": freq.as_dict() if freq else None}


# ── register exports ────────────────────────────────────────────────────────
# Declared ABOVE /equipment/{eid}: FastAPI matches in declaration order, and a
# literal path registered after a path-parameter route of the same shape is
# unreachable — "export.pdf" would arrive as an equipment id and 404.
async def _export_rows(db: AsyncSession, user: User, etype: str | None) -> list[dict[str, Any]]:
    """The rows the register screen shows, through the same scope filter.

    Extinguishers are excluded by default because they have their own controlled
    sixteen-column export (PIL/EHSD/CL/028-R1) — putting them in both is the
    duplication the consolidated register exists to remove.
    """
    await _require(db, user, perm.EXPORT)
    scope = await build_query_scope(db, user.id, _READ)
    stmt = scope.apply(select(FireEquipment).where(FireEquipment.isDeleted.is_(False)), FireEquipment)
    if etype:
        stmt = stmt.where(FireEquipment.type == etype)
    else:
        stmt = stmt.where(FireEquipment.type != "FIRE_EXTINGUISHER")
    rows = (await db.execute(stmt.order_by(FireEquipment.location.asc(), FireEquipment.equipmentCode.asc()))).scalars().all()
    return [_eq(e) for e in rows]


@router.get("/equipment/export.pdf")
async def export_equipment_pdf(
    etype: str | None = Query(None, alias="type"),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> Response:
    """The 'All other fire assets' tab as a controlled-looking printout."""
    rows = await _export_rows(db, user, etype)
    return Response(
        content=firepdf.render_assets(rows), media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="fire-asset-register.pdf"'},
    )


@router.get("/equipment/export.xlsx")
async def export_equipment_xlsx(
    etype: str | None = Query(None, alias="type"),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> Response:
    """The same rows as a workbook, with an autofilter on status and due date."""
    rows = await _export_rows(db, user, etype)
    return Response(
        content=firexlsx.render_assets(rows), media_type=firexlsx.MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="fire-asset-register.xlsx"'},
    )


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


class EquipmentUpdate(BaseModel):
    """Partial update. Only fields actually sent are applied (`exclude_unset`),
    so a client editing one field cannot blank the rest by omission.

    Deliberately NOT editable here:
      • `plantId` — the asset code, its zone and every access-scope decision are
        derived from it. Moving a site is a decommission-and-re-register, not an
        edit, and silently repointing it would orphan the code sequence.
      • `equipmentCode` / `qrCode` — identity. A printed QR label on the wall is
        the physical counterpart of this row; renaming it invalidates the label.
      • `status` — derived. Manual states go through /status-override, which
        demands a reason and writes the audit chain.
      • `lastInspectionDate` / `nextInspectionDueDate` — derived from inspection
        records and the frequency rule. Hand-editing a due date is how a register
        starts lying about compliance.
    """

    type: str | None = None
    assetSubtype: str | None = None
    location: str | None = Field(default=None, min_length=2)
    zoneId: str | None = None
    buildingId: str | None = None
    make: str | None = None
    model: str | None = None
    serialNo: str | None = None
    capacitySpec: str | None = None
    maintenanceContractor: str | None = None
    amcContractId: str | None = None
    installationDate: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    floorLevel: int | None = None
    # A per-asset cadence override is only honoured by fire_frequency.resolve()
    # when it carries a reason, so the two are validated as a pair below.
    inspectionFrequencyDays: int | None = Field(default=None, ge=1, le=3650)
    frequencyOverrideReason: str | None = None


@router.patch("/equipment/{eid}")
async def update_equipment(eid: str, body: EquipmentUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    e = await db.get(FireEquipment, eid)
    if not e or e.isDeleted:
        raise HTTPException(404, "Equipment not found")
    await _require(db, user, _WRITE, plant_id=e.plantId)

    patch = body.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(400, "No fields to update.")

    if "zoneId" in patch and patch["zoneId"]:
        z = await db.get(FireZone, patch["zoneId"])
        if not z or z.isDeleted:
            raise HTTPException(404, "Zone not found")
        if z.plantId != e.plantId:
            raise HTTPException(400, f"Zone {z.zoneCode} belongs to a different plant.")

    if "amcContractId" in patch and patch["amcContractId"]:
        c = await db.get(FireAmcContract, patch["amcContractId"])
        if not c or c.isDeleted:
            raise HTTPException(404, "AMC contract not found")
        if c.plantId != e.plantId:
            raise HTTPException(400, f"Contract {c.contractCode} belongs to a different plant.")

    # An override without a reason is invisible: fire_frequency.resolve() ignores
    # it and silently falls back to config, so the user would see their edit
    # "saved" and the cadence unchanged. Reject rather than accept-and-ignore.
    new_days = patch.get("inspectionFrequencyDays", e.inspectionFrequencyDays)
    new_reason = patch.get("frequencyOverrideReason", e.frequencyOverrideReason)
    if "inspectionFrequencyDays" in patch and not (new_reason or "").strip():
        raise HTTPException(
            400,
            "Changing the inspection interval requires frequencyOverrideReason — without one the "
            "configured frequency rule applies and the override has no effect.",
        )
    if "frequencyOverrideReason" in patch and (new_reason or "").strip() and not new_days:
        raise HTTPException(400, "frequencyOverrideReason requires inspectionFrequencyDays.")

    for field, value in patch.items():
        setattr(e, field, value)
    e.updatedBy = user.id
    await db.commit()
    await db.refresh(e)

    freq = None
    try:
        freq = await freqsvc.resolve_for_equipment(db, e)
    except Exception:  # noqa: BLE001 — provenance is a label, not the payload
        await db.rollback()
    return {**_eq(e), "frequency": freq.as_dict() if freq else None}


class EquipmentDeleteBody(BaseModel):
    # soft_delete() enforces 10 chars itself; declared here so the client gets a
    # 422 with a field error instead of a 400 with a string.
    reason: str = Field(min_length=10)


@router.delete("/equipment/{eid}")
async def delete_equipment(eid: str, body: EquipmentDeleteBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Soft-delete an asset.

    `FireEquipment` is a governed entity: the ORM guard blocks hard deletes
    outright, and soft-deleted rows are auto-excluded from every SELECT. A fire
    asset is statutory evidence — the regulator's question is "what did you have
    and when", so the row survives with who removed it and why.

    Refuses while open defects exist. Deleting the asset would strand them: the
    defect board reads `areaOrAssetRef`, and a defect whose asset has vanished
    can never be closed by re-inspection.
    """
    e = await db.get(FireEquipment, eid)
    if not e or e.isDeleted:
        raise HTTPException(404, "Equipment not found")
    await _require(db, user, _WRITE, plant_id=e.plantId)

    open_defects = [
        d for d in await defectsvc.defects_for_asset(db, eid) if d.status in ("OPEN", "IN_PROGRESS")
    ]
    if open_defects:
        raise HTTPException(
            409,
            f"{len(open_defects)} open defect(s) reference this asset "
            f"({', '.join(d.findingCode for d in open_defects[:3])}). Close or reassign them first.",
        )

    from app.core.soft_delete import soft_delete

    try:
        soft_delete(e, user.id, body.reason)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    return {"ok": True, "equipmentId": eid, "equipmentCode": e.equipmentCode, "reason": body.reason}


class OutOfServiceBody(BaseModel):
    reason: str = Field(min_length=5)


@router.post("/equipment/{eid}/out-of-service")
async def out_of_service(eid: str, body: OutOfServiceBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Take an asset out of service.

    Routes through `set_status_override` rather than writing `status` directly:
    spec §5.2 requires an audit-logged reason for any manual status, and the old
    direct write left no record of who decided or why — the next nightly
    recompute could not even tell it was a human decision.
    """
    e = await db.get(FireEquipment, eid)
    if not e or e.isDeleted:
        raise HTTPException(404, "Equipment not found")
    await _require(db, user, _WRITE, plant_id=e.plantId)
    await svc.set_status_override(db, e, status="OUT_OF_SERVICE", reason=body.reason, actor_id=user.id)
    await db.commit()
    await db.refresh(e)
    return _eq(e)


class StatusOverrideBody(BaseModel):
    # None clears the override and returns the asset to the engine.
    status: str | None = None
    reason: str = Field(min_length=5)


@router.post("/equipment/{eid}/status-override")
async def status_override(eid: str, body: StatusOverrideBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Set or clear a manual status override (spec §5.2). Reason mandatory, audit-logged."""
    e = await db.get(FireEquipment, eid)
    if not e or e.isDeleted:
        raise HTTPException(404, "Equipment not found")
    await _require(db, user, _WRITE, plant_id=e.plantId)
    try:
        res = await svc.set_status_override(db, e, status=body.status, reason=body.reason, actor_id=user.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    return res


@router.get("/equipment/{eid}/frequency")
async def equipment_frequency(eid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Why this asset is on the cadence it is on — the answer an inspector shows
    a regulator. Returns the matched rule, its regulatory citation, and whether it
    resolved at all (an unresolved asset is silently on the 30-day fallback)."""
    e = await db.get(FireEquipment, eid)
    if not e or e.isDeleted:
        raise HTTPException(404, "Equipment not found")
    await _require(db, user, _READ, plant_id=e.plantId)
    res = await freqsvc.resolve_for_equipment(db, e)
    return {"equipmentId": e.id, "equipmentCode": e.equipmentCode, **res.as_dict()}


@router.post("/equipment/{eid}/trigger-inspection", status_code=201)
async def trigger_inspection(eid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Create a CAMS inspection engagement for this equipment (single engine —
    sourceModule='FIRE', sourceEntityId=equipment.id). No parallel checklist store."""
    e = await db.get(FireEquipment, eid)
    if not e or e.isDeleted:
        raise HTTPException(404, "Equipment not found")
    await _require(db, user, perm.EXECUTE, plant_id=e.plantId)
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


# ═════════════════════════════════════════════════════════════════════════════
# Fire & Life Safety extension
# ═════════════════════════════════════════════════════════════════════════════

# ── Zones ────────────────────────────────────────────────────────────────────
def _zone(z: FireZone) -> dict[str, Any]:
    return {
        "id": z.id, "zoneCode": z.zoneCode, "name": z.name, "plantId": z.plantId,
        "buildingId": z.buildingId, "areaId": z.areaId, "parentZoneId": z.parentZoneId,
        "floor": z.floor, "areaSqm": z.areaSqm, "coverageType": z.coverageType,
        "criticality": z.criticality, "requiredAssetTypes": z.requiredAssetTypes,
        "panelAssetId": z.panelAssetId, "isActive": z.isActive,
    }


@router.get("/zones")
async def list_zones(
    buildingId: str | None = Query(None), criticality: str | None = Query(None),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, _READ)
    scope = await build_query_scope(db, user.id, _READ)
    stmt = scope.apply(select(FireZone).where(FireZone.isDeleted.is_(False)), FireZone)
    if buildingId:
        stmt = stmt.where(FireZone.buildingId == buildingId)
    if criticality:
        stmt = stmt.where(FireZone.criticality == criticality)
    rows = (await db.execute(stmt.order_by(FireZone.zoneCode))).scalars().all()
    # Asset counts per zone in one query — the zone list is the map legend and
    # rendering "0 assets" because the count was too expensive to fetch is worse
    # than not showing it.
    counts = dict(
        (
            await db.execute(
                select(FireEquipment.zoneId, func.count())
                .where(FireEquipment.isDeleted.is_(False))
                .where(FireEquipment.isActive.is_(True))
                .group_by(FireEquipment.zoneId)
            )
        ).all()
    )
    return {
        "items": [{**_zone(z), "assetCount": counts.get(z.id, 0)} for z in rows],
        "total": len(rows),
    }


class ZoneCreate(BaseModel):
    name: str = Field(min_length=2)
    plantId: str
    buildingId: str | None = None
    areaId: str | None = None
    parentZoneId: str | None = None
    floor: str | None = None
    areaSqm: float | None = None
    coverageType: str = "BOTH"
    criticality: str = "STANDARD"
    requiredAssetTypes: list[str] = []
    panelAssetId: str | None = None


@router.post("/zones", status_code=201)
async def create_zone(body: ZoneCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, _WRITE, plant_id=body.plantId)
    if body.coverageType not in ("DETECTION", "SUPPRESSION", "BOTH"):
        raise HTTPException(400, "coverageType must be DETECTION, SUPPRESSION or BOTH")
    if body.criticality not in ("CRITICAL", "HIGH", "STANDARD"):
        raise HTTPException(400, "criticality must be CRITICAL, HIGH or STANDARD")
    n = (await db.execute(select(func.count()).select_from(FireZone).where(FireZone.plantId == body.plantId))).scalar() or 0
    z = FireZone(
        zoneCode=f"FZ-{body.plantId[:4].upper()}-{n + 1:03d}", name=body.name, plantId=body.plantId,
        buildingId=body.buildingId, areaId=body.areaId, parentZoneId=body.parentZoneId, floor=body.floor,
        areaSqm=body.areaSqm, coverageType=body.coverageType, criticality=body.criticality,
        requiredAssetTypes=body.requiredAssetTypes, panelAssetId=body.panelAssetId, createdBy=user.id,
    )
    db.add(z)
    await db.commit()
    await db.refresh(z)
    return _zone(z)


@router.get("/zones/{zid}/compliance")
async def zone_compliance(zid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Suppression/detection readiness of one zone.

    This is the read the hot-work PTW guard (spec §4.6) will consume. It lives
    here rather than in the PTW router so there is one definition of "is this
    zone covered", and it already reports `recommendedAction` — block for
    CRITICAL zones, warn otherwise — so the PTW screen renders a decision rather
    than re-deriving one.
    """
    z = await db.get(FireZone, zid)
    if not z or z.isDeleted:
        raise HTTPException(404, "Zone not found")
    await _require(db, user, _READ, plant_id=z.plantId)
    assets = (
        await db.execute(
            select(FireEquipment).where(FireEquipment.zoneId == zid)
            .where(FireEquipment.isDeleted.is_(False)).where(FireEquipment.isActive.is_(True))
        )
    ).scalars().all()
    impaired = [a for a in assets if a.status in ("OVERDUE", "NON_COMPLIANT", "OUT_OF_SERVICE")]
    required = set(z.requiredAssetTypes or [])
    present = {a.type for a in assets if a.status not in ("OVERDUE", "NON_COMPLIANT", "OUT_OF_SERVICE")}
    missing = sorted(required - present)
    compliant = not impaired and not missing
    return {
        "zoneId": z.id, "zoneCode": z.zoneCode, "name": z.name, "criticality": z.criticality,
        "assetCount": len(assets), "compliant": compliant,
        "impairedAssets": [_eq(a) for a in impaired],
        "missingRequiredTypes": missing,
        "recommendedAction": (
            "ALLOW" if compliant else ("BLOCK" if z.criticality == "CRITICAL" else "WARN")
        ),
    }


# ── Inspection frequency master (admin config) ───────────────────────────────
def _ifm(r: InspectionFrequencyMaster) -> dict[str, Any]:
    return {
        "id": r.id, "plantId": r.plantId, "region": r.region, "assetType": r.assetType,
        "assetSubtype": r.assetSubtype, "frequency": r.frequency, "customIntervalDays": r.customIntervalDays,
        "intervalDays": freqsvc.interval_days(r), "regulatoryReference": r.regulatoryReference,
        "checklistTemplateId": r.checklistTemplateId, "auditTypeId": r.auditTypeId,
        "leadTimeDays": r.leadTimeDays, "isActive": r.isActive,
    }


@router.get("/frequency-master")
async def list_frequency_master(
    region: str = Query(freqsvc.DEFAULT_REGION), plantId: str | None = Query(None),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, _READ)
    stmt = (
        select(InspectionFrequencyMaster)
        .where(InspectionFrequencyMaster.isDeleted.is_(False))
        .where(InspectionFrequencyMaster.region == region)
    )
    if plantId:
        stmt = stmt.where(
            (InspectionFrequencyMaster.plantId == plantId) | (InspectionFrequencyMaster.plantId.is_(None))
        )
    rows = (await db.execute(stmt.order_by(InspectionFrequencyMaster.assetType))).scalars().all()
    return {
        "items": [_ifm(r) for r in rows],
        "total": len(rows),
        "region": region,
        # An asset type in the register with no rule is a silent 30-day fallback.
        # Surfaced beside the rules rather than on a separate screen nobody opens.
        "coverageGaps": await freqsvc.coverage_gaps(db, region=region),
    }


class FrequencyMasterCreate(BaseModel):
    assetType: str
    frequency: str
    region: str = freqsvc.DEFAULT_REGION
    plantId: str | None = None
    assetSubtype: str | None = None
    customIntervalDays: int | None = None
    regulatoryReference: str | None = None
    checklistTemplateId: str | None = None
    auditTypeId: str | None = None
    leadTimeDays: int = 7


@router.post("/frequency-master", status_code=201)
async def create_frequency_master(body: FrequencyMasterCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, _WRITE, plant_id=body.plantId)
    if body.frequency not in (*freqsvc.FREQUENCY_DAYS, "CUSTOM"):
        raise HTTPException(400, f"frequency must be one of {', '.join(freqsvc.FREQUENCY_DAYS)} or CUSTOM")
    if body.frequency == "CUSTOM" and not body.customIntervalDays:
        raise HTTPException(400, "CUSTOM frequency requires customIntervalDays")
    r = InspectionFrequencyMaster(
        plantId=body.plantId, region=body.region, assetType=body.assetType, assetSubtype=body.assetSubtype,
        frequency=body.frequency, customIntervalDays=body.customIntervalDays,
        regulatoryReference=body.regulatoryReference, checklistTemplateId=body.checklistTemplateId,
        auditTypeId=body.auditTypeId, leadTimeDays=body.leadTimeDays, createdBy=user.id,
    )
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return _ifm(r)


# ── AMC contracts ────────────────────────────────────────────────────────────
def _amc(c: FireAmcContract) -> dict[str, Any]:
    return {
        "id": c.id, "contractCode": c.contractCode, "plantId": c.plantId, "vendorName": c.vendorName,
        "vendorContactId": c.vendorContactId, "vendorEmail": c.vendorEmail, "vendorPhone": c.vendorPhone,
        "scopeSummary": c.scopeSummary, "status": c.status, "annualValueInr": c.annualValueInr,
        "startDate": c.startDate.isoformat() if c.startDate else None,
        "endDate": c.endDate.isoformat() if c.endDate else None,
        "daysRemaining": certsvc.days_remaining(c.endDate),
        "renewalReminderDays": certsvc.tiers_for(c.renewalReminderDays),
        "lastReminderTierSent": c.lastReminderTierSent,
        "escalatedAt": c.escalatedAt.isoformat() if c.escalatedAt else None,
        "contractDocumentIds": c.contractDocumentIds,
        # Stated on every payload so no UI has to remember spec §4.4.
        "affectsComplianceStatus": False,
    }


@router.get("/amc-contracts")
async def list_amc(
    status_filter: str | None = Query(None, alias="status"),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, _READ)
    scope = await build_query_scope(db, user.id, _READ)
    stmt = scope.apply(select(FireAmcContract).where(FireAmcContract.isDeleted.is_(False)), FireAmcContract)
    if status_filter:
        stmt = stmt.where(FireAmcContract.status == status_filter)
    rows = (await db.execute(stmt.order_by(FireAmcContract.endDate.asc()))).scalars().all()
    return {"items": [_amc(c) for c in rows], "total": len(rows)}


class AmcCreate(BaseModel):
    plantId: str
    vendorName: str = Field(min_length=2)
    startDate: datetime
    endDate: datetime
    vendorContactId: str | None = None
    vendorEmail: str | None = None
    vendorPhone: str | None = None
    scopeSummary: str | None = None
    annualValueInr: float | None = None
    renewalReminderDays: list[int] = []
    assetIds: list[str] = []


@router.post("/amc-contracts", status_code=201)
async def create_amc(body: AmcCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, _WRITE, plant_id=body.plantId)
    if body.endDate <= body.startDate:
        raise HTTPException(400, "endDate must be after startDate")
    n = (await db.execute(select(func.count()).select_from(FireAmcContract).where(FireAmcContract.plantId == body.plantId))).scalar() or 0
    c = FireAmcContract(
        contractCode=f"AMC-{body.plantId[:4].upper()}-{_now().year}-{n + 1:03d}", plantId=body.plantId,
        vendorName=body.vendorName, vendorContactId=body.vendorContactId, vendorEmail=body.vendorEmail,
        vendorPhone=body.vendorPhone, scopeSummary=body.scopeSummary, startDate=body.startDate,
        endDate=body.endDate, annualValueInr=body.annualValueInr,
        renewalReminderDays=certsvc.tiers_for(body.renewalReminderDays), createdBy=user.id,
    )
    db.add(c)
    await db.flush()
    # Assets point at the contract, not the reverse — one indexed column update
    # per asset, and re-assigning an asset never rewrites the contract row.
    if body.assetIds:
        assets = (await db.execute(select(FireEquipment).where(FireEquipment.id.in_(body.assetIds)))).scalars().all()
        for a in assets:
            a.amcContractId = c.id
            a.updatedBy = user.id
    c.status = certsvc.status_for(c.endDate, certsvc.tiers_for(c.renewalReminderDays))
    await db.commit()
    await db.refresh(c)
    return _amc(c)


# ── Asset certificates ───────────────────────────────────────────────────────
def _cert(c: FireAssetCertificate) -> dict[str, Any]:
    return {
        "id": c.id, "assetId": c.assetId, "plantId": c.plantId, "certificateType": c.certificateType,
        "certificateNo": c.certificateNo, "issuingAuthority": c.issuingAuthority, "status": c.status,
        "issueDate": c.issueDate.isoformat() if c.issueDate else None,
        "expiryDate": c.expiryDate.isoformat() if c.expiryDate else None,
        "daysRemaining": certsvc.days_remaining(c.expiryDate),
        "escalationTierDays": certsvc.tiers_for(c.escalationTierDays),
        "documentIds": c.documentIds, "notes": c.notes,
    }


@router.get("/certificates")
async def list_certificates(
    assetId: str | None = Query(None), status_filter: str | None = Query(None, alias="status"),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Asset-level certificates only.

    Site-level statutory certificates (Fire NOC, PESO licence) are NOT served
    here — they live in the Statutory Register (`RegulatoryRegistration`) and are
    read from `/api/factory-ext/registrations`. Two endpoints returning the same
    certificate would be the second source of truth spec §6 rules out.
    """
    await _require(db, user, _READ)
    scope = await build_query_scope(db, user.id, _READ)
    stmt = scope.apply(
        select(FireAssetCertificate).where(FireAssetCertificate.isDeleted.is_(False)), FireAssetCertificate
    )
    if assetId:
        stmt = stmt.where(FireAssetCertificate.assetId == assetId)
    if status_filter:
        stmt = stmt.where(FireAssetCertificate.status == status_filter)
    rows = (await db.execute(stmt.order_by(FireAssetCertificate.expiryDate.asc().nulls_last()))).scalars().all()
    return {"items": [_cert(c) for c in rows], "total": len(rows)}


class CertificateCreate(BaseModel):
    assetId: str
    certificateType: str
    plantId: str | None = None
    certificateNo: str | None = None
    issuingAuthority: str | None = None
    issueDate: datetime | None = None
    expiryDate: datetime | None = None
    escalationTierDays: list[int] = []
    documentIds: list[str] = []
    notes: str | None = None


@router.post("/certificates", status_code=201)
async def create_certificate(body: CertificateCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    asset = await db.get(FireEquipment, body.assetId)
    if not asset or asset.isDeleted:
        raise HTTPException(404, "Asset not found")
    await _require(db, user, _WRITE, plant_id=asset.plantId)
    c = FireAssetCertificate(
        assetId=asset.id, plantId=body.plantId or asset.plantId, certificateType=body.certificateType,
        certificateNo=body.certificateNo, issuingAuthority=body.issuingAuthority, issueDate=body.issueDate,
        expiryDate=body.expiryDate, escalationTierDays=certsvc.tiers_for(body.escalationTierDays),
        documentIds=body.documentIds, notes=body.notes, createdBy=user.id,
    )
    c.status = certsvc.status_for(c.expiryDate, certsvc.tiers_for(c.escalationTierDays))
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return _cert(c)


@router.post("/expiry-sweep")
async def expiry_sweep(plantId: str | None = Query(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Run the AMC + certificate + statutory-registration tier sweep (spec §4.4, §5.6).

    Idempotent: each tier fires once. Exposed as an endpoint so the nightly job
    and an operator running it on demand take the identical code path.
    """
    await _require(db, user, _WRITE, plant_id=plantId)
    res = await certsvc.sweep_all(db, plantId)
    await db.commit()
    return res


# ── Defects (CamsFinding on a FIRE engagement) ───────────────────────────────
def _defect(f: CamsFinding) -> dict[str, Any]:
    return {
        "id": f.id, "findingCode": f.findingCode, "engagementId": f.engagementId, "assetId": f.areaOrAssetRef,
        "title": f.title, "description": f.description, "severity": f.severity, "status": f.status,
        "ownerId": f.ownerId, "capaId": f.capaId, "requiresCapa": f.requiresCapa,
        "verificationEngagementId": f.verificationEngagementId, "verificationNote": f.verificationNote,
        "dueDate": f.dueDate.isoformat() if f.dueDate else None,
        "closedBy": f.closedBy, "closedAt": f.closedAt.isoformat() if f.closedAt else None,
        "evidenceAttachmentIds": f.evidenceAttachmentIds,
        "createdAt": f.createdAt.isoformat() if f.createdAt else None,
    }


@router.get("/defects")
async def list_defects(
    assetId: str | None = Query(None), zoneId: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"), severity: str | None = Query(None),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The defect kanban feed. Defects are CamsFindings raised on FIRE engagements —
    one findings register, filtered, not a parallel defect store."""
    await _require(db, user, _READ)
    fire_engagements = (
        await db.execute(select(CamsEngagement.id).where(CamsEngagement.sourceModule == "FIRE"))
    ).scalars().all()
    if not fire_engagements:
        return {"items": [], "total": 0, "byStatus": {}}
    stmt = (
        select(CamsFinding)
        .where(CamsFinding.engagementId.in_(fire_engagements))
        .where(CamsFinding.isDeleted.is_(False))
    )
    if assetId:
        stmt = stmt.where(CamsFinding.areaOrAssetRef == assetId)
    if zoneId:
        zone_assets = (
            await db.execute(select(FireEquipment.id).where(FireEquipment.zoneId == zoneId))
        ).scalars().all()
        stmt = stmt.where(CamsFinding.areaOrAssetRef.in_(zone_assets or ["__none__"]))
    if status_filter:
        stmt = stmt.where(CamsFinding.status == status_filter)
    if severity:
        stmt = stmt.where(CamsFinding.severity == defectsvc.normalise_severity(severity))
    rows = (await db.execute(stmt.order_by(CamsFinding.createdAt.desc()))).scalars().all()
    by_status: dict[str, int] = {}
    for f in rows:
        by_status[f.status] = by_status.get(f.status, 0) + 1
    return {"items": [_defect(f) for f in rows], "total": len(rows), "byStatus": by_status}


class DefectCreate(BaseModel):
    engagementId: str
    assetId: str
    title: str = Field(min_length=3)
    description: str = ""
    severity: str = "MAJOR"
    ownerId: str | None = None
    sourceQuestionId: str | None = None
    evidenceAttachmentIds: list[str] = []


@router.post("/defects", status_code=201)
async def create_defect(body: DefectCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Raise a defect. A CRITICAL one spawns its CAPA in this same transaction —
    spec §5.4 — and the deferred DB constraint refuses the commit if it did not."""
    eng = await db.get(CamsEngagement, body.engagementId)
    if not eng or eng.isDeleted:
        raise HTTPException(404, "Inspection engagement not found")
    asset = await db.get(FireEquipment, body.assetId)
    if not asset or asset.isDeleted:
        raise HTTPException(404, "Asset not found")
    await _require(db, user, _WRITE, plant_id=asset.plantId)
    res = await defectsvc.raise_defect(
        db, engagement=eng, asset=asset, title=body.title, description=body.description,
        severity=body.severity, owner_id=body.ownerId, actor_id=user.id,
        source_question_id=body.sourceQuestionId, evidence_attachment_ids=body.evidenceAttachmentIds,
    )
    await db.commit()
    return res


@router.get("/defects/{fid}/closure-gate")
async def defect_closure_gate(fid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Every reason this defect cannot close yet, all at once — so the UI renders
    one panel instead of surfacing blockers one failed submit at a time."""
    f = await db.get(CamsFinding, fid)
    if not f or f.isDeleted:
        raise HTTPException(404, "Defect not found")
    await _require(db, user, _READ, plant_id=f.siteId)
    blockers = await defectsvc.closure_blockers(db, f, actor_id=user.id)
    return {
        "defectId": f.id,
        "canClose": not [b for b in blockers if b.severity == "ERROR"],
        "blockers": [b.as_dict() for b in blockers],
    }


class DefectCloseBody(BaseModel):
    verificationEngagementId: str | None = None
    note: str | None = None


@router.post("/defects/{fid}/close")
async def close_defect(fid: str, body: DefectCloseBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    f = await db.get(CamsFinding, fid)
    if not f or f.isDeleted:
        raise HTTPException(404, "Defect not found")
    await _require(db, user, perm.CLOSE, plant_id=f.siteId)
    res = await defectsvc.close_defect(
        db, f, actor_id=user.id, verification_engagement_id=body.verificationEngagementId, note=body.note,
    )
    if not res["ok"]:
        # 409, not 400: the request is well-formed, the record's state refuses it.
        raise HTTPException(409, detail=res)
    await db.commit()
    return res


@router.post("/defects/{fid}/verify")
async def verify_defect(fid: str, body: DefectCloseBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    f = await db.get(CamsFinding, fid)
    if not f or f.isDeleted:
        raise HTTPException(404, "Defect not found")
    await _require(db, user, perm.CLOSE, plant_id=f.siteId)
    res = await defectsvc.verify_defect(db, f, actor_id=user.id, note=body.note)
    if not res["ok"]:
        raise HTTPException(409, detail=res)
    await db.commit()
    return res


# ── False alarm log ──────────────────────────────────────────────────────────
class FalseAlarmCreate(BaseModel):
    panelAssetId: str
    occurredAt: datetime
    cause: str
    zoneId: str | None = None
    causeNotes: str | None = None
    correctiveAction: str | None = None
    evacuationTriggered: bool = False
    fireServiceCalled: bool = False


@router.get("/false-alarms")
async def list_false_alarms(
    panelAssetId: str | None = Query(None), days: int = Query(365, ge=1, le=1825),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, _READ)
    scope = await build_query_scope(db, user.id, _READ)
    since = _now() - timedelta(days=days)
    stmt = scope.apply(
        select(FireFalseAlarmLog).where(FireFalseAlarmLog.occurredAt >= since), FireFalseAlarmLog
    )
    if panelAssetId:
        stmt = stmt.where(FireFalseAlarmLog.panelAssetId == panelAssetId)
    rows = (await db.execute(stmt.order_by(FireFalseAlarmLog.occurredAt.desc()))).scalars().all()
    by_cause: dict[str, int] = {}
    for r in rows:
        by_cause[r.cause] = by_cause.get(r.cause, 0) + 1
    return {
        "items": [
            {"id": r.id, "panelAssetId": r.panelAssetId, "plantId": r.plantId, "zoneId": r.zoneId,
             "occurredAt": r.occurredAt.isoformat(), "cause": r.cause, "causeNotes": r.causeNotes,
             "correctiveAction": r.correctiveAction, "evacuationTriggered": r.evacuationTriggered,
             "fireServiceCalled": r.fireServiceCalled, "reportedBy": r.reportedBy}
            for r in rows
        ],
        "total": len(rows),
        "byCause": by_cause,
        "windowDays": days,
    }


@router.post("/false-alarms", status_code=201)
async def create_false_alarm(body: FalseAlarmCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    panel = await db.get(FireEquipment, body.panelAssetId)
    if not panel or panel.isDeleted:
        raise HTTPException(404, "Panel asset not found")
    await _require(db, user, _WRITE, plant_id=panel.plantId)
    row = FireFalseAlarmLog(
        panelAssetId=panel.id, plantId=panel.plantId, zoneId=body.zoneId or panel.zoneId,
        occurredAt=body.occurredAt, cause=body.cause, causeNotes=body.causeNotes,
        correctiveAction=body.correctiveAction, evacuationTriggered=body.evacuationTriggered,
        fireServiceCalled=body.fireServiceCalled, reportedBy=user.id, createdBy=user.id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"id": row.id, "panelAssetId": row.panelAssetId, "occurredAt": row.occurredAt.isoformat()}


# ── Integrity check ──────────────────────────────────────────────────────────
@router.get("/integrity/capa-constraint")
async def capa_constraint_integrity(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Verify the §5.4 guarantee still holds.

    Should always report zero. It is checked anyway because the constraint trigger
    lives in hand-applied DDL on a schema Prisma also touches — a future
    `prisma db push` could drop it, and a guarantee nobody verifies is one nobody
    notices losing.
    """
    await _require(db, user, _READ)
    orphans = await defectsvc.unlinked_required_capa_findings(db)
    trigger = (
        await db.execute(
            text(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgname = 'trg_CamsFinding_requires_capa' AND NOT tgisinternal"
            )
        )
    ).scalar() or 0
    return {
        "triggerInstalled": bool(trigger),
        "unlinkedRequiredCapaCount": len(orphans),
        "unlinkedFindingCodes": [f.findingCode for f in orphans][:50],
        "healthy": bool(trigger) and not orphans,
    }
