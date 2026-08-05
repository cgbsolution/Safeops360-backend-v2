"""Waves 3-5 completion tables (WP-19/40/43/45/46) - additive DDL.

  AuditFinding             Finding first-class on the AUDIT side (F-3, F-40)
  EvidencePackJob          async certification evidence pack (WP-40)
  NotificationPreference   per-user digest frequency (WP-43)
  SupplierAuditLink        VendorProfile <-> engagement (WP-45, no new entity)
  CheckpointTranslation    per-language checkpoint text (WP-46, Q18: en + hi)

Additive + re-runnable. Never `prisma db push` (Cams* drift would drop tables).

    .venv/Scripts/python.exe scripts/add_cams_completion.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

STMTS: list[str] = [
    # ── WP-19 ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS "AuditFinding" (
        "id"                   TEXT PRIMARY KEY,
        "findingCode"          TEXT NOT NULL UNIQUE,
        "auditId"              TEXT NOT NULL,
        "checkpointResponseId" TEXT,
        "checkpointCode"       TEXT,
        "siteId"               TEXT,
        "disciplineCode"       TEXT,
        "title"                TEXT NOT NULL,
        "description"          TEXT NOT NULL DEFAULT '',
        "severity"             TEXT NOT NULL DEFAULT 'MINOR_NC',
        "observationOnly"      BOOLEAN NOT NULL DEFAULT FALSE,
        "standard"             TEXT,
        "clauseRef"            TEXT,
        "ownerId"              TEXT,
        "dueDate"              DATE,
        "status"               TEXT NOT NULL DEFAULT 'OPEN',
        "capaId"               TEXT,
        "isRepeatFinding"      BOOLEAN NOT NULL DEFAULT FALSE,
        "repeatOfFindingId"    TEXT,
        "repeatOfKind"         TEXT,
        "createdAt"            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "createdById"          TEXT,
        "closedAt"             TIMESTAMPTZ,
        "closedById"           TEXT,
        "isDeleted"            BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    'CREATE INDEX IF NOT EXISTS "ix_AuditFinding_audit_status" ON "AuditFinding" ("auditId", "status")',
    'CREATE INDEX IF NOT EXISTS "ix_AuditFinding_site_severity" ON "AuditFinding" ("siteId", "severity")',
    'CREATE INDEX IF NOT EXISTS "ix_AuditFinding_due" ON "AuditFinding" ("dueDate", "status")',
    'CREATE INDEX IF NOT EXISTS "AuditFinding_ownerId_idx" ON "AuditFinding" ("ownerId")',
    'CREATE INDEX IF NOT EXISTS "AuditFinding_capaId_idx" ON "AuditFinding" ("capaId")',
    'CREATE INDEX IF NOT EXISTS "AuditFinding_repeatOf_idx" ON "AuditFinding" ("repeatOfFindingId")',
    'CREATE INDEX IF NOT EXISTS "AuditFinding_isDeleted_idx" ON "AuditFinding" ("isDeleted")',
    'CREATE INDEX IF NOT EXISTS "AuditFinding_checkpoint_idx" ON "AuditFinding" ("checkpointResponseId")',
    'CREATE INDEX IF NOT EXISTS "AuditFinding_discipline_idx" ON "AuditFinding" ("disciplineCode")',
    # ── WP-40 ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS "EvidencePackJob" (
        "id"                     TEXT PRIMARY KEY,
        "scopeKind"              TEXT NOT NULL,
        "scopeId"                TEXT NOT NULL,
        "status"                 TEXT NOT NULL DEFAULT 'QUEUED',
        "progressPct"            INTEGER NOT NULL DEFAULT 0,
        "currentStep"            TEXT,
        "includeEvidencePhotos"  BOOLEAN NOT NULL DEFAULT TRUE,
        "includeFullRegister"    BOOLEAN NOT NULL DEFAULT TRUE,
        "itemCount"              INTEGER NOT NULL DEFAULT 0,
        "totalBytes"             INTEGER NOT NULL DEFAULT 0,
        "manifest"               JSONB NOT NULL DEFAULT '[]'::jsonb,
        "storagePath"            TEXT,
        "errorMessage"           TEXT,
        "requestedById"          TEXT NOT NULL,
        "requestedAt"            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "completedAt"            TIMESTAMPTZ
    )
    """,
    'CREATE INDEX IF NOT EXISTS "ix_EvidencePackJob_scope" ON "EvidencePackJob" ("scopeKind", "scopeId", "status")',
    # ── WP-43 ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS "NotificationPreference" (
        "id"             TEXT PRIMARY KEY,
        "userId"         TEXT NOT NULL,
        "module"         TEXT NOT NULL DEFAULT 'CAMS',
        "eventClass"     TEXT NOT NULL,
        "inAppEnabled"   BOOLEAN NOT NULL DEFAULT TRUE,
        "emailFrequency" TEXT NOT NULL DEFAULT 'DAILY',
        "createdAt"      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "updatedAt"      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_NotificationPreference" '
    'ON "NotificationPreference" ("userId", "module", "eventClass")',
    'CREATE INDEX IF NOT EXISTS "NotificationPreference_userId_idx" ON "NotificationPreference" ("userId")',
    # ── WP-45 ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS "SupplierAuditLink" (
        "id"                      TEXT PRIMARY KEY,
        "engagementKind"          TEXT NOT NULL,
        "engagementId"            TEXT NOT NULL,
        "vendorProfileId"         TEXT NOT NULL,
        "vendorSiteRef"           TEXT,
        "supplierContactName"     TEXT,
        "supplierContactEmail"    TEXT,
        "criticalityAtScheduling" TEXT,
        "tierAtScheduling"        TEXT,
        "createdAt"               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "createdById"             TEXT
    )
    """,
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_SupplierAuditLink" '
    'ON "SupplierAuditLink" ("engagementKind", "engagementId", "vendorProfileId")',
    'CREATE INDEX IF NOT EXISTS "ix_SupplierAuditLink_vendor" ON "SupplierAuditLink" ("vendorProfileId")',
    'CREATE INDEX IF NOT EXISTS "SupplierAuditLink_engagementId_idx" ON "SupplierAuditLink" ("engagementId")',
    # ── WP-46 ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS "CheckpointTranslation" (
        "id"             TEXT PRIMARY KEY,
        "libraryCode"    TEXT NOT NULL,
        "checkpointCode" TEXT NOT NULL,
        "language"       TEXT NOT NULL,
        "questionText"   TEXT NOT NULL,
        "guidance"       TEXT,
        "source"         TEXT NOT NULL DEFAULT 'HUMAN',
        "reviewedById"   TEXT,
        "reviewedAt"     TIMESTAMPTZ,
        "createdAt"      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "updatedAt"      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_CheckpointTranslation" '
    'ON "CheckpointTranslation" ("libraryCode", "checkpointCode", "language")',
    'CREATE INDEX IF NOT EXISTS "CheckpointTranslation_lib_idx" ON "CheckpointTranslation" ("libraryCode")',
    'CREATE INDEX IF NOT EXISTS "CheckpointTranslation_cp_idx" ON "CheckpointTranslation" ("checkpointCode")',
]

TABLES = [
    "AuditFinding",
    "EvidencePackJob",
    "NotificationPreference",
    "SupplierAuditLink",
    "CheckpointTranslation",
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
            ok = bool(s.execute(text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=:t"), {"t": t}).first())
            print(f"  table {t:<26} {'present' if ok else 'MISSING'}")
            failures += 0 if ok else 1

        # WP-19 is additive: the checkpoint columns stay authoritative until
        # WP-18 unifies the engines, so a zero backfill here is EXPECTED, not a
        # gap. `backfill_audit_findings.py` populates it separately.
        n = s.execute(text('SELECT count(*) FROM "AuditFinding"')).scalar_one()
        adverse = s.execute(text(
            'SELECT count(*) FROM "AuditCheckpointResponse" r '
            'JOIN "ComplianceAudit" a ON a.id = r."auditId" '
            "WHERE a.\"isDeleted\"=false AND r.\"assessmentStatus\" IN ('FAIL','PARTIAL')"
        )).scalar_one()
        print(f"\n  {n} AuditFinding row(s); {adverse} adverse checkpoint(s) on live audits.")
        if not n and adverse:
            print("  Run scripts/backfill_audit_findings.py to promote them.")

    print("\nDONE" if not failures else f"\nDONE with {failures} MISSING")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
