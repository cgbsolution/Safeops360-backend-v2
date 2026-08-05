"""One-off: create the shared Evidence Attachment table (Stream B §5). Additive.

  • Attachment — one generic attachment table keyed by (entityType, entityId),
    with documentCategory (§6 AI key), slot-based versioning, and reserved
    extraction JSON.

Idempotent (CREATE TABLE / INDEX IF NOT EXISTS). Follows the hand-DDL migration
policy — do NOT rely on `prisma db push` (it would drop drifted tables). Run:

    python -m scripts.create_attachment_tables

Mirror on the Prisma side: safeops_360/prisma/apply-attachment-ddl.ts (+ the
`Attachment` model in schema.prisma). Both are kept in agreement by hand.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

DDL = [
    """
    CREATE TABLE IF NOT EXISTS "Attachment" (
        "id" TEXT PRIMARY KEY,
        "entityType" TEXT NOT NULL,
        "entityId" TEXT NOT NULL,
        "category" TEXT NOT NULL,
        "documentCategory" TEXT,
        "fileName" TEXT NOT NULL,
        "storagePath" TEXT NOT NULL,
        "fileSize" INTEGER NOT NULL,
        "mimeType" TEXT NOT NULL,
        "caption" TEXT,
        "slotKey" TEXT,
        "version" INTEGER NOT NULL DEFAULT 1,
        "supersedesId" TEXT,
        "isCurrent" BOOLEAN NOT NULL DEFAULT true,
        "extraction" JSONB,
        "uploadedById" TEXT NOT NULL REFERENCES "User"("id"),
        "uploadedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "deletedAt" TIMESTAMPTZ
    )
    """,
    'CREATE INDEX IF NOT EXISTS "ix_Attachment_entityType" ON "Attachment" ("entityType")',
    'CREATE INDEX IF NOT EXISTS "ix_Attachment_entityId" ON "Attachment" ("entityId")',
    'CREATE INDEX IF NOT EXISTS "ix_Attachment_entity" ON "Attachment" ("entityType", "entityId")',
    'CREATE INDEX IF NOT EXISTS "ix_Attachment_entity_current" ON "Attachment" ("entityType", "entityId", "isCurrent")',
    'CREATE INDEX IF NOT EXISTS "ix_Attachment_documentCategory" ON "Attachment" ("documentCategory")',
    'CREATE INDEX IF NOT EXISTS "ix_Attachment_slotKey" ON "Attachment" ("slotKey")',
    'CREATE INDEX IF NOT EXISTS "ix_Attachment_isCurrent" ON "Attachment" ("isCurrent")',
    'CREATE INDEX IF NOT EXISTS "ix_Attachment_deletedAt" ON "Attachment" ("deletedAt")',
]
TABLES = ["Attachment"]


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
