"""IndependenceEvent - the append-only record of what the guard decided.

Additive DDL for one table. Run before restarting uvicorn: the model is mapped,
so `GET /api/assurance/independence/events` 500s until this exists.

**Why the table exists.** The independence guard was correct, shared and
enforced at both call sites - and left no trace. `create_audit` raises
`ValueError` -> HTTP 400 and the transaction rolls back, so nothing written
inside it survives; preflight returned a verdict and wrote nothing at all. The
only durable evidence the guard existed was `IndependenceWaiver`, which records
the guard being OVERRIDDEN, and which had zero rows. A module whose strongest
claim is "we block conflicted auditors" could not produce one example of having
done so.

Append-only: nothing updates a row here. A revoked waiver writes a new BLOCKED
event rather than editing the WAIVED one, so the timeline reads forwards.

    .venv/Scripts/python.exe scripts/add_independence_event.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

STMTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS "IndependenceEvent" (
        "id"                 TEXT PRIMARY KEY,
        "occurredAt"         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "attemptedByUserId"  TEXT,
        "subjectUserId"      TEXT NOT NULL,
        "engagementKind"     TEXT NOT NULL,
        "engagementId"       TEXT,
        "engagementCode"     TEXT,
        "siteId"             TEXT,
        "outcome"            TEXT NOT NULL,
        "rule"               TEXT,
        "source"             TEXT,
        "reason"             TEXT NOT NULL DEFAULT '',
        "conflictDetail"     JSONB,
        "waiverId"           TEXT,
        "origin"             TEXT NOT NULL DEFAULT 'PREFLIGHT',
        "createdAt"          TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # The outcome vocabulary is closed. A typo'd outcome would silently vanish
    # from every filtered view rather than showing up wrong, which is the worse
    # failure for an evidence table.
    """
    DO $$ BEGIN
      ALTER TABLE "IndependenceEvent" ADD CONSTRAINT "ck_IndependenceEvent_outcome"
        CHECK ("outcome" IN ('BLOCKED','WARNED','WAIVED','CLEARED'));
    EXCEPTION WHEN duplicate_object THEN NULL; END $$
    """,
    'CREATE INDEX IF NOT EXISTS "ix_IndependenceEvent_occurredAt" ON "IndependenceEvent" ("occurredAt")',
    'CREATE INDEX IF NOT EXISTS "ix_IndependenceEvent_subject" ON "IndependenceEvent" ("subjectUserId")',
    'CREATE INDEX IF NOT EXISTS "ix_IndependenceEvent_attemptedBy" ON "IndependenceEvent" ("attemptedByUserId")',
    'CREATE INDEX IF NOT EXISTS "ix_IndependenceEvent_site" ON "IndependenceEvent" ("siteId")',
    'CREATE INDEX IF NOT EXISTS "ix_IndependenceEvent_outcomeCol" ON "IndependenceEvent" ("outcome")',
    'CREATE INDEX IF NOT EXISTS "ix_IndependenceEvent_waiver" ON "IndependenceEvent" ("waiverId")',
    'CREATE INDEX IF NOT EXISTS "ix_IndependenceEvent_subject_time" ON "IndependenceEvent" ("subjectUserId", "occurredAt")',
    'CREATE INDEX IF NOT EXISTS "ix_IndependenceEvent_outcome_time" ON "IndependenceEvent" ("outcome", "occurredAt")',
    'CREATE INDEX IF NOT EXISTS "ix_IndependenceEvent_engagement" ON "IndependenceEvent" ("engagementKind", "engagementId")',
]


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        for stmt in STMTS:
            s.execute(text(stmt))
        s.commit()

        present = s.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name='IndependenceEvent'"
            )
        ).first()
        cols = [
            r[0]
            for r in s.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='IndependenceEvent' ORDER BY column_name"
                )
            )
        ]
        ck = s.execute(
            text(
                "SELECT 1 FROM information_schema.table_constraints "
                "WHERE constraint_name='ck_IndependenceEvent_outcome'"
            )
        ).first()
        n = s.execute(text('SELECT count(*) FROM "IndependenceEvent"')).scalar_one()

    print(f"  table      IndependenceEvent   {'present' if present else 'MISSING'}")
    print(f"  columns    {len(cols)}  {', '.join(cols)}")
    print(f"  CHECK      ck_IndependenceEvent_outcome  {'present' if ck else 'MISSING'}")
    print(f"  rows       {n}")
    ok = bool(present) and len(cols) == 16 and bool(ck)
    print("\nDONE. Restart uvicorn." if ok else "\nFAILED - schema incomplete.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
