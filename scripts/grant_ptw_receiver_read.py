"""Additive RBAC delta: give the roles that can be named on a permit or an
FLRA a working READ grant on the records they are personally named on.

Why this exists as a standalone script
--------------------------------------
`app.seed.seed_rbac.main()` is the source of truth but it is DESTRUCTIVE —
it runs `DELETE FROM "RolePermission"` and `DELETE FROM "UserRole"` before
rebuilding both. Running it against a live database wipes every grant
applied outside that file. This script only adds the missing rows.
Sibling of `grant_ptw_receiver_execute.py`, which fixed the same class of
gap for the EXECUTE side.

Background
----------
SUPERVISOR (the natural permit Receiver and FLRA leader) holds PTW.READ and
FLRA.READ at scope OWN_DEPARTMENT. That scope compares
`PermissionContext.department_id` — a Department **id** — against
`User.department`, which is a free-text department **name**. The two can
never be equal, so OWN_DEPARTMENT never matches a record-scoped check and
the named receiver is 403'd off the very permit they were assigned. The
receiver step then cannot be reached at all: no permit can activate or close.

OWN_RECORDS is the correct scope. `GET /api/ptw/{id}` passes
originatorId / issuerId / receiverId into the scope record, and
`GET /api/flra/{id}` passes leaderId plus the crew — so this grants nothing
beyond reading a record you are personally named on.

Run:
    python -m scripts.grant_ptw_receiver_read            # dry run
    python -m scripts.grant_ptw_receiver_read --apply
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.user import Permission, Role, RolePermission

SCOPE = "OWN_RECORDS"

# Roles that can be named as a permit Receiver / FLRA crew or leader but whose
# READ grant sits at a scope that cannot match a record. Roles that already
# hold the grant at a wider scope (HSE_MANAGER, ADMIN, SAFETY_OFFICER…) are
# skipped automatically by the "already holds" check below.
TARGETS: dict[str, list[str]] = {
    "PTW.READ": [
        "SUPERVISOR",
        "PERMIT_ISSUER",
        "DEPARTMENT_HEAD",
        "MAINTENANCE_HEAD",
        "CONTRACTOR_COORDINATOR",
    ],
    "FLRA.READ": [
        "SUPERVISOR",
        "PERMIT_ISSUER",
    ],
}


async def main() -> None:
    apply = "--apply" in sys.argv
    print("APPLY" if apply else "DRY RUN — pass --apply to write")

    async with AsyncSessionLocal() as db:
        added = 0
        for perm_code, role_codes in TARGETS.items():
            permission = (
                await db.execute(select(Permission).where(Permission.code == perm_code))
            ).scalar_one_or_none()
            if permission is None:
                print(f"  ?  {perm_code}: not in the catalogue — skipped")
                continue

            roles = {
                r.code: r
                for r in (
                    await db.execute(select(Role).where(Role.code.in_(role_codes)))
                ).scalars().all()
            }
            for code in role_codes:
                role = roles.get(code)
                if role is None:
                    print(f"  ?  {code}: role not found — skipped")
                    continue
                # RolePermission is UNIQUE on (roleId, permissionId), so a role
                # holds a permission at exactly one scope — the dead
                # OWN_DEPARTMENT row has to be re-scoped in place, not
                # supplemented. Anything already at OWN_RECORDS or wider is
                # left untouched.
                existing = (
                    await db.execute(
                        select(RolePermission).where(
                            RolePermission.roleId == role.id,
                            RolePermission.permissionId == permission.id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    print(f"  +  {code}: grant {perm_code} scope {SCOPE}")
                    added += 1
                    if apply:
                        db.add(
                            RolePermission(
                                roleId=role.id, permissionId=permission.id, scope=SCOPE
                            )
                        )
                    continue
                if existing.scope != "OWN_DEPARTMENT":
                    print(f"  =  {code}: {perm_code} already at {existing.scope} — left alone")
                    continue
                print(f"  ~  {code}: {perm_code} OWN_DEPARTMENT -> {SCOPE}")
                added += 1
                if apply:
                    existing.scope = SCOPE

        if apply:
            await db.commit()
            print(f"\ncommitted — {added} grant(s) added.")
        else:
            await db.rollback()
            print(f"\nrolled back — {added} grant(s) would be added.")


if __name__ == "__main__":
    asyncio.run(main())
