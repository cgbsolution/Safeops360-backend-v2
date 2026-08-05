"""Plants + Areas master-data router. Mounts at /api/plants.

Created to give the mobile app's CAPA / HIRA / PTW create forms a stable
endpoint for the plant picker. The web version reads plants directly from
Prisma, but the mobile app needs a REST surface.

Filtered to the plants the caller can act in — `get_accessible_plants(None)`
means SYSTEM_ADMIN sees everything, others see only their permitted scope.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.incident_intel import PlantCostConfig
from app.models.plant import Area, Plant
from app.models.user import User
from app.services import incident_cost
from app.services.permissions import get_accessible_plants

router = APIRouter(prefix="/api/plants", tags=["plants"])


@router.get("")
async def list_plants(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    plants = await get_accessible_plants(db, user.id)
    stmt = select(Plant)
    if plants is not None:
        if not plants:
            return []
        stmt = stmt.where(Plant.id.in_(plants))
    stmt = stmt.order_by(Plant.name)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": p.id,
            "code": p.code,
            "name": p.name,
            "location": p.location,
            "state": p.state,
            "unitType": p.unitType,
        }
        for p in rows
    ]


async def _assert_plant_accessible(db: AsyncSession, user: User, plant_id: str) -> None:
    plants = await get_accessible_plants(db, user.id)
    if plants is not None and plant_id not in plants:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Plant not accessible")


# ─── Feature 8 — Cost of unsafety ───────────────────────────────────────────

_COST_CONFIG_ROLES = {"PLANT_HEAD", "CORPORATE_HSE", "ADMIN", "SYSTEM_ADMIN"}


class CostConfigInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hourlyProductionValue: float | None = None
    defaultLaborRate: float | None = None
    loadedLaborRateByRole: dict[str, float] | None = None
    currency: str | None = None


@router.get("/{plant_id}/cost-of-unsafety")
async def cost_of_unsafety(
    plant_id: str,
    months: int = 12,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Feature 8 — rolling-window cost-of-unsafety rollup for a plant (total, by
    type, by area, month-over-month, + preventive-CAPA comparison)."""
    await _assert_plant_accessible(db, user, plant_id)
    return await incident_cost.plant_rollup(db, plant_id, months=max(1, min(36, months)))


@router.get("/{plant_id}/cost-config")
async def get_cost_config(
    plant_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _assert_plant_accessible(db, user, plant_id)
    cfg = await incident_cost.plant_config(db, plant_id)
    if cfg is None:
        return {"plantId": plant_id, "configured": False, "currency": "INR"}
    return {
        "plantId": plant_id, "configured": True,
        "hourlyProductionValue": cfg.hourlyProductionValue,
        "defaultLaborRate": cfg.defaultLaborRate,
        "loadedLaborRateByRole": cfg.loadedLaborRateByRole or {},
        "currency": cfg.currency,
    }


@router.put("/{plant_id}/cost-config")
async def put_cost_config(
    plant_id: str,
    payload: CostConfigInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _assert_plant_accessible(db, user, plant_id)
    if user.role not in _COST_CONFIG_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Plant Head or above may set cost config")
    if await db.get(Plant, plant_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plant not found")
    cfg = await incident_cost.plant_config(db, plant_id)
    if cfg is None:
        cfg = PlantCostConfig(plantId=plant_id)
        db.add(cfg)
    if payload.hourlyProductionValue is not None:
        cfg.hourlyProductionValue = payload.hourlyProductionValue
    if payload.defaultLaborRate is not None:
        cfg.defaultLaborRate = payload.defaultLaborRate
    if payload.loadedLaborRateByRole is not None:
        cfg.loadedLaborRateByRole = payload.loadedLaborRateByRole
    if payload.currency:
        cfg.currency = payload.currency
    await db.flush()
    return {"ok": True, "plantId": plant_id}


@router.get("/{plant_id}/areas")
async def list_plant_areas(
    plant_id: str,
    user: User = Depends(get_current_user),  # noqa: ARG001 — auth gate
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, str]]:
    rows = (
        await db.execute(
            select(Area).where(Area.plantId == plant_id).order_by(Area.name)
        )
    ).scalars().all()
    return [{"id": a.id, "name": a.name, "plantId": a.plantId} for a in rows]
