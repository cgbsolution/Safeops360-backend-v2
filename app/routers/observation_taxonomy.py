"""Observation taxonomy master — read-only lookup for the submission forms.

Both endpoints take `type`, which accepts either a bare axis (ACT / CONDITION)
or a full observation type (UNSAFE_ACT / SAFE_CONDITION / …) and normalises it.
The web form, the /capture PWA and the mobile app all hit these rather than
carrying a hardcoded list — the reason the old shared list drifted in three
places at once.

Auth-gated but not module-gated: this is dropdown data for a form the caller
has already been allowed to open, and it exposes no record data. Same posture
as the insights and capture routers.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.services import observation_taxonomy as tax

router = APIRouter(prefix="/api/observation-taxonomy", tags=["observation-taxonomy"])


def _axis_or_400(raw: str) -> str:
    axis = tax.normalise_axis(raw)
    if axis is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Invalid type '{raw}'. Expected ACT, CONDITION, or an ObservationType "
            "(SAFE_ACT / UNSAFE_ACT / SAFE_CONDITION / UNSAFE_CONDITION).",
        )
    return axis


@router.get("/categories")
async def get_categories(
    type: str = Query(..., description="ACT | CONDITION | an ObservationType value"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Categories with ≥1 active sub-category on this axis.

    Reactions/Positions of People are absent from the CONDITION response
    because no CONDITION sub-categories are seeded for them — the exclusion is
    derived from the data, never hardcoded here.
    """
    axis = _axis_or_400(type)
    return {"observationType": axis, "items": await tax.list_categories(db, axis)}


@router.get("/subcategories")
async def get_subcategories(
    type: str = Query(..., description="ACT | CONDITION | an ObservationType value"),
    category: str = Query(..., description="categoryCode from /categories"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    axis = _axis_or_400(type)
    items = await tax.list_subcategories(db, axis, category.strip())
    return {"observationType": axis, "categoryCode": category, "items": items}
