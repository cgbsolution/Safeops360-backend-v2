"""PPE Management router (PPE-01). Mounts at /api/ppe.

Core vertical slice: catalog + the full item lifecycle (commission → issue →
return → inspect → retire) + the People Compliance view + dashboards + a
statutory compliance CSV + the PTW gate-check contract (Pass 2 —
services/ppe_gate.py). Skill Matrix competency-on-issue lands in a later
pass. RBAC: reads need PPE.READ; each write needs its specific action
permission.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.ppe import PpeInspection, PpeIssuance, PpeItem, PpeType
from app.models.user import User
from app.services import ppe_inventory as svc
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/ppe", tags=["ppe"])


# ─── RBAC helper ─────────────────────────────────────────────────────────


async def _require(db: AsyncSession, user: User, permission_code: str) -> None:
    result = await can(db, user.id, permission_code, PermissionContext())
    if not result.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            result.reason or f"Missing permission {permission_code}",
        )


# ─── Request bodies ──────────────────────────────────────────────────────


class CommissionBody(BaseModel):
    plantId: str
    ppeTypeId: str
    quantity: int = 1
    manufacturer: str = ""
    model: str = ""
    batchLotNumber: str = ""
    manufactureDate: datetime
    purchaseDate: datetime | None = None
    storageLocation: str | None = None
    departmentId: str | None = None
    cost: float | None = None


# Closed vocabularies (mirror the model docstrings) — enforced at the API
# boundary so reports/filters never see free-text variants like "manual".
IssuancePurpose = Literal["personal_assignment", "permit_task", "training", "temporary_loan"]
ReturnCondition = Literal["good", "fair", "damaged", "destroyed"]
InspectionType = Literal["pre_use", "periodic", "annual", "post_incident", "fitness_reassessment"]
InspectionTrigger = Literal["scheduled", "recalled", "damage_assessment", "incident", "fitness_reassessment"]
InspectionResult = Literal["pass", "fail", "conditional_pass"]
ItemStatusAfter = Literal["returned_to_service", "recalled_to_store", "quarantined_pending_repair", "retired", "condemned"]


class IssueBody(BaseModel):
    toUserId: str
    purpose: IssuancePurpose = "personal_assignment"
    linkedPermitId: str | None = None
    conditionAtIssuance: str = "good"
    briefingProvided: bool = True
    recipientAcknowledged: bool = True
    notes: str = ""


class ReturnBody(BaseModel):
    conditionAtReturn: ReturnCondition = "good"
    notes: str = ""


class InspectBody(BaseModel):
    inspectionType: InspectionType = "periodic"
    trigger: InspectionTrigger = "scheduled"
    overallResult: InspectionResult = "pass"
    itemStatusAfter: ItemStatusAfter | None = None
    checklistItems: list[dict] | None = None
    defectsFound: list[dict] | None = None
    conditions: str = ""
    isThirdParty: bool = False
    thirdPartyCompany: str = ""


class RetireBody(BaseModel):
    reason: str


# ─── Dashboard ───────────────────────────────────────────────────────────


@router.get("/dashboard")
async def dashboard(
    plantId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "PPE.READ")
    now = datetime.now(timezone.utc)
    items = (await db.execute(select(PpeItem).where(PpeItem.plantId == plantId))).scalars().all()

    in_service = overdue = approaching_eol = recalls = repair_quarantine = 0
    for it in items:
        v = svc.item_validity(it, now)
        if it.status in ("in_stock", "issued", "under_inspection"):
            in_service += 1
        if v["inspectionStatus"] == "overdue":
            overdue += 1
        if not v["serviceLifeExceeded"] and 0 <= v["serviceLifeRemainingDays"] <= svc.SERVICE_LIFE_WARN_DAYS:
            approaching_eol += 1
        if it.batchUnderRecall:
            recalls += 1
        if it.status in ("under_repair", "quarantined"):
            repair_quarantine += 1

    active_iss = (
        await db.execute(
            select(PpeIssuance).where(PpeIssuance.plantId == plantId).where(PpeIssuance.status == "active")
        )
    ).scalars().all()
    overdue_returns = sum(
        1 for i in active_iss if i.expectedReturnDate and svc._aware(i.expectedReturnDate) < now
    )

    comp = await svc.people_compliance(db, plant_id=plantId)

    return {
        "plantId": plantId,
        "cards": {
            "itemsInService": in_service,
            "inspectionOverdue": overdue,
            "approachingServiceLife": approaching_eol,
            "complianceGaps": comp["summary"]["gaps"] + comp["summary"]["criticalGaps"],
            "activeRecalls": recalls,
            "underRepairQuarantine": repair_quarantine,
            "overdueReturns": overdue_returns,
        },
        "compliance": comp["summary"],
        "totalItems": len(items),
    }


# ─── Items ───────────────────────────────────────────────────────────────


@router.get("/items")
async def list_items(
    plantId: str = Query(...),
    status_filter: str | None = Query(None, alias="status"),
    category: str | None = Query(None),
    typeCode: str | None = Query(None),
    holderId: str | None = Query(None),
    overdueOnly: bool = Query(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "PPE.READ")
    stmt = select(PpeItem).where(PpeItem.plantId == plantId)
    if status_filter:
        stmt = stmt.where(PpeItem.status == status_filter)
    if typeCode:
        stmt = stmt.where(PpeItem.ppeTypeCode == typeCode)
    if holderId:
        stmt = stmt.where(PpeItem.currentHolderUserId == holderId)
    items = (await db.execute(stmt.order_by(PpeItem.itemNumber.asc()))).scalars().all()

    now = datetime.now(timezone.utc)
    # category filter needs the type → resolve codes once
    cat_codes: set[str] | None = None
    if category:
        rows = (
            await db.execute(select(PpeType.code).where(PpeType.category == category))
        ).scalars().all()
        cat_codes = set(rows)

    out: list[dict] = []
    for it in items:
        if cat_codes is not None and it.ppeTypeCode not in cat_codes:
            continue
        d = svc.item_dict(it, now)
        if overdueOnly and d["inspectionStatus"] != "overdue":
            continue
        out.append(d)
    return {"plantId": plantId, "count": len(out), "items": out}


@router.get("/items/{item_id}")
async def get_item(
    item_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "PPE.READ")
    item = await db.get(PpeItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PPE item not found")
    now = datetime.now(timezone.utc)

    issuances = (
        await db.execute(
            select(PpeIssuance).where(PpeIssuance.ppeItemId == item_id).order_by(PpeIssuance.issuedAt.desc())
        )
    ).scalars().all()
    inspections = (
        await db.execute(
            select(PpeInspection).where(PpeInspection.ppeItemId == item_id).order_by(PpeInspection.conductedAt.desc())
        )
    ).scalars().all()

    holder_name = None
    if item.currentHolderUserId:
        holder = await db.get(User, item.currentHolderUserId)
        holder_name = holder.name if holder else None

    ppe_type = await db.get(PpeType, item.ppeTypeId)

    return {
        "item": svc.item_dict(item, now),
        "holderName": holder_name,
        "stateHistory": item.stateHistory or [],
        "type": {
            "code": ppe_type.code if ppe_type else item.ppeTypeCode,
            "name": ppe_type.name if ppe_type else item.ppeTypeName,
            "category": ppe_type.category if ppe_type else None,
            "serviceLifeYears": ppe_type.serviceLifeYears if ppe_type else None,
            "applicableStandards": ppe_type.applicableStandards if ppe_type else [],
            "requiresCompetencyToUse": ppe_type.requiresCompetencyToUse if ppe_type else None,
            "inspectionSchedule": ppe_type.inspectionSchedule if ppe_type else [],
        } if ppe_type else None,
        "issuances": [
            {
                "id": i.id,
                "issuanceNumber": i.issuanceNumber,
                "issuedToName": i.issuedToName,
                "issuedByName": i.issuedByName,
                "issuedAt": svc._iso(i.issuedAt),
                "purpose": i.issuancePurpose,
                "status": i.status,
                "returnedAt": svc._iso(i.returnedAt),
                "conditionAtIssuance": i.conditionAtIssuance,
                "conditionAtReturn": i.conditionAtReturn,
            }
            for i in issuances
        ],
        "inspections": [
            {
                "id": ins.id,
                "inspectionType": ins.inspectionType,
                "trigger": ins.trigger,
                "conductedAt": svc._iso(ins.conductedAt),
                "inspectorName": ins.inspectorName,
                "overallResult": ins.overallResult,
                "defectsFound": ins.defectsFound,
                "itemStatusAfterInspection": ins.itemStatusAfterInspection,
            }
            for ins in inspections
        ],
    }


@router.post("/items/commission", status_code=status.HTTP_201_CREATED)
async def commission(
    body: CommissionBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "PPE.CREATE")
    try:
        items = await svc.commission_items(
            db,
            plant_id=body.plantId,
            ppe_type_id=body.ppeTypeId,
            quantity=max(1, min(body.quantity, 200)),
            manufacturer=body.manufacturer,
            model=body.model,
            batch_lot_number=body.batchLotNumber,
            manufacture_date=body.manufactureDate,
            purchase_date=body.purchaseDate,
            storage_location=body.storageLocation,
            department_id=body.departmentId,
            cost=body.cost,
            actor_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    dicts = [svc.item_dict(i) for i in items]
    # Goods received with an old manufacture date can be expired on arrival
    # (shelf life from manufacture). Surface it instead of creating silently
    # unissuable stock.
    born_expired = sum(1 for d in dicts if d["serviceLifeExceeded"])
    warning = (
        f"{born_expired} of {len(dicts)} item(s) are already past their service "
        "life based on the manufacture date — they cannot be issued. Check the "
        "manufacture date or retire them."
        if born_expired
        else None
    )
    return {"created": len(dicts), "items": dicts, "warning": warning}


@router.post("/items/{item_id}/issue", status_code=status.HTTP_201_CREATED)
async def issue(
    item_id: str,
    body: IssueBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "PPE.ISSUE")
    try:
        issuance = await svc.issue_item(
            db,
            item_id=item_id,
            to_user_id=body.toUserId,
            by_user_id=user.id,
            purpose=body.purpose,
            linked_permit_id=body.linkedPermitId,
            condition_at_issuance=body.conditionAtIssuance,
            briefing_provided=body.briefingProvided,
            recipient_acknowledged=body.recipientAcknowledged,
            notes=body.notes,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return {"issuanceId": issuance.id, "issuanceNumber": issuance.issuanceNumber, "status": issuance.status}


@router.post("/issuances/{issuance_id}/return")
async def return_(
    issuance_id: str,
    body: ReturnBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "PPE.ISSUE")
    try:
        issuance = await svc.return_item(
            db,
            issuance_id=issuance_id,
            by_user_id=user.id,
            condition_at_return=body.conditionAtReturn,
            notes=body.notes,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return {
        "issuanceId": issuance.id,
        "status": issuance.status,
        "postReturnInspectionRequired": issuance.postReturnInspectionRequired,
    }


@router.post("/items/{item_id}/inspect", status_code=status.HTTP_201_CREATED)
async def inspect(
    item_id: str,
    body: InspectBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "PPE.INSPECT")
    try:
        inspection = await svc.record_inspection(
            db,
            item_id=item_id,
            inspector_user_id=user.id,
            inspection_type=body.inspectionType,
            trigger=body.trigger,
            overall_result=body.overallResult,
            item_status_after=body.itemStatusAfter,
            checklist_items=body.checklistItems,
            defects_found=body.defectsFound,
            conditions=body.conditions,
            is_third_party=body.isThirdParty,
            third_party_company=body.thirdPartyCompany,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return {
        "inspectionId": inspection.id,
        "overallResult": inspection.overallResult,
        "itemStatusAfterInspection": inspection.itemStatusAfterInspection,
        "capaSpawned": inspection.capaSpawned,
    }


@router.post("/items/{item_id}/retire")
async def retire(
    item_id: str,
    body: RetireBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "PPE.RETIRE_APPROVE")
    try:
        item = await svc.retire_item(db, item_id=item_id, actor_id=user.id, reason=body.reason)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return {"itemId": item.id, "status": item.status}


# ─── Issuances ───────────────────────────────────────────────────────────


@router.get("/issuances")
async def list_issuances(
    plantId: str = Query(...),
    status_filter: str | None = Query(None, alias="status"),
    overdueOnly: bool = Query(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "PPE.READ")
    stmt = select(PpeIssuance).where(PpeIssuance.plantId == plantId)
    if status_filter:
        stmt = stmt.where(PpeIssuance.status == status_filter)
    rows = (await db.execute(stmt.order_by(PpeIssuance.issuedAt.desc()))).scalars().all()
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for i in rows:
        is_overdue = bool(i.status == "active" and i.expectedReturnDate and svc._aware(i.expectedReturnDate) < now)
        if overdueOnly and not is_overdue:
            continue
        out.append({
            "id": i.id,
            "issuanceNumber": i.issuanceNumber,
            "ppeTypeName": i.ppeTypeName,
            "serialNumber": i.serialNumber,
            "issuedToName": i.issuedToName,
            "issuedToDepartment": i.issuedToDepartment,
            "issuedByName": i.issuedByName,
            "issuedAt": svc._iso(i.issuedAt),
            "expectedReturnDate": svc._iso(i.expectedReturnDate),
            "purpose": i.issuancePurpose,
            "status": i.status,
            "overdueReturn": is_overdue,
            "returnedAt": svc._iso(i.returnedAt),
        })
    return {"plantId": plantId, "count": len(out), "issuances": out}


# ─── Inspections Due ─────────────────────────────────────────────────────


@router.get("/inspections/due")
async def inspections_due(
    plantId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "PPE.READ")
    items = (
        await db.execute(
            select(PpeItem)
            .where(PpeItem.plantId == plantId)
            .where(PpeItem.status.notin_(["retired", "lost", "stolen"]))
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    buckets = {"overdue": [], "this_week": [], "this_month": [], "upcoming": []}
    for it in items:
        st, overdue_days = svc.inspection_status(it, now)
        due = svc._aware(it.nextInspectionDueDate)
        if due is None:
            continue
        days = (due - now).days
        row = {
            "id": it.id,
            "itemNumber": it.itemNumber,
            "ppeTypeName": it.ppeTypeName,
            "serialNumber": it.serialNumber,
            "currentHolderUserId": it.currentHolderUserId,
            "nextInspectionDueDate": svc._iso(it.nextInspectionDueDate),
            "daysUntilDue": days,
            "overdueDays": overdue_days,
        }
        if st == "overdue":
            buckets["overdue"].append(row)
        elif days <= 7:
            buckets["this_week"].append(row)
        elif days <= 30:
            buckets["this_month"].append(row)
        elif days <= 90:
            buckets["upcoming"].append(row)
    for k in buckets:
        buckets[k].sort(key=lambda r: r["daysUntilDue"])
    return {
        "plantId": plantId,
        "counts": {k: len(v) for k, v in buckets.items()},
        "buckets": buckets,
    }


# ─── People Compliance ───────────────────────────────────────────────────


@router.get("/people-compliance")
async def people_compliance(
    plantId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "PPE.READ")
    return await svc.people_compliance(db, plant_id=plantId)


# ─── Catalog ─────────────────────────────────────────────────────────────


@router.get("/catalog")
async def catalog(
    plantId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "PPE.READ")
    types = (
        await db.execute(select(PpeType).where(PpeType.isActive.is_(True)).order_by(PpeType.category, PpeType.name))
    ).scalars().all()
    items = (await db.execute(select(PpeItem).where(PpeItem.plantId == plantId))).scalars().all()
    now = datetime.now(timezone.utc)
    counts: dict[str, dict] = {}
    for it in items:
        c = counts.setdefault(it.ppeTypeCode, {"inService": 0, "overdue": 0})
        if it.status in ("in_stock", "issued", "under_inspection"):
            c["inService"] += 1
        if svc.inspection_status(it, now)[0] == "overdue":
            c["overdue"] += 1
    out = []
    for t in types:
        c = counts.get(t.code, {"inService": 0, "overdue": 0})
        out.append({
            "id": t.id,
            "code": t.code,
            "name": t.name,
            "category": t.category,
            "subcategory": t.subcategory,
            "serviceLifeYears": t.serviceLifeYears,
            "tracksIndividualItems": t.tracksIndividualItems,
            "requiresCompetencyToUse": t.requiresCompetencyToUse,
            "enablesPermitTypes": t.enablesPermitTypes,
            "statutoryProvisionRequired": t.statutoryProvisionRequired,
            "inspectionSchedule": t.inspectionSchedule,
            "applicableStandards": t.applicableStandards,
            "itemsInService": c["inService"],
            "itemsOverdue": c["overdue"],
        })
    return {"plantId": plantId, "count": len(out), "types": out}


@router.get("/catalog/{code}")
async def catalog_detail(
    code: str,
    plantId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "PPE.READ")
    t = (await db.execute(select(PpeType).where(PpeType.code == code))).scalar_one_or_none()
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PPE type not found")
    items = (
        await db.execute(
            select(PpeItem).where(PpeItem.plantId == plantId).where(PpeItem.ppeTypeCode == code)
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    return {
        "type": {
            "code": t.code,
            "name": t.name,
            "description": t.description,
            "category": t.category,
            "subcategory": t.subcategory,
            "serviceLifeYears": t.serviceLifeYears,
            "serviceLifeHours": t.serviceLifeHours,
            "applicableStandards": t.applicableStandards,
            "minimumSpecification": t.minimumSpecification,
            "controlsHazards": t.controlsHazards,
            "enablesPermitTypes": t.enablesPermitTypes,
            "inspectionSchedule": t.inspectionSchedule,
            "requiresCompetencyToUse": t.requiresCompetencyToUse,
            "requiredTrainingPrograms": t.requiredTrainingPrograms,
            "requiresFitTest": t.requiresFitTest,
            "isPersonalIssue": t.isPersonalIssue,
            "statutoryProvisionRequired": t.statutoryProvisionRequired,
            "regulatoryReferences": t.regulatoryReferences,
        },
        "items": [svc.item_dict(i, now) for i in items],
    }


# ─── Reports ─────────────────────────────────────────────────────────────


@router.get("/reports/people-compliance.csv")
async def people_compliance_csv(
    plantId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "PPE.READ")
    comp = await svc.people_compliance(db, plant_id=plantId)
    buf = io.StringIO()
    buf.write("﻿")  # BOM for Excel
    w = csv.writer(buf)
    w.writerow(["PPE Compliance Report — ISO 45001 Clause 8.1 evidence"])
    w.writerow([f"Generated: {datetime.now(timezone.utc).isoformat()}"])
    w.writerow([
        f"People: {comp['summary']['totalPeople']}",
        f"Compliant: {comp['summary']['compliant']}",
        f"Gaps: {comp['summary']['gaps']}",
        f"Critical gaps: {comp['summary']['criticalGaps']}",
    ])
    w.writerow([])
    w.writerow(["Name", "Role", "Department", "Overall", "PPE Type", "Required", "Held", "Status", "Reason"])
    for p in comp["people"]:
        for r in p["requirements"]:
            w.writerow([
                p["name"], p["role"], p["department"], p["overall"].upper(),
                r["ppeTypeName"], r["requirementLevel"], "Yes" if r["held"] else "No",
                r["status"].upper(), r["reason"],
            ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="ppe-compliance-{plantId}.csv"'},
    )


# ─── PTW integration: PPE compliance gate check (Build Prompt §9.1) ───────


class PtwGateCheckBody(BaseModel):
    ptwId: str | None = None  # informational — echoed back
    plantId: str
    permitType: str | None = None  # e.g. WORK_AT_HEIGHT; adds enablesPermitTypes PPE
    workers: list[str]  # user ids


@router.post("/compliance/ptw-gate-check")
async def ptw_gate_check(
    body: PtwGateCheckBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Integration contract for the PTW module: are these workers PPE-
    compliant right now? Same engine as the activation gate
    (ppe_gate.check_ppe_for_crew) — exposed so the PTW UI can pre-flight a
    crew before submission. No override path exists yet: the activation
    gate is a hard block."""
    await _require(db, user, "PPE.READ")
    from app.services.ppe_gate import check_ppe_for_crew

    results = await check_ppe_for_crew(
        db,
        plant_id=body.plantId,
        user_ids=body.workers,
        permit_type_code=body.permitType,
    )
    user_rows = (
        await db.execute(select(User).where(User.id.in_(body.workers)))
    ).scalars().all() if body.workers else []
    names_by_id = {u.id: u.name for u in user_rows}

    compliant: list[str] = []
    non_compliant: list[dict] = []
    for worker_id in body.workers:
        res = results[worker_id]
        if res.ok:
            compliant.append(worker_id)
        else:
            non_compliant.append({
                "workerId": worker_id,
                "workerName": names_by_id.get(worker_id, worker_id),
                "gaps": [
                    {
                        "ppeTypeCode": g.ppeTypeCode,
                        "ppeTypeName": g.ppeTypeName,
                        "reason": g.code,
                        "message": g.message,
                    }
                    for g in res.blockers
                ],
            })
    return {
        "ptwId": body.ptwId,
        "gateStatus": "BLOCKED" if non_compliant else "CLEAR",
        "compliantWorkers": compliant,
        "nonCompliantWorkers": non_compliant,
        "overrideAllowed": False,
    }
