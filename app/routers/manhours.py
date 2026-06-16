from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.manhours import Manhours
from app.models.plant import Plant
from app.models.user import User
from app.schemas.manhours import ManhoursCreate, ManhoursOut
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
)

router = APIRouter(prefix="/api/manhours", tags=["manhours"])


def _compute_kpis(*, manhours_worked: int, contractor: int, lti: int, mtc: int, fatal: int, lost_days: int) -> dict[str, float | None]:
    """LTIFR, TRIR, Severity rate per OSHA-style 1,000,000-hour normalisation."""
    total = (manhours_worked or 0) + (contractor or 0)
    if total <= 0:
        return {"ltifr": None, "trir": None, "severityRate": None}
    factor = 1_000_000 / total
    return {
        "ltifr": round((lti + fatal) * factor, 4),
        "trir": round((lti + mtc + fatal) * factor, 4),
        "severityRate": round(lost_days * factor, 4),
    }


@router.get("")
async def list_manhours(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    read_check = await can(db, user.id, "MANHOURS.READ", PermissionContext())
    if not read_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, read_check.reason or "Access denied")
    plants = await get_accessible_plants(db, user.id)
    stmt = select(Manhours)
    if plants is None:
        pass
    elif not plants:
        return {"items": [], "total": 0}
    else:
        stmt = stmt.where(Manhours.plantId.in_(plants))
    rows = (await db.execute(stmt.order_by(Manhours.year.desc(), Manhours.month.desc()).limit(60))).scalars().all()
    return {"items": [ManhoursOut.model_validate(r) for r in rows], "total": len(rows)}


@router.post("", response_model=ManhoursOut, status_code=status.HTTP_201_CREATED)
async def create_manhours(
    payload: ManhoursCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ManhoursOut:
    await require_permission_with_context("MANHOURS.CREATE", user, db, plant_id=payload.plantId)
    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")

    now = datetime.now(timezone.utc)
    if payload.year > now.year or (payload.year == now.year and payload.month > now.month):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot enter manhours for a future month.")

    # Idempotent upsert via unique key (plantId, year, month)
    existing = (
        await db.execute(
            select(Manhours).where(
                Manhours.plantId == payload.plantId,
                Manhours.year == payload.year,
                Manhours.month == payload.month,
            )
        )
    ).scalar_one_or_none()

    kpis = _compute_kpis(
        manhours_worked=payload.manhoursWorked,
        contractor=payload.contractorManhours,
        lti=payload.ltiCount,
        mtc=payload.mtcCount,
        fatal=payload.fatalCount,
        lost_days=payload.lostDays,
    )

    target = existing or Manhours(plantId=payload.plantId, year=payload.year, month=payload.month)
    target.headcount = payload.headcount
    target.manhoursWorked = payload.manhoursWorked
    target.contractorManhours = payload.contractorManhours
    target.ltiCount = payload.ltiCount
    target.mtcCount = payload.mtcCount
    target.fatalCount = payload.fatalCount
    target.lostDays = payload.lostDays
    target.notes = payload.notes
    target.ltifr = kpis["ltifr"]
    target.trir = kpis["trir"]
    target.severityRate = kpis["severityRate"]
    target.submittedById = user.id
    target.submittedAt = now
    if existing is None:
        db.add(target)
    await db.flush()
    await db.refresh(target)
    return ManhoursOut.model_validate(target)
