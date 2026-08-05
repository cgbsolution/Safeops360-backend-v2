"""One-off: add custom-checkpoint storage to AuditTemplate (audit-lifecycle v2,
Gate 4). Additive only — ALTER TABLE ... ADD COLUMN IF NOT EXISTS through the
SYNC engine, same pattern as add_audit_lifecycle_v2.py. Re-runnable.

Run from the backend root:
    .venv/Scripts/python.exe scripts/add_template_custom_checkpoints.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

DDL: list[str] = [
    'ALTER TABLE "AuditTemplate" ADD COLUMN IF NOT EXISTS "customCheckpoints" JSONB NOT NULL DEFAULT \'[]\'::jsonb',
    'ALTER TABLE "AuditTemplate" ADD COLUMN IF NOT EXISTS "parentTemplateId" TEXT',
]

CHECK = """
SELECT column_name FROM information_schema.columns
WHERE table_name = 'AuditTemplate'
  AND column_name IN ('customCheckpoints', 'parentTemplateId')
ORDER BY column_name
"""


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        before = [r[0] for r in s.execute(text(CHECK))]
        print(f"Before: {before or 'neither column exists'}")
        for ddl in DDL:
            s.execute(text(ddl))
        s.commit()
        after = [r[0] for r in s.execute(text(CHECK))]
        print(f"After:  {after}")
        ok = len(after) == 2
        print("RESULT:", "OK" if ok else "INCOMPLETE")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
