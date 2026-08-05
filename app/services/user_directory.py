"""User directory resolution.

A single batched lookup that turns a set of user IDs into display-ready
references (name + plant + role). Used to replace raw user IDs in API
responses and by the generic /api/users/by-ids endpoint so the frontend never
has to render an opaque cuid.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import User
from app.schemas.common import UserRefOut


async def resolve_user_directory(
    db: AsyncSession, ids: Iterable[str | None]
) -> dict[str, UserRefOut]:
    """Resolve user IDs into a `{id: UserRefOut}` map.

    `None`/empty ids are ignored. IDs with no matching user (e.g. a deleted
    account) are simply absent from the map — callers should fall back to the
    raw id when a lookup misses. Plant is eager-loaded so the role and plant
    name come back in one round-trip.
    """
    wanted = {i for i in ids if i}
    if not wanted:
        return {}

    rows = (
        await db.execute(
            select(User).options(selectinload(User.plant)).where(User.id.in_(wanted))
        )
    ).scalars().all()

    return {
        u.id: UserRefOut(
            id=u.id,
            name=u.name,
            role=u.role,
            designation=u.designation,
            department=u.department,
            plantId=u.plantId,
            plantName=u.plant.name if u.plant else None,
            plantCode=u.plant.code if u.plant else None,
        )
        for u in rows
    }
