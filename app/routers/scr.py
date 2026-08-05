"""SCR router — Statutory Compliance Register (SCR-01). Mounts at /api/scr.

Phase-1 vertical slice: Form 18 (Register of Accidents), auto-populated from
the Incident module. Read endpoints + a backfill sync + statutory CSV export.
Registers are never hand-written here; entries originate from source modules.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.scr import RegisterEntry, RegisterMaster
from app.models.user import User
from app.services.access_scope import build_query_scope
from app.services.scr_register import sync_all

router = APIRouter(prefix="/api/scr", tags=["scr"])


async def _require_scr_plant(db: AsyncSession, user: User, plant_id: str) -> None:
    """Authorise a statutory-register read (Form 18 accident register carries
    injured-person PII). Allowed if the caller can read this plant's compliance
    data OR its incidents (the register is incident-fed). Uses permission-
    specific plant scope so it fails CLOSED for OWN_DEPARTMENT/OWN_RECORDS and
    an unrelated ALL_PLANTS grant on another module can't unlock every plant's
    register."""
    for perm in ("COMPLIANCE.READ", "INCIDENT.READ"):
        scope = await build_query_scope(db, user.id, perm)
        if scope.allows_plant(plant_id):
            return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied for this plant")


def _register_dict(reg: RegisterMaster, entry_count: int, last_entry: datetime | None) -> dict:
    return {
        "id": reg.id,
        "registerCode": reg.registerCode,
        "registerName": reg.registerName,
        "legalAct": reg.legalAct,
        "sectionRule": reg.sectionRule,
        "sourceModule": reg.sourceModule,
        "submissionFrequency": reg.submissionFrequency,
        "submissionAuthority": reg.submissionAuthority,
        "authorisedSignatoryRole": reg.authorisedSignatoryRole,
        "nextSubmissionDue": reg.nextSubmissionDue.isoformat() if reg.nextSubmissionDue else None,
        "complianceStatus": reg.complianceStatus,
        "isActive": reg.isActive,
        "entryCount": entry_count,
        "lastEntryDate": last_entry.isoformat() if last_entry else None,
    }


async def _entry_stats(db: AsyncSession, register_id: str) -> tuple[int, datetime | None]:
    count = (
        await db.execute(
            select(func.count(RegisterEntry.id))
            .where(RegisterEntry.registerId == register_id)
            .where(RegisterEntry.isVoided.is_(False))
        )
    ).scalar_one()
    last = (
        await db.execute(
            select(func.max(RegisterEntry.entryDate)).where(RegisterEntry.registerId == register_id)
        )
    ).scalar_one()
    return count or 0, last


@router.get("/registers")
async def list_registers(
    plantId: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    await _require_scr_plant(db, user, plantId)
    regs = (
        await db.execute(
            select(RegisterMaster)
            .where(RegisterMaster.plantId == plantId)
            .order_by(RegisterMaster.registerName.asc())
        )
    ).scalars().all()
    out: list[dict] = []
    for reg in regs:
        count, last = await _entry_stats(db, reg.id)
        out.append(_register_dict(reg, count, last))
    return out


@router.get("/dashboard")
async def dashboard(
    plantId: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    await _require_scr_plant(db, user, plantId)
    regs = (
        await db.execute(select(RegisterMaster).where(RegisterMaster.plantId == plantId))
    ).scalars().all()
    registers: list[dict] = []
    compliant = 0
    total_entries = 0
    for reg in regs:
        count, last = await _entry_stats(db, reg.id)
        total_entries += count
        if reg.complianceStatus == "COMPLIANT":
            compliant += 1
        registers.append(_register_dict(reg, count, last))

    # Activity feed — most recent auto-created entries across the plant's registers.
    reg_ids = [r.id for r in regs]
    feed: list[dict] = []
    if reg_ids:
        rows = (
            await db.execute(
                select(RegisterEntry, RegisterMaster.registerName)
                .join(RegisterMaster, RegisterMaster.id == RegisterEntry.registerId)
                .where(RegisterEntry.registerId.in_(reg_ids))
                .order_by(RegisterEntry.createdAt.desc())
                .limit(20)
            )
        ).all()
        for e, reg_name in rows:
            feed.append(
                {
                    "registerName": reg_name,
                    "sourceRef": e.sourceRef,
                    "sourceModule": e.sourceModule,
                    "entryDate": e.entryDate.isoformat() if e.entryDate else None,
                    "createdAt": e.createdAt.isoformat() if e.createdAt else None,
                }
            )

    health = round((compliant / len(regs) * 100.0), 0) if regs else 0
    return {
        "plantId": plantId,
        "complianceHealth": health,
        "registerCount": len(regs),
        "totalEntries": total_entries,
        "registers": registers,
        "activityFeed": feed,
    }


@router.get("/registers/{register_code}")
async def get_register(
    register_code: str,
    plantId: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    await _require_scr_plant(db, user, plantId)
    reg = (
        await db.execute(
            select(RegisterMaster)
            .where(RegisterMaster.registerCode == register_code)
            .where(RegisterMaster.plantId == plantId)
        )
    ).scalar_one_or_none()
    if reg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Register not configured for this plant")

    entries = (
        await db.execute(
            select(RegisterEntry)
            .where(RegisterEntry.registerId == reg.id)
            .order_by(RegisterEntry.entryDate.asc())
        )
    ).scalars().all()
    count, last = await _entry_stats(db, reg.id)
    return {
        "register": _register_dict(reg, count, last),
        "entries": [
            {
                "id": e.id,
                "sourceTransactionId": e.sourceTransactionId,
                "sourceModule": e.sourceModule,
                "sourceRef": e.sourceRef,
                "entryDate": e.entryDate.isoformat() if e.entryDate else None,
                "entryCreatedBy": e.entryCreatedBy,
                "fields": e.entryFieldsJson,
                "isManualCorrection": e.isManualCorrection,
                "isVoided": e.isVoided,
                "voidReason": e.voidReason,
                "auditTrail": e.auditTrail,
            }
            for e in entries
        ],
    }


@router.post("/sync")
async def sync(
    plantId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Back-fill every register for the plant from its source modules."""
    await _require_scr_plant(db, user, plantId)
    result = await sync_all(db, plant_id=plantId, actor="SYSTEM")
    # Shape kept backwards-compatible for the existing UI (results[0].created/updated).
    return {"plantId": plantId, "results": [result], **result}


# Prescribed column order per register family (mirrors the frontend view).
_COLS_FORM18 = [
    ("srNo", "Sr. No."), ("injuredPersonName", "Name of Injured Person"), ("department", "Department / Section"),
    ("dateOfAccident", "Date of Accident"), ("timeOfAccident", "Time"), ("natureOfInjury", "Nature of Injury / Occurrence"),
    ("causeOfAccident", "Cause of Accident"), ("location", "Place of Accident"), ("daysLost", "Days Lost"),
    ("investigationReference", "Reference No."),
]
_COLS_PTW = [
    ("srNo", "Sr. No."), ("permitNumber", "Permit No."), ("workLocation", "Work Location"), ("issuedTo", "Issued To"),
    ("issuedBy", "Issued By"), ("validFrom", "Valid From"), ("validTo", "Valid To"), ("status", "Status"), ("closedOn", "Closed On"),
]
_COLS_EQUIP = [
    ("srNo", "Sr. No."), ("equipmentCode", "Equipment Code"), ("equipmentName", "Equipment / Machinery"),
    ("statutoryRegNo", "Statutory Reg. No."), ("examinationDate", "Date of Examination"), ("inspector", "Competent Person"),
    ("result", "Result"), ("nextDue", "Next Due"), ("reference", "Reference No."),
]
_COLS_TRAIN = [
    ("srNo", "Sr. No."), ("employeeName", "Name of Employee"), ("programCode", "Programme"), ("programName", "Training Subject"),
    ("trainingDate", "Date of Training"), ("validUntil", "Valid Until"), ("status", "Status"), ("certificateNumber", "Certificate No."),
]
_COLS_CAPA = [
    ("srNo", "Sr. No."), ("capaNumber", "CAPA No."), ("title", "Title"), ("source", "Source"), ("severity", "Severity"),
    ("status", "Status"), ("raisedOn", "Raised On"), ("dueOn", "Closure Target"), ("owner", "Owner"),
]


def _columns_for(register_code: str) -> list[tuple[str, str]]:
    if register_code == "FORM18":
        return _COLS_FORM18
    if register_code.startswith("PTW-"):
        return _COLS_PTW
    if register_code in ("FORM10", "FORM11", "FORM13", "EQUIP-EXAM", "FIRE-EXT"):
        return _COLS_EQUIP
    if register_code == "TRAIN-REGISTER":
        return _COLS_TRAIN
    if register_code == "CAPA-REGISTER":
        return _COLS_CAPA
    return _COLS_FORM18


@router.get("/registers/{register_code}/export.csv")
async def export_register_csv(
    register_code: str,
    plantId: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _require_scr_plant(db, user, plantId)
    reg = (
        await db.execute(
            select(RegisterMaster)
            .where(RegisterMaster.registerCode == register_code)
            .where(RegisterMaster.plantId == plantId)
        )
    ).scalar_one_or_none()
    if reg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Register not configured for this plant")
    entries = (
        await db.execute(
            select(RegisterEntry)
            .where(RegisterEntry.registerId == reg.id)
            .where(RegisterEntry.isVoided.is_(False))
            .order_by(RegisterEntry.entryDate.asc())
        )
    ).scalars().all()

    cols = _columns_for(reg.registerCode)
    buf = io.StringIO()
    buf.write("﻿")  # BOM for Excel
    w = csv.writer(buf)
    w.writerow([reg.registerName])
    w.writerow([f"{reg.legalAct} · {reg.sectionRule or ''}"])
    w.writerow([f"Generated: {datetime.now(timezone.utc).isoformat()}"])
    w.writerow([])
    w.writerow([label for _, label in cols])
    for e in entries:
        f = e.entryFieldsJson or {}
        w.writerow([f.get(key, "") for key, _ in cols])

    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{reg.registerCode}-{plantId}.csv"'},
    )
