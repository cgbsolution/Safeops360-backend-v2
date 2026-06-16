"""Read-only sanity check for the incident access filter.

Confirms what INCIDENT.READ scope the logged-in user's role(s) carry and how
many incidents the new filter would show vs the global total.

    .venv/Scripts/python.exe scripts/check_incident_scope.py [userEmailOrName]
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, text

from app.core.config import get_settings

WHO = sys.argv[1] if len(sys.argv) > 1 else "Lalit Nair"


def main() -> int:
    eng = create_engine(get_settings().sync_database_url, future=True)
    with eng.connect() as c:
        u = c.execute(
            text('SELECT id, name, email, role, "plantId", department '
                 'FROM "User" WHERE name = :w OR email = :w LIMIT 1'),
            {"w": WHO},
        ).mappings().first()
        if not u:
            print(f"!! no user matching {WHO!r}")
            return 1
        print(f"User: {u['name']} <{u['email']}>  role={u['role']}  "
              f"plantId={u['plantId']}  department={u['department']}")

        scopes = c.execute(
            text(
                'SELECT DISTINCT rp.scope '
                'FROM "UserRole" ur '
                'JOIN "Role" r ON r.id = ur."roleId" AND r."isActive" = true '
                'JOIN "RolePermission" rp ON rp."roleId" = r.id '
                'JOIN "Permission" p ON p.id = rp."permissionId" '
                'WHERE ur."userId" = :uid AND p.code = :perm '
                'AND (ur."validTo" IS NULL OR ur."validTo" > now())'
            ),
            {"uid": u["id"], "perm": "INCIDENT.READ"},
        ).scalars().all()
        print(f"INCIDENT.READ scopes: {scopes or '(none — would see nothing)'}")

        total = c.execute(text('SELECT count(*) FROM "Incident"')).scalar_one()
        at_plant = c.execute(
            text('SELECT count(*) FROM "Incident" WHERE "plantId" = :p'),
            {"p": u["plantId"]},
        ).scalar_one()
        own = c.execute(
            text('SELECT count(*) FROM "Incident" WHERE "reporterId" = :uid'),
            {"uid": u["id"]},
        ).scalar_one()

        print(f"\nIncidents — total={total}  at_user_plant={at_plant}  reported_by_user={own}")
        if "ALL_PLANTS" in scopes:
            print("=> Filter result: ALL (unrestricted)")
        elif "OWN_PLANT" in scopes:
            print(f"=> Filter result: {at_plant} (their plant)   [was showing {total}]")
        elif "OWN_RECORDS" in scopes:
            print(f"=> Filter result: {own} (their own)          [was showing {total}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
