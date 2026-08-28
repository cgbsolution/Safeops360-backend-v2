"""Chemical / Hazmat permission codes and the one gate every chemical route calls.

WHY THIS MODULE EXISTS
----------------------
The chemical module shipped borrowing `INCIDENT.READ` / `INCIDENT.UPDATE`, with
`CONFIGURATION.MASTERS` for the config masters. Its own router documented that
as a bootstrap "to swap for CHEMICAL.* once a licence including them is issued".
It is the same defect `fire_permissions.py` was written to fix, and it has the
same consequence here:

  * `WORKER` and `CONTRACTOR_WORKMAN` hold `INCIDENT.READ` at OWN_RECORDS, which
    `get_accessible_plants_for` widens to the whole plant — so a contractor
    could read the complete hazmat inventory of a site: every chemical, every
    quantity, every storage location, and the regulatory-threshold dashboard
    that says which of them are near a reportable limit.
  * `AUDITOR` and `LEAD_AUDITOR` hold no INCIDENT grant at all, so the roles
    whose job includes inspecting chemical storage could not open the register.

That second one matters more here than it did for fire: a chemical inventory is
the input to the site's MAH / threshold obligations, and an auditor who cannot
read it cannot verify them.

THE ACTION SHAPE
----------------
Deliberately narrower than FIRE's. Fire has EXECUTE/VERIFY/APPROVE because its
source documents carry a printed three-stage sign-off block that defines the
segregation of duties. Chemical has no such block — the distinctions its router
actually makes today are read / write / configure, and inventing lifecycle codes
with no endpoint behind them would be vocabulary nobody can grant meaningfully:

    READ       the register, inventory, ledger, dashboards, count sheets
    CREATE     add a chemical to the master, a storage location, an inventory item
    UPDATE     edit a master, upload an SDS, move stock, transfer, dispose,
               acknowledge an MOC trigger, reconcile a stock count
    CONFIGURE  the threshold rules and the incompatibility matrix

`CONFIGURE` stays separate from `UPDATE` for the reason the router already had
it separate: the threshold rules decide when a site crosses a *regulatory*
reporting limit and the incompatibility matrix decides what may be stored beside
what. Those are master data set once by an administrator, not day-to-day stock
work, and a storekeeper who can book stock in must not be able to raise the
threshold that would otherwise have flagged it.

DELETE is absent because no endpoint deletes: chemical records are statutory and
the module retires by status change, not removal.

THE MIGRATION GUARD
-------------------
Identical to fire's, and for the identical reason. Switching to `CHEMICAL.*` in
one step would 403 every user on any deployment whose RBAC has not been
reseeded, because the permission rows would not exist for anyone to hold. So
`require()` distinguishes two failures that `can()` reports the same way:

    the CHEMICAL permission is not seeded at all  -> fall back to the legacy code
    the CHEMICAL permission exists, user lacks it -> deny, as intended

The fallback is a migration ramp, not a permanent alias: once `seed-rbac.ts`
(and `app/seed/seed_rbac.py`) have run, the legacy path is dead and the real
grants apply — which is the moment the contractor loses the inventory. Cached
per process, so the extra lookup happens once rather than per request.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Permission, User
from app.services.permissions import PermissionContext, can

log = logging.getLogger(__name__)

# ── Codes ────────────────────────────────────────────────────────────────────
READ = "CHEMICAL.READ"
CREATE = "CHEMICAL.CREATE"
UPDATE = "CHEMICAL.UPDATE"
CONFIGURE = "CHEMICAL.CONFIGURE"   # threshold rules + incompatibility matrix

# What each CHEMICAL code degrades to while RBAC is un-reseeded — i.e. exactly
# the three-code behaviour the module shipped with, no wider. CONFIGURE keeps
# mapping to CONFIGURATION.MASTERS rather than to the write grant, so the ramp
# never briefly hands the threshold masters to whoever could book stock.
_LEGACY: dict[str, str] = {
    READ: "INCIDENT.READ",
    CREATE: "INCIDENT.UPDATE",
    UPDATE: "INCIDENT.UPDATE",
    CONFIGURE: "CONFIGURATION.MASTERS",
}

# None = not yet checked. Cached per process: whether CHEMICAL.* is seeded is a
# deployment-lifetime fact, not a per-request one.
_chemical_rbac_seeded: bool | None = None


async def chemical_rbac_seeded(db: AsyncSession) -> bool:
    """True once the RBAC seeders have created the CHEMICAL permission rows."""
    global _chemical_rbac_seeded
    if _chemical_rbac_seeded is None:
        found = (
            await db.execute(select(Permission.id).where(Permission.code == READ).limit(1))
        ).scalars().first()
        _chemical_rbac_seeded = found is not None
        if not _chemical_rbac_seeded:
            log.warning(
                "CHEMICAL.* permissions are not seeded; chemical routes are falling back "
                "to INCIDENT.READ/UPDATE. Run `npx tsx prisma/seed-rbac.ts` to activate "
                "the real chemical grants (this also fixes AUDITOR being unable to read "
                "the hazmat inventory, and CONTRACTOR_WORKMAN being able to)."
            )
    return _chemical_rbac_seeded


def reset_cache() -> None:
    """Test hook — forget whether CHEMICAL.* was seeded."""
    global _chemical_rbac_seeded
    _chemical_rbac_seeded = None


async def allowed(db: AsyncSession, user: User, code: str, plant_id: str | None = None) -> bool:
    """Non-raising check, for deciding whether to *offer* an action."""
    ctx = PermissionContext(plant_id=plant_id)
    if await chemical_rbac_seeded(db):
        return (await can(db, user.id, code, ctx)).allowed
    legacy = _LEGACY.get(code)
    return bool(legacy) and (await can(db, user.id, legacy, ctx)).allowed


async def require(db: AsyncSession, user: User, code: str, plant_id: str | None = None) -> None:
    """Gate a chemical route. Raises 403 with the reason the engine gave."""
    ctx = PermissionContext(plant_id=plant_id)
    if await chemical_rbac_seeded(db):
        res = await can(db, user.id, code, ctx)
        if not res.allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Requires {code}")
        return

    legacy = _LEGACY.get(code)
    if legacy is None:
        # A code with no legacy equivalent (a new authority) must not be silently
        # granted to whoever happened to hold INCIDENT.UPDATE.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"{code} requires the CHEMICAL permission set. Run prisma/seed-rbac.ts.",
        )
    res = await can(db, user.id, legacy, ctx)
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Requires {code}")


async def capabilities(db: AsyncSession, user: User, plant_id: str | None = None) -> dict[str, bool]:
    """What this principal may do, for the UI to hide controls it cannot use.

    A screen that offers "Add chemical" to someone who will get a 403 teaches the
    rule by failing at it; on a shared store-room terminal the operator cannot
    tell a permission problem from a bug.
    """
    codes = {"read": READ, "create": CREATE, "update": UPDATE, "configure": CONFIGURE}
    out: dict[str, bool] = {key: await allowed(db, user, code, plant_id) for key, code in codes.items()}
    out["rbacSeeded"] = await chemical_rbac_seeded(db)
    return out


__all__ = [
    "READ", "CREATE", "UPDATE", "CONFIGURE",
    "require", "allowed", "capabilities", "chemical_rbac_seeded", "reset_cache",
]
