"""Generic master / lookup data. Mounts at /api/masters.

`MasterItem` is the shared key-value table behind most dropdowns (SHIFT,
ACTIVITY_TYPE, HAZARD_CATEGORY, ENERGY_SOURCE, ROOT_CAUSE_CATEGORY, …). Near-Miss
already exposed its own copy at /api/near-miss/masters/items, but that route is
gated on the NEAR_MISS module — so a form in any other module could not use it.
This router is the module-agnostic surface those forms read.

Ungated by design (ROUTER_MODULE → None, like `plants` and `workforce`): this is
reference data, not a module feature, and gating it would break dropdowns on
modules whose own licence is perfectly valid. Authentication is still required.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.masters import MasterItem
from app.models.user import User

router = APIRouter(prefix="/api/masters", tags=["masters"])


@router.get("/items")
async def list_master_items(
    type: str = Query(..., description="The MasterItem.type discriminator, e.g. SHIFT."),
    includeInactive: bool = False,
    user: User = Depends(get_current_user),  # noqa: ARG001 — auth gate only
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Lookup rows of one type, in display order.

    Inactive rows are excluded unless asked for: a retired option must stop
    appearing in new-record dropdowns, while an admin screen still needs to see
    it to re-activate it.
    """
    stmt = select(MasterItem).where(MasterItem.type == type)
    if not includeInactive:
        stmt = stmt.where(MasterItem.active.is_(True))
    rows = (
        await db.execute(stmt.order_by(MasterItem.sortOrder, MasterItem.label))
    ).scalars().all()
    return [
        {
            "code": m.code,
            "label": m.label,
            "sortOrder": m.sortOrder,
            "active": m.active,
            "metadata": m.metadata_,
        }
        for m in rows
    ]
