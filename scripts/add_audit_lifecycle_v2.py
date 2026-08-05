"""One-off: Audit Lifecycle v2 schema (additive).

Adds the scoped-scheduling / ownership / two-axis-state / ad-hoc columns to the
existing ComplianceAudit + AuditCheckpointResponse tables, and creates the two
new tables CheckpointInteraction (append-only iteration thread) and AuditReport
(Interim + Final snapshots).

Additive only — ALTER TABLE ... ADD COLUMN IF NOT EXISTS and CREATE TABLE IF
NOT EXISTS through the SYNC (psycopg2) engine, same pattern as
create_audit_tables.py / add_ptw_ppe_gate_columns.py, so we never risk a
`prisma db push` dropping the Cams* tables. Re-runnable.

Run from the backend root:
    .venv/Scripts/python.exe scripts/add_audit_lifecycle_v2.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

# ── 1. New columns on existing tables ────────────────────────────────────
ALTERS: list[str] = [
    # ComplianceAudit — discipline scope.
    'ALTER TABLE "ComplianceAudit" ADD COLUMN IF NOT EXISTS "selectedDisciplineIds" JSONB NOT NULL DEFAULT \'[]\'::jsonb',
    'ALTER TABLE "ComplianceAudit" ADD COLUMN IF NOT EXISTS "scopePresetUsed" TEXT',
    'ALTER TABLE "ComplianceAudit" ADD COLUMN IF NOT EXISTS "materializedCheckpointCount" INTEGER NOT NULL DEFAULT 0',
    'ALTER TABLE "ComplianceAudit" ADD COLUMN IF NOT EXISTS "adHocCount" INTEGER NOT NULL DEFAULT 0',
    # AuditCheckpointResponse — ownership.
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "assignedOwnerId" TEXT',
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "assignedById" TEXT',
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "assignedAt" TIMESTAMPTZ',
    # AuditCheckpointResponse — ad-hoc.
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "isAdHoc" BOOLEAN NOT NULL DEFAULT false',
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "addedById" TEXT',
    # AuditCheckpointResponse — two-axis state.
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "assessmentStatus" TEXT NOT NULL DEFAULT \'NOT_ASSESSED\'',
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "workflowState" TEXT NOT NULL DEFAULT \'OPEN\'',
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "currentRound" INTEGER NOT NULL DEFAULT 0',
    # AuditCheckpointResponse — carousel capture.
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "observation" TEXT',
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "auditorNote" TEXT',
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "orderIndex" INTEGER NOT NULL DEFAULT 0',
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "auditorEvidenceIds" JSONB NOT NULL DEFAULT \'[]\'::jsonb',
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "auditeeEvidenceIds" JSONB NOT NULL DEFAULT \'[]\'::jsonb',
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "capaId" TEXT',
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "finalizedAt" TIMESTAMPTZ',
    # New indexes on the ownership / state columns.
    'CREATE INDEX IF NOT EXISTS "AuditCheckpointResponse_auditId_assignedOwnerId_idx" ON "AuditCheckpointResponse" ("auditId", "assignedOwnerId")',
    'CREATE INDEX IF NOT EXISTS "AuditCheckpointResponse_auditId_workflowState_idx" ON "AuditCheckpointResponse" ("auditId", "workflowState")',
]

# ── 2. New tables ────────────────────────────────────────────────────────
CREATES: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS "CheckpointInteraction" (
        "id" TEXT PRIMARY KEY,
        "checkpointInstanceId" TEXT NOT NULL REFERENCES "AuditCheckpointResponse"("id") ON DELETE CASCADE,
        "auditId" TEXT NOT NULL,
        "round" INTEGER NOT NULL DEFAULT 0,
        "actorId" TEXT NOT NULL,
        "actorRole" TEXT NOT NULL,
        "action" TEXT NOT NULL,
        "comment" TEXT,
        "evidenceIds" JSONB NOT NULL DEFAULT '[]'::jsonb,
        "resultingState" TEXT NOT NULL,
        "timestamp" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    'CREATE INDEX IF NOT EXISTS "CheckpointInteraction_checkpointInstanceId_idx" ON "CheckpointInteraction" ("checkpointInstanceId")',
    'CREATE INDEX IF NOT EXISTS "CheckpointInteraction_auditId_idx" ON "CheckpointInteraction" ("auditId")',
    """
    CREATE TABLE IF NOT EXISTS "AuditReport" (
        "id" TEXT PRIMARY KEY,
        "auditId" TEXT NOT NULL REFERENCES "ComplianceAudit"("id") ON DELETE CASCADE,
        "siteId" TEXT NOT NULL,
        "reportType" TEXT NOT NULL,
        "reportCode" TEXT NOT NULL UNIQUE,
        "generatedById" TEXT NOT NULL,
        "generatedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "snapshot" JSONB NOT NULL DEFAULT '{}'::jsonb,
        "signOffs" JSONB,
        "pdfAttachmentId" TEXT,
        "isSuperseded" BOOLEAN NOT NULL DEFAULT false,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    'CREATE INDEX IF NOT EXISTS "AuditReport_auditId_reportType_idx" ON "AuditReport" ("auditId", "reportType")',
]

NEW_TABLES = ["CheckpointInteraction", "AuditReport"]

CHECK_COLS = """
SELECT table_name, column_name FROM information_schema.columns
WHERE table_schema = 'public'
  AND (
    (table_name = 'ComplianceAudit' AND column_name IN
      ('selectedDisciplineIds','scopePresetUsed','materializedCheckpointCount','adHocCount'))
    OR
    (table_name = 'AuditCheckpointResponse' AND column_name IN
      ('assignedOwnerId','assignedById','assignedAt','isAdHoc','addedById','assessmentStatus',
       'workflowState','currentRound','observation','auditorNote','orderIndex',
       'auditorEvidenceIds','auditeeEvidenceIds','capaId','finalizedAt'))
  )
ORDER BY table_name, column_name
"""

CHECK_TABLES = (
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema='public' AND table_name = ANY(:n)"
)


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        cols_before = {(r[0], r[1]) for r in s.execute(text(CHECK_COLS))}
        tbl_before = set(s.execute(text(CHECK_TABLES), {"n": NEW_TABLES}).scalars().all())

        for stmt in ALTERS + CREATES:
            s.execute(text(stmt))
        s.commit()

        cols_after = {(r[0], r[1]) for r in s.execute(text(CHECK_COLS))}
        tbl_after = set(s.execute(text(CHECK_TABLES), {"n": NEW_TABLES}).scalars().all())

        added_cols = sorted(f"{t}.{c}" for (t, c) in (cols_after - cols_before))
        added_tbls = sorted(tbl_after - tbl_before)
        print(f"Columns added:  {added_cols or '(all existed)'}")
        print(f"Tables created: {added_tbls or '(all existed)'}")
        print(f"Columns present: {len(cols_after)}/19 | Tables present: {sorted(tbl_after)}")
        ok = len(cols_after) == 19 and set(NEW_TABLES) <= tbl_after
        print("RESULT:", "OK" if ok else "INCOMPLETE")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
