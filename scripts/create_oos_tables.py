"""One-off: create the OosInvestigation table (Pharma IMS Module 3). Additive."""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

DDL = [
    """
    CREATE TABLE IF NOT EXISTS "OosInvestigation" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT,
        "number" TEXT NOT NULL UNIQUE,
        "plantId" TEXT NOT NULL,
        "productName" TEXT NOT NULL,
        "batchNumber" TEXT NOT NULL,
        "testName" TEXT NOT NULL,
        "specificationReference" TEXT NOT NULL DEFAULT '',
        "specificationLimit" TEXT NOT NULL DEFAULT '',
        "initialResult" TEXT NOT NULL,
        "initialResultNumeric" DOUBLE PRECISION,
        "resultUnit" TEXT NOT NULL DEFAULT '',
        "analystUserId" TEXT NOT NULL,
        "analysisDate" TIMESTAMPTZ NOT NULL,
        "instrumentId" TEXT,
        "phase1" JSONB,
        "phase1Conclusion" TEXT,
        "phase1ByUserId" TEXT,
        "phase1CompletedAt" TIMESTAMPTZ,
        "phase2" JSONB,
        "phase2Conclusion" TEXT,
        "phase2ByUserId" TEXT,
        "phase2CompletedAt" TIMESTAMPTZ,
        "deviationRaised" BOOLEAN NOT NULL DEFAULT false,
        "deviationId" TEXT,
        "deviationNumber" TEXT,
        "rootCauseCategory" TEXT,
        "rootCauseDescription" TEXT NOT NULL DEFAULT '',
        "batchDisposition" TEXT,
        "batchDispositionJustification" TEXT NOT NULL DEFAULT '',
        "batchDispositionByUserId" TEXT,
        "batchDispositionAt" TIMESTAMPTZ,
        "status" TEXT NOT NULL DEFAULT 'phase_1_in_progress',
        "createdByUserId" TEXT NOT NULL,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "closedAt" TIMESTAMPTZ
    )
    """,
    'CREATE INDEX IF NOT EXISTS "OosInvestigation_plantId_status_idx" ON "OosInvestigation" ("plantId", "status")',
    'CREATE INDEX IF NOT EXISTS "OosInvestigation_deviationId_idx" ON "OosInvestigation" ("deviationId")',
]


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        for stmt in DDL:
            s.execute(text(stmt))
        s.commit()
        exists = s.execute(text("SELECT 1 FROM information_schema.tables WHERE table_name='OosInvestigation'")).scalar_one_or_none()
        print("OosInvestigation present:", bool(exists))
        return 0 if exists else 1


if __name__ == "__main__":
    raise SystemExit(main())
