"""Platform-wide query scoping (P1-2).

A single, fail-closed source of truth for *which rows an actor may see*. Every
list/get query routes a SELECT through `QueryScope.apply()` before it touches the
DB — no query returns a row outside the actor's plant (and, when present, tenant).

Two axes:
  • plant scope — which plants this actor may read (today's isolation boundary)
  • tenant seam — `tenantId` is filtered unconditionally when the model carries it;
    today single-tenant (DEPLOYMENT_TENANT_ID), so the filter is always-true but
    present — multi-client isolation then costs zero additional work.

Built from the permission-SPECIFIC scope (`get_accessible_plants_for`), so a list
query never shows rows the per-record `can(<read_permission>)` check would deny.
`all_plants` is True only when the actor holds ALL_PLANTS on the read permission.
No plants → empty result (fail closed), never "all rows".

System/job actors use `system_scope(plant_ids)` and MUST declare an explicit
scope — a job never defaults to "all plants".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import false
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.permissions import get_accessible_plants_for

DEPLOYMENT_TENANT_ID = "default"  # single-tenant deployment; seam for multi-client


@dataclass
class QueryScope:
    actor_id: str
    all_plants: bool                       # True == unrestricted (ALL_PLANTS on read perm)
    plant_ids: list[str] = field(default_factory=list)  # [] == fail-closed (nothing visible)
    tenant_id: str = DEPLOYMENT_TENANT_ID
    is_system: bool = False

    def apply(self, stmt, model, plant_attr: str = "plantId"):
        """Scope a SELECT to this actor: tenant (if the model has tenantId) + plant.
        Models without a plant column are returned unscoped on the plant axis."""
        if hasattr(model, "tenantId"):
            stmt = stmt.where(getattr(model, "tenantId") == self.tenant_id)
        if self.all_plants:
            return stmt
        col = getattr(model, plant_attr, None)
        if col is None:
            # try the common alternate column name (CAMS / Facilities use siteId)
            col = getattr(model, "siteId", None)
        if col is None:
            return stmt  # not plant-scoped → tenant filter only
        if not self.plant_ids:
            return stmt.where(false())  # fail closed — no plants, no rows
        return stmt.where(col.in_(self.plant_ids))

    def plant_filter(self, column):
        """A boolean clause for ad-hoc queries that can't pass a model
        (e.g. raw column on a join). all_plants → no clause (True)."""
        if self.all_plants:
            return True
        return column.in_(self.plant_ids) if self.plant_ids else false()

    def allows_plant(self, plant_id: str | None) -> bool:
        return self.all_plants or (plant_id is not None and plant_id in self.plant_ids)


async def build_query_scope(db: AsyncSession, actor_id: str, read_permission: str) -> QueryScope:
    """Build the scope for a human actor from their grant on `read_permission`.
    ALL_PLANTS → unrestricted; OWN_PLANT/etc → their plant set; permission absent
    → empty (fail closed)."""
    plants = await get_accessible_plants_for(db, actor_id, read_permission)
    if plants is None:
        return QueryScope(actor_id=actor_id, all_plants=True, plant_ids=[])
    return QueryScope(actor_id=actor_id, all_plants=False, plant_ids=list(plants))


def system_scope(plant_ids: list[str], *, all_plants: bool = False, job_name: str = "system") -> QueryScope:
    """Scope for a background job/automated process. MUST declare its plant scope;
    a job that wants all plants states it explicitly — it never defaults to it."""
    return QueryScope(
        actor_id=f"SYSTEM:{job_name}",
        all_plants=all_plants,
        plant_ids=list(plant_ids),
        is_system=True,
    )
