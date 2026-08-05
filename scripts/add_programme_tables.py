"""Annual Audit Programme (CAMS Wave 2) - additive DDL.

Creates the nine tables behind docs/cams/08-audit-programme.md:

  AuditProgramme            standing programme (per tenant + standard set)
  ProgrammeCycle            one period instance; APPROVED freezes an immutable snapshot
  ProgrammeScopeUnit        the atomic covered thing - the matrix is built from these
  ProgrammeSlot             a PLANNED engagement (a slot is NOT an engagement)
  SlotScopeUnit             join: one slot covers N scope units
  ProgrammeReview           ISO 19011 §5.6 review OF THE PROGRAMME
  ProgrammeAmendment        every deferral/cancellation/waiver after approval
  ProgrammeRecommendation   risk-based frequency recommendation + its inputs
  DisciplineHazardMap       incident category -> audit discipline (the missing join)

**The CHECK constraint is the point of this script.** docs/cams/08 §3.2 requires
that no slot leaves PLANNED without either a materialised engagement or an
amendment. The service enforces it, but the module's own history shows
service-layer-only guards being bypassed by scripts, so the storage layer
enforces it too.

Additive + re-runnable through the SYNC engine - never `prisma db push` (known
Cams* drift would drop tables).

    .venv/Scripts/python.exe scripts/add_programme_tables.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

STMTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS "AuditProgramme" (
        "id"                       TEXT PRIMARY KEY,
        "tenantId"                 TEXT,
        "programmeCode"            TEXT NOT NULL UNIQUE,
        "name"                     TEXT NOT NULL,
        "objectives"               TEXT NOT NULL DEFAULT '',
        "scopeStatement"           TEXT NOT NULL DEFAULT '',
        "standardRefs"             JSONB NOT NULL DEFAULT '[]'::jsonb,
        "ownerUserId"              TEXT NOT NULL,
        "status"                   TEXT NOT NULL DEFAULT 'ACTIVE',
        "revision"                 INTEGER NOT NULL DEFAULT 1,
        "revisionHistory"          JSONB NOT NULL DEFAULT '[]'::jsonb,
        "fullCoverageThresholdPct" DOUBLE PRECISION NOT NULL DEFAULT 80.0,
        "createdAt"                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "createdBy"                TEXT,
        "updatedAt"                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "isDeleted"                BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    'CREATE INDEX IF NOT EXISTS "ix_AuditProgramme_tenant_status" ON "AuditProgramme" ("tenantId", "status")',
    'CREATE INDEX IF NOT EXISTS "ix_AuditProgramme_isDeleted" ON "AuditProgramme" ("isDeleted")',
    """
    CREATE TABLE IF NOT EXISTS "ProgrammeCycle" (
        "id"                   TEXT PRIMARY KEY,
        "programmeId"          TEXT NOT NULL REFERENCES "AuditProgramme"("id") ON DELETE CASCADE,
        "cycleLabel"           TEXT NOT NULL,
        "periodStart"          DATE NOT NULL,
        "periodEnd"            DATE NOT NULL,
        "periodsPerCycle"      INTEGER NOT NULL DEFAULT 4,
        "status"               TEXT NOT NULL DEFAULT 'DRAFT',
        "submittedForReviewAt" TIMESTAMPTZ,
        "approvedByUserId"     TEXT,
        "approvedAt"           TIMESTAMPTZ,
        "approvedSnapshot"     JSONB,
        "approvedSnapshotHash" TEXT,
        "closedAt"             TIMESTAMPTZ,
        "createdAt"            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "createdBy"            TEXT,
        "updatedAt"            TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_ProgrammeCycle_label" ON "ProgrammeCycle" ("programmeId", "cycleLabel")',
    'CREATE INDEX IF NOT EXISTS "ix_ProgrammeCycle_status" ON "ProgrammeCycle" ("status")',
    'CREATE INDEX IF NOT EXISTS "ix_ProgrammeCycle_period" ON "ProgrammeCycle" ("periodStart", "periodEnd")',
    'CREATE INDEX IF NOT EXISTS "ProgrammeCycle_programmeId_idx" ON "ProgrammeCycle" ("programmeId")',
    """
    CREATE TABLE IF NOT EXISTS "ProgrammeScopeUnit" (
        "id"               TEXT PRIMARY KEY,
        "cycleId"          TEXT NOT NULL REFERENCES "ProgrammeCycle"("id") ON DELETE CASCADE,
        "dimension"        TEXT NOT NULL DEFAULT 'DISCIPLINE',
        "siteId"           TEXT,
        "dimensionKey"     TEXT NOT NULL,
        "dimensionLabel"   TEXT NOT NULL DEFAULT '',
        "requiredPerCycle" INTEGER,
        "riskWeight"       INTEGER NOT NULL DEFAULT 3,
        "rationale"        TEXT NOT NULL DEFAULT '',
        "waiverReason"     TEXT,
        "waivedByUserId"   TEXT,
        "waivedAt"         TIMESTAMPTZ,
        "createdAt"        TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_ProgrammeScopeUnit_key" '
    'ON "ProgrammeScopeUnit" ("cycleId", "dimension", "siteId", "dimensionKey")',
    'CREATE INDEX IF NOT EXISTS "ix_ProgrammeScopeUnit_cycle_dim" ON "ProgrammeScopeUnit" ("cycleId", "dimension")',
    'CREATE INDEX IF NOT EXISTS "ProgrammeScopeUnit_siteId_idx" ON "ProgrammeScopeUnit" ("siteId")',
    """
    CREATE TABLE IF NOT EXISTS "ProgrammeSlot" (
        "id"                    TEXT PRIMARY KEY,
        "cycleId"               TEXT NOT NULL REFERENCES "ProgrammeCycle"("id") ON DELETE CASCADE,
        "slotCode"              TEXT NOT NULL,
        "windowStart"           DATE NOT NULL,
        "windowEnd"             DATE NOT NULL,
        "periodIndex"           INTEGER NOT NULL DEFAULT 0,
        "origin"                TEXT NOT NULL DEFAULT 'INTERNAL',
        "externalBody"          TEXT,
        "engagementKind"        TEXT,
        "engagementId"          TEXT,
        "engagementTypeRef"     TEXT,
        "intendedLeadUserId"    TEXT,
        "ownerUserId"           TEXT,
        "estimatedAuditorDays"  DOUBLE PRECISION NOT NULL DEFAULT 1.0,
        "actualAuditorDays"     DOUBLE PRECISION,
        "samplingApproach"      TEXT NOT NULL DEFAULT 'FULL',
        "samplingJustification" TEXT,
        "status"                TEXT NOT NULL DEFAULT 'PLANNED',
        "amendmentCount"        INTEGER NOT NULL DEFAULT 0,
        "notes"                 TEXT,
        "createdAt"             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "createdBy"             TEXT,
        "updatedAt"             TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_ProgrammeSlot_code" ON "ProgrammeSlot" ("cycleId", "slotCode")',
    'CREATE INDEX IF NOT EXISTS "ix_ProgrammeSlot_cycle_status" ON "ProgrammeSlot" ("cycleId", "status")',
    'CREATE INDEX IF NOT EXISTS "ix_ProgrammeSlot_window" ON "ProgrammeSlot" ("windowStart", "windowEnd")',
    'CREATE INDEX IF NOT EXISTS "ix_ProgrammeSlot_engagement" ON "ProgrammeSlot" ("engagementKind", "engagementId")',
    'CREATE INDEX IF NOT EXISTS "ProgrammeSlot_intendedLeadUserId_idx" ON "ProgrammeSlot" ("intendedLeadUserId")',
    # -- THE constraint (docs/cams/08 §3.2) ----------------------------
    # A slot in any non-PLANNED state must have EITHER a materialised engagement
    # OR at least one amendment explaining why it did not happen. This is the
    # audit trail, expressed where a stray UPDATE cannot dodge it.
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_ProgrammeSlot_left_planned'
        ) THEN
            ALTER TABLE "ProgrammeSlot" ADD CONSTRAINT "ck_ProgrammeSlot_left_planned"
            CHECK (
                "status" = 'PLANNED'
                OR "engagementId" IS NOT NULL
                OR "amendmentCount" > 0
            );
        END IF;
    END $$;
    """,
    """
    CREATE TABLE IF NOT EXISTS "SlotScopeUnit" (
        "id"          TEXT PRIMARY KEY,
        "slotId"      TEXT NOT NULL REFERENCES "ProgrammeSlot"("id") ON DELETE CASCADE,
        "scopeUnitId" TEXT NOT NULL REFERENCES "ProgrammeScopeUnit"("id") ON DELETE CASCADE
    )
    """,
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_SlotScopeUnit" ON "SlotScopeUnit" ("slotId", "scopeUnitId")',
    'CREATE INDEX IF NOT EXISTS "SlotScopeUnit_slotId_idx" ON "SlotScopeUnit" ("slotId")',
    'CREATE INDEX IF NOT EXISTS "SlotScopeUnit_scopeUnitId_idx" ON "SlotScopeUnit" ("scopeUnitId")',
    """
    CREATE TABLE IF NOT EXISTS "ProgrammeReview" (
        "id"                      TEXT PRIMARY KEY,
        "cycleId"                 TEXT NOT NULL REFERENCES "ProgrammeCycle"("id") ON DELETE CASCADE,
        "reviewDate"              DATE NOT NULL,
        "participantUserIds"      JSONB NOT NULL DEFAULT '[]'::jsonb,
        "externalParticipants"    JSONB NOT NULL DEFAULT '[]'::jsonb,
        "programmeFindings"       TEXT NOT NULL DEFAULT '',
        "decisions"               TEXT NOT NULL DEFAULT '',
        "effectivenessAssessment" TEXT,
        "reviewedByUserId"        TEXT NOT NULL,
        "createdAt"               TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    'CREATE INDEX IF NOT EXISTS "ix_ProgrammeReview_cycle_date" ON "ProgrammeReview" ("cycleId", "reviewDate")',
    """
    CREATE TABLE IF NOT EXISTS "ProgrammeAmendment" (
        "id"               TEXT PRIMARY KEY,
        "cycleId"          TEXT NOT NULL REFERENCES "ProgrammeCycle"("id") ON DELETE CASCADE,
        "slotId"           TEXT,
        "scopeUnitId"      TEXT,
        "amendmentType"    TEXT NOT NULL,
        "reason"           TEXT NOT NULL,
        "beforeValue"      JSONB,
        "afterValue"       JSONB,
        "approvedByUserId" TEXT NOT NULL,
        "approvedAt"       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "raisedByUserId"   TEXT,
        "createdAt"        TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    'CREATE INDEX IF NOT EXISTS "ix_ProgrammeAmendment_cycle_type" ON "ProgrammeAmendment" ("cycleId", "amendmentType")',
    'CREATE INDEX IF NOT EXISTS "ProgrammeAmendment_slotId_idx" ON "ProgrammeAmendment" ("slotId")',
    """
    CREATE TABLE IF NOT EXISTS "ProgrammeRecommendation" (
        "id"                   TEXT PRIMARY KEY,
        "cycleId"              TEXT NOT NULL,
        "scopeUnitId"          TEXT NOT NULL,
        "currentFrequency"     INTEGER,
        "recommendedFrequency" INTEGER NOT NULL,
        "score"                DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        "band"                 TEXT NOT NULL,
        "inputs"               JSONB NOT NULL DEFAULT '[]'::jsonb,
        "unavailableInputs"    JSONB NOT NULL DEFAULT '[]'::jsonb,
        "narrative"            TEXT NOT NULL DEFAULT '',
        "computedAt"           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "acceptedByUserId"     TEXT,
        "acceptedAt"           TIMESTAMPTZ,
        "acceptedFrequency"    INTEGER,
        "rejectedByUserId"     TEXT,
        "rejectedAt"           TIMESTAMPTZ,
        "rejectionReason"      TEXT
    )
    """,
    'CREATE INDEX IF NOT EXISTS "ix_ProgrammeRecommendation_cycle" ON "ProgrammeRecommendation" ("cycleId", "acceptedAt")',
    'CREATE INDEX IF NOT EXISTS "ProgrammeRecommendation_scopeUnitId_idx" ON "ProgrammeRecommendation" ("scopeUnitId")',
    """
    CREATE TABLE IF NOT EXISTS "DisciplineHazardMap" (
        "id"             TEXT PRIMARY KEY,
        "plantId"        TEXT,
        "disciplineCode" TEXT NOT NULL,
        "hazardCategory" TEXT NOT NULL,
        "sourceModule"   TEXT NOT NULL DEFAULT 'INCIDENT',
        "weight"         DOUBLE PRECISION NOT NULL DEFAULT 1.0,
        "isActive"       BOOLEAN NOT NULL DEFAULT TRUE,
        "createdAt"      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "createdBy"      TEXT
    )
    """,
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_DisciplineHazardMap" '
    'ON "DisciplineHazardMap" ("plantId", "disciplineCode", "hazardCategory", "sourceModule")',
    'CREATE INDEX IF NOT EXISTS "ix_DisciplineHazardMap_lookup" ON "DisciplineHazardMap" ("hazardCategory", "isActive")',
    'CREATE INDEX IF NOT EXISTS "DisciplineHazardMap_disciplineCode_idx" ON "DisciplineHazardMap" ("disciplineCode")',
]

TABLES = [
    "AuditProgramme",
    "ProgrammeCycle",
    "ProgrammeScopeUnit",
    "ProgrammeSlot",
    "SlotScopeUnit",
    "ProgrammeReview",
    "ProgrammeAmendment",
    "ProgrammeRecommendation",
    "DisciplineHazardMap",
]


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    failures = 0
    with Session(engine) as s:
        for stmt in STMTS:
            s.execute(text(stmt))
        s.commit()

        print("-- verification -----------------------------")
        for t in TABLES:
            ok = bool(
                s.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name=:t"
                    ),
                    {"t": t},
                ).first()
            )
            print(f"  table      {t:<28} {'present' if ok else 'MISSING'}")
            failures += 0 if ok else 1

        ck = bool(
            s.execute(
                text(
                    "SELECT 1 FROM pg_constraint WHERE conname='ck_ProgrammeSlot_left_planned'"
                )
            ).first()
        )
        print(f"  constraint {'ck_ProgrammeSlot_left_planned':<28} {'present' if ck else 'MISSING'}")
        failures += 0 if ck else 1

        # The zero-row verification queries the migration policy requires
        # (docs/cams/04-target.md §9 rule 2). All three must read 0.
        print("\n-- invariants (each must be 0) --------------")
        checks = [
            (
                "slots non-PLANNED with neither engagement nor amendment",
                'SELECT count(*) FROM "ProgrammeSlot" WHERE "status" <> \'PLANNED\' '
                'AND "engagementId" IS NULL AND "amendmentCount" = 0',
            ),
            (
                "scope units on an approved cycle with no frequency and no waiver",
                'SELECT count(*) FROM "ProgrammeScopeUnit" u JOIN "ProgrammeCycle" c '
                'ON c."id" = u."cycleId" WHERE c."status" IN (\'APPROVED\',\'ACTIVE\',\'CLOSED\') '
                'AND u."requiredPerCycle" IS NULL AND u."waiverReason" IS NULL',
            ),
            (
                "slots pointing at an engagement kind we cannot resolve",
                'SELECT count(*) FROM "ProgrammeSlot" WHERE "engagementId" IS NOT NULL '
                'AND ("engagementKind" IS NULL OR "engagementKind" NOT IN (\'AUDIT\',\'INSPECTION\'))',
            ),
        ]
        for label, sql in checks:
            n = s.execute(text(sql)).scalar_one()
            print(f"  {n:>4}  {label}")
            failures += 0 if n == 0 else 1

    print("\nDONE" if not failures else f"\nDONE with {failures} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
