"""One-off: PIL/MR/F04-R1 Internal Audit NC Report columns on AuditFinding.

Page Industries issue one numbered Non Conformance Report per non-conformity
raised in a management-system audit (QMS / EMS / OHSMS / EnMS). Revision R1 of
that form replaced its preventive-action box with a **Root Cause Analysis**
section, so an NC now carries a mandatory Why-Why analysis before any action
can be planned against it.

Adds the parts of the form that belong to the NC itself:

    ncrNumber            "NCR Number : 01" — per-audit sequence, auditor half
    rcaId                the governed RootCauseAnalysis holding the Why ladder
    rcaStatus            mirror of that RCA's status, so the register needs no join
    orgRepresentativeId  "Organization Representative" (form row 14)
    verificationDetails  "Verification Details for effective closure" (row 26)
    auditorSignedById    "Auditor Signature"  (row 30)
    auditorSignedAt
    mrSignedById         "M.R. Signature"     (row 30)
    mrSignedAt

The RCA content itself is NOT stored here — it lives in `RootCauseAnalysis`
(methodology FIVE_WHY, the ladder in `analysisPayload`), and the Correction /
Preventive Action rows live in `CapaAction`. This table only holds the join and
the closure signatures, because a second copy of an analysis is a second thing
that can be wrong.

Additive only — ALTER TABLE ... ADD COLUMN IF NOT EXISTS through the SYNC
(psycopg2) engine, same pattern as add_page_grading_columns.py, so a later
`prisma db push` cannot be what applies this. Re-runnable.

Run from the backend root:
    python scripts/add_nc_report_rca_capa.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Running a FILE puts `scripts/` on sys.path, not the backend root, so `app` is
# not importable however sensible the working directory is. Same bootstrap as
# scripts/seed_page_audit_category_libraries.py, so the command in the docstring
# above actually works instead of needing PYTHONPATH=. in front of it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import get_settings  # noqa: E402

ALTERS: list[str] = [
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "ncrNumber" TEXT',
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "rcaId" TEXT',
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "rcaStatus" TEXT',
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "orgRepresentativeId" TEXT',
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "verificationDetails" TEXT',
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "auditorSignedById" TEXT',
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "auditorSignedAt" TIMESTAMPTZ',
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "mrSignedById" TEXT',
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "mrSignedAt" TIMESTAMPTZ',
    # The auditor's half as written on the form — seeded from the checkpoint,
    # then editable, so editing an issued NC report cannot rewrite the audit
    # evidence it was raised from.
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "requirementText" TEXT',
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "observedNonconformity" TEXT',
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "evidenceNote" TEXT',
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "gradeText" TEXT',
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "clauseNo" TEXT',
    # Custody — the two moments the form changes hands.
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "issuedAt" TIMESTAMPTZ',
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "issuedById" TEXT',
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "auditeeSubmittedAt" TIMESTAMPTZ',
    'ALTER TABLE "AuditFinding" ADD COLUMN IF NOT EXISTS "auditeeSubmittedById" TEXT',
    'CREATE INDEX IF NOT EXISTS "ix_AuditFinding_rca" ON "AuditFinding" ("rcaId")',
    # Partial: findings from every other library carry a NULL ncrNumber and are
    # not in this index at all.
    'CREATE INDEX IF NOT EXISTS "ix_AuditFinding_audit_ncr" '
    'ON "AuditFinding" ("auditId", "ncrNumber") WHERE "ncrNumber" IS NOT NULL',
]

# An NCR number must be unique WITHIN its audit — "NCR 03" has to name one
# non-conformity at the closure meeting. Enforced in the database rather than
# only in `next_ncr_number`, because two auditors pressing Trigger at the same
# moment is exactly how a sequence allocated by reading the current maximum
# issues the same number twice.
CONSTRAINTS: list[str] = [
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_AuditFinding_audit_ncr" '
    'ON "AuditFinding" ("auditId", "ncrNumber") '
    'WHERE "ncrNumber" IS NOT NULL AND "isDeleted" = FALSE',
]


def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, pool_pre_ping=True)
    with engine.begin() as conn:
        for stmt in ALTERS + CONSTRAINTS:
            conn.execute(text(stmt))
            print(f"  ok  {stmt[:90]}")
    print("PIL NC report columns applied.")


if __name__ == "__main__":
    main()
