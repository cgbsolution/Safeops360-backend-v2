"""Create AUDIT_COMPLIANCE.ALLOCATE and grant it to the governance roles.

Why the permission exists
-------------------------
Allocating checkpoints (`POST /audit-compliance/{id}/allocate`) and re-seating a
live team (`PATCH /audit-compliance/{id}/team`) used to be gated on
AUDIT_COMPLIANCE.UPDATE. Both endpoints pass a record naming the lead auditor,
the plant manager and the creator — but `can()` only consults a record for an
OWN_RECORDS grant, and AUDITEE holds UPDATE at **ALL_PLANTS** by design
(`_scope_covers_plant` reads the auditee slot's permission to decide who may be
seated; OWN_RECORDS would stop an auditee being named on another unit's audit).

An ALL_PLANTS grant satisfies `can()` outright, so the record was never
evaluated: the audited party could reallocate the very disciplines under audit,
and recast the team, on any audit at any site. A scope cannot express "not the
people under audit". A separate permission can.

Who gets it
-----------
Everyone who legitimately decides who conducts what — mirroring each role's
existing AUDIT_COMPLIANCE scope so nobody's reach changes:

    ADMIN / SYSTEM_ADMIN / CAMS_ADMIN / AUDIT_MANAGER   ALL_PLANTS
    HSE_MANAGER / LEAD_AUDITOR                          ALL_PLANTS
    PLANT_HEAD / QUALITY_MANAGER / INTERNAL_AUDIT_LEAD  OWN_PLANT

Deliberately NOT granted: AUDITOR and AUDITEE (the two parties under or
performing the audit), nor the auditee-class roles that hold UPDATE at
OWN_RECORDS — WORKER, SUPERVISOR, SAFETY_OFFICER, DEPARTMENT_HEAD.

RUN THIS BEFORE OR WITH THE DEPLOY. The endpoints now require ALLOCATE; until
the permission exists in this database, nobody can allocate or edit a team.

Mirrors the ROLE_GRANTS edit in SafeOps360/prisma/seed-rbac.ts — a full
`seed-rbac` run produces the same state. Idempotent, no RolePermission wipe.

    python scripts/apply_audit_allocate_permission.py            # dry run
    python scripts/apply_audit_allocate_permission.py --commit
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.models.user import Permission, Role, RolePermission  # noqa: E402

CODE = "AUDIT_COMPLIANCE.ALLOCATE"
DESCRIPTION = "Allocate checkpoints to auditors / auditees and re-seat a live audit team"

GRANTS: dict[str, str] = {
    "ADMIN": "ALL_PLANTS",
    "SYSTEM_ADMIN": "ALL_PLANTS",
    "CAMS_ADMIN": "ALL_PLANTS",
    "AUDIT_MANAGER": "ALL_PLANTS",
    "HSE_MANAGER": "ALL_PLANTS",
    "LEAD_AUDITOR": "ALL_PLANTS",
    "PLANT_HEAD": "OWN_PLANT",
    "QUALITY_MANAGER": "OWN_PLANT",
    "INTERNAL_AUDIT_LEAD": "OWN_PLANT",
}

# Named so the dry run states the exclusion rather than leaving it implied —
# "AUDITEE is absent" and "AUDITEE was considered and excluded" look identical
# in a list that only shows what was granted.
WITHHELD = ["AUDITOR", "AUDITEE", "WORKER", "SUPERVISOR", "SAFETY_OFFICER", "DEPARTMENT_HEAD"]


def main(commit: bool) -> int:
    engine = create_engine(get_settings().sync_database_url, pool_pre_ping=True, future=True)
    with Session(engine) as session:
        perm = session.execute(
            select(Permission).where(Permission.code == CODE)
        ).scalar_one_or_none()
        if perm is None:
            print(f"+ permission {CODE}")
            perm = Permission(
                code=CODE, module="AUDIT_COMPLIANCE", action="ALLOCATE", description=DESCRIPTION
            )
            session.add(perm)
            session.flush()  # need the id for the grants below
        else:
            print(f"= permission {CODE} already exists")

        added = fixed = 0
        for role_code, scope in GRANTS.items():
            role = session.execute(
                select(Role).where(Role.code == role_code)
            ).scalar_one_or_none()
            if role is None:
                print(f"  ? role {role_code} not found — skipped")
                continue
            rp = session.execute(
                select(RolePermission).where(
                    RolePermission.roleId == role.id,
                    RolePermission.permissionId == perm.id,
                )
            ).scalar_one_or_none()
            if rp is None:
                session.add(RolePermission(roleId=role.id, permissionId=perm.id, scope=scope))
                print(f"  + {role_code}: {CODE} @ {scope}")
                added += 1
            elif rp.scope != scope:
                print(f"  ~ {role_code}: scope {rp.scope} -> {scope}")
                rp.scope = scope
                fixed += 1
            else:
                print(f"  = {role_code}: already @ {scope}")

        print(f"\nWithheld by design: {', '.join(WITHHELD)}")
        print(f"{added} grant(s) added, {fixed} scope(s) corrected.")

        if commit:
            session.commit()
            print("Committed. Permission caches expire within 5 minutes, or call")
            print("POST /api/auth/permissions/invalidate to clear them now.")
        else:
            session.rollback()
            print("Dry run — nothing written. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="apply; otherwise dry run")
    raise SystemExit(main(ap.parse_args().commit))
