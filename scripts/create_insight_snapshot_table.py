"""One-off: create the InsightSnapshot table (Weekly Insight Engine, spec §2).

Backend-only table (not in schema.prisma) — reached only through FastAPI. The
lifecycle state machine and meta-insight promotion read prior weeks off it, so it
MUST exist before the weekly job runs or lifecycle can never advance past `new`.

Idempotent (CREATE TABLE / INDEX IF NOT EXISTS). Hand-DDL policy — do NOT rely on
`prisma db push`. Run from safeops_360_bakend:

    python -m scripts.create_insight_snapshot_table
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

DDL = [
    """
    CREATE TABLE IF NOT EXISTS "InsightSnapshot" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT NOT NULL,
        "module" TEXT NOT NULL,
        "identityKey" TEXT NOT NULL,
        "type" TEXT NOT NULL,
        "weekOf" TIMESTAMPTZ NOT NULL,
        "computedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "score" DOUBLE PRECISION NOT NULL DEFAULT 0,
        "scoreComponents" JSONB,
        "lifecycleState" TEXT NOT NULL DEFAULT 'new',
        "consecutiveWeeksSurfaced" INTEGER NOT NULL DEFAULT 0,
        "consecutiveEscalations" INTEGER NOT NULL DEFAULT 0,
        "firstSeenWeek" TIMESTAMPTZ NOT NULL,
        "lastHeroWeek" TIMESTAMPTZ,
        "payload" JSONB,
        "recordIds" JSONB,
        "wasHero" BOOLEAN NOT NULL DEFAULT false,
        "rowPosition" INTEGER,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    'CREATE UNIQUE INDEX IF NOT EXISTS "ux_InsightSnapshot_identity_week" '
    'ON "InsightSnapshot" ("tenantId", "module", "identityKey", "weekOf")',
    'CREATE INDEX IF NOT EXISTS "ix_InsightSnapshot_identity_weekdesc" '
    'ON "InsightSnapshot" ("tenantId", "module", "identityKey", "weekOf" DESC)',
    'CREATE INDEX IF NOT EXISTS "ix_InsightSnapshot_week_score" '
    'ON "InsightSnapshot" ("tenantId", "module", "weekOf" DESC, "score" DESC)',
]
TABLES = ["InsightSnapshot"]


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        before = set(
            s.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name = ANY(:n)"
                ),
                {"n": TABLES},
            ).scalars().all()
        )
        for stmt in DDL:
            s.execute(text(stmt))
        s.commit()
        after = set(
            s.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name = ANY(:n)"
                ),
                {"n": TABLES},
            ).scalars().all()
        )
        print(f"Created: {sorted(after - before) or '(all existed)'} | present: {sorted(after)}")
        return 0 if set(TABLES) <= after else 1


if __name__ == "__main__":
    raise SystemExit(main())
