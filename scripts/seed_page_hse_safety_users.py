"""Six Page Industries accounts: two HSE Managers and four unit Safety Officers.

These are real `@jockeyindia.com` client identities for the Page PoC, not demo
fixtures. Both roles already exist and already carry their full grant set, so
this script assigns roles — it never invents or edits a role, because a role
with no grants looks identical to one whose grants were dropped.

    HSE_MANAGER      237 grants   Pavan Kumar K S, Ramesha Bhaskar
    SAFETY_OFFICER    75 grants   Safety Unit 14 / 17 / 20 / 28

**Cross-plant for the HSE Managers.** ~180 of HSE_MANAGER's 237 grants are
OWN_PLANT (HIRA, CAPA, PPE, PTW, training, inspections), so a home plant alone
would pin both managers to one site. Following the pattern in
`grant_hse_manager_cross_plant.py`, each gets a `UserRole(scopeType='PLANT')`
row for every Page Industries plant; `_load_user_snapshot()` unions those with
`User.plantId`, so their OWN_PLANT grants then resolve at every Page site and
the plant picker lists them. Deliberately NOT ALL_PLANTS — that would also
expose the Meridian Apparel and CGB demo tenants sharing this database.

**Home plants.** `User.plantId` is NOT NULL and drives OWN_PLANT scoping. All
four Safety Officers sit on `PAGE-INDUSTRIES-UNIT` ("Page Industries - Unit-20",
Tiptur) — the one Page unit that exists as a plant with a live factory profile.
Units 14, 17 and 28 have no Plant/FactoryProfile row of their own yet, so their
officers share Unit-20 rather than being scattered; the unit number lives in
their `designation` so they stay tellable apart in the people picker. When those
units are onboarded through the Add Factory Site picker, re-point `plantId` by
editing ACCOUNTS below and re-running — the script is idempotent. Do NOT
script-create the missing plants: that would break the enforced
plant-equals-factory 1:1 and mint orphan Plant rows.

The two HSE Managers stay on NW (Hassan), where every other @jockeyindia.com
account sits; their cross-plant scope reaches Unit-20 and the rest anyway.

Idempotent: re-running updates names, roles, plants and passwords rather than
duplicating, and skips cross-plant rows that already exist.

    python scripts/seed_page_hse_safety_users.py            # dry run
    python scripts/seed_page_hse_safety_users.py --commit
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402

PASSWORD = "demo123"

# Home plant for anyone whose own unit is not yet a plant. Every existing
# @jockeyindia.com account sits here.
FALLBACK_PLANT_CODE = "NW"

# (name, email, role code, department, designation, home plant code or None)
ACCOUNTS = [
    ("Pavan Kumar K S", "PavanKumar.KS@jockeyindia.com", "HSE_MANAGER",
     "HSE", "HSE Manager", None),
    ("Ramesha Bhaskar", "Ramesha.Bhaskar@jockeyindia.com", "HSE_MANAGER",
     "HSE", "HSE Manager", None),
    ("Safety Unit14", "Safety.Unit14@jockeyindia.com", "SAFETY_OFFICER",
     "HSE", "Safety Officer - Unit 14", "PAGE-INDUSTRIES-UNIT"),
    ("Safety Unit17", "Safety.Unit17@jockeyindia.com", "SAFETY_OFFICER",
     "HSE", "Safety Officer - Unit 17", "PAGE-INDUSTRIES-UNIT"),
    ("Safety Unit20", "Safety.Unit20@jockeyindia.com", "SAFETY_OFFICER",
     "HSE", "Safety Officer - Unit 20", "PAGE-INDUSTRIES-UNIT"),
    ("Safety Unit28", "Safety.Unit28@jockeyindia.com", "SAFETY_OFFICER",
     "HSE", "Safety Officer - Unit 28", "PAGE-INDUSTRIES-UNIT"),
]

# Only HSE Managers get cross-plant reach; Safety Officers stay on their unit.
CROSS_PLANT_ROLES = {"HSE_MANAGER"}


def main(commit: bool) -> None:
    engine = create_engine(get_settings().sync_database_url, pool_pre_ping=True)
    pw_hash = hash_password(PASSWORD)

    with engine.begin() as conn:
        # --- roles ------------------------------------------------------
        roles: dict[str, str] = {}
        for code in sorted({a[2] for a in ACCOUNTS}):
            row = conn.execute(
                text('SELECT id, name FROM "Role" WHERE code = :c'), {"c": code}
            ).first()
            if row is None:
                raise SystemExit(
                    f"Role {code} is not seeded. Run the RBAC seed first — this "
                    f"script deliberately does not invent a role."
                )
            roles[code] = row[0]
            n = conn.execute(
                text('SELECT count(*) FROM "RolePermission" WHERE "roleId" = :r'),
                {"r": row[0]},
            ).scalar()
            print(f'Role "{row[1]}" ({code}) - {n} grant(s)')
        print()

        # --- plants -----------------------------------------------------
        page_plants = conn.execute(
            text('SELECT id, code, name FROM "Plant" WHERE name ILIKE :p ORDER BY code'),
            {"p": "Page%"},
        ).all()
        if not page_plants:
            raise SystemExit("No Page Industries plants found - wrong database?")
        by_code = {code: pid for pid, code, _ in page_plants}

        fallback_id = by_code.get(FALLBACK_PLANT_CODE)
        if fallback_id is None:
            raise SystemExit(f"Fallback plant {FALLBACK_PLANT_CODE} not found.")
        print(f"Page Industries plants: {len(page_plants)} "
              f"(cross-plant scope for {'/'.join(sorted(CROSS_PLANT_ROLES))})")
        print(f"Fallback home plant: {FALLBACK_PLANT_CODE}\n")

        # --- accounts ---------------------------------------------------
        created = updated = scopes = 0
        for name, email, role_code, dept, designation, plant_code in ACCOUNTS:
            role_id = roles[role_code]
            plant_id = by_code.get(plant_code) if plant_code else None
            if plant_code and plant_id is None:
                raise SystemExit(f"Plant {plant_code} not found for {email}.")
            home_id = plant_id or fallback_id
            home_code = plant_code or FALLBACK_PLANT_CODE
            parked = "  + cross-plant" if role_code in CROSS_PLANT_ROLES else ""

            # Store the address lower-cased. `/api/auth/login` lower-cases the
            # typed email and then compares with a case-SENSITIVE `==` against
            # the stored column, and `User_email_key` is a plain btree, so a
            # mixed-case row is unreachable: the lookup 404s "User not found"
            # and the unique index does not even catch the duplicate. Every
            # pre-existing @jockeyindia.com row is lower-case for this reason.
            email = email.lower()

            existing = conn.execute(
                text('SELECT id FROM "User" WHERE lower(email) = :e'), {"e": email}
            ).first()
            params = {"n": name, "r": role_code, "p": home_id, "h": pw_hash,
                      "d": dept, "g": designation, "e": email}

            if existing:
                user_id = existing[0]
                print(f"  update  {name:16} {email:34} {role_code:15} {home_code}{parked}")
                updated += 1
                if commit:
                    conn.execute(
                        text(
                            'UPDATE "User" SET name = :n, email = :e, role = :r, '
                            '"plantId" = :p, "passwordHash" = :h, department = :d, '
                            "designation = :g, \"rosterStatus\" = 'active' WHERE id = :i"
                        ),
                        {**params, "i": user_id},
                    )
            else:
                user_id = uuid.uuid4().hex
                print(f"  create  {name:16} {email:34} {role_code:15} {home_code}{parked}")
                created += 1
                if commit:
                    conn.execute(
                        text(
                            'INSERT INTO "User" (id, name, email, role, "plantId", '
                            '"passwordHash", department, designation, "rosterStatus", "createdAt") '
                            "VALUES (:i, :n, :e, :r, :p, :h, :d, :g, 'active', now())"
                        ),
                        {**params, "i": user_id},
                    )

            if not commit:
                continue

            # UserRole is what the permission snapshot reads; the User.role text
            # column alone grants nothing. Base row = the role itself, unscoped.
            has = conn.execute(
                text('SELECT 1 FROM "UserRole" WHERE "userId" = :u AND "roleId" = :r '
                     'AND "scopeType" IS NULL'),
                {"u": user_id, "r": role_id},
            ).first()
            if not has:
                conn.execute(
                    text('INSERT INTO "UserRole" (id, "userId", "roleId", "validFrom", "assignedAt") '
                         "VALUES (:i, :u, :r, now(), now())"),
                    {"i": uuid.uuid4().hex, "u": user_id, "r": role_id},
                )

            # Cross-plant: one PLANT-scoped row per Page plant beyond home.
            if role_code in CROSS_PLANT_ROLES:
                for pid, _code, _pname in page_plants:
                    if pid == home_id:
                        continue  # home plant already reachable via User.plantId
                    dup = conn.execute(
                        text('SELECT 1 FROM "UserRole" WHERE "userId" = :u AND "roleId" = :r '
                             "AND \"scopeType\" = 'PLANT' AND \"scopeValue\" = :v"),
                        {"u": user_id, "r": role_id, "v": pid},
                    ).first()
                    if dup:
                        continue
                    conn.execute(
                        text('INSERT INTO "UserRole" (id, "userId", "roleId", "scopeType", '
                             '"scopeValue", "validFrom", "assignedAt") '
                             "VALUES (:i, :u, :r, 'PLANT', :v, now(), now())"),
                        {"i": uuid.uuid4().hex, "u": user_id, "r": role_id, "v": pid},
                    )
                    scopes += 1

        print()
        if commit:
            print(f"Committed. {created} created, {updated} updated, "
                  f"{scopes} cross-plant scope(s) added. Password: {PASSWORD}")
        else:
            print(f"DRY RUN - nothing written. {created} would be created, "
                  f"{updated} updated. Re-run with --commit.")


if __name__ == "__main__":
    main("--commit" in sys.argv)
