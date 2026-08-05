"""Supplier portal tables (WP-45 stage 2) - additive DDL.

  SupplierPortalToken       one opaque, expiring credential per audit
  SupplierPortalSubmission  what the SUPPLIER sent (comments + evidence)
  SupplierPortalAccessLog   every attempt against a token, successful or not

**Why submissions are their own table.** `CapaComment.authorUserId` and
`Attachment.uploadedById` are NOT NULL foreign keys to `User`. A vendor factory
manager has no `User` row, so recording their response in either would mean
minting a fake identity in the RBAC system or widening two core platform tables
every module depends on. Neither is justifiable, and keeping external input
separate is also what makes "the supplier said this" structurally impossible to
confuse with "our engineer typed it on their behalf".

Additive + re-runnable. Never `prisma db push` (Cams* drift would drop tables).

    .venv/Scripts/python.exe scripts/add_supplier_portal.py

WARNING: the backend .env points at PRODUCTION.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

STMTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS "SupplierPortalToken" (
        "id"                    TEXT PRIMARY KEY,
        "engagementKind"        TEXT NOT NULL DEFAULT 'AUDIT',
        "auditId"               TEXT NOT NULL,
        "vendorProfileId"       TEXT,
        -- Only the SHA-256 hash is stored: a database read must not be
        -- replayable as portal access.
        "tokenHash"             TEXT NOT NULL UNIQUE,
        "tokenPrefix"           TEXT NOT NULL,
        "supplierContactEmail"  TEXT NOT NULL,
        "supplierContactName"   TEXT,
        "expiresAt"             TIMESTAMPTZ NOT NULL,
        "createdAt"             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "createdById"           TEXT,
        "revokedAt"             TIMESTAMPTZ,
        "revokedById"           TEXT,
        "revokedReason"         TEXT,
        "lastAccessedAt"        TIMESTAMPTZ,
        "accessCount"           INTEGER NOT NULL DEFAULT 0,
        "emailSentAt"           TIMESTAMPTZ
    )
    """,
    'CREATE INDEX IF NOT EXISTS "SupplierPortalToken_auditId_idx" ON "SupplierPortalToken" ("auditId")',
    'CREATE INDEX IF NOT EXISTS "SupplierPortalToken_vendor_idx" ON "SupplierPortalToken" ("vendorProfileId")',
    'CREATE INDEX IF NOT EXISTS "SupplierPortalToken_tokenHash_idx" ON "SupplierPortalToken" ("tokenHash")',
    'CREATE INDEX IF NOT EXISTS "SupplierPortalToken_expiresAt_idx" ON "SupplierPortalToken" ("expiresAt")',
    'CREATE INDEX IF NOT EXISTS "ix_SupplierPortalToken_audit_live" '
    'ON "SupplierPortalToken" ("auditId", "revokedAt", "expiresAt")',
    """
    CREATE TABLE IF NOT EXISTS "SupplierPortalSubmission" (
        "id"                     TEXT PRIMARY KEY,
        "tokenId"                TEXT NOT NULL REFERENCES "SupplierPortalToken"("id") ON DELETE CASCADE,
        "auditId"                TEXT NOT NULL,
        "kind"                   TEXT NOT NULL,
        "checkpointResponseId"   TEXT,
        "capaId"                 TEXT,
        "body"                   TEXT NOT NULL DEFAULT '',
        "fileName"               TEXT,
        "storagePath"            TEXT,
        "fileSize"               INTEGER,
        "mimeType"               TEXT,
        -- The external actor, denormalised so it survives token revocation.
        "submittedByEmail"       TEXT NOT NULL,
        "submittedByName"        TEXT,
        "submittedAt"            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "acknowledgedAt"         TIMESTAMPTZ,
        "acknowledgedById"       TEXT
    )
    """,
    'CREATE INDEX IF NOT EXISTS "SupplierPortalSubmission_tokenId_idx" ON "SupplierPortalSubmission" ("tokenId")',
    'CREATE INDEX IF NOT EXISTS "SupplierPortalSubmission_auditId_idx" ON "SupplierPortalSubmission" ("auditId")',
    'CREATE INDEX IF NOT EXISTS "SupplierPortalSubmission_cp_idx" ON "SupplierPortalSubmission" ("checkpointResponseId")',
    'CREATE INDEX IF NOT EXISTS "SupplierPortalSubmission_capa_idx" ON "SupplierPortalSubmission" ("capaId")',
    'CREATE INDEX IF NOT EXISTS "ix_SupplierPortalSubmission_audit_kind" '
    'ON "SupplierPortalSubmission" ("auditId", "kind")',
    """
    CREATE TABLE IF NOT EXISTS "SupplierPortalAccessLog" (
        "id"           TEXT PRIMARY KEY,
        "tokenId"      TEXT,
        "tokenPrefix"  TEXT,
        "auditId"      TEXT,
        "outcome"      TEXT NOT NULL,
        "action"       TEXT NOT NULL,
        "ipAddress"    TEXT,
        "userAgent"    TEXT,
        "at"           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "isWrite"      BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    'CREATE INDEX IF NOT EXISTS "SupplierPortalAccessLog_tokenId_idx" ON "SupplierPortalAccessLog" ("tokenId")',
    'CREATE INDEX IF NOT EXISTS "SupplierPortalAccessLog_prefix_idx" ON "SupplierPortalAccessLog" ("tokenPrefix")',
    'CREATE INDEX IF NOT EXISTS "SupplierPortalAccessLog_auditId_idx" ON "SupplierPortalAccessLog" ("auditId")',
    'CREATE INDEX IF NOT EXISTS "SupplierPortalAccessLog_outcome_idx" ON "SupplierPortalAccessLog" ("outcome")',
    'CREATE INDEX IF NOT EXISTS "SupplierPortalAccessLog_at_idx" ON "SupplierPortalAccessLog" ("at")',
]

TABLES = [
    "SupplierPortalToken",
    "SupplierPortalSubmission",
    "SupplierPortalAccessLog",
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
            print(f"  table {t:<28} {'present' if ok else 'MISSING'}")
            failures += 0 if ok else 1

        # SupplierAuditLink is the prerequisite: a portal token is scoped to a
        # supplier audit, and there are none without links.
        linked = s.execute(text(
            'SELECT count(*) FROM "SupplierAuditLink" WHERE "engagementKind" = \'AUDIT\''
        )).scalar_one()
        live = s.execute(text(
            'SELECT count(*) FROM "SupplierPortalToken" '
            'WHERE "revokedAt" IS NULL AND "expiresAt" > NOW()'
        )).scalar_one()
        print(f"\n  {linked} supplier audit link(s); {live} live portal token(s).")

    print("\nDONE" if not failures else f"\nDONE with {failures} MISSING")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
