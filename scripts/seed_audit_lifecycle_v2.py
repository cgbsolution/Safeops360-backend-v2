"""Seed the Audit Lifecycle v2 demo (Section 6).

Drives the REAL service functions end-to-end (so it doubles as a demo dry-run +
integration check):
  - one template forked with a custom checkpoint (v2),
  - a Garments audit AUD-GT-2026-NW-0010 scoped to a SUBSET of disciplines
    (Fire + Worker Welfare & SA8000 + PPE) — proves scoped materialization,
  - checkpoints allocated by discipline to owners,
  - one ad-hoc checkpoint added by the auditor (flagged custom),
  - mixed states: most pass; multi-round iteration threads — one ending
    ACCEPTED_WITH_CAPA, one left ESCALATED_PM (open) — so the finalize gate is
    demonstrable,
  - one prior Interim report,
  - audit left short of finalization (open iterations).

Idempotent: deletes any prior AUD-GT-2026-NW-0010 and only forks the template
once. Run from the backend root:
    .venv/Scripts/python.exe scripts/seed_audit_lifecycle_v2.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

try:  # Windows consoles default to cp1252 — make prints UTF-8 safe.
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.audit_compliance import AuditCheckpointResponse, AuditTemplate, ComplianceAudit
from app.models.plant import Plant
from app.models.user import User
from app.services import audit_compliance as svc

DEMO_NUMBER = "AUD-GT-2026-NW-0010"
SCOPE = ["FIRE-LIFE-SAFETY", "WORKER-WELFARE", "PPE-COMPLIANCE"]


async def _user(db, name: str) -> User:
    u = (await db.execute(select(User).where(User.name == name))).scalars().first()
    if u is None:
        raise SystemExit(f"User '{name}' not found — run the base seed first")
    return u


async def main() -> None:
    async with AsyncSessionLocal() as db:
        nw = (await db.execute(select(Plant).where(Plant.code == "NW"))).scalars().first()
        if nw is None:
            raise SystemExit("NW plant not found — run the base seed first")
        lead = await _user(db, "Priya Nair")          # HSE_MANAGER -> lead auditor
        pm = await _user(db, "Bhavesh Mukherjee")     # PLANT_HEAD -> plant manager
        a_fire = await _user(db, "Deepak Tomar")      # SAFETY_OFFICER -> Fire + PPE owner
        a_welfare = await _user(db, "Yogesh Patel")   # DEPARTMENT_HEAD -> Worker Welfare owner

        # ── 0. Idempotency: drop any prior demo audit ───────────────────
        prior = (await db.execute(select(ComplianceAudit).where(ComplianceAudit.auditNumber == DEMO_NUMBER))).scalars().first()
        if prior is not None:
            await db.delete(prior)
            await db.flush()
            print(f"  removed prior {DEMO_NUMBER}")

        # ── 1. Template fork with a custom checkpoint (v2) ──────────────
        tmpl = (
            await db.execute(
                select(AuditTemplate).where(
                    AuditTemplate.baseIndustry == "GARMENTS_TEXTILE", AuditTemplate.isActive.is_(True)
                ).order_by(AuditTemplate.name)
            )
        ).scalars().first()
        if tmpl and not (tmpl.customCheckpoints or []):
            fork = await svc.add_template_custom_checkpoint(
                db, user=lead, template_id=tmpl.id,
                payload={
                    "disciplineId": "FIRE-LIFE-SAFETY", "disciplineName": "Fire Safety & Emergency Preparedness",
                    "question": "Is the new lithium-battery storage area fire-rated and segregated per NFPA 855?",
                    "severity": "critical", "guidance": "Check enclosure rating, suppression and segregation.",
                    "standardClauseRef": "NFPA 855 | Factories Act §38",
                },
            )
            print(f"  template forked -> v{fork['version']} (+1 custom checkpoint)")
        elif tmpl:
            print(f"  template '{tmpl.name}' already has custom checkpoints — skip fork")

        # ── 2. Schedule the scoped audit (Fire + Worker Welfare + PPE) ──
        auditees = [
            {"userId": a_fire.id, "responsibleCategories": ["FIRE-LIFE-SAFETY", "PPE-COMPLIANCE"]},
            {"userId": a_welfare.id, "responsibleCategories": ["WORKER-WELFARE"]},
        ]
        audit = await svc.create_audit(
            db, user=lead,
            data={
                "title": "Q3 Integrated SA8000 + ISO 45001 Audit — Meridian North Works",
                "plantId": nw.id, "industryCode": "GARMENTS_TEXTILE",
                "selectedDisciplineIds": SCOPE, "scopePresetUsed": "WORKER_WELFARE",
                "scheduledDate": datetime.now(timezone.utc) - timedelta(days=3),
                "leadAuditorUserId": lead.id, "plantManagerUserId": pm.id,
                "auditees": auditees,
                "scopeDescription": "Garments compliance audit — fire, worker welfare & PPE.",
            },
        )
        audit.auditNumber = DEMO_NUMBER  # pin the demo number
        await db.flush()
        print(f"  scheduled {audit.auditNumber} — {audit.materializedCheckpointCount} checkpoints across {len(audit.selectedDisciplineIds)} disciplines")

        # ── 3. Allocate by discipline (Plant Head action) ───────────────
        await svc.allocate_checkpoints(db, user=lead, audit_id=audit.id, owner_id=a_fire.id, discipline_id="FIRE-LIFE-SAFETY")
        await svc.allocate_checkpoints(db, user=lead, audit_id=audit.id, owner_id=a_welfare.id, discipline_id="WORKER-WELFARE")
        await svc.allocate_checkpoints(db, user=lead, audit_id=audit.id, owner_id=a_fire.id, discipline_id="PPE-COMPLIANCE")

        # ── 4. Ad-hoc checkpoint (auditor, flagged custom) ──────────────
        await svc.add_adhoc_checkpoint(
            db, user=lead, audit_id=audit.id,
            payload={"disciplineId": "WORKER-WELFARE", "question": "Are on-site crèche facilities provided per SA8000 §6?",
                     "severity": "major", "guidance": "Verify capacity, staffing and hours.", "assignedOwnerId": a_welfare.id},
        )

        # ── 5. Assess: most pass; fail a handful to drive iteration ─────
        rows = (await db.execute(select(AuditCheckpointResponse).where(AuditCheckpointResponse.auditId == audit.id).order_by(AuditCheckpointResponse.sequence))).scalars().all()
        # fail 5 non-critical-ish rows; pass the rest.
        fail_idx = set(range(0, len(rows), max(1, len(rows) // 6)))  # ~6 spread-out fails
        for i, r in enumerate(rows):
            val = "fail" if i in fail_idx else "pass"
            obs = "Gap observed during walkthrough." if val == "fail" else ""
            # photos satisfy the requiresPhotoOnFail rule for failed checkpoints.
            photos = [{"storagePath": "audit-compliance/demo/evidence.jpg", "url": "u"}] if val == "fail" else []
            await svc.save_response(db, user=lead, audit_id=audit.id, payload={"checkpointCode": r.checkpointCode, "value": val, "textObservation": obs, "photos": photos})

        await svc.submit_audit(db, user=lead, audit_id=audit.id)
        print(f"  submitted — score {audit.overallCompliancePct}% ")

        # ── 6. Iteration threads on open findings ───────────────────────
        rows = (await db.execute(select(AuditCheckpointResponse).where(AuditCheckpointResponse.auditId == audit.id))).scalars().all()
        awaiting = [r for r in rows if r.workflowState == "AWAITING_AUDITEE"]
        owner_of = {a_fire.id: a_fire, a_welfare.id: a_welfare}

        def owner_user(r):
            uid = r.assignedOwnerId or r.routedToUserId
            return owner_of.get(uid) or (pm if uid == pm.id else lead)

        async def tr(r, user, action, **kw):
            await svc.transition_checkpoint(db, user=user, audit_id=audit.id, checkpoint_id=r.id, action=action, payload=kw)

        # Thread 1 -> ACCEPTED_WITH_CAPA (respond -> more-info -> respond -> raise CAPA)
        if len(awaiting) >= 1:
            r = awaiting[0]; ow = owner_user(r)
            await tr(r, ow, "AUDITEE_RESPOND", comment="Initial corrective action taken; barrier replaced.")
            await tr(r, lead, "REQUEST_MORE_INFO", comment="Provide photo evidence and closure date.")
            await tr(r, ow, "AUDITEE_RESPOND", comment="Photo evidence attached; closure 2026-07-01.", evidenceIds=["audit-compliance/demo/ev1.jpg"])
            await tr(r, lead, "RAISE_CAPA", comment="Systemic — tracking to closure via CAPA.")
            print(f"  thread 1 ({r.checkpointCode}) -> ACCEPTED_WITH_CAPA")

        # Thread 2 -> ESCALATED_PM (respond -> more-info -> respond -> escalate) — left open
        if len(awaiting) >= 2:
            r = awaiting[1]; ow = owner_user(r)
            await tr(r, ow, "AUDITEE_RESPOND", comment="Disagree on severity; controls already in place.")
            await tr(r, lead, "REQUEST_MORE_INFO", comment="Evidence of the existing control?")
            await tr(r, ow, "AUDITEE_RESPOND", comment="Attached SOP; requesting review.")
            await tr(r, lead, "ESCALATE", comment="Needs plant-manager decision on acceptability.")
            print(f"  thread 2 ({r.checkpointCode}) -> ESCALATED_PM (open)")

        # Thread 3 -> left AUDITEE_RESPONDED (open) for variety
        if len(awaiting) >= 3:
            r = awaiting[2]; ow = owner_user(r)
            await tr(r, ow, "AUDITEE_RESPOND", comment="Remediated; awaiting auditor review.")
            print(f"  thread 3 ({r.checkpointCode}) -> AUDITEE_RESPONDED (open)")

        # ── 7. One prior Interim report ─────────────────────────────────
        rep = await svc.generate_report(db, user=lead, audit_id=audit.id, report_type="INTERIM")
        fin = svc._finalizability(await svc._load_audit(db, audit.id, with_responses=True))
        print(f"  interim report {rep['reportCode']} | finalizable: {fin['finalizable']} ({fin['blockerCount']} open) — left short of finalization")

        await db.commit()
        print(f"\nSEEDED {DEMO_NUMBER}: scoped({len(SCOPE)}) | allocated | 1 ad-hoc | 3 threads | 1 interim | finalize-blocked")


if __name__ == "__main__":
    asyncio.run(main())
