"""Plants + Areas master-data router. Mounts at /api/plants.

Created to give the mobile app's CAPA / HIRA / PTW create forms a stable
endpoint for the plant picker. The web version reads plants directly from
Prisma, but the mobile app needs a REST surface.

Filtered to the plants the caller can act in — `get_accessible_plants(None)`
means SYSTEM_ADMIN sees everything, others see only their permitted scope.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.plant import Area, Plant
from app.models.user import User
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
