"""Phase 2 smoke test — slim get_audit, aggregate dashboard, restructured
report (disciplineRag + no inlined register), lazy register endpoint.

Creates a transient audit, answers a sample, submits, generates an interim
report, reads the lazy register — then ROLLS BACK.

    .venv/Scripts/python.exe scripts/test_audit_scale_p2.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.audit_compliance import AuditCheckpointResponse
from app.models.plant import Plant
from app.models.user import User
from app.services import audit_compliance as svc

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


async def main() -> None:
    async with AsyncSessionLocal() as db:
        plant = (await db.execute(select(Plant).where(Plant.code == "NW"))).scalars().first()
        users = (await db.execute(select(User).where(User.plantId == plant.id).limit(2))).scalars().all()
        lead, owner = users[0], users[1]

        audit = await svc.create_audit(db, user=lead, data={
            "title": "P2 scale harness", "plantId": plant.id, "industryCode": "GARMENTS_TEXTILE",
            "selectedDisciplineIds": [], "scheduledDate": datetime.now(timezone.utc),
            "plantManagerUserId": owner.id,
            "auditees": [],
        })
        rows = (await db.execute(select(AuditCheckpointResponse).where(
            AuditCheckpointResponse.auditId == audit.id).order_by(AuditCheckpointResponse.sequence))).scalars().all()
        total = len(rows)

        # Answer: most pass, one fail (with observation), one partial (with obs).
        for r in rows:
            await svc.save_response(db, user=lead, audit_id=audit.id, payload={"checkpointCode": r.checkpointCode, "value": "pass"})
        fail_row, partial_row = rows[0], rows[1]
        ev = [{"storagePath": "audit-compliance/test/x.jpg", "url": "x"}]
        await svc.save_response(db, user=lead, audit_id=audit.id, payload={"checkpointCode": fail_row.checkpointCode, "value": "fail", "textObservation": "deliberate finding", "photos": ev})
        await svc.save_response(db, user=lead, audit_id=audit.id, payload={"checkpointCode": partial_row.checkpointCode, "value": "partial", "textObservation": "partial obs", "photos": ev})

        # ── get_audit is slim: responses = findings only, has rollup + aggregates
        d = await svc.get_audit(db, audit.id)
        check("get_audit has disciplineRollup", bool(d.get("disciplineRollup")), f"{len(d['disciplineRollup'])} disciplines")
        check("get_audit responses = findings only", len(d["responses"]) == 2, f"{len(d['responses'])} (expect 2 adverse)")
        check("get_audit allocationSummary", d.get("allocationSummary", {}).get("total") == total, f"{d.get('allocationSummary')}")
        check("get_audit progress from rollup", d["progress"]["total"] == total and d["progress"]["answered"] == total, f"{d['progress']['answered']}/{d['progress']['total']}")
        roll_total = sum(c["total"] for c in d["disciplineRollup"])
        check("rollup sums to total", roll_total == total, f"{roll_total} vs {total}")

        # ── audit_dashboard score via aggregate
        dash = await svc.audit_dashboard(db, audit.id)
        sc = dash["score"]
        check("dashboard score answered", sc["answered"] == total, f"{sc['answered']}")
        check("dashboard fail count", sc["failed"] == 1 and sc["partially_passed"] == 1, f"fail={sc['failed']} partial={sc['partially_passed']}")

        # ── submit routes the findings
        sub = await svc.submit_audit(db, user=lead, audit_id=audit.id)
        check("submit ok", sub.get("ok") is True or "score" in sub, f"{list(sub)[:4]}")

        # ── interim report: disciplineRag present, no inlined register
        rep = await svc.generate_report(db, user=lead, audit_id=audit.id, report_type="INTERIM")
        snap = rep["snapshot"]
        check("report has disciplineRag", bool(snap.get("disciplineRag")), f"{len(snap.get('disciplineRag', []))} disciplines")
        check("report findings = 2", len(snap.get("findings", [])) == 2, f"{len(snap.get('findings', []))}")
        check("interim has no inlined register", "checkpointRegister" not in snap, "ok")

        # ── lazy register endpoint paginates over the whole audit
        seen: set[str] = set()
        cursor = None
        reg_total = None
        while True:
            page = await svc.list_report_register(db, report_id=rep["id"], cursor=cursor, limit=30)
            reg_total = reg_total if reg_total is not None else page["total"]
            for e in page["register"]:
                seen.add(e["checkpointCode"])
            cursor = page["nextCursor"]
            if not cursor:
                break
        check("lazy register total", reg_total == total, f"{reg_total}")
        check("lazy register walks all rows", len(seen) == total, f"{len(seen)}")

        await db.rollback()

    npass = sum(1 for _, ok, _ in results if ok)
    for nm, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {nm}" + (f" — {detail}" if detail else ""))
    print(f"\n{npass}/{len(results)} checks passed")
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
