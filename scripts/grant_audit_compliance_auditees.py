"""One-off: grant auditee-class roles access to the audit workflow (QA fix C1).

Audit findings route to SAFETY_OFFICER / SUPERVISOR / DEPARTMENT_HEAD, but those
roles held NO AUDIT_COMPLIANCE permission, so they 403'd on my-checkpoints,
get_audit (READ) and AUDITEE_RESPOND (UPDATE). This grants:
  - AUDIT_COMPLIANCE.READ @ OWN_PLANT   (view plant audits + their inbox)
  - AUDIT_COMPLIANCE.UPDATE @ OWN_RECORDS (respond — the service owner-guard
    restricts it to findings actually routed to them)

Idempotent (no RolePermission wipe), mirrors the ROLE_GRANTS edit in
seed_rbac.py / seed-rbac.ts. Run from the backend root:
    .venv/Scripts/python.exe scripts/grant_audit_compliance_auditees.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.user import Permission, Role, RolePermission

ROLES = ["SAFETY_OFFICER", "SUPERVISOR", "DEPARTMENT_HEAD"]
GRANTS = [("AUDIT_COMPLIANCE.READ", "OWN_PLANT"), ("AUDIT_COMPLIANCE.UPDATE", "OWN_RECORDS")]


async def main() -> int:
    async with AsyncSessionLocal() as db:
        perms = {
            code: (await db.execute(select(Permission).where(Permission.code == code))).scalar_one_or_none()
            for code, _ in GRANTS
        }
        missing_perm = [c for c, p in perms.items() if p is None]
        if missing_perm:
            print(f"Permission(s) missing — run seed-rbac first: {missing_perm}")
            return 1

        added = 0
        for role_code in ROLES:
            role = (await db.execute(select(Role).where(Role.code == role_code))).scalar_one_or_none()
            if role is None:
                print(f"  role {role_code} not found — skip")
                continue
            for code, scope in GRANTS:
                perm = perms[code]
                rp = (
                    await db.execute(
                        select(RolePermission).where(
                            RolePermission.roleId == role.id, RolePermission.permissionId == perm.id
                        )
                    )
                ).scalar_one_or_none()
                if rp is None:
                    db.add(RolePermission(roleId=role.id, permissionId=perm.id, scope=scope))
                    added += 1
                    print(f"  + {role_code}: {code} @ {scope}")
                elif rp.scope != scope:
                    rp.scope = scope
                    print(f"  ~ {role_code}: {code} scope -> {scope}")
                else:
                    print(f"  = {role_code}: {code} @ {scope} (exists)")
        await db.commit()
        print(f"\nGranted {added} new RolePermission row(s).")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
