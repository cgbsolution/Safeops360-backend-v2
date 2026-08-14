from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
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
    # Newest-created first — platform-wide register convention. A submission
    # entered now for an older period must still lead the list.
    rows = (
        await db.execute(
            stmt.order_by(Manhours.createdAt.desc(), Manhours.id.desc()).limit(60)
        )
    ).scalars().all()
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


# -- KPI computation for arbitrary plant + period ----------------------
#
# Distinct from /api/manhours-submissions/{id}/kpi, which is tied to one
# return. The comparison, trend and MIS screens ask for periods that may have
# no submission at all, so they address plant+year+month directly.


@router.get("/kpi")
async def compute_kpis(
    plantId: str = Query(...),
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    codes: str | None = Query(None, description="Comma-separated KPI codes; default all."),
    preferSnapshot: bool = Query(
        True,
        description="Use the frozen snapshot when the period is LOCKED. Turn off to force a recompute.",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """KPIs for one plant-month.

    Prefers the LOCKED snapshot by default: once a period is locked its numbers
    are a reported figure, and recomputing could quietly disagree with what was
    filed if a source incident has since been reclassified.
    """
    check = await can(db, user.id, "MANHOURS.READ", PermissionContext(plant_id=plantId))
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    plants = await get_accessible_plants(db, user.id)
    if plants is not None and plantId not in plants:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Plant not accessible")

    from app.models.manhours_submission import ManhoursSubmission
    from app.services.manhours_kpi_engine import KpiEngine
    from app.services.manhours_kpi_registry import KPI_CODES, REGISTRY_VERSION

    wanted = tuple(c.strip() for c in codes.split(",") if c.strip()) if codes else KPI_CODES
    unknown = [c for c in wanted if c not in KPI_CODES]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown KPI code(s): {', '.join(unknown)}"
        )

    if preferSnapshot:
        submission = (
            await db.execute(
                select(ManhoursSubmission)
                .where(ManhoursSubmission.plantId == plantId)
                .where(ManhoursSubmission.reportingYear == year)
                .where(ManhoursSubmission.reportingMonth == month)
            )
        ).scalar_one_or_none()
        if submission is not None and submission.status == "LOCKED" and submission.kpiSnapshot:
            snap = submission.kpiSnapshot
            stored = snap.get("kpis", {}) if isinstance(snap, dict) else {}
            return {
                "source": "snapshot",
                "registryVersion": snap.get("registryVersion"),
                "capturedAt": snap.get("capturedAt"),
                "scope": {"plantId": plantId},
                "period": {"year": year, "month": month},
                "kpis": {c: stored[c] for c in wanted if c in stored},
            }

    engine = KpiEngine(db)
    return {
        "source": "live",
        "registryVersion": REGISTRY_VERSION,
        "scope": {"plantId": plantId},
        "period": {"year": year, "month": month},
        "kpis": await engine.compute_all(plantId, year, month, wanted),
    }


@router.get("/kpi/trend")
async def kpi_trend(
    plantId: str = Query(...),
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    months: int = Query(12, ge=2, le=36),
    codes: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """A KPI series ending at the given month, oldest first.

    One engine instance across the whole series so its per-period memoisation
    actually helps - the derived KPIs (FSI, Heinrich) would otherwise recompute
    their inputs for every point on the chart.
    """
    check = await can(db, user.id, "MANHOURS.READ", PermissionContext(plant_id=plantId))
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    plants = await get_accessible_plants(db, user.id)
    if plants is not None and plantId not in plants:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Plant not accessible")

    from app.services.manhours_kpi_engine import KpiEngine
    from app.services.manhours_kpi_registry import KPI_CODES

    wanted = tuple(c.strip() for c in codes.split(",") if c.strip()) if codes else KPI_CODES
    unknown = [c for c in wanted if c not in KPI_CODES]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown KPI code(s): {', '.join(unknown)}"
        )

    engine = KpiEngine(db)
    points: list[dict[str, Any]] = []
    total = year * 12 + (month - 1)
    for offset in range(months - 1, -1, -1):
        t = total - offset
        y, m = t // 12, t % 12 + 1
        points.append(
            {
                "year": y,
                "month": m,
                "label": f"{y}-{m:02d}",
                "kpis": await engine.compute_all(plantId, y, m, wanted),
            }
        )
    return {"scope": {"plantId": plantId}, "points": points}
