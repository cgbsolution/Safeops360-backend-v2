"""Restrict "Schedule Audit" to HSE Manager + admins.

Raising an audit used to be gated on AUDIT_COMPLIANCE.CREATE, which nine roles
held. But CREATE also gates checkpoint-library import and template
custom-checkpoint authoring, so locking scheduling down by revoking CREATE
would have silently stripped content authoring from the audit roles too.

So scheduling gets its own permission, AUDIT_COMPLIANCE.SCHEDULE, granted to
HSE_MANAGER (own plant) and ADMIN (all plants) and nobody else.
Everyone keeps CREATE and therefore keeps library/template authoring.

Widening this later is a tick-box in Configuration -> Roles: grant
AUDIT_COMPLIANCE.SCHEDULE to whichever role should raise audits. That is the
whole reason it is a named permission rather than a hard-coded role check.

Idempotent. Run from the backend root:
    .venv/Scripts/python.exe scripts/restrict_audit_scheduling.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.models.user import Permission, Role, RolePermission
from app.core.db import AsyncSessionLocal

CODE = "AUDIT_COMPLIANCE.SCHEDULE"
GRANTS: dict[str, str] = {
    "HSE_MANAGER": "OWN_PLANT",
    "ADMIN": "ALL_PLANTS",
}


async def main() -> int:
    async with AsyncSessionLocal() as db:
        perm = (await db.execute(select(Permission).where(Permission.code == CODE))).scalar_one_or_none()
        if perm is None:
            perm = Permission(
                code=CODE,
                module="AUDIT_COMPLIANCE",
                action="SCHEDULE",
                description="Schedule (raise) an audit — the Schedule Audit action",
            )
            db.add(perm)
            await db.flush()
            print(f"  + created permission {CODE}")
        else:
            print(f"  = permission {CODE} exists")

        existing = (
            await db.execute(select(RolePermission).where(RolePermission.permissionId == perm.id))
        ).scalars().all()
        roles = {r.id: r for r in (await db.execute(select(Role))).scalars().all()}
        by_code = {r.code: r for r in roles.values()}

        # Revoke from anyone not on the list — re-running after someone was
        # granted it by hand converges back to the intended set.
        for rp in existing:
            code = roles[rp.roleId].code if rp.roleId in roles else "?"
            if code not in GRANTS:
                await db.delete(rp)
                print(f"  - {code}: revoked {CODE}")

        held = {roles[rp.roleId].code: rp for rp in existing if rp.roleId in roles}
        for role_code, scope in GRANTS.items():
            role = by_code.get(role_code)
            if role is None:
                print(f"  ! role {role_code} not found — skipped")
                continue
            rp = held.get(role_code)
            if rp is None:
                db.add(RolePermission(roleId=role.id, permissionId=perm.id, scope=scope))
                print(f"  + {role_code}: {CODE} @ {scope}")
            elif rp.scope != scope:
                rp.scope = scope
                print(f"  ~ {role_code}: scope -> {scope}")
            else:
                print(f"  = {role_code}: {CODE} @ {scope} (exists)")

        await db.commit()
        print("\nDone. Restart uvicorn — the permission snapshot cache is 300s.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
