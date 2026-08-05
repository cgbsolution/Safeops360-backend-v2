"""Phase 1 (1500-checkpoint scaling) smoke test — read endpoints + bulk save.

Creates a transient audit, exercises list_checkpoints pagination/filters,
_discipline_rollup, _finalizability_db, get_checkpoint_interactions, and
bulk_save_response (incl. its fail/partial safety guard), then ROLLS BACK.

    .venv/Scripts/python.exe scripts/test_audit_scale_p1.py
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
        lead = (await db.execute(select(User).where(User.plantId == plant.id).limit(1))).scalars().first()
        GT = "GARMENTS_TEXTILE"

        audit = await svc.create_audit(db, user=lead, data={
            "title": "P1 scale harness", "plantId": plant.id, "industryCode": GT,
            "selectedDisciplineIds": [], "scheduledDate": datetime.now(timezone.utc),
        })
        total = audit.totalCheckpoints
        check("create full-library audit", total and total > 20, f"{total} checkpoints")

        # ── _discipline_rollup sums to total ────────────────────────────────
        rollup = await svc._discipline_rollup(db, audit.id)
        roll_total = sum(c["total"] for c in rollup)
        check("rollup sums to total", roll_total == total, f"rollup={roll_total} total={total}")
        check("rollup all unanswered", all(c["answered"] == 0 for c in rollup), "answered=0 at start")

        # ── list_checkpoints pagination walks the whole set exactly once ────
        seen: set[str] = set()
        cursor = None
        pages = 0
        first_total = None
        while True:
            page = await svc.list_checkpoints(db, audit_id=audit.id, limit=20, cursor=cursor)
            first_total = first_total if first_total is not None else page["total"]
            for it in page["items"]:
                seen.add(it["id"])
            pages += 1
            cursor = page["nextCursor"]
            if not cursor or pages > 200:
                break
        check("pagination total field", first_total == total, f"page.total={first_total}")
        check("pagination walks every row once", len(seen) == total, f"seen={len(seen)} total={total}")

        # ── value=unanswered returns all (none answered yet) ───────────────
        un = await svc.list_checkpoints(db, audit_id=audit.id, value="unanswered", limit=1)
        check("value=unanswered total", un["total"] == total, f"{un['total']}")

        # ── discipline filter matches the rollup count ─────────────────────
        d0 = rollup[0]["categoryId"]
        df = await svc.list_checkpoints(db, audit_id=audit.id, discipline_id=d0, limit=1)
        check("discipline filter count", df["total"] == rollup[0]["total"], f"{df['total']} vs {rollup[0]['total']}")

        # ── q search hits at least the row we look for ─────────────────────
        sample = (await db.execute(select(AuditCheckpointResponse).where(
            AuditCheckpointResponse.auditId == audit.id).limit(1))).scalars().first()
        token = sample.checkpointCode[:6]
        qs = await svc.list_checkpoints(db, audit_id=audit.id, q=token, limit=5)
        check("q search returns results", qs["total"] >= 1, f"q='{token}' -> {qs['total']}")

        # ── interactions lazy load (empty thread on a fresh row) ───────────
        inter = await svc.get_checkpoint_interactions(db, audit_id=audit.id, checkpoint_id=sample.id)
        check("interactions lazy load", inter["interactions"] == [], "empty thread")

        # ── bulk_save_response: mark one whole discipline pass ─────────────
        before_pass = rollup[0]["passed"]
        bulk = await svc.bulk_save_response(db, user=lead, audit_id=audit.id, value="pass", discipline_id=d0)
        roll2 = {c["categoryId"]: c for c in await svc._discipline_rollup(db, audit.id)}
        check("bulk pass updated count", bulk["updated"] == rollup[0]["total"], f"updated={bulk['updated']}")
        check("bulk pass reflected in rollup",
              roll2[d0]["passed"] == rollup[0]["total"] and roll2[d0]["answered"] == rollup[0]["total"],
              f"passed={roll2[d0]['passed']}")

        # ── bulk safety: a FAIL must NOT be clobbered by a later bulk pass ──
        d1 = rollup[1]["categoryId"]
        fail_row = (await db.execute(select(AuditCheckpointResponse).where(
            AuditCheckpointResponse.auditId == audit.id,
            AuditCheckpointResponse.categoryId == d1).limit(1))).scalars().first()
        await svc.save_response(db, user=lead, audit_id=audit.id, payload={
            "checkpointCode": fail_row.checkpointCode, "value": "fail",
            "textObservation": "deliberate finding"})
        bulk2 = await svc.bulk_save_response(db, user=lead, audit_id=audit.id, value="pass",
                                             discipline_id=d1, only_unanswered=False)
        await db.refresh(fail_row)
        check("bulk never clobbers a FAIL", fail_row.assessmentStatus == "FAIL",
              f"fail_row status={fail_row.assessmentStatus}; bulk updated {bulk2['updated']}")

        # ── finalizability not finalizable while scheduled/in_progress ─────
        fin = await svc._finalizability_db(db, audit)
        check("not finalizable pre-submit", fin["finalizable"] is False and fin["total"] == total,
              f"submitted={fin['submitted']} blockers={fin['blockerCount']}")

        await db.rollback()  # no DB pollution

    npass = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    print(f"\n{npass}/{len(results)} checks passed")
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
