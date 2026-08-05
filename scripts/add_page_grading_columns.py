"""One-off: Page Industries grading columns on AuditCheckpointResponse.

Adds the workbook's columns C–F, H and I as first-class columns:

    requirementType   I — STATUTORY_REGULATORY | INTERNAL_REQUIREMENT (master data)
    gradeAwarded      C — the auditor's grade
    scoreAllotted     D — 3, or NULL for an N/A checkpoint
    scoreObtained     E — 3 | 2 | 1 | 0 | -1
    complianceStatus  F — Complied / Non Compliance / … / MAS (N/A)
    riskGrade         H — HIGH | MEDIUM | LOW

They are columns rather than keys inside the `auditorResponse` JSON because the
discipline rollup and the audit score are SQL aggregates over audits that can
run to 1,500 checkpoints; summing points out of a JSON blob would force a full
row load on every navigator repaint.

Additive only — ALTER TABLE ... ADD COLUMN IF NOT EXISTS through the SYNC
(psycopg2) engine, same pattern as add_audit_lifecycle_v2.py, so a later
`prisma db push` cannot be what applies this. Re-runnable.

Run from the backend root:
    python scripts/add_page_grading_columns.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from app.core.config import get_settings

ALTERS: list[str] = [
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "requirementType" TEXT',
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "gradeAwarded" TEXT',
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "scoreAllotted" INTEGER',
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "scoreObtained" INTEGER',
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "complianceStatus" TEXT',
    'ALTER TABLE "AuditCheckpointResponse" ADD COLUMN IF NOT EXISTS "riskGrade" TEXT',
    'CREATE INDEX IF NOT EXISTS "AuditCheckpointResponse_auditId_gradeAwarded_idx" '
    'ON "AuditCheckpointResponse" ("auditId", "gradeAwarded")',
]

# Backfill for checkpoints already answered under the old pass/partial/fail/na
# verdict. Without it every pre-existing demo audit would read "not graded" and
# score 0% — the rows ARE assessed, they just predate the vocabulary. The map is
# the inverse of page_grading.GRADE_TO_VALUE, with PARTIAL landing on
# `Some Improvement Needed` (its 0.5 credit becomes 2 of 3).
BACKFILL: list[str] = [
    """
    UPDATE "AuditCheckpointResponse" SET
        "gradeAwarded" = CASE "assessmentStatus"
            WHEN 'PASS'    THEN 'EFFECTIVE'
            WHEN 'PARTIAL' THEN 'SOME_IMPROVEMENT_NEEDED'
            WHEN 'FAIL'    THEN 'MAJOR_IMPROVEMENT_NEEDED'
            WHEN 'NA'      THEN 'NA'
        END,
        "scoreAllotted" = CASE WHEN "assessmentStatus" = 'NA' THEN NULL ELSE 3 END,
        "scoreObtained" = CASE "assessmentStatus"
            WHEN 'PASS'    THEN 3
            WHEN 'PARTIAL' THEN 2
            WHEN 'FAIL'    THEN 1
            ELSE NULL
        END,
        "complianceStatus" = CASE "assessmentStatus"
            WHEN 'PASS'    THEN 'COMPLIED'
            WHEN 'PARTIAL' THEN 'NEW_OBSERVATION'
            WHEN 'FAIL'    THEN 'NON_COMPLIANCE'
            WHEN 'NA'      THEN 'NA'
        END,
        -- Risk grade is the auditor's judgement and cannot be invented, so it
        -- is seeded from the checkpoint's inherent criticality and stays
        -- editable rather than being left null on a live finding.
        "riskGrade" = CASE
            WHEN "assessmentStatus" IN ('PASS', 'NA') THEN NULL
            WHEN "criticality" = 'critical' THEN 'HIGH'
            WHEN "criticality" = 'major'    THEN 'MEDIUM'
            ELSE 'LOW'
        END
    WHERE "assessmentStatus" <> 'NOT_ASSESSED' AND "gradeAwarded" IS NULL
    """,
]


def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, pool_pre_ping=True)
    with engine.begin() as conn:
        for stmt in ALTERS:
            conn.execute(text(stmt))
            print(f"  ok  {stmt[:90]}")
        for stmt in BACKFILL:
            result = conn.execute(text(stmt))
            print(f"  backfilled {result.rowcount} answered checkpoint(s)")
    print("Page grading columns applied.")


if __name__ == "__main__":
    main()
