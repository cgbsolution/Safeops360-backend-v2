"""WP-49 - the audit type becomes the configuration home (additive DDL).

  CamsAuditType."scoringRules"           per-type pass mark + critical gate (F-22)
  CamsAuditType."regimeCode"             buyer-regime vocabulary (WP-47)
  CamsAuditType."competenceEnforcement"  WARN | BLOCK (WP-36)

`MINIMUM_PASS_SCORE = 80.0` was a module constant applied to every audit of
every type while `AuditTemplate.scoring` sat unused. Scoring policy belongs to
the type: a fire-equipment inspection and an SA8000 social audit do not share a
pass mark. NULL scoringRules falls back to the historic constant, so nothing
changes for existing types until someone configures them.

Additive + re-runnable. Never `prisma db push`.

    .venv/Scripts/python.exe scripts/add_audit_type_config.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

STMTS = [
    'ALTER TABLE "CamsAuditType" ADD COLUMN IF NOT EXISTS "scoringRules" JSONB',
    'ALTER TABLE "CamsAuditType" ADD COLUMN IF NOT EXISTS "regimeCode" TEXT',
    'ALTER TABLE "CamsAuditType" ADD COLUMN IF NOT EXISTS "competenceEnforcement" '
    "TEXT NOT NULL DEFAULT 'WARN'",
    'CREATE INDEX IF NOT EXISTS "CamsAuditType_regimeCode_idx" ON "CamsAuditType" ("regimeCode")',
]

COLUMNS = [
    ("CamsAuditType", "scoringRules"),
    ("CamsAuditType", "regimeCode"),
    ("CamsAuditType", "competenceEnforcement"),
]


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    failures = 0
    with Session(engine) as s:
        for stmt in STMTS:
            s.execute(text(stmt))
        s.commit()
        print("-- verification -----------------------------")
        for tbl, col in COLUMNS:
            ok = bool(s.execute(text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                "AND table_name=:t AND column_name=:c"), {"t": tbl, "c": col}).first())
            print(f"  column {tbl}.{col:<24} {'present' if ok else 'MISSING'}")
            failures += 0 if ok else 1
        n = s.execute(text(
            'SELECT count(*) FROM "CamsAuditType" WHERE "requiresAuditorCompetency" = \'[]\'::jsonb'
        )).scalar_one()
        total = s.execute(text('SELECT count(*) FROM "CamsAuditType"')).scalar_one()
        print(f"\n  {n} of {total} audit type(s) still declare NO required competencies.")
        print("  The WP-36 competence check no-ops for those until they are configured.")
    print("\nDONE" if not failures else f"\nDONE with {failures} MISSING")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
