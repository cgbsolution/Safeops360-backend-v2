"""Six demo accounts on the AUDITEE role, usable at any plant.

The auditee is the only party who can say WHY a non-conformity happened and WHAT
will be done about it, so a demo needs several of them — one per department in
scope, with spares. These are `@safeops360.in` demo identities; the real
`@jockeyindia.com` auditors are reserved for the client's PoC and must never be
consumed by a practice audit (naming one makes them a declared auditee and the
independence engine then blocks them from their own programme).

**Why the role is AUDITEE and nothing else.** SUPERVISOR, DEPARTMENT_HEAD and
SAFETY_OFFICER can act as auditees too, but they carry unrelated grants from
their day job. AUDITEE carries exactly what the PIL/MR/F04-R1 green half needs
and nothing more:

    AUDIT_COMPLIANCE.READ      OWN_RECORDS   open the audits they are seated on
    AUDIT_COMPLIANCE.UPDATE    OWN_RECORDS   write the analysis + the actions
    CAPA.READ / UPDATE / EXECUTE OWN_RECORDS the CAPA behind their NC report
    CAMS.READ                  OWN_PLANT     the inspection register

**Any plant, by record not by residence.** `can()` evaluates OWN_RECORDS on
record membership and never on plant, so an auditee named on an audit at any
site can already act there. Each account is still given a home plant because
`User.plantId` is NOT NULL and the directory groups by it — but that home plant
does not limit them, and `list_audits` no longer applies a plant bound to a
party-filtered reader.

Idempotent: re-running updates names and passwords rather than duplicating.

    python scripts/seed_auditee_demo_users.py            # dry run
    python scripts/seed_auditee_demo_users.py --commit
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bcrypt  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import get_settings  # noqa: E402

PASSWORD = "demo123"
ROLE_CODE = "AUDITEE"

# Six auditees: the three departments a PAGE_IMS audit covers, plus three spares
# so a second concurrent audit does not have to reuse the same people (reuse is
# what creates the independence collisions).
AUDITEES = [
    ("Kavya Suresh",   "auditee.hr@safeops360.in",         "Human Resources"),
    ("Imran Sheikh",   "auditee.admin@safeops360.in",      "Administration"),
    ("Neha Bhandari",  "auditee.ohc@safeops360.in",        "Occupational Health Centre"),
    ("Vikram Chauhan", "auditee.production@safeops360.in", "Production"),
    ("Farida Qureshi", "auditee.quality@safeops360.in",    "Quality"),
    ("Joseph Mathew",  "auditee.stores@safeops360.in",     "Stores"),
]


def main(commit: bool) -> None:
    engine = create_engine(get_settings().sync_database_url, pool_pre_ping=True)
    pw_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()

    with engine.begin() as conn:
        role = conn.execute(
            text('SELECT id, name FROM "Role" WHERE code = :c'), {"c": ROLE_CODE}
        ).first()
        if role is None:
            raise SystemExit(
                f"Role {ROLE_CODE} is not seeded. Run the RBAC seed first — this "
                f"script deliberately does not invent a role, because a role with "
                f"no grants looks identical to one whose grants were dropped."
            )
        role_id, role_name = role
        grants = conn.execute(
            text(
                'SELECT p.code, rp.scope FROM "RolePermission" rp '
                'JOIN "Permission" p ON p.id = rp."permissionId" '
                'WHERE rp."roleId" = :r ORDER BY p.code'
            ),
            {"r": role_id},
        ).all()
        print(f'Role "{role_name}" ({ROLE_CODE}) — {len(grants)} grant(s):')
        for code, scope in grants:
            print(f"    {code:34} {scope}")
        print()

        # Home plant. Not a restriction — see the module docstring — but the
        # column is NOT NULL and the people picker groups by it.
        plant = conn.execute(
            text('SELECT id, code, name FROM "Plant" WHERE code = :c'), {"c": "NW"}
        ).first()
        if plant is None:
            plant = conn.execute(
                text('SELECT id, code, name FROM "Plant" ORDER BY code LIMIT 1')
            ).first()
        plant_id, plant_code, plant_name = plant
        print(f"Home plant: {plant_code} — {plant_name}\n")

        created = updated = 0
        for name, email, dept in AUDITEES:
            existing = conn.execute(
                text('SELECT id FROM "User" WHERE lower(email) = lower(:e)'), {"e": email}
            ).first()
            if existing:
                print(f"  update  {name:16} {email:36} {dept}")
                updated += 1
                if commit:
                    conn.execute(
                        text(
                            'UPDATE "User" SET name = :n, role = :r, "plantId" = :p, '
                            '"passwordHash" = :h, department = :d, '
                            "designation = 'Auditee', \"rosterStatus\" = 'active' "
                            "WHERE id = :i"
                        ),
                        {"n": name, "r": ROLE_CODE, "p": plant_id, "h": pw_hash,
                         "d": dept, "i": existing[0]},
                    )
                    user_id = existing[0]
                else:
                    user_id = None
            else:
                user_id = uuid.uuid4().hex
                print(f"  create  {name:16} {email:36} {dept}")
                created += 1
                if commit:
                    conn.execute(
                        text(
                            'INSERT INTO "User" (id, name, email, role, "plantId", '
                            '"passwordHash", department, designation, "rosterStatus", "createdAt") '
                            "VALUES (:i, :n, :e, :r, :p, :h, :d, 'Auditee', 'active', now())"
                        ),
                        {"i": user_id, "n": name, "e": email, "r": ROLE_CODE,
                         "p": plant_id, "h": pw_hash, "d": dept},
                    )

            # UserRole is what the permission snapshot actually reads; the
            # `User.role` text column alone grants nothing.
            if commit and user_id:
                has = conn.execute(
                    text('SELECT 1 FROM "UserRole" WHERE "userId" = :u AND "roleId" = :r'),
                    {"u": user_id, "r": role_id},
                ).first()
                if not has:
                    conn.execute(
                        text(
                            'INSERT INTO "UserRole" (id, "userId", "roleId", "validFrom", "assignedAt") '
                            "VALUES (:i, :u, :r, now(), now())"
                        ),
                        {"i": uuid.uuid4().hex, "u": user_id, "r": role_id},
                    )

        print()
        if commit:
            print(f"Committed. {created} created, {updated} updated. Password: {PASSWORD}")
        else:
            print(f"DRY RUN — nothing written. {created} would be created, "
                  f"{updated} updated. Re-run with --commit.")


if __name__ == "__main__":
    main("--commit" in sys.argv)
