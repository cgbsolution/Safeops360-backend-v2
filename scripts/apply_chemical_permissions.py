#!/usr/bin/env python
"""Seed the CHEMICAL.* permission set and its role grants — additively.

    python scripts/apply_chemical_permissions.py            # dry run
    python scripts/apply_chemical_permissions.py --commit

WHY NOT JUST RUN THE SEEDER
---------------------------
Both `prisma/seed-rbac.ts` and `app/seed/seed_rbac.py` DELETE every
RolePermission row and rebuild from their own matrix — and the two matrices have
drifted, so a full run also silently applies whatever else that seeder disagrees
with. On a live database that is a much larger change than "give chemical its own
codes". This script does only the chemical part: idempotent, additive, no wipe.
Same pattern and same rationale as scripts/apply_audit_allocate_permission.py.

A later full `seed-rbac` run produces the same state — the matrices in both
seeders were updated alongside this script, which is the point.

WHAT THIS TURNS ON
------------------
Until CHEMICAL.READ exists in this database, services/chemical_permissions.py
runs its migration ramp and every chemical route falls back to INCIDENT.READ /
INCIDENT.UPDATE. That is the live defect: WORKER and CONTRACTOR_WORKMAN hold
INCIDENT.READ at OWN_RECORDS, which get_accessible_plants_for widens to the whole
plant, so a contractor can read the site's complete hazmat inventory — every
chemical, every quantity, every storage location, and the threshold dashboard
saying which sit near a reportable regulatory limit.

Creating the permission rows is therefore the moment the ramp ends and the fix
lands. It is also the moment AUDITOR and LEAD_AUDITOR can finally read the
register, which they could not before because they hold no INCIDENT grant at all.

ORDER OF OPERATIONS
-------------------
Run this BEFORE or WITH the deploy that adds `chemical` to ROUTER_MODULE. Doing
it after leaves a window where the routes are licence-gated but still handing the
inventory to contractors behind that gate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.models.user import Permission, Role, RolePermission  # noqa: E402

MODULE = "CHEMICAL"

# Four codes, not nine. FIRE carries EXECUTE/VERIFY/APPROVE because its source
# sheets print a three-stage sign-off block; chemical has no such block, and the
# only distinctions its router makes are read / create / write / configure.
# No DELETE — chemical records are statutory and retire by status change.
PERMISSIONS: list[tuple[str, str]] = [
    ("READ", "Read the chemical register, inventory, ledger and regulatory-threshold dashboards"),
    ("CREATE", "Add a chemical to the master, a storage location, or an inventory item"),
    ("UPDATE", "Edit a chemical, upload an SDS, move / transfer / dispose stock, reconcile a count"),
    ("CONFIGURE", "Manage regulatory-threshold rules and the storage incompatibility matrix"),
]

# role -> (actions, scope). Mirrors ROLE_GRANTS in both seeders.
GRANTS: dict[str, tuple[list[str], str]] = {
    "ADMIN":                          (["CREATE", "READ", "UPDATE", "CONFIGURE"], "ALL_PLANTS"),
    "SYSTEM_ADMIN":                   (["CREATE", "READ", "UPDATE", "CONFIGURE"], "ALL_PLANTS"),
    "SUPER_ADMIN":                    (["CREATE", "READ", "UPDATE", "CONFIGURE"], "ALL_PLANTS"),
    "HSE_MANAGER":                    (["CREATE", "READ", "UPDATE", "CONFIGURE"], "ALL_PLANTS"),
    "CORPORATE_HSE":                  (["CREATE", "READ", "UPDATE", "CONFIGURE"], "ALL_PLANTS"),
    "SAFETY_OFFICER":                 (["CREATE", "READ", "UPDATE"], "OWN_PLANT"),
    "ENVIRONMENT_MANAGER":            (["CREATE", "READ", "UPDATE"], "OWN_PLANT"),
    "STORE_KEEPER":                   (["CREATE", "READ", "UPDATE"], "OWN_PLANT"),
    "SITE_HSE_MANAGER":               (["CREATE", "READ", "UPDATE"], "OWN_PLANT"),
    "PLANT_HEAD":                     (["READ"], "OWN_PLANT"),
    "PLANT_HSE_HEAD":                 (["READ"], "OWN_PLANT"),
    "DEPARTMENT_HEAD":                (["READ"], "OWN_PLANT"),
    "SUPERVISOR":                     (["READ"], "OWN_PLANT"),
    "MAINTENANCE_HEAD":               (["READ"], "OWN_PLANT"),
    "EMERGENCY_RESPONSE_COORDINATOR": (["READ"], "OWN_PLANT"),
    "OCCUPATIONAL_HEALTH_OFFICER":    (["READ"], "OWN_PLANT"),
    "INDUSTRIAL_HYGIENIST":           (["READ"], "OWN_PLANT"),
    "EXTERNAL_ASSESSOR":              (["READ"], "OWN_PLANT"),
    # The other half of the defect: these two verify chemical storage and could
    # not open the register at all, because they hold no INCIDENT grant.
    "AUDITOR":                        (["READ"], "OWN_PLANT"),
    "LEAD_AUDITOR":                   (["READ"], "OWN_PLANT"),
    "COMPLIANCE_OFFICER":             (["READ"], "ALL_PLANTS"),
    "EXECUTIVE_VIEWER":               (["READ"], "ALL_PLANTS"),
}

# Named so the dry run states the exclusion rather than leaving it implied —
# "WORKER is absent" and "WORKER was considered and excluded" look identical in
# a list that only shows what was granted. These are the roles that can read the
# hazmat inventory today and must not after this runs.
WITHHELD = ["WORKER", "CONTRACTOR_WORKMAN", "GATE_GUARD", "CONTRACTOR_COORDINATOR",
            "FIELD_TECHNICIAN", "TRAINER", "LD_MANAGER", "HR_HEAD"]

# Scope note: SUPERVISOR and DEPARTMENT_HEAD get OWN_PLANT rather than the
# OWN_DEPARTMENT that FIRE uses for them. OWN_DEPARTMENT does not resolve to
# anything app-wide today, so an OWN_DEPARTMENT grant here would read as
# "restricted" while actually removing the access they have right now.


def main(commit: bool) -> int:
    engine = create_engine(get_settings().sync_database_url, pool_pre_ping=True, future=True)
    with Session(engine) as session:
        perms: dict[str, Permission] = {}
        for action, description in PERMISSIONS:
            code = f"{MODULE}.{action}"
            perm = session.execute(
                select(Permission).where(Permission.code == code)
            ).scalar_one_or_none()
            if perm is None:
                print(f"+ permission {code}")
                perm = Permission(code=code, module=MODULE, action=action, description=description)
                session.add(perm)
                session.flush()  # need the id for the grants below
            else:
                print(f"= permission {code} already exists")
            perms[action] = perm

        added = fixed = 0
        for role_code, (actions, scope) in GRANTS.items():
            role = session.execute(
                select(Role).where(Role.code == role_code)
            ).scalar_one_or_none()
            if role is None:
                print(f"  ? role {role_code} not found — skipped")
                continue
            for action in actions:
                perm = perms[action]
                rp = session.execute(
                    select(RolePermission).where(
                        RolePermission.roleId == role.id,
                        RolePermission.permissionId == perm.id,
                    )
                ).scalar_one_or_none()
                if rp is None:
                    session.add(RolePermission(roleId=role.id, permissionId=perm.id, scope=scope))
                    added += 1
                elif rp.scope != scope:
                    print(f"  ~ {role_code}: {MODULE}.{action} scope {rp.scope} -> {scope}")
                    rp.scope = scope
                    fixed += 1
            print(f"  + {role_code}: {', '.join(actions)} @ {scope}")

        print(f"\nWithheld by design: {', '.join(WITHHELD)}")
        print(f"{added} grant(s) added, {fixed} scope(s) corrected.")

        if commit:
            session.commit()
            print("\nCommitted. The migration ramp in chemical_permissions.py is now OVER:")
            print("chemical routes ask CHEMICAL.* and no longer accept INCIDENT.READ.")
            print("Permission caches expire within 5 minutes, or call")
            print("POST /api/auth/permissions/invalidate to clear them now.")
            print("NOTE: chemical_rbac_seeded() is cached per process — restart the")
            print("API (or call reset_cache) so running workers stop using the ramp.")
        else:
            session.rollback()
            print("Dry run — nothing written. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="apply; otherwise dry run")
    raise SystemExit(main(ap.parse_args().commit))
