"""Build the Page Industries demo audit set, and retire the garment ones.

The instance shipped with five seeded GARMENTS_TEXTILE audits from the generic
product. They are SOFT-deleted here, not dropped: `ComplianceAudit` is a
governed entity behind the global soft-delete filter, so `isDeleted = true`
removes them from every list the product builds while leaving the rows (and
their reports, CAPA links and audit trail) intact and recoverable. Deleting
them outright would destroy an audit trail to tidy a demo — a bad trade in a
compliance product even when the records are synthetic.

In their place: four Page Industries audits, one per lifecycle state, so the
demo can be walked end to end without anyone having to conduct an audit first.

  1. Scheduled          — nothing graded; opens on the conduct screen empty.
  2. In progress        — two thirds graded; the grading UI with real data in it.
  3. Awaiting response  — fully graded and submitted; findings routed to auditees.
  4. Closed             — the full loop, with a FINAL report generated.

Grades are spread so every column of the workbook is exercised: both improvement
grades, an Unsatisfactory, an N/A, and Repeated statuses carrying the -1 penalty.

Idempotent: audits are keyed by title and rebuilt on every run.

Run from the backend root:
    python scripts/seed_page_demo_audits.py
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
from app.services import signoff

INDUSTRY_CODE = "PAGE_INDUSTRIES"

# (title, disciplines, fraction graded, terminal state)
PLAN: list[tuple[str, list[str], float, str]] = [
    ("Q4 Internal Audit — HR, EHS & Production (Scheduled)", [], 0.0, "scheduled"),
    ("Q3 Internal Audit — HR, EHS & Production (In Progress)", [], 0.66, "in_progress"),
    ("Q2 Internal Audit — HR & EHS (Awaiting Response)", ["HR", "EHS"], 1.0, "submitted"),
    ("Q1 Internal Audit — HR, EHS & Production (Closed)", [], 1.0, "closed"),
]

# Applied round-robin within each discipline. Weighted toward Effective — a
# factory failing two thirds of its checkpoints is not a believable demo — but
# carrying enough of every other grade that each column is visible.
PATTERN: list[tuple[str, str | None, str | None]] = [
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
        "Largely in place, but records were incomplete for two of the six months "
        "sampled. Discussed with the department head during the walkthrough."
    ),
    pg.GRADE_MAJOR_IMPROVEMENT: (
        "Requirement only partially met. Evidence was produced for the current "
        "period, but the process is undocumented and depends on one individual."
    ),
    pg.GRADE_UNSATISFACTORY: (
        "Not met. No evidence could be produced during the audit and the "
        "responsible owner confirmed the control is not currently operating."
    ),
}

AUDITEE_RESPONSE = (
    "Accepted. Corrective action has been planned with the department head and "
    "the revised procedure will be circulated this month."
)

# Critical and major checkpoints require an evidence photo before submit, and
# that gate is NOT bypassed here — the demo has to pass the same rule a real
# audit does, or it would be demonstrating a submit path the product does not
# actually allow.
#
# The placeholder is a self-describing SVG carried as a data URI rather than an
# object in Supabase storage: it renders in the evidence strip like any other
# attachment, needs no bucket or credentials to seed, and is unmistakably
# labelled so nobody mistakes it for a real site photograph. `storagePath` is
# deliberately absent, so deleting one is a no-op instead of a broken request.
_EVIDENCE_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' width='320' height='240'>"
    "<rect width='320' height='240' fill='#f1f5f9'/>"
    "<rect x='8' y='8' width='304' height='224' fill='none' stroke='#94a3b8' "
    "stroke-width='2' stroke-dasharray='8 6'/>"
    "<text x='160' y='112' font-family='sans-serif' font-size='17' fill='#475569' "
    "text-anchor='middle'>Demo evidence</text>"
    "<text x='160' y='138' font-family='sans-serif' font-size='12' fill='#94a3b8' "
    "text-anchor='middle'>placeholder — not a site photograph</text></svg>"
)
EVIDENCE_PHOTO = {
    "url": "data:image/svg+xml;utf8," + _EVIDENCE_SVG.replace("#", "%23"),
    "caption": "Demo evidence (placeholder)",
}


async def _grade(db, user, audit_id: str, fraction: float) -> int:
    rows = (await db.execute(
        select(AuditCheckpointResponse)
        .where(AuditCheckpointResponse.auditId == audit_id)
        .order_by(AuditCheckpointResponse.sequence)
    )).scalars().all()
    by_disc: dict[str, list[AuditCheckpointResponse]] = {}
    for r in rows:
        by_disc.setdefault(r.categoryId, []).append(r)

    graded = 0
    for items in by_disc.values():
        for i, r in enumerate(items[: int(len(items) * fraction)]):
            grade, status, risk = PATTERN[i % len(PATTERN)]
            payload: dict = {"checkpointCode": r.checkpointCode, "gradeAwarded": grade}
            if status:
                payload["complianceStatus"] = status
            if risk:
                payload["riskGrade"] = risk
            if grade in FINDINGS:
                payload["auditFindings"] = FINDINGS[grade]
                if r.requiresPhotoOnFail:
                    payload["photos"] = [EVIDENCE_PHOTO]
            await svc.save_response(db, user=user, audit_id=audit_id, payload=payload)
            graded += 1
    return graded


DISCIPLINES = ["HR", "EHS", "PRODUCTION"]


async def _cast(db, plant_id: str) -> dict:
    """Resolve DISTINCT people for each seat from the same scope-filtered lists
    the scheduling modal offers.

    Distinct is the whole point: `create_audit` runs an independence check and
    refuses to seat one person as both auditor and auditee on an engagement. A
    seeder that reuses one account would either be rejected or — worse, if the
    check were ever relaxed — quietly demo a segregation-of-duties breach.
    """
    from app.services import audit_assignment as assignment

    slots = (await assignment.assignable_users(db, plant_id=plant_id))["assignable"]
    lead = slots["leadAuditor"][0]
    pm = next(u for u in slots["plantManager"] if u["id"] != lead["id"])
    taken = {lead["id"], pm["id"]}
    auditees = []
    for u in slots["auditee"]:
        if u["id"] not in taken:
            auditees.append(u)
            taken.add(u["id"])
        if len(auditees) == len(DISCIPLINES):
            break
    if len(auditees) < len(DISCIPLINES):
        raise SystemExit("Not enough distinct assignable users to seat the demo cast.")
    return {"lead": lead, "pm": pm, "auditees": auditees}


async def main() -> None:
    async with AsyncSessionLocal() as db:
        plant_id = (await db.execute(
            text('SELECT id FROM "Plant" ORDER BY "createdAt" LIMIT 1')
        )).scalar()
        if not plant_id:
            raise SystemExit("No plant in this database — seed a plant first.")

        cast = await _cast(db, plant_id)
        user = await db.get(User, cast["lead"]["id"])          # the auditor
        pm_user = await db.get(User, cast["pm"]["id"])          # the plant manager
        # One auditee per discipline, so submit routes findings to three
        # different inboxes rather than piling them on one person.
        auditee_map = {
            d: cast["auditees"][i] for i, d in enumerate(DISCIPLINES)
        }
        auditees_payload = [
            {"userId": u["id"], "responsibleCategories": [d]}
            for d, u in auditee_map.items()
        ]
        print(f"Lead auditor : {cast['lead']['name']}")
        print(f"Plant manager: {cast['pm']['name']}")
        for d, u in auditee_map.items():
            print(f"Auditee {d:<11}: {u['name']}")
        print()

        # ── Retire the garment demo audits (reversible) ──────────────────
        retired = (await db.execute(text(
            'UPDATE "ComplianceAudit" SET "isDeleted" = true, "deletedAt" = now(), '
            '"deletionReason" = \'Retired: this instance audits the Page Industries '
            'checklist only.\' '
            'WHERE "industryCode" <> :code AND "isDeleted" = false RETURNING "auditNumber"'
        ), {"code": INDUSTRY_CODE})).scalars().all()
        if retired:
            print(f"Retired {len(retired)} non-Page audit(s): {', '.join(retired)}")

        # ── Rebuild the Page demo set ────────────────────────────────────
        titles = [t for t, _, _, _ in PLAN] + ["Internal Audit — HR, EHS & Production (demo)"]
        old = (await db.execute(
            select(ComplianceAudit).where(ComplianceAudit.title.in_(titles))
        )).scalars().all()
        for a in old:
            await db.delete(a)
        if old:
            await db.flush()
            print(f"Removed {len(old)} previous Page demo audit(s).")

        now = datetime.now(timezone.utc)
        for idx, (title, discs, fraction, terminal) in enumerate(PLAN):
            # Newest first in the register: Q4 ahead, Q1 well behind.
            when = now + timedelta(days=30) if terminal == "scheduled" else now - timedelta(days=10 + idx * 45)
            audit = await svc.create_audit(db, user=user, data={
                "plantId": plant_id,
                "title": title,
                "industryCode": INDUSTRY_CODE,
                "selectedDisciplineIds": discs,
                "scheduledDate": when,
                "leadAuditorUserId": user.id,
                "plantManagerUserId": pm_user.id,
                "auditees": auditees_payload,
                "auditType": "internal",
                "scopeDescription": "Annual internal audit programme — Page Industries checklist.",
            })
            graded = await _grade(db, user, audit.id, fraction) if fraction else 0
            note = f"{graded}/{audit.totalCheckpoints} graded"

            if terminal in ("submitted", "closed"):
                res = await svc.submit_audit(db, user=user, audit_id=audit.id)
                note += f", submitted ({res.get('capasSpawned', 0)} CAPA)"

            if terminal == "closed":
                # Walk every routed finding through the auditee + plant-manager
                # loop, because close_audit refuses to close over an open one —
                # a demo that reached "Closed" by skipping the gate would be
                # showing a state the product cannot actually produce.
                routed = (await db.execute(
                    select(AuditCheckpointResponse).where(
                        AuditCheckpointResponse.auditId == audit.id,
                        AuditCheckpointResponse.workflowState == "AWAITING_AUDITEE",
                    )
                )).scalars().all()
                for r in routed:
                    # Act as the ACTUAL owner of each finding, not as the
                    # auditor — the interaction thread records who acted, and a
                    # demo whose thread shows the auditor answering his own
                    # findings would be showing the wrong thing.
                    owner_id = r.assignedOwnerId or r.routedToUserId
                    owner = await db.get(User, owner_id) if owner_id else user
                    await svc.auditee_respond(db, user=owner or user, audit_id=audit.id, payload={
                        "checkpointCode": r.checkpointCode,
                        "responseText": AUDITEE_RESPONSE,
                        "actionTaken": "Procedure updated and re-communicated to the department.",
                    })
                    # The AUDITOR closes the loop, not the plant manager:
                    # `pm_review` only decides checkpoints that were escalated,
                    # and an auditee response that satisfies the auditor never
                    # reaches the plant manager at all. Accepting here is the
                    # ordinary path, and it is what moves the checkpoint to
                    # RESOLVED so the audit can close.
                    await db.refresh(r)
                    await svc.transition_checkpoint(
                        db, user=user, audit_id=audit.id, checkpoint_id=r.id,
                        action="ACCEPT",
                        payload={"comment": "Evidence reviewed and accepted at the closing meeting."},
                    )
                # Closure needs a NAMED lead auditor and a NAMED auditee owner
                # to have signed — the finalizability gate proves the work is
                # done, the sign-off proves someone accepted it. Each signature
                # is recorded by the person it belongs to; `record_signoff`
                # rejects a lead-auditor signature from anyone else, which is
                # exactly the check that makes the demo's closed audit mean
                # something.
                a = await db.get(ComplianceAudit, audit.id)
                await signoff.record_signoff(
                    db, audit=a, user=user, role="LEAD_AUDITOR",
                    signature_kind="TYPED", typed_name=user.name,
                    statement="I confirm this audit was conducted per the approved programme.",
                )
                first_auditee = await db.get(User, cast["auditees"][0]["id"])
                await signoff.record_signoff(
                    db, audit=a, user=first_auditee, role="AUDITEE_OWNER",
                    signature_kind="TYPED", typed_name=first_auditee.name,
                    statement="Findings received and corrective actions agreed.",
                )
                await svc.close_audit(
                    db, user=user, audit_id=audit.id,
                    closing_remarks="All findings responded to and accepted. Audit closed.",
                )
                rep = await svc.generate_report(db, user=user, audit_id=audit.id, report_type="FINAL")
                note += f", closed, report {rep.get('reportCode', '')}"

            await db.flush()
            fresh = (await db.execute(
                select(AuditCheckpointResponse).where(AuditCheckpointResponse.auditId == audit.id)
            )).scalars().all()
            a = await db.get(ComplianceAudit, audit.id)
            score = svc._compute_score(a, fresh)
            a.score = score
            a.overallCompliancePct = score["overall_score_pct"]
            a.criticalFailureCount = score["critical_failures"]

            pts = (f"{score['score_obtained']}/{score['score_allotted']} = "
                   f"{score['overall_score_pct']}%") if score["score_allotted"] else "not scored"
            print(f"  {a.auditNumber:<22} {a.status:<28} {note:<44} {pts}")

        await db.commit()
        print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
