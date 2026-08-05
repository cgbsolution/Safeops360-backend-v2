"""WP-19 - promote existing adverse checkpoints to first-class AuditFinding rows.

Every `AuditCheckpointResponse` with `assessmentStatus IN (FAIL, PARTIAL)` on a
live audit becomes an `AuditFinding`. Derivable from what is already there, so
no data is invented and nothing is lost.

**Depends on the WP-02 backfill.** Before that ran, 242 rows read NOT_ASSESSED
while holding a real verdict in JSON, so this would have silently skipped them.
Run order matters: backfill_assessment_status.py, then this.

Idempotent - a checkpoint that already has a live finding is skipped.

    .venv/Scripts/python.exe scripts/backfill_audit_findings.py            # dry run
    .venv/Scripts/python.exe scripts/backfill_audit_findings.py --commit

WARNING: the backend .env points at PRODUCTION.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.cams_completion import AuditFinding
from app.services.audit_findings import severity_for, sync_finding_for_checkpoint


async def run(commit: bool) -> int:
    async with AsyncSessionLocal() as db:
        audits = (
            await db.execute(
                select(ComplianceAudit).where(ComplianceAudit.isDeleted.is_(False))
            )
        ).scalars().all()

        existing = {
            r[0]
            for r in (
                await db.execute(
                    select(AuditFinding.checkpointResponseId).where(
                        AuditFinding.isDeleted.is_(False)
                    )
                )
            ).all()
            if r[0]
        }

        print("-- adverse checkpoints on live audits ---------------")
        created = skipped = 0
        by_sev: dict[str, int] = {}

        for audit in audits:
            rows = (
                await db.execute(
                    select(AuditCheckpointResponse).where(
                        AuditCheckpointResponse.auditId == audit.id,
                        AuditCheckpointResponse.assessmentStatus.in_(("FAIL", "PARTIAL")),
                    )
                )
            ).scalars().all()
            if not rows:
                continue

            fresh = [r for r in rows if r.id not in existing]
            skipped += len(rows) - len(fresh)
            if not fresh:
                continue

            sev_counts: dict[str, int] = {}
            for r in fresh:
                sev = severity_for(r.criticality)
                sev_counts[sev] = sev_counts.get(sev, 0) + 1
                by_sev[sev] = by_sev.get(sev, 0) + 1
                if commit:
                    await sync_finding_for_checkpoint(
                        db, audit=audit, response=r, actor_id="backfill_audit_findings"
                    )
            created += len(fresh)
            detail = ", ".join(f"{k}={v}" for k, v in sorted(sev_counts.items()))
            print(f"   {audit.auditNumber:<24} {len(fresh):>3} finding(s)  {detail}")

        if commit:
            await db.commit()

        print(f"\n   {created} created, {skipped} already had a finding")
        if by_sev:
            print("   by severity: " + ", ".join(f"{k}={v}" for k, v in sorted(by_sev.items())))
            obs = by_sev.get("OBSERVATION", 0)
            if obs:
                print(f"\n   {obs} OBSERVATION finding(s) — this class was dropped entirely at")
                print("   the CamsFinding boundary before WP-19 (its enum has no such value).")

        total = (await db.execute(select(AuditFinding))).scalars().all()
        overdue = [f for f in total if f.dueDate and f.status == "OPEN"]
        repeats = [f for f in total if f.isRepeatFinding]
        print(f"\n-- verification -------------------------------------")
        print(f"   {len(total)} AuditFinding row(s) total")
        print(f"   {len(overdue)} carry a due date and are open "
              "(this column was structurally blank before WP-19)")
        print(f"   {len(repeats)} linked into a repeat chain")

    print("\nCOMMITTED." if commit else "\nDRY RUN - nothing written. Re-run with --commit.")
    return 0


def main(commit: bool) -> int:
    return asyncio.run(run(commit))


if __name__ == "__main__":
    raise SystemExit(main("--commit" in sys.argv))
