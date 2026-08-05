"""WP-01 + WP-07 - cleanse the demo tenant.

Two problems, one script, because the second depends on the first:

  **WP-01 fixture purge (F-7, F-23, F-51)** - 10 of 18 audits are titled `Test`,
  `test 2`, `Demo 123`, `Test Audit PP`. A client opening the audit register sees
  a majority of junk. Q14 answered: **soft-delete**, not hard-delete - several
  carry real checkpoint responses, and `ComplianceAudit` is a governed
  soft-delete entity, so the platform's own guard forbids hard deletion anyway.

  **WP-07 allocation cleanse (F-36)** - checkpoints allocated to role-implausible
  owners (the diagnosis found an insurance manager owning 513 audit checkpoints).
  Going forward `allocate_checkpoints` blocks the same-engagement dual role, but
  it cannot judge plausibility, so historic rows need clearing.

The scale-demo library is deactivated rather than deleted: `AUD-SD1-2026-NW-0012`
(1,500 checkpoints) is genuinely useful for internal scale testing, it just must
not appear in a client-facing picker.

Idempotent. Dry run by default.

    .venv/Scripts/python.exe scripts/cleanse_demo_tenant.py            # dry run
    .venv/Scripts/python.exe scripts/cleanse_demo_tenant.py --commit

WARNING: the backend .env points at PRODUCTION.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.audit_compliance import (
    AuditCheckpointLibrary,
    AuditCheckpointResponse,
    ComplianceAudit,
)
from app.models.user import User

# Titles that mark an audit as fixture data. Anchored to the START of the title
# so a genuine audit called "Protest march contingency" is not caught.
#
# NOTE: Postgres ARE uses \y for a word boundary, NOT \b (which is a backspace
# outside brackets). Using \b here silently matched nothing and missed 6 of the
# 10 fixture audits on the first run.
FIXTURE_TITLE_SQL = r"""(
    title ~* '^\s*(test|demo)\y'
 OR title ~* '^\s*test\s*[0-9]'
 OR title ~* '^\s*demo\s*[0-9]'
)"""

# Roles that could plausibly OWN an audit finding on a shop floor. Anyone
# outside this set holding checkpoint ownership is the F-36 defect.
PLAUSIBLE_OWNER_ROLES = {
    "PLANT_HSE_HEAD", "HSE_MANAGER", "SAFETY_OFFICER", "CORPORATE_HSE",
    "PLANT_HEAD", "PRODUCTION_MANAGER", "MAINTENANCE_MANAGER", "AUDIT_MANAGER",
    "LEAD_AUDITOR", "AUDITOR", "COMPLIANCE_OFFICER", "ADMIN", "SUPER_ADMIN",
    "QUALITY_MANAGER", "ENGINEERING_MANAGER", "STORE_MANAGER", "HR_MANAGER",
}


def main(commit: bool) -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    now = datetime.now(timezone.utc)

    with Session(engine) as s:
        # ---- WP-01: fixture audits -----------------------------------
        fixtures = list(
            s.execute(
                select(ComplianceAudit).where(
                    ComplianceAudit.isDeleted.is_(False),
                    text(FIXTURE_TITLE_SQL),
                )
            ).scalars().all()
        )
        print(f"-- WP-01: {len(fixtures)} fixture-titled audit(s) ----------")
        for a in fixtures:
            n = s.execute(
                text('SELECT count(*) FROM "AuditCheckpointResponse" WHERE "auditId"=:i'),
                {"i": a.id},
            ).scalar_one()
            print(f"   soft-delete {a.auditNumber:<22} {a.title!r:<26} ({n} checkpoints, {a.status})")
            if commit:
                a.isDeleted = True
                a.deletedAt = now
                a.deletedBy = "cleanse_demo_tenant"
                a.deletionReason = "Fixture/test data removed from the client-facing register (WP-01)"

        # ---- WP-01b: gate the scale-demo library ---------------------
        libs = list(
            s.execute(
                select(AuditCheckpointLibrary).where(
                    AuditCheckpointLibrary.isActive.is_(True),
                    AuditCheckpointLibrary.industryName.ilike("%scale demo%"),
                )
            ).scalars().all()
        )
        print(f"\n-- WP-01b: {len(libs)} scale-demo librar(y/ies) -----------")
        for lib in libs:
            print(f"   deactivate {lib.industryCode} ({lib.checkpointCount} checkpoints)")
            print("      kept for internal scale testing; hidden from the Schedule picker")
            if commit:
                lib.isActive = False

        # ---- WP-07: implausible checkpoint allocation ----------------
        rows = s.execute(
            text(
                '''
                SELECT r."assignedOwnerId", u.name, u.role, u.designation, count(*) AS n
                FROM "AuditCheckpointResponse" r
                JOIN "User" u ON u.id = r."assignedOwnerId"
                WHERE r."assignedOwnerId" IS NOT NULL
                GROUP BY 1,2,3,4 ORDER BY n DESC
                '''
            )
        ).all()
        print(f"\n-- WP-07: checkpoint ownership by person ------------------")

        # REASSIGN, don't clear. WP-07's acceptance criteria is two-part:
        # "checkpoints are allocated to role-plausible owners" AND "the demo HSE
        # Manager login has a non-empty My Checkpoints". Nulling ownership
        # satisfies the first and breaks the second — an empty inbox is a worse
        # demo than a wrong one. So each implausible owner's checkpoints move to
        # a plausible owner AT THE SAME PLANT, round-robin by discipline so one
        # person does not inherit all 513.
        bad = [(uid, name, role, desig, n) for uid, name, role, desig, n in rows
               if role not in PLAUSIBLE_OWNER_ROLES]
        good_by_plant: dict[str, list[User]] = {}
        for u in s.execute(
            select(User).where(User.role.in_(sorted(PLAUSIBLE_OWNER_ROLES)))
        ).scalars().all():
            if u.plantId:
                good_by_plant.setdefault(u.plantId, []).append(u)

        moved = orphaned = 0
        for uid, name, role, desig, n in rows:
            if role in PLAUSIBLE_OWNER_ROLES:
                print(f"   keep  {name:<22} {role:<20} {n:>5} checkpoints  ({desig or '-'})")
                continue

            # Which plants do this person's checkpoints sit in?
            plants = [
                r[0] for r in s.execute(
                    text('''SELECT DISTINCT "plantId" FROM "AuditCheckpointResponse"
                            WHERE "assignedOwnerId"=:u AND "plantId" IS NOT NULL'''),
                    {"u": uid},
                ).all()
            ]
            targets = [t for p in plants for t in good_by_plant.get(p, [])]
            if not targets:
                orphaned += n
                print(f"   CLEAR {name:<22} {role:<20} {n:>5} -> no plausible owner at their plant")
                if commit:
                    s.execute(
                        text('''UPDATE "AuditCheckpointResponse" SET "assignedOwnerId"=NULL
                                WHERE "assignedOwnerId"=:u'''),
                        {"u": uid},
                    )
                continue

            names = ", ".join(sorted({t.name for t in targets})[:3])
            print(f"   MOVE  {name:<22} {role:<20} {n:>5} -> {names}"
                  f"{' +more' if len({t.name for t in targets}) > 3 else ''}")
            moved += n
            if commit:
                # Spread by discipline so the reassignment looks like a real
                # allocation rather than a bulk dump on one inbox.
                discs = [
                    r[0] for r in s.execute(
                        text('''SELECT DISTINCT "categoryId" FROM "AuditCheckpointResponse"
                                WHERE "assignedOwnerId"=:u'''),
                        {"u": uid},
                    ).all()
                ]
                for i, disc in enumerate(discs):
                    tgt = targets[i % len(targets)]
                    s.execute(
                        text('''UPDATE "AuditCheckpointResponse"
                                SET "assignedOwnerId"=:t,
                                    "routedToUserId"=CASE WHEN "routedToUserId"=:u
                                                     THEN :t ELSE "routedToUserId" END
                                WHERE "assignedOwnerId"=:u AND "categoryId"=:d'''),
                        {"t": tgt.id, "u": uid, "d": disc},
                    )

        print(f"\n   {moved} checkpoint(s) reassigned to plausible owners"
              f"{'' if commit else ' (planned)'}"
              + (f"; {orphaned} cleared (no candidate)" if orphaned else ""))

        if commit:
            s.commit()

        # ---- verification ------------------------------------------
        print("\n-- verification (each must be 0) -------------------------")
        checks = [
            ("fixture-titled audits still visible",
             f'SELECT count(*) FROM "ComplianceAudit" WHERE "isDeleted"=false AND {FIXTURE_TITLE_SQL}'),
            ("checkpoints still owned by an implausible role",
             'SELECT count(*) FROM "AuditCheckpointResponse" r JOIN "User" u '
             'ON u.id = r."assignedOwnerId" WHERE u.role NOT IN ('
             + ",".join(f"'{r}'" for r in sorted(PLAUSIBLE_OWNER_ROLES)) + ")"),
            ("active scale-demo libraries",
             '''SELECT count(*) FROM "AuditCheckpointLibrary"
                WHERE "isActive"=true AND "industryName" ILIKE '%scale demo%' '''),
        ]
        failures = 0
        for label, sql in checks:
            n = s.execute(text(sql)).scalar_one()
            print(f"   {n:>4}  {label}")
            failures += 0 if n == 0 else 1

        visible = s.execute(
            text('SELECT count(*) FROM "ComplianceAudit" WHERE "isDeleted"=false')
        ).scalar_one()
        print(f"\n   {visible} audit(s) remain in the client-facing register.")

    print("\nCOMMITTED." if commit else "\nDRY RUN - nothing written. Re-run with --commit.")
    return 1 if (commit and failures) else 0


if __name__ == "__main__":
    raise SystemExit(main("--commit" in sys.argv))
