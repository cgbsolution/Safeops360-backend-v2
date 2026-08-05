"""Assurance integrity (CAMS Wave 1) - additive DDL.

Creates the tables behind docs/cams/09-module-completion.md Part 2 and the Q17
ownership answer:

  * Area."ownerUserId"              - area-level responsibility (Q17)
  * DisciplineOwner                 - discipline-level responsibility (Q17)
  * IndependenceWaiver              - §2.1.6 governed exception
  * EngagementCompetenceSnapshot    - §2.2 "who was qualified when"
  * EngagementMeeting               - §2.3 opening/closing meeting record
  * ReportErratum                   - §2.5 correction without touching the snapshot
  * AuditReport."snapshotHashFull"  - §2.5 gap 1, full-length SHA-256
  * ComplianceAudit."reopenCount"   - §2.5 gap 3, governed reopen

Additive + re-runnable (CREATE TABLE / ADD COLUMN ... IF NOT EXISTS) through the
SYNC engine - never `prisma db push` (known Cams* drift would drop tables).

    .venv/Scripts/python.exe scripts/add_assurance_integrity.py

Ends with verification SELECTs that must all report present (the rule from
docs/cams/04-target.md §9: no migration ships without one).
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

STMTS: list[str] = [
    # -- Q17: ownership ------------------------------------------------
    'ALTER TABLE "Area" ADD COLUMN IF NOT EXISTS "ownerUserId" TEXT',
    'CREATE INDEX IF NOT EXISTS "Area_ownerUserId_idx" ON "Area" ("ownerUserId")',
    """
    CREATE TABLE IF NOT EXISTS "DisciplineOwner" (
        "id"              TEXT PRIMARY KEY,
        "plantId"         TEXT,
        "disciplineCode"  TEXT NOT NULL,
        "disciplineLabel" TEXT NOT NULL DEFAULT '',
        "ownerUserId"     TEXT NOT NULL,
        "ownershipType"   TEXT NOT NULL DEFAULT 'ACCOUNTABLE',
        "isActive"        BOOLEAN NOT NULL DEFAULT TRUE,
        "createdAt"       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "createdBy"       TEXT
    )
    """,
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_DisciplineOwner_scope" '
    'ON "DisciplineOwner" ("plantId", "disciplineCode", "ownerUserId")',
    'CREATE INDEX IF NOT EXISTS "ix_DisciplineOwner_lookup" '
    'ON "DisciplineOwner" ("plantId", "disciplineCode", "isActive")',
    'CREATE INDEX IF NOT EXISTS "DisciplineOwner_ownerUserId_idx" '
    'ON "DisciplineOwner" ("ownerUserId")',
    # -- §2.1.6 independence waiver ------------------------------------
    """
    CREATE TABLE IF NOT EXISTS "IndependenceWaiver" (
        "id"               TEXT PRIMARY KEY,
        "engagementKind"   TEXT NOT NULL,
        "engagementId"     TEXT NOT NULL,
        "subjectUserId"    TEXT NOT NULL,
        "ruleViolated"     TEXT NOT NULL,
        "conflictDetail"   JSONB,
        "justification"    TEXT NOT NULL,
        "approvedByUserId" TEXT NOT NULL,
        "approvedAt"       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "scope"            TEXT NOT NULL DEFAULT 'ENGAGEMENT',
        "checkpointCodes"  JSONB NOT NULL DEFAULT '[]'::jsonb,
        "revokedAt"        TIMESTAMPTZ,
        "revokedByUserId"  TEXT,
        "createdAt"        TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    'CREATE INDEX IF NOT EXISTS "ix_IndependenceWaiver_engagement" '
    'ON "IndependenceWaiver" ("engagementKind", "engagementId")',
    'CREATE INDEX IF NOT EXISTS "ix_IndependenceWaiver_subject" '
    'ON "IndependenceWaiver" ("subjectUserId", "revokedAt")',
    # -- §2.2 competence snapshot --------------------------------------
    """
    CREATE TABLE IF NOT EXISTS "EngagementCompetenceSnapshot" (
        "id"                           TEXT PRIMARY KEY,
        "engagementKind"               TEXT NOT NULL,
        "engagementId"                 TEXT NOT NULL,
        "userId"                       TEXT NOT NULL,
        "competencyId"                 TEXT NOT NULL,
        "competencyCode"               TEXT NOT NULL DEFAULT '',
        "competencyName"               TEXT NOT NULL DEFAULT '',
        "state"                        TEXT,
        "validUntil"                   TIMESTAMPTZ,
        "externalCertificateReference" TEXT,
        "held"                         BOOLEAN NOT NULL DEFAULT FALSE,
        "waivedGap"                    BOOLEAN NOT NULL DEFAULT FALSE,
        "capturedAt"                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "capturedByUserId"             TEXT
    )
    """,
    'CREATE INDEX IF NOT EXISTS "ix_EngCompSnapshot_eng_user" '
    'ON "EngagementCompetenceSnapshot" ("engagementKind", "engagementId", "userId")',
    # -- §2.3 meeting records ------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS "EngagementMeeting" (
        "id"                          TEXT PRIMARY KEY,
        "engagementKind"              TEXT NOT NULL,
        "engagementId"                TEXT NOT NULL,
        "meetingType"                 TEXT NOT NULL,
        "heldAt"                      TIMESTAMPTZ NOT NULL,
        "attendees"                   JSONB NOT NULL DEFAULT '[]'::jsonb,
        "scopeConfirmed"              BOOLEAN NOT NULL DEFAULT FALSE,
        "findingsSummaryPresented"    TEXT,
        "auditeeAcknowledgedByUserId" TEXT,
        "auditeeAcknowledgedAt"       TIMESTAMPTZ,
        "notes"                       TEXT,
        "recordedByUserId"            TEXT NOT NULL,
        "createdAt"                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "updatedAt"                   TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_EngagementMeeting_type" '
    'ON "EngagementMeeting" ("engagementKind", "engagementId", "meetingType")',
    'CREATE INDEX IF NOT EXISTS "ix_EngagementMeeting_engagement" '
    'ON "EngagementMeeting" ("engagementKind", "engagementId")',
    # -- §2.5 report integrity -----------------------------------------
    """
    CREATE TABLE IF NOT EXISTS "ReportErratum" (
        "id"               TEXT PRIMARY KEY,
        "reportId"         TEXT NOT NULL,
        "auditId"          TEXT NOT NULL,
        "sequence"         INTEGER NOT NULL DEFAULT 1,
        "text"             TEXT NOT NULL,
        "raisedByUserId"   TEXT NOT NULL,
        "approvedByUserId" TEXT NOT NULL,
        "createdAt"        TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_ReportErratum_seq" '
    'ON "ReportErratum" ("reportId", "sequence")',
    'CREATE INDEX IF NOT EXISTS "ix_ReportErratum_report" ON "ReportErratum" ("reportId")',
    'ALTER TABLE "AuditReport" ADD COLUMN IF NOT EXISTS "snapshotHashFull" TEXT',
    'ALTER TABLE "ComplianceAudit" ADD COLUMN IF NOT EXISTS "reopenCount" INTEGER NOT NULL DEFAULT 0',
    'ALTER TABLE "ComplianceAudit" ADD COLUMN IF NOT EXISTS "lastReopenedAt" TIMESTAMPTZ',
    'ALTER TABLE "ComplianceAudit" ADD COLUMN IF NOT EXISTS "lastReopenReason" TEXT',
]

TABLES = [
    "DisciplineOwner",
    "IndependenceWaiver",
    "EngagementCompetenceSnapshot",
    "EngagementMeeting",
    "ReportErratum",
]
COLUMNS = [
    ("Area", "ownerUserId"),
    ("AuditReport", "snapshotHashFull"),
    ("ComplianceAudit", "reopenCount"),
    ("ComplianceAudit", "lastReopenedAt"),
    ("ComplianceAudit", "lastReopenReason"),
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
            print(f"  table  {t:<32} {'present' if ok else 'MISSING'}")
            failures += 0 if ok else 1
        for tbl, col in COLUMNS:
            ok = bool(
                s.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:t AND column_name=:c"
                    ),
                    {"t": tbl, "c": col},
                ).first()
            )
            print(f"  column {tbl}.{col:<24} {'present' if ok else 'MISSING'}")
            failures += 0 if ok else 1

        # Backfill the full-length hash where we can: existing reports carry the
        # truncated hash inside their snapshot JSON. We cannot recompute the full
        # digest without re-serialising the snapshot identically, so this reports
        # how many rows will show "legacy truncated hash" on the report until
        # they are regenerated. Honest gap, surfaced rather than papered over.
        legacy = s.execute(
            text('SELECT count(*) FROM "AuditReport" WHERE "snapshotHashFull" IS NULL')
        ).scalar_one()
        print(f"\n  {legacy} existing report(s) carry only the legacy 16-char hash.")
        print("  They verify as LEGACY_TRUNCATED, not as tampered. New reports carry both.")

    print("\nDONE" if not failures else f"\nDONE with {failures} MISSING object(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
