"""One-off: per-discipline auditor assignment (additive).

Adds "assignedAuditorId" to AuditCheckpointResponse so each checkpoint records
which auditor conducts it (the twin of assignedOwnerId, which is the auditee).
coAuditors on ComplianceAudit is already JSONB — its shape becomes
[{userId, disciplineIds}] at the app layer (legacy flat [userId] still accepted),
so no DDL is needed there.

Additive + re-runnable (ADD COLUMN / CREATE INDEX IF NOT EXISTS) through the
SYNC engine — never `prisma db push` (would drop the Cams* tables).

    .venv/Scripts/python.exe scripts/add_audit_multi_auditor.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

STMTS: list[str] = [
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "assignedAuditorId" TEXT',
    'CREATE INDEX IF NOT EXISTS "AuditCheckpointResponse_auditId_assignedAuditorId_idx" '
    'ON "AuditCheckpointResponse" ("auditId", "assignedAuditorId")',
]

CHECK_COL = (
    "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
    "AND table_name='AuditCheckpointResponse' AND column_name='assignedAuditorId'"
)
CHECK_IDX = (
    "SELECT 1 FROM pg_indexes WHERE schemaname='public' "
    "AND indexname='AuditCheckpointResponse_auditId_assignedAuditorId_idx'"
)


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        for stmt in STMTS:
            s.execute(text(stmt))
        s.commit()
        col = bool(s.execute(text(CHECK_COL)).first())
        idx = bool(s.execute(text(CHECK_IDX)).first())
        print(f"assignedAuditorId column: {'present' if col else 'MISSING'}")
        print(f"auditor index: {'present' if idx else 'MISSING'}")
        ok = col and idx
        print("RESULT:", "OK" if ok else "INCOMPLETE")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
