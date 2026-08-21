"""Give HSE_MANAGER and LEAD_AUDITOR the same audit reach on a live database.

Mirrors `SafeOps360/prisma/seed-rbac.ts` onto environments that were seeded
before that edit. Fresh environments get it from the seed; existing ones need
this.

    HSE_MANAGER   AUDIT_COMPLIANCE.*   OWN_PLANT -> ALL_PLANTS
    HSE_MANAGER   CAMS.*               OWN_PLANT -> ALL_PLANTS
    LEAD_AUDITOR  AUDIT_COMPLIANCE.*   -> ALL_PLANTS, and GAINS SCHEDULE
    LEAD_AUDITOR  CAMS.*               OWN_PLANT -> ALL_PLANTS

**Why the reach changes.** `OWN_PLANT` resolves to the plants a person is
SEATED at, not the plants their role covers. An HSE Manager holding a NW + SW
seat therefore saw two of twenty-eight sites in the Owning-site picker, and
could not schedule an audit anywhere else. Both roles own the audit programme
across sites, so both are ALL_PLANTS. It stores no plant list, so a site added
next year is covered without another RBAC change.

**Why LEAD_AUDITOR gains SCHEDULE.** Its own seed comment already read "Audit
engine: schedules…" while the grant list omitted it, and the Audits screen gates
its "+ Schedule Audit" button on exactly that permission — so a Lead Auditor
could not raise the audits they are the named owner of, with no error to explain
it. The CAMS grant always included SCHEDULE, so the two engines disagreed about
the same role.

**What is deliberately NOT equalised.** LEAD_AUDITOR still has no
`AUDIT_COMPLIANCE.APPROVE`. Plant-manager review of auditee responses is the
segregation-of-duties counterparty to the lead auditor; reach is about WHERE
someone may work, APPROVE is about who signs off on whom, and widening the first
is no reason to collapse the second. That is the "almost" in "almost the same".

Idempotent. Dry run by default.

    python scripts/align_audit_role_scopes.py
    python scripts/align_audit_role_scopes.py --apply
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import get_settings  # noqa: E402

ROLES = ("HSE_MANAGER", "LEAD_AUDITOR")
MODULES = ("AUDIT_COMPLIANCE", "CAMS")
TARGET = "ALL_PLANTS"

# Permissions a role must hold, granted at TARGET if absent. APPROVE is
# deliberately absent for LEAD_AUDITOR — see the module docstring.
REQUIRED = {"LEAD_AUDITOR": ("AUDIT_COMPLIANCE.SCHEDULE",)}


def main(apply: bool) -> None:
    engine = create_engine(get_settings().sync_database_url, pool_pre_ping=True)
    widened = granted = 0

    with engine.begin() as conn:
        for role_code in ROLES:
            role = conn.execute(
                text('SELECT id FROM "Role" WHERE code = :c'), {"c": role_code}
            ).first()
            if role is None:
                print(f"!! role {role_code} not found — skipped")
                continue
            role_id = role[0]

            rows = conn.execute(
                text(
                    'SELECT p.code, rp.scope FROM "RolePermission" rp '
                    'JOIN "Permission" p ON p.id = rp."permissionId" '
                    'WHERE rp."roleId" = :r AND (p.code LIKE \'AUDIT_COMPLIANCE.%\' '
                    "OR p.code LIKE 'CAMS.%') ORDER BY p.code"
                ),
                {"r": role_id},
            ).all()
            held = {c for c, _ in rows}
            narrow = [c for c, s in rows if s != TARGET]

            print(f"\n{'=' * 70}\n{role_code}: {len(rows)} grant(s), {len(narrow)} narrower than {TARGET}")
            for code, scope in rows:
                mark = "->" if scope != TARGET else "  "
                print(f"   {mark} {code:34} {scope}")

            if narrow and apply:
                n = conn.execute(
                    text(
                        'UPDATE "RolePermission" rp SET scope = :t FROM "Permission" p '
                        'WHERE p.id = rp."permissionId" AND rp."roleId" = :r '
                        "AND (p.code LIKE 'AUDIT_COMPLIANCE.%' OR p.code LIKE 'CAMS.%') "
                        "AND rp.scope <> :t"
                    ),
                    {"r": role_id, "t": TARGET},
                ).rowcount
                widened += n
            else:
                widened += len(narrow)

            for code in REQUIRED.get(role_code, ()):
                if code in held:
                    print(f"      {code} already granted")
                    continue
                perm = conn.execute(
                    text('SELECT id FROM "Permission" WHERE code = :c'), {"c": code}
                ).first()
                if perm is None:
                    print(f"   !! permission {code} is not seeded — skipped")
                    continue
                print(f"   ++ {code:34} GRANT at {TARGET}")
                granted += 1
                if apply:
                    conn.execute(
                        text(
                            'INSERT INTO "RolePermission" (id, "roleId", "permissionId", scope) '
                            "VALUES (:i, :r, :p, :t) ON CONFLICT DO NOTHING"
                        ),
                        {"i": uuid.uuid4().hex, "r": role_id, "p": perm[0], "t": TARGET},
                    )

    print(f"\n{'=' * 70}")
    if apply:
        print(f"Applied: {widened} scope change(s), {granted} new grant(s).")
        print("The API caches each user's permissions for 5 minutes and holds that")
        print("cache per process — restart the backend, or wait, before testing.")
        print("Users must also sign out and back in for a fresh session scope.")
    else:
        print(f"DRY RUN — {widened} scope change(s) and {granted} new grant(s) pending.")
        print("Re-run with --apply.")


if __name__ == "__main__":
    main("--apply" in sys.argv)
