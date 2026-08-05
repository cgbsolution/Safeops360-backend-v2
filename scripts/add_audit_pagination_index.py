"""One-off: pagination index for large-audit checkpoint listing (additive).

Adds an index on ("auditId", "sequence") so the cursor-paginated
GET /api/audit-compliance/{id}/checkpoints endpoint (1500-checkpoint support)
orders + seeks efficiently. Additive + re-runnable (CREATE INDEX IF NOT EXISTS)
through the SYNC engine — never `prisma db push` (would drop the Cams* tables).

Run from the backend root:
    .venv/Scripts/python.exe scripts/add_audit_pagination_index.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

STMTS: list[str] = [
    'CREATE INDEX IF NOT EXISTS "AuditCheckpointResponse_auditId_sequence_idx" '
    'ON "AuditCheckpointResponse" ("auditId", "sequence")',
]

CHECK = (
    "SELECT indexname FROM pg_indexes "
    "WHERE schemaname='public' AND tablename='AuditCheckpointResponse' "
    "AND indexname = 'AuditCheckpointResponse_auditId_sequence_idx'"
)


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        before = set(s.execute(text(CHECK)).scalars().all())
        for stmt in STMTS:
            s.execute(text(stmt))
        s.commit()
        after = set(s.execute(text(CHECK)).scalars().all())
        print(f"Index added: {sorted(after - before) or '(already existed)'}")
        ok = "AuditCheckpointResponse_auditId_sequence_idx" in after
        print("RESULT:", "OK" if ok else "INCOMPLETE")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
