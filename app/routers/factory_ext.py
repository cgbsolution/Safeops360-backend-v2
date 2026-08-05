"""Facilities extension router — Equipment, Hazardous Materials, Regulatory
Registrations, and the Lifecycle workflow.

Shares the ``/api/factory`` prefix with ``routers/factory.py`` (registered as a
second APIRouter in main.py). Reads require ``FACILITY.READ``; all writes require
``FACILITY.UPDATE`` (the same gate the Buildings tab uses) — finer-grained codes
(EQUIPMENT_MANAGE / HAZMAT_MANAGE …) can be layered later without changing these
handlers. Lifecycle transitions additionally record the actor's role; the stage-
owner role is advisory (recorded, not hard-enforced) so admins are never locked
out. Plant-scoping is enforced on every row via its denormalised ``siteId``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.factory import FactoryProfile
from app.models.factory_ext import (
    FactoryEquipment,
    FactoryEquipmentInspection,
    FactoryLifecycleEvent,
    HazardousMaterial,
    RegulatoryRegistration,
)
from app.models.user import User
from app.schemas import factory_ext as Sx
from app.services import factory_ext as svc
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/factory", tags=["factory"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _require(db: AsyncSession, user: User, code: str, *, plant_id=None) -> None:
    res = await can(db, user.id, code, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


async def _load_profile(db: AsyncSession, user: User, profile_id: str, code: str) -> FactoryProfile:
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, code, plant_id=p.siteId)
    return p


async def _load_child(db: AsyncSession, user: User, model, child_id: str, code: str, label: str):
    row = await db.get(model, child_id)
    if not row or row.isDeleted:
        raise HTTPException(404, f"{label} not found")
    await _require(db, user, code, plant_id=row.siteId)
    return row


def _apply(row, data: dict, user: User) -> None:
    for k, v in data.items():
        setattr(row, k, v)
    row.updatedBy = user.id


# ════════════════════════════════════════════════════════════════════════════
# Equipment  (F-02 Equipment tab)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/profiles/{profile_id}/equipment", response_model=list[Sx.EquipmentOut])
async def list_equipment(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _load_profile(db, user, profile_id, "FACILITY.READ")
    rows = (
        await db.execute(
            select(FactoryEquipment)
            .where(FactoryEquipment.factoryProfileId == profile_id)
            .where(FactoryEquipment.isDeleted.is_(False))
            .order_by(FactoryEquipment.equipmentName.asc())
        )
    ).scalars().all()
    return [svc.equipment_out(e) for e in rows]


@router.post("/profiles/{profile_id}/equipment", response_model=Sx.EquipmentOut, status_code=201)
async def create_equipment(
    profile_id: str, body: Sx.EquipmentCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    p = await _load_profile(db, user, profile_id, "FACILITY.UPDATE")
    errors = svc.validate_equipment(body.model_dump())
    if errors:
        raise HTTPException(400, "; ".join(errors))
    e = FactoryEquipment(factoryProfileId=p.id, siteId=p.siteId, createdBy=user.id, **body.model_dump())
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return svc.equipment_out(e)


@router.patch("/equipment/{equipment_id}", response_model=Sx.EquipmentOut)
async def update_equipment(
    equipment_id: str, body: Sx.EquipmentUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    e = await _load_child(db, user, FactoryEquipment, equipment_id, "FACILITY.UPDATE", "Equipment")
    data = body.model_dump(exclude_unset=True)
    merged = {**Sx.EquipmentOut.model_validate(e).model_dump(), **data}
    errors = svc.validate_equipment(merged)
    if errors:
        raise HTTPException(400, "; ".join(errors))
    _apply(e, data, user)
    await db.commit()
    await db.refresh(e)
    return svc.equipment_out(e)


@router.delete("/equipment/{equipment_id}", status_code=204)
async def delete_equipment(equipment_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    e = await _load_child(db, user, FactoryEquipment, equipment_id, "FACILITY.UPDATE", "Equipment")
    e.isDeleted = True
    e.updatedBy = user.id
    await db.commit()


@router.post("/equipment/{equipment_id}/maintenance", response_model=Sx.EquipmentOut)
async def record_maintenance(
    equipment_id: str, body: Sx.MaintenanceRecord, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    e = await _load_child(db, user, FactoryEquipment, equipment_id, "FACILITY.UPDATE", "Equipment")
    e.lastMaintenanceDate = body.date or _now()
    e.lastMaintenanceType = body.maintenanceType
    e.downtimeHoursYtd = (e.downtimeHoursYtd or 0) + body.downtimeHours
    if body.nextScheduledDate is not None:
        e.nextScheduledDate = body.nextScheduledDate
    if body.notes:
        e.notes = (f"{e.notes}\n" if e.notes else "") + f"[{e.lastMaintenanceDate:%Y-%m-%d}] {body.notes}"
    e.updatedBy = user.id
    await db.commit()
    await db.refresh(e)
    return svc.equipment_out(e)


@router.post("/equipment/{equipment_id}/inspections", response_model=Sx.InspectionResponse, status_code=201)
async def record_inspection(
    equipment_id: str, body: Sx.InspectionCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Record one statutory inspection (Change 3). Creates the immutable log row,
    rolls the equipment's cached inspection state + regime next-dues forward, and
    returns { inspection, updatedEquipment } so the row refreshes in place."""
    e = await _load_child(db, user, FactoryEquipment, equipment_id, "FACILITY.UPDATE", "Equipment")
    when = body.inspectionDate or _now()
    insp = FactoryEquipmentInspection(
        factoryProfileId=e.factoryProfileId, equipmentId=e.id, siteId=e.siteId,
        inspectionDate=when, inspectorName=body.inspectorName.strip(), result=body.result,
        findings=body.findings, createdBy=user.id,
    )
    db.add(insp)
    svc.apply_inspection_to_equipment(e, when, body.result)
    e.updatedBy = user.id
    await db.commit()
    await db.refresh(e)
    await db.refresh(insp)
    return Sx.InspectionResponse(inspection=svc.inspection_out(insp), updatedEquipment=svc.equipment_out(e))


# ════════════════════════════════════════════════════════════════════════════
# Hazardous Materials  (F-02 Hazmat tab)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/profiles/{profile_id}/hazmat", response_model=list[Sx.HazmatOut])
async def list_hazmat(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _load_profile(db, user, profile_id, "FACILITY.READ")
    rows = (
        await db.execute(
            select(HazardousMaterial)
            .where(HazardousMaterial.factoryProfileId == profile_id)
            .where(HazardousMaterial.isDeleted.is_(False))
            .order_by(HazardousMaterial.chemicalName.asc())
        )
    ).scalars().all()
    return [svc.hazmat_out(h) for h in rows]


@router.post("/profiles/{profile_id}/hazmat", response_model=Sx.HazmatOut, status_code=201)
async def create_hazmat(
    profile_id: str, body: Sx.HazmatCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    p = await _load_profile(db, user, profile_id, "FACILITY.UPDATE")
    errors = svc.validate_hazmat(body.model_dump())
    if errors:
        raise HTTPException(400, "; ".join(errors))
    h = HazardousMaterial(factoryProfileId=p.id, siteId=p.siteId, createdBy=user.id, **body.model_dump())
    db.add(h)
    await db.commit()
    await db.refresh(h)
    return svc.hazmat_out(h)


@router.patch("/hazmat/{hazmat_id}", response_model=Sx.HazmatOut)
async def update_hazmat(
    hazmat_id: str, body: Sx.HazmatUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    h = await _load_child(db, user, HazardousMaterial, hazmat_id, "FACILITY.UPDATE", "Hazardous material")
    data = body.model_dump(exclude_unset=True)
    merged = {**Sx.HazmatOut.model_validate(h).model_dump(), **data}
    errors = svc.validate_hazmat(merged)
    if errors:
        raise HTTPException(400, "; ".join(errors))
    _apply(h, data, user)
    await db.commit()
    await db.refresh(h)
    return svc.hazmat_out(h)


@router.delete("/hazmat/{hazmat_id}", status_code=204)
async def delete_hazmat(hazmat_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    h = await _load_child(db, user, HazardousMaterial, hazmat_id, "FACILITY.UPDATE", "Hazardous material")
    h.isDeleted = True
    h.updatedBy = user.id
    await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# Regulatory Registrations  (F-02 Regulatory tab)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/profiles/{profile_id}/regulatory", response_model=list[Sx.RegulatoryOut])
async def list_regulatory(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _load_profile(db, user, profile_id, "FACILITY.READ")
    rows = (
        await db.execute(
            select(RegulatoryRegistration)
            .where(RegulatoryRegistration.factoryProfileId == profile_id)
            .where(RegulatoryRegistration.isDeleted.is_(False))
            .order_by(RegulatoryRegistration.expiryDate.asc().nulls_last())
        )
    ).scalars().all()
    return [svc.regulatory_out(r) for r in rows]


@router.get("/profiles/{profile_id}/regulatory/summary")
async def regulatory_summary(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _load_profile(db, user, profile_id, "FACILITY.READ")
    rows = (
        await db.execute(
            select(RegulatoryRegistration)
            .where(RegulatoryRegistration.factoryProfileId == profile_id)
            .where(RegulatoryRegistration.isDeleted.is_(False))
        )
    ).scalars().all()
    outs = [svc.regulatory_out(r) for r in rows]
    by_status: dict[str, int] = {}
    for o in outs:
        by_status[o.status] = by_status.get(o.status, 0) + 1
    upcoming = sorted(
        [o for o in outs if o.daysToExpiry is not None and o.daysToExpiry >= 0],
        key=lambda o: o.daysToExpiry,  # type: ignore[arg-type]
    )[:5]
    return {
        "total": len(outs),
        "compliantCount": by_status.get("VALID", 0),
        "expiringSoonCount": by_status.get("EXPIRING_SOON", 0),
        "pendingRenewalCount": by_status.get("PENDING_RENEWAL", 0),
        "overdueCount": by_status.get("EXPIRED", 0),
        "suspendedCount": by_status.get("SUSPENDED", 0),
        "statusCounts": by_status,
        "nextRenewals": [
            {
                "id": o.id,
                "registrationType": o.registrationType,
                "registrationName": o.registrationName,
                "expiryDate": o.expiryDate,
                "daysToExpiry": o.daysToExpiry,
                "status": o.status,
            }
            for o in upcoming
        ],
    }


@router.post("/profiles/{profile_id}/regulatory", response_model=Sx.RegulatoryOut, status_code=201)
async def create_regulatory(
    profile_id: str, body: Sx.RegulatoryCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    p = await _load_profile(db, user, profile_id, "FACILITY.UPDATE")
    errors = svc.validate_regulatory(body.model_dump())
    if errors:
        raise HTTPException(400, "; ".join(errors))
    r = RegulatoryRegistration(factoryProfileId=p.id, siteId=p.siteId, createdBy=user.id, **body.model_dump())
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return svc.regulatory_out(r)


@router.patch("/regulatory/{registration_id}", response_model=Sx.RegulatoryOut)
async def update_regulatory(
    registration_id: str, body: Sx.RegulatoryUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    r = await _load_child(db, user, RegulatoryRegistration, registration_id, "FACILITY.UPDATE", "Registration")
    data = body.model_dump(exclude_unset=True)
    merged = {**Sx.RegulatoryOut.model_validate(r).model_dump(), **data}
    errors = svc.validate_regulatory(merged)
    if errors:
        raise HTTPException(400, "; ".join(errors))
    _apply(r, data, user)
    await db.commit()
    await db.refresh(r)
    return svc.regulatory_out(r)


@router.delete("/regulatory/{registration_id}", status_code=204)
async def delete_regulatory(registration_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    r = await _load_child(db, user, RegulatoryRegistration, registration_id, "FACILITY.UPDATE", "Registration")
    r.isDeleted = True
    r.updatedBy = user.id
    await db.commit()


@router.post("/regulatory/{registration_id}/mark-renewed", response_model=Sx.RegulatoryOut)
async def mark_renewed(
    registration_id: str, body: Sx.MarkRenewedRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    r = await _load_child(db, user, RegulatoryRegistration, registration_id, "FACILITY.UPDATE", "Registration")
    r.lastRenewedDate = _now()
    r.expiryDate = body.newExpiryDate
    r.nextRenewalDue = body.newExpiryDate
    r.renewalInProgress = False
    r.status = "VALID"  # cleared override; the engine recomputes from the new expiry
    if body.renewalCost is not None:
        r.renewalEstimatedCost = body.renewalCost
    if body.documentId:
        r.documentationIds = [*(r.documentationIds or []), body.documentId]
    if body.notes:
        r.renewalNotes = body.notes
    r.updatedBy = user.id
    await db.commit()
    await db.refresh(r)
    return svc.regulatory_out(r)


# ════════════════════════════════════════════════════════════════════════════
# Lifecycle workflow  (INITIATED → EXECUTION → VALIDATION → ACTIVE)
# ════════════════════════════════════════════════════════════════════════════
async def _lifecycle_status(db: AsyncSession, p: FactoryProfile) -> Sx.LifecycleStatusOut:
    events = (
        await db.execute(
            select(FactoryLifecycleEvent)
            .where(FactoryLifecycleEvent.factoryProfileId == p.id)
            .where(FactoryLifecycleEvent.isDeleted.is_(False))
            .order_by(FactoryLifecycleEvent.createdAt.desc())
        )
    ).scalars().all()
    return Sx.LifecycleStatusOut(
        factoryProfileId=p.id,
        lifecycleStage=p.lifecycleStage,
        lifecycleStageOwnerRole=p.lifecycleStageOwnerRole,
        lifecycleUpdatedAt=p.lifecycleUpdatedAt,
        allowedNextStages=svc.allowed_next_stages(p.lifecycleStage),
        canRequestRevisions=svc.can_request_revisions(p.lifecycleStage),
        events=[svc.lifecycle_event_out(ev) for ev in events],
    )


@router.get("/profiles/{profile_id}/lifecycle", response_model=Sx.LifecycleStatusOut)
async def get_lifecycle(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await _load_profile(db, user, profile_id, "FACILITY.READ")
    return await _lifecycle_status(db, p)


@router.post("/profiles/{profile_id}/lifecycle/advance", response_model=Sx.LifecycleStatusOut)
async def advance_stage(
    profile_id: str, body: Sx.AdvanceStageRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    p = await _load_profile(db, user, profile_id, "FACILITY.UPDATE")
    err = svc.validate_advance(p.lifecycleStage, body.toStage)
    if err:
        raise HTTPException(400, err)
    from_stage = p.lifecycleStage
    p.lifecycleStage = body.toStage
    p.lifecycleStageOwnerRole = svc.stage_owner_role(body.toStage)
    p.lifecycleUpdatedAt = _now()
    p.updatedBy = user.id
    db.add(
        FactoryLifecycleEvent(
            factoryProfileId=p.id, siteId=p.siteId, fromStage=from_stage, toStage=body.toStage,
            action="ADVANCE", performedBy=user.id, performedByRole=getattr(user, "role", None),
            comment=body.comment, validations=body.validations, createdBy=user.id,
        )
    )
    await db.commit()
    await db.refresh(p)
    return await _lifecycle_status(db, p)


@router.post("/profiles/{profile_id}/lifecycle/request-revisions", response_model=Sx.LifecycleStatusOut)
async def request_revisions(
    profile_id: str, body: Sx.RequestRevisionsRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    p = await _load_profile(db, user, profile_id, "FACILITY.UPDATE")
    if not svc.can_request_revisions(p.lifecycleStage):
        raise HTTPException(400, f"Revisions can only be requested from VALIDATION (current: {p.lifecycleStage}).")
    from_stage = p.lifecycleStage
    p.lifecycleStage = "EXECUTION"
    p.lifecycleStageOwnerRole = svc.stage_owner_role("EXECUTION")
    p.lifecycleUpdatedAt = _now()
    p.updatedBy = user.id
    db.add(
        FactoryLifecycleEvent(
            factoryProfileId=p.id, siteId=p.siteId, fromStage=from_stage, toStage="EXECUTION",
            action="REQUEST_REVISIONS", performedBy=user.id, performedByRole=getattr(user, "role", None),
            comment=f"[{body.priority}] {body.comment}", issues=body.issues, createdBy=user.id,
        )
    )
    await db.commit()
    await db.refresh(p)
    return await _lifecycle_status(db, p)
