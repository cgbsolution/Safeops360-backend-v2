"""Fire & Life Safety permission codes and the one gate every fire route calls.

WHY THIS MODULE EXISTS
----------------------
The fire module shipped borrowing `INCIDENT.READ` / `INCIDENT.UPDATE`. Its own
router documented that as a bootstrap with two known defects, and both were real:

  * `AUDITOR` and `LEAD_AUDITOR` hold no INCIDENT grant at all, so the two roles
    whose job is to inspect the fire register could not open it.
  * `WORKER` and `CONTRACTOR_WORKMAN` hold `INCIDENT.READ` at OWN_RECORDS, which
    `get_accessible_plants_for` widens to the whole plant — so a contractor could
    read every extinguisher, panel and signed checklist on site.

Dedicated `FIRE.*` codes now exist (see prisma/seed-rbac.ts). The action shape is
taken from the sign-off block printed on the client's own sheets, because that is
where the segregation of duties is already defined:

    EXECUTE  fill the sheet      -> "Prepared by: Person In-charge"
    VERIFY   review it           -> "Reviewed by: Intermediatory Head"
    APPROVE  approve and lock it -> "Approved by: HOD"

Those three go to different roles deliberately. One principal holding all three
can sign their own work, which is the single thing a three-stage sign-off block
exists to prevent.

THE MIGRATION GUARD
-------------------
Switching the routers to `FIRE.*` in one step would 403 every user on any
deployment whose RBAC has not been reseeded — the permission rows would not exist,
so nobody could hold them. `require()` therefore distinguishes two failures that
`can()` reports identically:

    the FIRE permission is not seeded at all  -> fall back to the legacy code
    the FIRE permission exists, user lacks it -> deny, as intended

The fallback is a migration ramp, not a permanent alias: the moment
`seed-rbac.ts` has run, the legacy path is dead and the real grants apply. It is
cached per process, so the extra lookup happens once, not per request.
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
READ = "FIRE.READ"
CREATE = "FIRE.CREATE"
UPDATE = "FIRE.UPDATE"
DELETE = "FIRE.DELETE"
EXECUTE = "FIRE.EXECUTE"      # fill a checklist — "Prepared by"
VERIFY = "FIRE.VERIFY"        # review a checklist — "Reviewed by"
APPROVE = "FIRE.APPROVE"      # approve + lock — "Approved by"
CLOSE = "FIRE.CLOSE"
EXPORT = "FIRE.EXPORT"
TEMPLATE_AUTHOR = "FIRE.TEMPLATE_AUTHOR"
TEMPLATE_APPROVE = "FIRE.TEMPLATE_APPROVE"
CALENDAR = "FIRE.CALENDAR"

# What each FIRE code degrades to while RBAC is un-reseeded. Read-shaped codes
# map to the old read grant, everything that writes maps to the old write grant —
# i.e. exactly the two-code behaviour the module shipped with, no wider.
_LEGACY: dict[str, str] = {
    READ: "INCIDENT.READ",
    EXPORT: "INCIDENT.READ",
    CREATE: "INCIDENT.UPDATE",
    UPDATE: "INCIDENT.UPDATE",
    DELETE: "INCIDENT.UPDATE",
    EXECUTE: "INCIDENT.UPDATE",
    VERIFY: "INCIDENT.UPDATE",
    APPROVE: "INCIDENT.UPDATE",
    CLOSE: "INCIDENT.UPDATE",
    TEMPLATE_AUTHOR: "INCIDENT.UPDATE",
    TEMPLATE_APPROVE: "INCIDENT.UPDATE",
    CALENDAR: "INCIDENT.UPDATE",
}

# None = not yet checked. Cached per process: whether FIRE.* is seeded is a
# deployment-lifetime fact, not a per-request one.
_fire_rbac_seeded: bool | None = None


async def fire_rbac_seeded(db: AsyncSession) -> bool:
    """True once `seed-rbac.ts` has created the FIRE permission rows."""
    global _fire_rbac_seeded
    if _fire_rbac_seeded is None:
        found = (
            await db.execute(select(Permission.id).where(Permission.code == READ).limit(1))
        ).scalars().first()
        _fire_rbac_seeded = found is not None
        if not _fire_rbac_seeded:
            log.warning(
                "FIRE.* permissions are not seeded; fire routes are falling back to "
                "INCIDENT.READ/UPDATE. Run `npx tsx prisma/seed-rbac.ts` to activate "
                "the real fire grants (this also fixes AUDITOR being unable to read "
                "the register, and CONTRACTOR_WORKMAN being able to)."
            )
    return _fire_rbac_seeded


def reset_cache() -> None:
    """Test hook — forget whether FIRE.* was seeded."""
    global _fire_rbac_seeded
    _fire_rbac_seeded = None


async def allowed(db: AsyncSession, user: User, code: str, plant_id: str | None = None) -> bool:
    """Non-raising check, for deciding whether to *offer* an action."""
    ctx = PermissionContext(plant_id=plant_id)
    if await fire_rbac_seeded(db):
        return (await can(db, user.id, code, ctx)).allowed
    legacy = _LEGACY.get(code)
    return bool(legacy) and (await can(db, user.id, legacy, ctx)).allowed


async def require(db: AsyncSession, user: User, code: str, plant_id: str | None = None) -> None:
    """Gate a fire route. Raises 403 with the reason the engine gave."""
    ctx = PermissionContext(plant_id=plant_id)
    if await fire_rbac_seeded(db):
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
            f"{code} requires the FIRE permission set. Run prisma/seed-rbac.ts.",
        )
    res = await can(db, user.id, legacy, ctx)
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Requires {code}")


async def capabilities(db: AsyncSession, user: User, plant_id: str | None = None) -> dict[str, bool]:
    """What this principal may do, for the UI to hide controls it cannot use.

    A screen that offers an Approve button to someone who will get a 403 is worse
    than one that hides it: the operator learns the rule by failing at it, and on
    a shared plant terminal they cannot tell a permission problem from a bug.
    """
    codes = {
        "read": READ, "create": CREATE, "update": UPDATE, "delete": DELETE,
        "execute": EXECUTE, "verify": VERIFY, "approve": APPROVE, "close": CLOSE,
        "export": EXPORT, "templateAuthor": TEMPLATE_AUTHOR,
        "templateApprove": TEMPLATE_APPROVE, "calendar": CALENDAR,
    }
    out: dict[str, bool] = {}
    for key, code in codes.items():
        out[key] = await allowed(db, user, code, plant_id)
    out["rbacSeeded"] = await fire_rbac_seeded(db)
    return out


__all__ = [
    "READ", "CREATE", "UPDATE", "DELETE", "EXECUTE", "VERIFY", "APPROVE", "CLOSE",
    "EXPORT", "TEMPLATE_AUTHOR", "TEMPLATE_APPROVE", "CALENDAR",
    "require", "allowed", "capabilities", "fire_rbac_seeded", "reset_cache",
]
