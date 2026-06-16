"""One-off: create Document Control tables (Pharma IMS Module 2). Additive."""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS "ControlledDocument" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT,
        "documentNumber" TEXT NOT NULL UNIQUE,
        "title" TEXT NOT NULL,
        "documentType" TEXT NOT NULL,
        "category" TEXT NOT NULL DEFAULT '',
        "plantId" TEXT NOT NULL,
        "currentVersion" TEXT NOT NULL DEFAULT '1.0',
        "currentVersionStatus" TEXT NOT NULL DEFAULT 'draft',
        "currentVersionEffectiveFrom" TIMESTAMPTZ,
        "currentDocumentFileUrl" TEXT,
        "currentDocumentFileHash" TEXT,
        "nextReviewDue" TIMESTAMPTZ,
        "reviewFrequencyMonths" INTEGER NOT NULL DEFAULT 24,
        "reviewOwnerRole" TEXT,
        "reviewOwnerUserId" TEXT,
        "applicableAreas" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "applicableRoles" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "applicableProducts" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "originatedFromMocId" TEXT,
        "originatedFromDeviationId" TEXT,
        "originatedFromCapaId" TEXT,
        "originatedFromAuditId" TEXT,
        "requiresTrainingOnNewVersion" BOOLEAN NOT NULL DEFAULT false,
        "trainingCompletionBeforeEffective" BOOLEAN NOT NULL DEFAULT false,
        "linkedTrainingProgramId" TEXT,
        "distributionList" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "referencedDocuments" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "regulatoryReference" TEXT NOT NULL DEFAULT '',
        "retentionYears" INTEGER NOT NULL DEFAULT 7,
        "createdByUserId" TEXT NOT NULL,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    'CREATE INDEX IF NOT EXISTS "ControlledDocument_plantId_documentType_idx" ON "ControlledDocument" ("plantId", "documentType")',
    'CREATE INDEX IF NOT EXISTS "ControlledDocument_plantId_currentVersionStatus_idx" ON "ControlledDocument" ("plantId", "currentVersionStatus")',
    'CREATE INDEX IF NOT EXISTS "ControlledDocument_plantId_nextReviewDue_idx" ON "ControlledDocument" ("plantId", "nextReviewDue")',
    """
    CREATE TABLE IF NOT EXISTS "DocumentVersion" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT,
        "documentId" TEXT NOT NULL REFERENCES "ControlledDocument"("id") ON DELETE CASCADE,
        "version" TEXT NOT NULL,
        "status" TEXT NOT NULL DEFAULT 'draft',
        "authoredByUserId" TEXT NOT NULL,
        "authoredAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "technicalReviewByUserId" TEXT,
        "technicalReviewAt" TIMESTAMPTZ,
        "qaReviewByUserId" TEXT,
        "qaReviewAt" TIMESTAMPTZ,
        "approvedByUserId" TEXT,
        "approvedAt" TIMESTAMPTZ,
        "effectiveFrom" TIMESTAMPTZ,
        "supersededAt" TIMESTAMPTZ,
        "changeSummary" TEXT NOT NULL DEFAULT '',
        "documentFileUrl" TEXT,
        "documentFileHash" TEXT,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT "DocumentVersion_documentId_version_key" UNIQUE ("documentId", "version")
    )
    """,
    'CREATE INDEX IF NOT EXISTS "DocumentVersion_documentId_status_idx" ON "DocumentVersion" ("documentId", "status")',
]
TABLES = ["ControlledDocument", "DocumentVersion"]


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        before = set(s.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name = ANY(:n)"), {"n": TABLES}).scalars().all())
        for stmt in DDL:
            s.execute(text(stmt))
        s.commit()
        after = set(s.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name = ANY(:n)"), {"n": TABLES}).scalars().all())
        print(f"Created: {sorted(after - before) or '(all existed)'} | present: {sorted(after)}")
        return 0 if set(TABLES) <= after else 1


if __name__ == "__main__":
    raise SystemExit(main())
