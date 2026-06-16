"""One-off: create the PPE Management tables (PPE-01).

Additive only — CREATE TABLE / INDEX IF NOT EXISTS. Mirrors the Prisma models
appended to schema.prisma and the SQLAlchemy models in app/models/ppe.py. We
create the live tables with raw DDL through the SYNC (psycopg2) engine rather
than `prisma migrate` / `db push` so we never risk touching the 80+ existing
tables on the shared Supabase database. Re-runnable; prints which tables exist
before and after.

Run from the backend root:
    .venv/Scripts/python.exe scripts/create_ppe_tables.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

# Column / table identifiers are camelCase → MUST be double-quoted so Postgres
# does not fold them to lowercase. jsonb arrays default to '[]', timestamps to
# now().
DDL: list[str] = [
    # ── PpeType (catalog) ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS "PpeType" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT,
        "code" TEXT NOT NULL UNIQUE,
        "name" TEXT NOT NULL,
        "description" TEXT NOT NULL DEFAULT '',
        "category" TEXT NOT NULL,
        "subcategory" TEXT NOT NULL DEFAULT '',
        "applicableStandards" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "minimumSpecification" TEXT NOT NULL DEFAULT '',
        "acceptableBrandsOrModels" TEXT NOT NULL DEFAULT '',
        "controlsHazards" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "enablesPermitTypes" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "requiredForAreas" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "serviceLifeYears" INTEGER NOT NULL DEFAULT 5,
        "serviceLifeHours" INTEGER,
        "inspectionSchedule" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "requiresCompetencyToUse" TEXT,
        "requiresFitTest" BOOLEAN NOT NULL DEFAULT false,
        "fitTestValidityMonths" INTEGER,
        "requiredTrainingPrograms" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "tracksIndividualItems" BOOLEAN NOT NULL DEFAULT true,
        "reorderPointPer100Workers" INTEGER NOT NULL DEFAULT 0,
        "isPersonalIssue" BOOLEAN NOT NULL DEFAULT false,
        "statutoryProvisionRequired" BOOLEAN NOT NULL DEFAULT true,
        "regulatoryReferences" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "isActive" BOOLEAN NOT NULL DEFAULT true,
        "isGlobal" BOOLEAN NOT NULL DEFAULT true,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    'CREATE INDEX IF NOT EXISTS "PpeType_category_idx" ON "PpeType" ("category")',
    'CREATE INDEX IF NOT EXISTS "PpeType_tenantId_isActive_idx" ON "PpeType" ("tenantId", "isActive")',
    # ── PpeItem (individual unit) ────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS "PpeItem" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT,
        "itemNumber" TEXT NOT NULL UNIQUE,
        "serialNumber" TEXT NOT NULL,
        "ppeTypeId" TEXT NOT NULL REFERENCES "PpeType"("id"),
        "ppeTypeCode" TEXT NOT NULL,
        "ppeTypeName" TEXT NOT NULL,
        "manufacturer" TEXT NOT NULL DEFAULT '',
        "model" TEXT NOT NULL DEFAULT '',
        "batchLotNumber" TEXT NOT NULL DEFAULT '',
        "manufactureDate" TIMESTAMPTZ NOT NULL,
        "purchaseDate" TIMESTAMPTZ,
        "purchaseOrderReference" TEXT,
        "cost" DOUBLE PRECISION,
        "costCurrency" TEXT NOT NULL DEFAULT 'INR',
        "plantId" TEXT NOT NULL,
        "departmentId" TEXT,
        "storageLocation" TEXT,
        "status" TEXT NOT NULL DEFAULT 'in_stock',
        "currentIssuanceId" TEXT,
        "currentHolderUserId" TEXT,
        "issuedSince" TIMESTAMPTZ,
        "condition" TEXT NOT NULL DEFAULT 'new',
        "lastConditionUpdateAt" TIMESTAMPTZ,
        "lastConditionUpdateByUserId" TEXT,
        "commissionedAt" TIMESTAMPTZ NOT NULL,
        "serviceLifeEndDate" TIMESTAMPTZ NOT NULL,
        "serviceHoursUsed" INTEGER,
        "lastInspectedAt" TIMESTAMPTZ,
        "lastInspectedByUserId" TEXT,
        "nextInspectionDueDate" TIMESTAMPTZ,
        "lastFitTestedAt" TIMESTAMPTZ,
        "fitTestValidUntil" TIMESTAMPTZ,
        "fitTestHolderUserId" TEXT,
        "batchUnderRecall" BOOLEAN NOT NULL DEFAULT false,
        "recallReference" TEXT,
        "recallIssuedAt" TIMESTAMPTZ,
        "stateHistory" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "versionNumber" INTEGER NOT NULL DEFAULT 1,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    'CREATE INDEX IF NOT EXISTS "PpeItem_tenantId_status_plantId_idx" ON "PpeItem" ("tenantId", "status", "plantId")',
    'CREATE INDEX IF NOT EXISTS "PpeItem_plantId_ppeTypeId_status_idx" ON "PpeItem" ("plantId", "ppeTypeId", "status")',
    'CREATE INDEX IF NOT EXISTS "PpeItem_plantId_currentHolderUserId_idx" ON "PpeItem" ("plantId", "currentHolderUserId")',
    'CREATE INDEX IF NOT EXISTS "PpeItem_plantId_nextInspectionDueDate_status_idx" ON "PpeItem" ("plantId", "nextInspectionDueDate", "status")',
    'CREATE INDEX IF NOT EXISTS "PpeItem_plantId_serviceLifeEndDate_idx" ON "PpeItem" ("plantId", "serviceLifeEndDate")',
    # ── PpeIssuance (transaction) ────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS "PpeIssuance" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT,
        "issuanceNumber" TEXT NOT NULL UNIQUE,
        "ppeItemId" TEXT NOT NULL,
        "ppeTypeCode" TEXT NOT NULL,
        "ppeTypeName" TEXT NOT NULL,
        "serialNumber" TEXT NOT NULL,
        "issuedToUserId" TEXT NOT NULL,
        "issuedToName" TEXT NOT NULL,
        "issuedToDepartment" TEXT NOT NULL DEFAULT '',
        "issuedToRole" TEXT NOT NULL DEFAULT '',
        "issuedByUserId" TEXT NOT NULL,
        "issuedByName" TEXT NOT NULL,
        "issuedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "expectedReturnDate" TIMESTAMPTZ,
        "issuancePurpose" TEXT NOT NULL DEFAULT 'personal_assignment',
        "linkedPermitId" TEXT,
        "linkedWorkOrder" TEXT,
        "conditionAtIssuance" TEXT NOT NULL DEFAULT 'good',
        "conditionNotesAtIssuance" TEXT NOT NULL DEFAULT '',
        "preIssuanceInspectionDone" BOOLEAN NOT NULL DEFAULT false,
        "preIssuanceInspectorUserId" TEXT,
        "recipientAcknowledged" BOOLEAN NOT NULL DEFAULT false,
        "recipientAcknowledgedAt" TIMESTAMPTZ,
        "recipientSignatureUrl" TEXT,
        "briefingProvided" BOOLEAN NOT NULL DEFAULT false,
        "briefingByUserId" TEXT,
        "status" TEXT NOT NULL DEFAULT 'active',
        "returnedAt" TIMESTAMPTZ,
        "returnedByUserId" TEXT,
        "conditionAtReturn" TEXT,
        "conditionNotesAtReturn" TEXT NOT NULL DEFAULT '',
        "postReturnInspectionRequired" BOOLEAN NOT NULL DEFAULT false,
        "plantId" TEXT NOT NULL,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    'CREATE INDEX IF NOT EXISTS "PpeIssuance_tenantId_issuedToUserId_status_idx" ON "PpeIssuance" ("tenantId", "issuedToUserId", "status")',
    'CREATE INDEX IF NOT EXISTS "PpeIssuance_ppeItemId_status_idx" ON "PpeIssuance" ("ppeItemId", "status")',
    'CREATE INDEX IF NOT EXISTS "PpeIssuance_plantId_status_idx" ON "PpeIssuance" ("plantId", "status")',
    # ── PpeInspection (serviceability lifecycle) ─────────────────────────
    """
    CREATE TABLE IF NOT EXISTS "PpeInspection" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT,
        "ppeItemId" TEXT NOT NULL,
        "ppeTypeCode" TEXT NOT NULL,
        "serialNumber" TEXT NOT NULL,
        "inspectionType" TEXT NOT NULL,
        "trigger" TEXT NOT NULL,
        "linkedPermitId" TEXT,
        "linkedIncidentId" TEXT,
        "scheduledDate" TIMESTAMPTZ,
        "conductedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "inspectorUserId" TEXT NOT NULL,
        "inspectorName" TEXT NOT NULL,
        "inspectorQualification" TEXT NOT NULL DEFAULT '',
        "isThirdPartyInspection" BOOLEAN NOT NULL DEFAULT false,
        "thirdPartyCompany" TEXT NOT NULL DEFAULT '',
        "thirdPartyCertificateReference" TEXT NOT NULL DEFAULT '',
        "checklistTemplateId" TEXT,
        "checklistItems" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "overallResult" TEXT NOT NULL,
        "defectsFound" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "conditions" TEXT NOT NULL DEFAULT '',
        "reInspectionRequired" BOOLEAN NOT NULL DEFAULT false,
        "reInspectionDueDate" TIMESTAMPTZ,
        "itemStatusAfterInspection" TEXT NOT NULL,
        "serviceLifeRemainingDays" INTEGER,
        "inspectionCertificateUrl" TEXT,
        "certificateValidUntil" TIMESTAMPTZ,
        "capaSpawned" BOOLEAN NOT NULL DEFAULT false,
        "capaId" TEXT,
        "plantId" TEXT NOT NULL,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    'CREATE INDEX IF NOT EXISTS "PpeInspection_ppeItemId_conductedAt_idx" ON "PpeInspection" ("ppeItemId", "conductedAt")',
    'CREATE INDEX IF NOT EXISTS "PpeInspection_plantId_inspectionType_idx" ON "PpeInspection" ("plantId", "inspectionType")',
    # ── PpeRequirementProfile ────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS "PpeRequirementProfile" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT,
        "plantId" TEXT NOT NULL,
        "scopeType" TEXT NOT NULL,
        "scopeId" TEXT NOT NULL,
        "scopeName" TEXT NOT NULL,
        "requiredPpe" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "effectiveFrom" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "supersededAt" TIMESTAMPTZ,
        "approvedByUserId" TEXT,
        "approvedAt" TIMESTAMPTZ,
        "isActive" BOOLEAN NOT NULL DEFAULT true,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT "PpeRequirementProfile_plantId_scopeType_scopeId_key" UNIQUE ("plantId", "scopeType", "scopeId")
    )
    """,
    'CREATE INDEX IF NOT EXISTS "PpeRequirementProfile_plantId_scopeType_isActive_idx" ON "PpeRequirementProfile" ("plantId", "scopeType", "isActive")',
    # ── PpeBatch (recall tracking) ───────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS "PpeBatch" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT,
        "plantId" TEXT NOT NULL,
        "ppeTypeId" TEXT NOT NULL,
        "batchLotNumber" TEXT NOT NULL,
        "manufacturer" TEXT NOT NULL DEFAULT '',
        "manufactureDate" TIMESTAMPTZ NOT NULL,
        "purchaseDate" TIMESTAMPTZ,
        "itemsInBatch" INTEGER NOT NULL DEFAULT 0,
        "underRecall" BOOLEAN NOT NULL DEFAULT false,
        "recallReason" TEXT NOT NULL DEFAULT '',
        "recallIssuedBy" TEXT NOT NULL DEFAULT '',
        "recallIssuedAt" TIMESTAMPTZ,
        "recallActionRequired" TEXT NOT NULL DEFAULT '',
        "recallResolvedAt" TIMESTAMPTZ,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    'CREATE INDEX IF NOT EXISTS "PpeBatch_tenantId_underRecall_idx" ON "PpeBatch" ("tenantId", "underRecall")',
    'CREATE INDEX IF NOT EXISTS "PpeBatch_plantId_ppeTypeId_idx" ON "PpeBatch" ("plantId", "ppeTypeId")',
]

PPE_TABLES = ["PpeType", "PpeItem", "PpeIssuance", "PpeInspection", "PpeRequirementProfile", "PpeBatch"]


def _existing(s: Session) -> set[str]:
    rows = s.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(:names)"
        ),
        {"names": PPE_TABLES},
    ).scalars().all()
    return set(rows)


def main() -> int:
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, future=True)

    with Session(engine) as s:
        before = _existing(s)
        print(f"Before: {sorted(before) or 'none of the PPE tables exist yet'}")

        for stmt in DDL:
            s.execute(text(stmt))
        s.commit()

        after = _existing(s)
        created = sorted(after - before)
        print(f"After : {sorted(after)}")
        print(f"Created this run: {created or '(all already existed — no-op)'}")
        if set(PPE_TABLES) - after:
            print(f"!! MISSING: {sorted(set(PPE_TABLES) - after)}")
            return 1
        print("Done. All 6 PPE tables present.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
