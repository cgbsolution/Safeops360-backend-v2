"""One-off: add the residualAutoCalculated column to HiraEntry.

Backs the "auto-calculate residual risk from controls (with override)" feature.
When True, the residual likelihood/severity are derived from the entry's existing
controls; when False it is a manual matrix pick; Null (legacy rows) is treated as
a manual override so their hand-set residual is never silently recomputed.

Additive only — ALTER TABLE ... ADD COLUMN IF NOT EXISTS. Nullable, no default,
so existing rows keep their behaviour. Raw DDL through the SYNC (psycopg2)
engine, same pattern as add_ptw_ppe_gate_columns.py, so we never risk touching
existing tables with prisma migrate. Re-runnable.

Run from the backend root:
    .venv/Scripts/python.exe scripts/add_hira_residual_autocalc_column.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

DDL: list[str] = [
    'ALTER TABLE "HiraEntry" ADD COLUMN IF NOT EXISTS "residualAutoCalculated" BOOLEAN',
]

CHECK = """
SELECT column_name FROM information_schema.columns
WHERE table_name = 'HiraEntry'
  AND column_name = 'residualAutoCalculated'
"""


def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, future=True)
    with Session(engine) as session:
        before = [r[0] for r in session.execute(text(CHECK))]
        print(f"Before: {before or 'column does not exist'}")
        for ddl in DDL:
            session.execute(text(ddl))
        session.commit()
        after = [r[0] for r in session.execute(text(CHECK))]
        print(f"After:  {after}")


if __name__ == "__main__":
    main()
