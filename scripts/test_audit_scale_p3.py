"""Phase 3 smoke test — per-discipline auditor assignment.

Verifies create_audit routes assignedAuditorId by discipline (co-auditor wins,
lead covers the rest), list_checkpoints filters by auditor, _actor_role_for and
_route_auditor_for_category behave, and legacy flat coAuditors degrade. Rolls back.

    .venv/Scripts/python.exe scripts/test_audit_scale_p3.py
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
        users = (await db.execute(select(User).where(User.plantId == plant.id).limit(3))).scalars().all()
        lead, co1, co2 = users[0], users[1], users[2]
        disc = ["FIRE-LIFE-SAFETY", "WORKER-WELFARE", "PPE-COMPLIANCE"]

        audit = await svc.create_audit(db, user=lead, data={
            "title": "P3 multi-auditor", "plantId": plant.id, "industryCode": "GARMENTS_TEXTILE",
            "selectedDisciplineIds": disc, "scheduledDate": datetime.now(timezone.utc),
            "leadAuditorUserId": lead.id,
            "coAuditors": [
                {"userId": co1.id, "disciplineIds": ["FIRE-LIFE-SAFETY"]},
                {"userId": co2.id, "disciplineIds": ["WORKER-WELFARE"]},
            ],
        })
        rows = (await db.execute(select(AuditCheckpointResponse).where(
            AuditCheckpointResponse.auditId == audit.id))).scalars().all()

        by_auditor: dict[str, set[str]] = {}
        for r in rows:
            by_auditor.setdefault(r.assignedAuditorId, set()).add(r.categoryId)
        check("co1 conducts only Fire", by_auditor.get(co1.id) == {"FIRE-LIFE-SAFETY"}, f"{by_auditor.get(co1.id)}")
        check("co2 conducts only Worker-Welfare", by_auditor.get(co2.id) == {"WORKER-WELFARE"}, f"{by_auditor.get(co2.id)}")
        check("lead conducts the rest (PPE)", by_auditor.get(lead.id) == {"PPE-COMPLIANCE"}, f"{by_auditor.get(lead.id)}")

        # ── list_checkpoints filters by auditor (the conduct "mine" path) ──
        co1_page = await svc.list_checkpoints(db, audit_id=audit.id, assigned_auditor_id=co1.id, limit=200)
        co1_cats = {it["categoryId"] for it in co1_page["items"]}
        check("auditor filter scopes to co1's disciplines", co1_cats == {"FIRE-LIFE-SAFETY"}, f"{co1_cats}")
        check("auditor filter total matches", co1_page["total"] == sum(1 for r in rows if r.categoryId == "FIRE-LIFE-SAFETY"), f"{co1_page['total']}")

        # ── _actor_role_for + _route_auditor_for_category ──
        check("role: lead → LEAD_AUDITOR", svc._actor_role_for(lead, audit) == "LEAD_AUDITOR")
        check("role: co1 → CO_AUDITOR", svc._actor_role_for(co1, audit) == "CO_AUDITOR")
        check("route auditor: fire → co1", svc._route_auditor_for_category("FIRE-LIFE-SAFETY", audit.coAuditors, lead.id) == co1.id)
        check("route auditor: unassigned → lead", svc._route_auditor_for_category("PPE-COMPLIANCE", audit.coAuditors, lead.id) == lead.id)

        # ── legacy flat coAuditors → lead conducts all ──
        legacy = await svc.create_audit(db, user=lead, data={
            "title": "P3 legacy", "plantId": plant.id, "industryCode": "GARMENTS_TEXTILE",
            "selectedDisciplineIds": ["PPE-COMPLIANCE"], "scheduledDate": datetime.now(timezone.utc),
            "leadAuditorUserId": lead.id, "coAuditors": [co1.id],  # flat string
        })
        lrows = (await db.execute(select(AuditCheckpointResponse).where(
            AuditCheckpointResponse.auditId == legacy.id))).scalars().all()
        check("legacy flat coAuditors → lead conducts all", all(r.assignedAuditorId == lead.id for r in lrows), "ok")

        await db.rollback()

    npass = sum(1 for _, ok, _ in results if ok)
    for nm, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {nm}" + (f" — {detail}" if detail else ""))
    print(f"\n{npass}/{len(results)} checks passed")
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
