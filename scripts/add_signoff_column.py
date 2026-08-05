"""WP-41 - engagement sign-off column (additive DDL).

`AuditReport.signOffs` already existed but was never written to. Sign-off
belongs on the ENGAGEMENT, because it gates CLOSURE - which happens before a
final report exists. The report snapshot freezes a copy at generation.

Additive + re-runnable. Never `prisma db push`.

    .venv/Scripts/python.exe scripts/add_signoff_column.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

STMTS = [
    'ALTER TABLE "ComplianceAudit" ADD COLUMN IF NOT EXISTS "signOffs" JSONB',
]


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        for stmt in STMTS:
            s.execute(text(stmt))
        s.commit()
        ok = bool(s.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
            "AND table_name='ComplianceAudit' AND column_name='signOffs'"
        )).first())
        print(f"  column ComplianceAudit.signOffs  {'present' if ok else 'MISSING'}")
    print("\nDONE" if ok else "\nFAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
