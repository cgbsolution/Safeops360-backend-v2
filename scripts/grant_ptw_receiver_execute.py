"""Additive RBAC delta: grant PTW.EXECUTE to every role that can be named
as a permit Receiver.

Why this exists as a standalone script
--------------------------------------
`app.seed.seed_rbac.main()` is the source of truth, but it is DESTRUCTIVE —
it runs `DELETE FROM "RolePermission"` and `DELETE FROM "UserRole"` before
rebuilding both from ROLE_GRANTS. Running it against the live database
would wipe every grant and multi-role assignment applied outside that file.
This script applies only the missing rows, and never deletes.

Background
----------
A PTW's Receiver is a free choice of User. The receiver-acknowledgement
step is an ASSIGNEE_TASK, so workflow_engine._rbac_gate requires
PTW.EXECUTE from whoever is named. Only HSE_MANAGER held it, so the step
was unreachable for realistic receivers and no permit could reach
activation or closure. /api/ptw/{id}/accept even documents the intent
("so a worker-receiver without PTW.UPDATE isn't blocked") but the engine
blocked them one layer down anyway.

OWN_RECORDS is the correct scope — the engine enforces an exact assignee
match and the accept route re-checks receiverId == user.id, so this
grants nothing beyond acting on a task you were personally named on.

Run:
    python -m scripts.grant_ptw_receiver_execute            # dry run
    python -m scripts.grant_ptw_receiver_execute --apply
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.user import Permission, Role, RolePermission

PERMISSION_CODE = "PTW.EXECUTE"
SCOPE = "OWN_RECORDS"

# Every role that holds any PTW grant today and could therefore be picked
# as a Receiver. HSE_MANAGER / ADMIN / SYSTEM_ADMIN already hold EXECUTE at
# a wider scope and are deliberately left alone.
TARGET_ROLES = [
    "WORKER",
    "CONTRACTOR_WORKMAN",
    "SUPERVISOR",
    "PERMIT_ISSUER",
    "SAFETY_OFFICER",
    "DEPARTMENT_HEAD",
    "PLANT_HEAD",
    "MAINTENANCE_HEAD",
    "CONTRACTOR_COORDINATOR",
]


async def main() -> None:
    apply = "--apply" in sys.argv
    print("APPLY" if apply else "DRY RUN — pass --apply to write")

    async with AsyncSessionLocal() as db:
        permission = (
            await db.execute(select(Permission).where(Permission.code == PERMISSION_CODE))
        ).scalar_one_or_none()
        if permission is None:
            raise SystemExit(f"Permission {PERMISSION_CODE} is not in the catalogue.")

        roles = {
            r.code: r
            for r in (
                await db.execute(select(Role).where(Role.code.in_(TARGET_ROLES)))
            ).scalars().all()
        }

        added = 0
        for code in TARGET_ROLES:
            role = roles.get(code)
            if role is None:
                print(f"  ?  {code}: role not found — skipped")
                continue
            existing = (
                await db.execute(
                    select(RolePermission).where(
                        RolePermission.roleId == role.id,
                        RolePermission.permissionId == permission.id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                print(f"  =  {code}: already holds {PERMISSION_CODE} (scope {existing.scope})")
                continue
            print(f"  +  {code}: grant {PERMISSION_CODE} scope {SCOPE}")
            added += 1
            if apply:
                db.add(
                    RolePermission(
                        roleId=role.id, permissionId=permission.id, scope=SCOPE
                    )
                )

        if apply:
            await db.commit()
            print(f"\ncommitted — {added} grant(s) added.")
        else:
            await db.rollback()
            print(f"\nrolled back — {added} grant(s) would be added.")


if __name__ == "__main__":
    asyncio.run(main())
