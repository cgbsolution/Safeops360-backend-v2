"""Audit Lifecycle v2 — TL-01..12 backend test harness (Section 6).

Runs each testable TL item against transient audits and rolls back (no DB
pollution). UI-only items (TL-04 carousel interactions, TL-10 A-03 render) are
asserted at the data/contract layer where possible and noted otherwise.

    .venv/Scripts/python.exe scripts/test_audit_lifecycle_v2.py
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.audit_compliance import AuditCheckpointResponse, AuditTemplate, CheckpointInteraction
from app.models.plant import Plant
from app.models.user import User
from app.services import audit_compliance as svc

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(tl: str, ok: bool, detail: str = "") -> None:
    results.append((tl, PASS if ok else FAIL, detail))


async def main() -> None:
    async with AsyncSessionLocal() as db:
        plant = (await db.execute(select(Plant).where(Plant.code == "NW"))).scalars().first()
        users = (await db.execute(select(User).where(User.plantId == plant.id).limit(3))).scalars().all()
        lead, owner, owner2 = users[0], users[1], users[2]
        GT = "GARMENTS_TEXTILE"

        async def mk(disc, auditees=None, pm=None):
            a = await svc.create_audit(db, user=lead, data={
                "title": "TL harness", "plantId": plant.id, "industryCode": GT,
                "selectedDisciplineIds": disc, "plantManagerUserId": pm or owner2.id,
                "scheduledDate": datetime.now(timezone.utc), "auditees": auditees or []})
            rows = (await db.execute(select(AuditCheckpointResponse).where(AuditCheckpointResponse.auditId == a.id).order_by(AuditCheckpointResponse.sequence))).scalars().all()
            return a, rows

        # ── TL-01 scoped materialization ────────────────────────────────
        a, rows = await mk(["FIRE-LIFE-SAFETY", "WORKER-WELFARE", "PPE-COMPLIANCE"])
        by = Counter(r.categoryId for r in rows)
        check("TL-01", len(rows) == 32 and set(by) == {"FIRE-LIFE-SAFETY", "WORKER-WELFARE", "PPE-COMPLIANCE"}, f"{len(rows)} cps, disc={dict(by)}")

        # ── TL-02 add discipline to running audit ───────────────────────
        before = len(rows)
        res = await svc.add_disciplines(db, user=lead, audit_id=a.id, discipline_ids=["TRAINING-COMPETENCY"])
        again = await svc.add_disciplines(db, user=lead, audit_id=a.id, discipline_ids=["TRAINING-COMPETENCY"])
        check("TL-02", res["added"] == 5 and again["added"] == 0 and res["totalCheckpoints"] == before + 5, f"+{res['added']}, dup={again['added']}")

        # ── TL-03 template fork + ad-hoc + promote ──────────────────────
        tmpl = (await db.execute(select(AuditTemplate).where(AuditTemplate.baseIndustry == GT, AuditTemplate.isActive.is_(True)))).scalars().first()
        fork = await svc.add_template_custom_checkpoint(db, user=lead, template_id=tmpl.id, payload={"disciplineId": "FIRE-LIFE-SAFETY", "question": "Fork test checkpoint?", "severity": "major"})
        forked = await db.get(AuditTemplate, fork["templateId"])
        parent = await db.get(AuditTemplate, tmpl.id)
        a3, r3 = await mk(["FIRE-LIFE-SAFETY"])
        # new audit on forked template materializes the custom
        a3b = await svc.create_audit(db, user=lead, data={"title": "fork mat", "plantId": plant.id, "templateId": forked.id, "selectedDisciplineIds": ["FIRE-LIFE-SAFETY"], "scheduledDate": datetime.now(timezone.utc), "auditees": []})
        r3b = (await db.execute(select(AuditCheckpointResponse).where(AuditCheckpointResponse.auditId == a3b.id))).scalars().all()
        adhoc = await svc.add_adhoc_checkpoint(db, user=lead, audit_id=a3.id, payload={"disciplineId": "FIRE-LIFE-SAFETY", "question": "Ad-hoc TL03?", "severity": "minor", "promoteToTemplate": False})
        inter = (await db.execute(select(CheckpointInteraction).where(CheckpointInteraction.auditId == a3.id, CheckpointInteraction.action == "ADHOC_ADDED"))).scalars().all()
        check("TL-03", forked.version != parent.version and not parent.isActive and any(r.isAdHoc for r in r3b)
              and adhoc["checkpoint"]["isAdHoc"] and len(inter) == 1,
              f"fork v{forked.version}, custom-materialized={sum(r.isAdHoc for r in r3b)}, ADHOC_ADDED logged={len(inter)}")

        # ── TL-05 allocation by discipline/row + unassigned routing ─────
        a5, r5 = await mk(["PPE-COMPLIANCE"])  # no auditees -> unassigned
        alloc = await svc.allocate_checkpoints(db, user=lead, audit_id=a5.id, owner_id=owner.id, discipline_id="PPE-COMPLIANCE")
        await db.refresh(r5[0])
        # an UNASSIGNED fail routes to default (pm/lead) at submit; here all assigned, so test default via a 2nd audit
        a5b, r5b = await mk(["PPE-COMPLIANCE"])  # unassigned
        await svc.save_response(db, user=lead, audit_id=a5b.id, payload={"checkpointCode": r5b[0].checkpointCode, "value": "fail", "textObservation": "gap", "photos": [{"storagePath": "demo/p.jpg", "url": "u"}]})
        await svc.submit_audit(db, user=lead, audit_id=a5b.id)
        await db.refresh(r5b[0])
        check("TL-05", alloc["updated"] == 6 and r5[0].assignedOwnerId == owner.id
              and r5b[0].assignedOwnerId is None and r5b[0].routedToUserId in (a5b.plantManagerUserId, a5b.leadAuditorUserId),
              f"alloc={alloc['updated']}, unassigned-default-routed={r5b[0].routedToUserId is not None}")

        # ── TL-06 auditee transparency (sees passed read-only + fail actionable) ─
        a6, r6 = await mk(["PPE-COMPLIANCE"], auditees=[{"userId": owner.id, "responsibleCategories": ["PPE-COMPLIANCE"]}])
        await svc.allocate_checkpoints(db, user=lead, audit_id=a6.id, owner_id=owner.id, discipline_id="PPE-COMPLIANCE")
        await svc.save_response(db, user=lead, audit_id=a6.id, payload={"checkpointCode": r6[0].checkpointCode, "value": "fail", "textObservation": "gap", "photos": [{"storagePath": "demo/p.jpg", "url": "u"}]})
        for r in r6[1:]:
            await svc.save_response(db, user=lead, audit_id=a6.id, payload={"checkpointCode": r.checkpointCode, "value": "pass"})
        await svc.submit_audit(db, user=lead, audit_id=a6.id)
        mine = await svc.my_assigned_checkpoints(db, user=owner, accessible_plants=[plant.id])
        g6 = [x for x in mine["audits"] if x["auditId"] == a6.id][0]
        needs = [i for i in g6["items"] if i["needsResponse"]]
        passed = [i for i in g6["items"] if i["assessmentStatus"] == "PASS"]
        check("TL-06", g6["scorecard"]["total"] == 6 and len(needs) == 1 and len(passed) == 5,
              f"total={g6['scorecard']['total']}, needsResponse={len(needs)}, passed-readonly={len(passed)}")

        # ── TL-07 multi-round + actions ─────────────────────────────────
        cp = r6[0]
        await svc.transition_checkpoint(db, user=owner, audit_id=a6.id, checkpoint_id=cp.id, action="AUDITEE_RESPOND", payload={"comment": "round zero response"})
        await svc.transition_checkpoint(db, user=lead, audit_id=a6.id, checkpoint_id=cp.id, action="REQUEST_MORE_INFO", payload={"comment": "more"})
        await db.refresh(cp); round_after_mi = cp.currentRound
        await svc.transition_checkpoint(db, user=owner, audit_id=a6.id, checkpoint_id=cp.id, action="AUDITEE_RESPOND", payload={"comment": "round one response"})
        await svc.transition_checkpoint(db, user=lead, audit_id=a6.id, checkpoint_id=cp.id, action="ESCALATE", payload={"comment": "esc"})
        await db.refresh(cp); escalated = cp.workflowState
        await svc.transition_checkpoint(db, user=owner2, audit_id=a6.id, checkpoint_id=cp.id, action="PM_RAISE_CAPA", payload={"comment": "capa"})
        await db.refresh(cp)
        thread = [i.action for i in (await db.execute(select(CheckpointInteraction).where(CheckpointInteraction.checkpointInstanceId == cp.id).order_by(CheckpointInteraction.timestamp))).scalars().all()]
        check("TL-07", round_after_mi == 1 and escalated == "ESCALATED_PM" and cp.workflowState == "ACCEPTED_WITH_CAPA" and bool(cp.capaId)
              and thread[:2] == ["ASSESSED", "ROUTED_TO_OWNER"],
              f"round={round_after_mi}, end={cp.workflowState}, capa={bool(cp.capaId)}, thread={thread}")

        # ── TL-08 finalize gate blocks while non-terminal ───────────────
        a8, r8 = await mk(["PPE-COMPLIANCE"], auditees=[{"userId": owner.id, "responsibleCategories": ["PPE-COMPLIANCE"]}])
        for r in r8:
            await svc.save_response(db, user=lead, audit_id=a8.id, payload={"checkpointCode": r.checkpointCode, "value": "pass"})
        await svc.save_response(db, user=lead, audit_id=a8.id, payload={"checkpointCode": r8[0].checkpointCode, "value": "fail", "textObservation": "gap", "photos": [{"storagePath": "demo/p.jpg", "url": "u"}]})
        await svc.submit_audit(db, user=lead, audit_id=a8.id)
        blocked = False
        try:
            await svc.close_audit(db, user=lead, audit_id=a8.id)
        except ValueError:
            blocked = True
        full8 = await svc._load_audit(db, a8.id, with_responses=True)
        fin = svc._finalizability(full8)
        check("TL-08", blocked and not fin["finalizable"] and fin["blockerCount"] >= 1, f"blocked={blocked}, blockers={fin['blockerCount']}")

        # ── TL-09 interim + final reports + immutability ────────────────
        i1 = await svc.generate_report(db, user=lead, audit_id=a8.id, report_type="INTERIM")
        snap_hash = i1["snapshot"]["snapshotHash"]
        final_blocked = False
        try:
            await svc.generate_report(db, user=lead, audit_id=a8.id, report_type="FINAL")
        except ValueError:
            final_blocked = True
        # resolve the one finding then final
        cpf = next(r for r in (await db.execute(select(AuditCheckpointResponse).where(AuditCheckpointResponse.auditId == a8.id))).scalars().all() if r.workflowState == "AWAITING_AUDITEE")
        await svc.transition_checkpoint(db, user=owner, audit_id=a8.id, checkpoint_id=cpf.id, action="AUDITEE_RESPOND", payload={"comment": "fixed"})
        await svc.transition_checkpoint(db, user=lead, audit_id=a8.id, checkpoint_id=cpf.id, action="ACCEPT", payload={"comment": "ok"})
        f1 = await svc.generate_report(db, user=lead, audit_id=a8.id, report_type="FINAL", sign_offs=[{"role": "LEAD_AUDITOR", "userId": lead.id}])
        # The full register is now served lazily (not inlined into the immutable
        # snapshot — 1500-cp safe). FINAL sets hasFullRegister; the register
        # endpoint paginates the whole set.
        reg = await svc.list_report_register(db, report_id=f1["id"], limit=200)
        has_register = (f1["snapshot"].get("hasFullRegister") is True) and reg is not None and reg["total"] == len(r8)
        check("TL-09", i1["reportType"] == "INTERIM" and "checkpointRegister" not in i1["snapshot"] and final_blocked
              and has_register and bool(snap_hash) and f1["signOffs"],
              f"interim+final ok, final-blocked-while-open={final_blocked}, lazy-register={has_register}")

        # ── TL-11 tenant isolation (my-checkpoints plant-scoped) ────────
        empty = await svc.my_assigned_checkpoints(db, user=owner, accessible_plants=[])
        check("TL-11", empty["totals"]["total"] == 0, f"plant-scoped to [] -> {empty['totals']['total']} (expect 0)")

        # ── TL-12 thread append-only + ordered + server-set timestamps ──
        ints = (await db.execute(select(CheckpointInteraction).where(CheckpointInteraction.checkpointInstanceId == cp.id).order_by(CheckpointInteraction.timestamp))).scalars().all()
        ordered = all(ints[i].timestamp <= ints[i + 1].timestamp for i in range(len(ints) - 1))
        all_stamped = all(i.timestamp is not None for i in ints)
        check("TL-12", ordered and all_stamped and len(ints) >= 6, f"ordered={ordered}, stamped={all_stamped}, n={len(ints)}")

        await db.rollback()

    # ── Report ──
    print("\n=== TL-01..12 RESULTS ===")
    for tl, st, detail in results:
        print(f"  {tl}: {st}  {detail}")
    print("  TL-04 (carousel UI) + TL-10 (A-03 render): manual/visual — covered by build (conduct-screen, audit-detail).")
    failed = [r for r in results if r[1] == FAIL]
    print(f"\n{'ALL BACKEND TL CHECKS PASSED' if not failed else f'{len(failed)} FAILED'}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
