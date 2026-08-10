"""Seed the audit assignment pickers with a credible team at one plant.

The schedule wizard's four pickers are derived from RBAC scope (see
services/audit_assignment.py):

    Lead auditor / Co-auditor  -> AUDIT_COMPLIANCE.EXECUTE
    Plant manager (reviewer)   -> AUDIT_COMPLIANCE.APPROVE
    Auditee                    -> AUDIT_COMPLIANCE.UPDATE

Every slot already resolved to more than five people at Meridian North Works,
but almost all of them were ADMIN accounts holding the
permission incidentally, while the actual audit personas (Anjali Verma, Lead
Auditor) were absent because the CAMS roles carried no AUDIT_COMPLIANCE grant
at all. Run scripts/grant_audit_compliance_cams_auditors.py FIRST — that fixes
the role→permission half; this script only does the user→role half.

What it adds: a PLANT-scoped UserRole for a handful of people whose day job is
already the audit function, so each picker shows a sensible team rather than
the administrators. Nothing is removed and nobody's primary role changes.

Idempotent — re-running makes no further changes. Reverting is a matter of
deleting the UserRole rows it prints. Run from the backend root:
    .venv/Scripts/python.exe scripts/assign_audit_role_scopes.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.plant import Plant
from app.models.user import Role, User, UserRole

PLANT_CODE = "NW"

# (user name, role code to add). AUDITOR carries EXECUTE at OWN_PLANT, which is
# what the auditor pickers require; these three already do assurance work in
# other modules, so auditing is a natural extension rather than a new privilege.
ASSIGNMENTS: list[tuple[str, str]] = [
    ("Devendra Kulkarni", "AUDITOR"),      # Plant HSE Head — audits own site
    ("Ravi Menon", "AUDITOR"),             # Controls Tester — internal controls testing
    ("Nandini Subramaniam", "AUDITOR"),    # Compliance Officer — compliance audits
]


async def main() -> int:
    async with AsyncSessionLocal() as db:
        plant = (await db.execute(select(Plant).where(Plant.code == PLANT_CODE))).scalar_one_or_none()
        if plant is None:
            print(f"Plant {PLANT_CODE} not found")
            return 1
        print(f"Plant {PLANT_CODE} = {plant.id}\n")

        added = 0
        for name, role_code in ASSIGNMENTS:
            users = (await db.execute(select(User).where(User.name == name))).scalars().all()
            if len(users) != 1:
                print(f"  ! {name}: matched {len(users)} users — skipped (needs a unique name)")
                continue
            u = users[0]
            role = (await db.execute(select(Role).where(Role.code == role_code))).scalar_one_or_none()
            if role is None:
                print(f"  ! role {role_code} not found — skipped")
                continue
            existing = (
                await db.execute(
                    select(UserRole).where(UserRole.userId == u.id, UserRole.roleId == role.id)
                )
            ).scalar_one_or_none()
            if existing is not None:
                print(f"  = {name}: already holds {role_code}")
                continue
            db.add(UserRole(userId=u.id, roleId=role.id, scopeType="PLANT", scopeValue=plant.id))
            added += 1
            print(f"  + {name} ({u.role}): + {role_code} @ PLANT {PLANT_CODE}")

        await db.commit()
        print(f"\nAdded {added} UserRole row(s). Restart uvicorn to clear the permission cache.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
