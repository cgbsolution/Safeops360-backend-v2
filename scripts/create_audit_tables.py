"""One-off: create Audit Management tables (Pharma IMS Module 4). Additive."""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

DDL = [
    """
    CREATE TABLE IF NOT EXISTS "Audit" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT,
        "number" TEXT NOT NULL UNIQUE,
        "title" TEXT NOT NULL,
        "description" TEXT NOT NULL DEFAULT '',
        "auditType" TEXT NOT NULL,
        "plantId" TEXT NOT NULL,
        "scope" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "applicableStandards" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "regulatoryAuthority" TEXT,
        "inspectionType" TEXT,
        "supplierName" TEXT,
        "supplierSite" TEXT,
        "plannedStart" TIMESTAMPTZ NOT NULL,
        "plannedEnd" TIMESTAMPTZ NOT NULL,
        "actualStart" TIMESTAMPTZ,
        "actualEnd" TIMESTAMPTZ,
        "leadAuditorUserId" TEXT NOT NULL,
        "auditTeam" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "auditeeDepartmentHeadUserId" TEXT,
        "status" TEXT NOT NULL DEFAULT 'planned',
        "auditReportUrl" TEXT,
        "reportIssuedAt" TIMESTAMPTZ,
        "regulatoryCommitments" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "createdByUserId" TEXT NOT NULL,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "closedAt" TIMESTAMPTZ
    )
    """,
    'CREATE INDEX IF NOT EXISTS "Audit_plantId_status_idx" ON "Audit" ("plantId", "status")',
    'CREATE INDEX IF NOT EXISTS "Audit_plantId_auditType_idx" ON "Audit" ("plantId", "auditType")',
    """
    CREATE TABLE IF NOT EXISTS "AuditFinding" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT,
        "auditId" TEXT NOT NULL REFERENCES "Audit"("id") ON DELETE CASCADE,
        "findingNumber" TEXT NOT NULL,
        "type" TEXT NOT NULL,
        "area" TEXT NOT NULL DEFAULT '',
        "description" TEXT NOT NULL,
        "referenceRequirement" TEXT NOT NULL DEFAULT '',
        "evidence" TEXT NOT NULL DEFAULT '',
        "responseDueDate" TIMESTAMPTZ,
        "auditeeResponse" TEXT NOT NULL DEFAULT '',
        "capaId" TEXT,
        "capaNumber" TEXT,
        "capaStatus" TEXT,
        "findingStatus" TEXT NOT NULL DEFAULT 'open',
        "closedAt" TIMESTAMPTZ,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    'CREATE INDEX IF NOT EXISTS "AuditFinding_auditId_findingStatus_idx" ON "AuditFinding" ("auditId", "findingStatus")',
]
TABLES = ["Audit", "AuditFinding"]


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
