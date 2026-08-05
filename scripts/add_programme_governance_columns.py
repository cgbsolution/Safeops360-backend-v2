"""Programme governance columns - additive DDL for the creation/approval UI.

Three columns the governance surface needs and `add_programme_tables.py` did
not create:

  ProgrammeCycle.submittedByUserId     four-eyes on the pair that matters
  ProgrammeCycle.activatedAt           APPROVED -> ACTIVE, the step to closable
  ProgrammeReview.resultingAmendmentIds  which amendments this review DECIDED

**Run this before restarting uvicorn.** A mapped column with no DDL behind it
does not fail on the write that needs it - it fails on EVERY query against the
table, because SQLAlchemy names every column in its SELECT. Until this runs,
`GET /api/programme` 500s.

Why `submittedByUserId` exists at all: the approval guard already refused to let
the programme OWNER approve their own cycle, but the person who prepared and
submitted the plan was anonymous. A delegate could submit a cycle they did not
own and then approve their own submission - four eyes on paper, two in practice.

Additive + re-runnable. Never `prisma db push` (known Cams* drift would drop
hand-DDL tables).

    .venv/Scripts/python.exe scripts/add_programme_governance_columns.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

STMTS: list[str] = [
    'ALTER TABLE "ProgrammeCycle" ADD COLUMN IF NOT EXISTS "submittedByUserId" TEXT',
    'ALTER TABLE "ProgrammeCycle" ADD COLUMN IF NOT EXISTS "activatedAt" TIMESTAMPTZ',
    """
    ALTER TABLE "ProgrammeReview"
      ADD COLUMN IF NOT EXISTS "resultingAmendmentIds" JSONB NOT NULL DEFAULT '[]'::jsonb
    """,
]

VERIFY = """
SELECT table_name, column_name
FROM information_schema.columns
WHERE (table_name = 'ProgrammeCycle'
       AND column_name IN ('submittedByUserId', 'activatedAt'))
   OR (table_name = 'ProgrammeReview' AND column_name = 'resultingAmendmentIds')
ORDER BY table_name, column_name
"""


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        for stmt in STMTS:
            s.execute(text(stmt))
        s.commit()
        found = [(r[0], r[1]) for r in s.execute(text(VERIFY))]

    for table, col in found:
        print(f"  ok  {table}.{col}")
    missing = 3 - len(found)
    if missing:
        print(f"\nFAILED - {missing} column(s) missing after the ALTERs.")
        return 1
    print("\nAll three columns present. Restart uvicorn.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
