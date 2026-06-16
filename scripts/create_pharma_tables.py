"""One-off: create the Pharma IMS tables — Part 11 primitives + Deviation.

Additive only (CREATE TABLE / INDEX IF NOT EXISTS) through the SYNC engine, so
the 80+ existing tables on the shared Supabase DB are never touched. Mirrors the
Prisma models appended to schema.prisma and the SQLAlchemy models in
app/models/part11.py + app/models/deviation.py. Re-runnable.

Run from the backend root:
    .venv/Scripts/python.exe scripts/create_pharma_tables.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

DDL: list[str] = [
    # ── ElectronicSignature (21 CFR Part 11) ─────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS "ElectronicSignature" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT,
        "recordType" TEXT NOT NULL,
        "recordId" TEXT NOT NULL,
        "recordNumber" TEXT,
        "signerUserId" TEXT NOT NULL,
        "signerFullName" TEXT NOT NULL,
        "signerRole" TEXT NOT NULL,
        "signerDepartment" TEXT,
        "signedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "signatureMeaning" TEXT NOT NULL,
        "ipAddress" TEXT,
        "reAuthenticated" BOOLEAN NOT NULL DEFAULT true,
        "authenticationMethod" TEXT NOT NULL DEFAULT 'password',
        "recordHash" TEXT NOT NULL,
        "signatureHash" TEXT NOT NULL,
        "isValid" BOOLEAN NOT NULL DEFAULT true,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    'CREATE INDEX IF NOT EXISTS "ElectronicSignature_recordType_recordId_idx" ON "ElectronicSignature" ("recordType", "recordId")',
    'CREATE INDEX IF NOT EXISTS "ElectronicSignature_signerUserId_idx" ON "ElectronicSignature" ("signerUserId")',
    # ── GmpAuditEntry (immutable audit trail) ────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS "GmpAuditEntry" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT,
        "recordType" TEXT NOT NULL,
        "recordId" TEXT NOT NULL,
        "recordNumber" TEXT,
        "eventType" TEXT NOT NULL,
        "eventAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "eventByUserId" TEXT NOT NULL,
        "eventByFullName" TEXT NOT NULL,
        "eventByRole" TEXT,
        "fieldName" TEXT,
        "oldValue" TEXT,
        "newValue" TEXT,
        "reasonForChange" TEXT NOT NULL DEFAULT '',
        "sessionId" TEXT,
        "ipAddress" TEXT,
        "userAgent" TEXT,
        "entryHash" TEXT NOT NULL,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    'CREATE INDEX IF NOT EXISTS "GmpAuditEntry_recordType_recordId_eventAt_idx" ON "GmpAuditEntry" ("recordType", "recordId", "eventAt")',
    'CREATE INDEX IF NOT EXISTS "GmpAuditEntry_eventByUserId_idx" ON "GmpAuditEntry" ("eventByUserId")',
    # ── Deviation (Module 1) ─────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS "Deviation" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT,
        "number" TEXT NOT NULL UNIQUE,
        "title" TEXT NOT NULL,
        "description" TEXT NOT NULL,
        "type" TEXT NOT NULL,
        "category" TEXT NOT NULL,
        "severity" TEXT NOT NULL,
        "plantId" TEXT NOT NULL,
        "department" TEXT NOT NULL DEFAULT '',
        "area" TEXT NOT NULL DEFAULT '',
        "detectionDate" TIMESTAMPTZ NOT NULL,
        "occurrenceDate" TIMESTAMPTZ,
        "detectionMethod" TEXT NOT NULL DEFAULT '',
        "detectedByUserId" TEXT NOT NULL,
        "affectedProductName" TEXT,
        "affectedProductCode" TEXT,
        "affectedBatchNumbers" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "affectedBatchSize" INTEGER,
        "batchStatusAtDetection" TEXT,
        "approvedProcessReference" TEXT NOT NULL DEFAULT '',
        "approvedProcessVersion" TEXT NOT NULL DEFAULT '',
        "immediateActionsTaken" TEXT NOT NULL DEFAULT '',
        "batchQuarantined" BOOLEAN NOT NULL DEFAULT false,
        "productionStopped" BOOLEAN NOT NULL DEFAULT false,
        "qaClassifiedByUserId" TEXT,
        "qaClassifiedAt" TIMESTAMPTZ,
        "impactAssessment" JSONB,
        "batchDispositionRecommendation" TEXT,
        "batchDispositionJustification" TEXT NOT NULL DEFAULT '',
        "batchDispositionDecidedByUserId" TEXT,
        "batchDispositionDecidedAt" TIMESTAMPTZ,
        "investigationAssignedToUserId" TEXT,
        "investigationDueDate" TIMESTAMPTZ,
        "investigationExtendedDueDate" TIMESTAMPTZ,
        "investigationCompletedAt" TIMESTAMPTZ,
        "rootCauseCategory" TEXT,
        "rootCauseDescription" TEXT NOT NULL DEFAULT '',
        "rootCauseMethodology" TEXT,
        "contributingFactors" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "similarPastDeviations" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "capaRequired" BOOLEAN NOT NULL DEFAULT false,
        "capaId" TEXT,
        "capaNumber" TEXT,
        "plannedDeviation" JSONB,
        "status" TEXT NOT NULL DEFAULT 'draft',
        "regulatoryReportable" BOOLEAN NOT NULL DEFAULT false,
        "regulatoryAuthority" TEXT,
        "regulatoryReport" JSONB,
        "isRecurring" BOOLEAN NOT NULL DEFAULT false,
        "previousDeviationNumbers" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "trendingTags" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "createdByUserId" TEXT NOT NULL,
        "versionNumber" INTEGER NOT NULL DEFAULT 1,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "closedAt" TIMESTAMPTZ
    )
    """,
    'CREATE INDEX IF NOT EXISTS "Deviation_plantId_status_idx" ON "Deviation" ("plantId", "status")',
    'CREATE INDEX IF NOT EXISTS "Deviation_plantId_category_idx" ON "Deviation" ("plantId", "category")',
    'CREATE INDEX IF NOT EXISTS "Deviation_plantId_severity_idx" ON "Deviation" ("plantId", "severity")',
    'CREATE INDEX IF NOT EXISTS "Deviation_investigationAssignedToUserId_idx" ON "Deviation" ("investigationAssignedToUserId")',
    'CREATE INDEX IF NOT EXISTS "Deviation_capaId_idx" ON "Deviation" ("capaId")',
]

TABLES = ["ElectronicSignature", "GmpAuditEntry", "Deviation"]


def _existing(s: Session) -> set[str]:
    rows = s.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(:names)"
        ),
        {"names": TABLES},
    ).scalars().all()
    return set(rows)


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        before = _existing(s)
        print(f"Before: {sorted(before) or 'none of the pharma tables exist yet'}")
        for stmt in DDL:
            s.execute(text(stmt))
        s.commit()
        after = _existing(s)
        print(f"After : {sorted(after)}")
        print(f"Created this run: {sorted(after - before) or '(all already existed)'}")
        missing = set(TABLES) - after
        if missing:
            print(f"!! MISSING: {sorted(missing)}")
            return 1
        print("Done. All 3 pharma tables present.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
