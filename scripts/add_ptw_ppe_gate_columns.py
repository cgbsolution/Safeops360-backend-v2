"""One-off: add the PPE-snapshot columns to PermitCrewMember (PPE-01 Pass 2).

Additive only — ALTER TABLE ... ADD COLUMN IF NOT EXISTS. Mirrors the fields
appended to the Prisma model and app/models/permit.py. Raw DDL through the
SYNC (psycopg2) engine, same pattern as create_ppe_tables.py, so we never
risk touching the existing tables with prisma migrate. Re-runnable.

Run from the backend root:
    .venv/Scripts/python.exe scripts/add_ptw_ppe_gate_columns.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

DDL: list[str] = [
    'ALTER TABLE "PermitCrewMember" ADD COLUMN IF NOT EXISTS "ppeValidAtIssuance" BOOLEAN',
    'ALTER TABLE "PermitCrewMember" ADD COLUMN IF NOT EXISTS "ppeValidationNotes" TEXT',
]

CHECK = """
SELECT column_name FROM information_schema.columns
WHERE table_name = 'PermitCrewMember'
  AND column_name IN ('ppeValidAtIssuance', 'ppeValidationNotes')
ORDER BY column_name
"""


def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, future=True)
    with Session(engine) as session:
        before = [r[0] for r in session.execute(text(CHECK))]
        print(f"Before: {before or 'neither column exists'}")
        for ddl in DDL:
            session.execute(text(ddl))
        session.commit()
        after = [r[0] for r in session.execute(text(CHECK))]
        print(f"After:  {after}")


if __name__ == "__main__":
    main()
