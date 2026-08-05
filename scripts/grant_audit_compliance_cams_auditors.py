"""One-off: grant the CAMS auditor-class roles access to the audit engine.

Audits under CAMS run on the ComplianceAudit engine (`/cams/audits`), which is
gated on AUDIT_COMPLIANCE.* — CAMS.* only covers inspections, templates, the
programme and analytics. The four CAMS roles (CAMS_ADMIN, AUDIT_MANAGER,
LEAD_AUDITOR, AUDITOR) held CAMS.* only, so the very people an audit is
assigned to 403'd on:
  - GET /api/audit-compliance            (register — the UI swallows it as "No audits")
  - GET /api/audit-compliance/{id}       (open the audit)
  - GET /api/audit-compliance/my-checkpoints
  - POST .../conduct + .../submit        (EXECUTE — conducting the audit)
and the sidebar hid "Audits" + "My Checkpoints" (both gate on
AUDIT_COMPLIANCE.READ).

Grants (mirrors the ROLE_GRANTS edit in safeops_360/prisma/seed-rbac.ts):
  CAMS_ADMIN / AUDIT_MANAGER : full lifecycle @ ALL_PLANTS
  LEAD_AUDITOR               : CREATE/READ/UPDATE/EXECUTE/VERIFY/CLOSE/EXPORT @ OWN_PLANT
                               (no APPROVE — plant-manager review is the SoD counterparty)
  AUDITOR                    : READ/EXECUTE/EXPORT @ OWN_PLANT

Idempotent (no RolePermission wipe). Run from the backend root:
    .venv/Scripts/python.exe scripts/grant_audit_compliance_cams_auditors.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.user import Permission, Role, RolePermission

FULL = ["CREATE", "READ", "UPDATE", "APPROVE", "EXECUTE", "VERIFY", "CLOSE", "EXPORT"]
LEAD = ["CREATE", "READ", "UPDATE", "EXECUTE", "VERIFY", "CLOSE", "EXPORT"]
FIELD = ["READ", "EXECUTE", "EXPORT"]

# role_code -> (actions, scope)
GRANTS: dict[str, tuple[list[str], str]] = {
    "CAMS_ADMIN": (FULL, "ALL_PLANTS"),
    "AUDIT_MANAGER": (FULL, "ALL_PLANTS"),
    "LEAD_AUDITOR": (LEAD, "OWN_PLANT"),
    "AUDITOR": (FIELD, "OWN_PLANT"),
}


async def main() -> int:
    async with AsyncSessionLocal() as db:
        codes = {f"AUDIT_COMPLIANCE.{a}" for actions, _ in GRANTS.values() for a in actions}
        perms = {
            c: (await db.execute(select(Permission).where(Permission.code == c))).scalar_one_or_none()
            for c in sorted(codes)
        }
        missing = [c for c, p in perms.items() if p is None]
        if missing:
            print(f"Permission(s) missing — run seed-rbac first: {missing}")
            return 1

        added = 0
        for role_code, (actions, scope) in GRANTS.items():
            role = (await db.execute(select(Role).where(Role.code == role_code))).scalar_one_or_none()
            if role is None:
                print(f"  role {role_code} not found — skip")
                continue
            for action in actions:
                perm = perms[f"AUDIT_COMPLIANCE.{action}"]
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
                    print(f"  + {role_code}: AUDIT_COMPLIANCE.{action} @ {scope}")
                elif rp.scope != scope:
                    rp.scope = scope
                    print(f"  ~ {role_code}: AUDIT_COMPLIANCE.{action} scope -> {scope}")
                else:
                    print(f"  = {role_code}: AUDIT_COMPLIANCE.{action} @ {scope} (exists)")
        await db.commit()
        print(f"\nGranted {added} new RolePermission row(s).")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
