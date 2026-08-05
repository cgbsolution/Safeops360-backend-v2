"""Plant (site) directory resolution.

The sibling of `user_directory`. A `siteId` column anywhere on the platform
carries `Plant.id` — a cuid — and a cuid is not a thing an auditor, a plant head
or a regulator can read. Payloads therefore carry `siteId` **and** `siteName`
side by side: the id stays for filtering and links, the name is what renders.

That convention was already in place across CAMS engagements, ERM and
Facilities (`siteId` + `siteName` on the Out schemas); the programme module
never adopted it, so its screens printed raw cuids. This module is the one
lookup all of them can share instead of each router re-rolling `_plant_index`.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plant import Plant

# What a null `siteId` means: the scope is the whole estate, not a missing
# value. Rendered as-is, so web / mobile / PDF all say the same word.
ESTATE_WIDE_LABEL = "Estate-wide"


async def resolve_plant_names(
    db: AsyncSession, ids: Iterable[str | None] | None = None
) -> dict[str, str]:
    """Resolve plant ids into a `{id: name}` map.

    `None`/empty ids are ignored. An id with no matching Plant (a deleted site
    referenced by an older cycle) is simply absent — callers fall back through
    `site_label`, which never returns a raw cuid.

    Passing `ids=None` loads every plant. Tenants here run tens of plants, not
    thousands, so that is one small query — worth it for callers that would
    otherwise need two passes over their rows to collect the ids first.
    """
    q = select(Plant.id, Plant.name)
    if ids is not None:
        wanted = {i for i in ids if i}
        if not wanted:
            return {}
        q = q.where(Plant.id.in_(wanted))
    return {r[0]: r[1] for r in (await db.execute(q)).all()}


def site_label(names: dict[str, str], site_id: str | None) -> str:
    """The one place the site display rule lives.

    Null id → "Estate-wide" (a real scope, not a gap). Known id → its name.
    Unknown id → "Unknown site" rather than the cuid: a reader can act on
    "unknown", but a cuid only looks like a bug.
    """
    if not site_id:
        return ESTATE_WIDE_LABEL
    return names.get(site_id) or "Unknown site"
