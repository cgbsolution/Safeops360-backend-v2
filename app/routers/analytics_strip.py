"""Analytics-strip endpoints — one per module landing page.

Mounted under its own `/api/analytics-strip` prefix rather than appended to
each module router, for two reasons:

  * route ordering — `observations.py`, `ptw.py` and friends all own a
    `GET /{record_id}`, which would swallow a sibling `/analytics-strip`
    declared after it. A separate prefix cannot collide.
  * one place to read — the seven strips share a response contract, so
    keeping them together makes a drift between them obvious.

Entitlement is enforced PER ROUTE (`require_module`) instead of on the router,
because this router spans seven differently-licensed modules. The router itself
maps to None in ROUTER_MODULE; each route below carries its own guard, so a
tenant without PPE still gets a 403 on the PPE strip.

A caller with no read grant on a module gets `{"denied": true}` with 200 rather
than a 403: the strip sits above the list on a page the user reached legally,
and rendering an honest zeroed band beats collapsing the page into an error.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.licensing.enforcement import require_module
from app.models.user import User
from app.services import analytics_strip as strips

router = APIRouter(prefix="/api/analytics-strip", tags=["analytics-strip"])


async def _run(fn, db: AsyncSession, user: User, **kwargs) -> dict[str, Any]:
    """Shared wrapper: stamp `now` once per request so every window in a strip
    is measured against the same instant, and translate StripDenied into the
    zeroed-strip contract."""
    now = datetime.now(timezone.utc)
    try:
        data = await fn(db, user, now, **kwargs)
    except strips.StripDenied as denied:
        # Same field set as a successful response, all zero/None — so the strip
        # component never has to branch to avoid rendering `undefined`.
        return {"denied": True, **denied.empty}
    return {"denied": False, **data}


@router.get("/observations", dependencies=[Depends(require_module("OBSERVATION"))])
async def observations_strip(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await _run(strips.observation_strip, db, user)


@router.get("/near-miss", dependencies=[Depends(require_module("NEAR_MISS"))])
async def near_miss_strip(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await _run(strips.near_miss_strip, db, user)


@router.get("/incidents", dependencies=[Depends(require_module("INCIDENT"))])
async def incidents_strip(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await _run(strips.incident_strip, db, user)


@router.get("/capa", dependencies=[Depends(require_module("CAPA"))])
async def capa_strip(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await _run(strips.capa_strip, db, user)


@router.get("/ppe", dependencies=[Depends(require_module("PPE"))])
async def ppe_strip(
    plantId: str = Query(..., description="The plant the PPE page has selected."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # plantId is narrowed by the caller's accessible plants inside the service —
    # passing another tenant's plant here yields an empty strip, not their data.
    return await _run(strips.ppe_strip, db, user, plant_id=plantId)


@router.get("/ptw", dependencies=[Depends(require_module("PTW"))])
async def ptw_strip(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await _run(strips.ptw_strip, db, user)


@router.get("/moc", dependencies=[Depends(require_module("MOC"))])
async def moc_strip(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await _run(strips.moc_strip, db, user)


@router.get("/training", dependencies=[Depends(require_module("TRAINING"))])
async def training_strip(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await _run(strips.training_strip, db, user)


@router.get("/skill-matrix", dependencies=[Depends(require_module("COMPETENCY"))])
async def skill_matrix_strip(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await _run(strips.skill_matrix_strip, db, user)
