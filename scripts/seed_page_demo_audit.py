"""Seed one demo audit against the Page Industries checklist.

Materialises a full HR + EHS + Production audit and grades roughly two thirds of
it, spreading the grades so every column of the workbook has something to show:
Effective, both improvement grades, an Unsatisfactory, an N/A, and a Repeated
Non Compliance carrying the -1 penalty.

The audit is left IN PROGRESS rather than submitted — submit routes findings to
auditees and spawns CAPAs, and a demo should show the conduct screen with real
grading on it, not a closed record.

Idempotent by title: re-running deletes the previous demo audit (and its
checkpoint rows, by cascade) and rebuilds it. It touches nothing else.

Run from the backend root:
    python scripts/seed_page_demo_audit.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.core.db import AsyncSessionLocal
from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.user import User
from app.services import audit_compliance as svc
from app.services import page_grading as pg

TITLE = "Internal Audit — HR, EHS & Production (demo)"
INDUSTRY_CODE = "PAGE_INDUSTRIES"

# The grading pattern, applied round-robin across each discipline. Weighted
# toward Effective because a factory that failed two thirds of its checkpoints
# is not a believable demo — but it carries enough of every other grade that
# each column, the -1 penalty and the risk-grade rules are all visible.
PATTERN: list[tuple[str, str | None, str | None]] = [
    # (grade, status override, risk grade)
    (pg.GRADE_EFFECTIVE, None, None),
    (pg.GRADE_EFFECTIVE, None, None),
    (pg.GRADE_SOME_IMPROVEMENT, None, pg.RISK_LOW),
    (pg.GRADE_EFFECTIVE, None, None),
    (pg.GRADE_MAJOR_IMPROVEMENT, None, pg.RISK_MEDIUM),
    (pg.GRADE_EFFECTIVE, None, None),
    (pg.GRADE_EFFECTIVE, None, None),
    (pg.GRADE_SOME_IMPROVEMENT, pg.STATUS_REPEATED_OBSERVATION, pg.RISK_MEDIUM),
    (pg.GRADE_EFFECTIVE, None, None),
    (pg.GRADE_NA, None, None),
    (pg.GRADE_EFFECTIVE, None, None),
    (pg.GRADE_UNSATISFACTORY, pg.STATUS_REPEATED_NON_COMPLIANCE, pg.RISK_HIGH),
    (pg.GRADE_EFFECTIVE, None, None),
    (pg.GRADE_EFFECTIVE, None, None),
    (pg.GRADE_MAJOR_IMPROVEMENT, None, pg.RISK_HIGH),
]

FINDINGS = {
    pg.GRADE_SOME_IMPROVEMENT: (
        "Largely in place, but the records were incomplete for two of the six "
        "months sampled. Discussed with the department head during the walkthrough."
    ),
    pg.GRADE_MAJOR_IMPROVEMENT: (
        "Requirement is only partially met. Evidence was produced for the current "
        "period but the process is not documented and depends on one individual."
    ),
    pg.GRADE_UNSATISFACTORY: (
        "Not met. No evidence could be produced during the audit and the responsible "
        "owner confirmed the control is not currently operating."
    ),
}


async def main() -> None:
    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.email.ilike("%rahul%")).limit(1))
        ).scalar_one_or_none() or (await db.execute(select(User).limit(1))).scalar_one()
        plant_id = (await db.execute(text('SELECT id FROM "Plant" ORDER BY "createdAt" LIMIT 1'))).scalar()
        if not plant_id:
            raise SystemExit("No plant in this database — seed a plant first.")

        # Re-runnable: drop the previous demo audit, checkpoint rows and all.
        old = (
            await db.execute(select(ComplianceAudit).where(ComplianceAudit.title == TITLE))
        ).scalars().all()
        for a in old:
            await db.delete(a)
        if old:
            await db.flush()
            print(f"Removed {len(old)} previous demo audit(s).")

        audit = await svc.create_audit(db, user=user, data={
            "plantId": plant_id,
            "title": TITLE,
            "industryCode": INDUSTRY_CODE,
            "selectedDisciplineIds": [],  # empty = the full library
            "scheduledDate": datetime.now(timezone.utc) - timedelta(days=2),
            "leadAuditorUserId": user.id,
            "auditType": "internal",
            "scopeDescription": "Annual internal audit across HR, EHS and Production.",
        })
        print(f"Created {audit.auditNumber} — {audit.totalCheckpoints} checkpoints.")

        rows = (await db.execute(
            select(AuditCheckpointResponse)
            .where(AuditCheckpointResponse.auditId == audit.id)
            .order_by(AuditCheckpointResponse.sequence)
        )).scalars().all()

        # Grade the first two thirds of each discipline, so the conduct screen
        # opens with work visibly remaining rather than a finished audit.
        by_disc: dict[str, list[AuditCheckpointResponse]] = {}
        for r in rows:
            by_disc.setdefault(r.categoryId, []).append(r)

        graded = 0
        for disc, items in by_disc.items():
            target = int(len(items) * 2 / 3)
            for i, r in enumerate(items[:target]):
                grade, status, risk = PATTERN[i % len(PATTERN)]
                payload: dict = {"checkpointCode": r.checkpointCode, "gradeAwarded": grade}
                if status:
                    payload["complianceStatus"] = status
                if risk:
                    payload["riskGrade"] = risk
                if grade in FINDINGS:
                    payload["auditFindings"] = FINDINGS[grade]
                await svc.save_response(db, user=user, audit_id=audit.id, payload=payload)
                graded += 1

        await db.flush()
        fresh = (await db.execute(
            select(AuditCheckpointResponse).where(AuditCheckpointResponse.auditId == audit.id)
        )).scalars().all()
        a = await db.get(ComplianceAudit, audit.id)
        score = svc._compute_score(a, fresh)
        a.score = score
        a.overallCompliancePct = score["overall_score_pct"]
        a.totalCheckpoints = score["total_checkpoints"]
        a.criticalFailureCount = score["critical_failures"]
        await db.commit()

        print(f"Graded {graded}/{len(rows)} checkpoints.")
        print(f"Score: {score['score_obtained']}/{score['score_allotted']} points "
              f"= {score['overall_score_pct']}%  ({score['score_band']})")
        print(f"  {score['repeat_findings']} repeat finding(s) · "
              f"{score['statutory_findings']} statutory finding(s) · "
              f"{score['critical_failures']} critical failure(s)")
        for c in score["category_scores"]:
            print(f"  {c['category_name']:<32} {c['score_obtained']:>4}/{c['score_allotted']:<4} "
                  f"= {c['score_pct']:>6}%")


if __name__ == "__main__":
    asyncio.run(main())
