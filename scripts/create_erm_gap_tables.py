"""One-off: create the ERM gap-closure tables (Page Industries demo build). Additive.

  • RiskAttachment    — supporting documents on an enterprise risk (§2g)
  • ControlAttachment — evidence files + review-schedule evidence on a control (§6d)
  • Notification      — in-app + email alert feed (§8b/c/d/e)

Idempotent (CREATE TABLE / INDEX IF NOT EXISTS). Follows the hand-DDL migration
policy — do NOT rely on `prisma db push` (it would drop drifted tables). Run:

    python -m scripts.create_erm_gap_tables
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

DDL = [
    """
    CREATE TABLE IF NOT EXISTS "RiskAttachment" (
        "id" TEXT PRIMARY KEY,
        "riskId" TEXT NOT NULL REFERENCES "EnterpriseRisk"("id") ON DELETE CASCADE,
        "category" TEXT NOT NULL,
        "fileName" TEXT NOT NULL,
        "storagePath" TEXT NOT NULL,
        "fileSize" INTEGER NOT NULL,
        "mimeType" TEXT NOT NULL,
        "caption" TEXT,
        "uploadedById" TEXT NOT NULL REFERENCES "User"("id"),
        "uploadedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "deletedAt" TIMESTAMPTZ
    )
    """,
    'CREATE INDEX IF NOT EXISTS "ix_RiskAttachment_riskId" ON "RiskAttachment" ("riskId")',
    'CREATE INDEX IF NOT EXISTS "ix_RiskAttachment_deletedAt" ON "RiskAttachment" ("deletedAt")',
    """
    CREATE TABLE IF NOT EXISTS "ControlAttachment" (
        "id" TEXT PRIMARY KEY,
        "controlId" TEXT NOT NULL REFERENCES "Control"("id") ON DELETE CASCADE,
        "controlTestId" TEXT,
        "category" TEXT NOT NULL,
        "fileName" TEXT NOT NULL,
        "storagePath" TEXT NOT NULL,
        "fileSize" INTEGER NOT NULL,
        "mimeType" TEXT NOT NULL,
        "caption" TEXT,
        "reviewDate" TIMESTAMPTZ,
        "uploadedById" TEXT NOT NULL REFERENCES "User"("id"),
        "uploadedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "deletedAt" TIMESTAMPTZ
    )
    """,
    'CREATE INDEX IF NOT EXISTS "ix_ControlAttachment_controlId" ON "ControlAttachment" ("controlId")',
    'CREATE INDEX IF NOT EXISTS "ix_ControlAttachment_deletedAt" ON "ControlAttachment" ("deletedAt")',
    """
    CREATE TABLE IF NOT EXISTS "Notification" (
        "id" TEXT PRIMARY KEY,
        "userId" TEXT NOT NULL REFERENCES "User"("id"),
        "type" TEXT NOT NULL,
        "severity" TEXT NOT NULL DEFAULT 'INFO',
        "title" TEXT NOT NULL,
        "body" TEXT NOT NULL DEFAULT '',
        "entityType" TEXT,
        "entityId" TEXT,
        "linkUrl" TEXT,
        "isRead" BOOLEAN NOT NULL DEFAULT false,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "readAt" TIMESTAMPTZ
    )
    """,
    'CREATE INDEX IF NOT EXISTS "ix_Notification_userId" ON "Notification" ("userId")',
    'CREATE INDEX IF NOT EXISTS "ix_Notification_isRead" ON "Notification" ("isRead")',
    'CREATE INDEX IF NOT EXISTS "ix_Notification_createdAt" ON "Notification" ("createdAt")',
    'CREATE INDEX IF NOT EXISTS "ix_Notification_user_read" ON "Notification" ("userId", "isRead")',
]
TABLES = ["RiskAttachment", "ControlAttachment", "Notification"]


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
