"""Verify the PPE service against live data (PPE-01).

1. people_compliance() returns a sane compliant/gaps/critical split.
2. Full item lifecycle E2E through the SERVICE layer:
   commission -> issue -> return -> inspect -> retire, asserting the item
   status + holder at each hop. The throwaway item ends 'retired' and is then
   hard-deleted so re-runs don't accumulate test rows.

Run from the backend root:
    .venv/Scripts/python.exe scripts/verify_ppe.py
"""

from __future__ import annotations

import asyncio

asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # noqa: E402

from sqlalchemy import delete, select  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.models.plant import Plant  # noqa: E402
from app.models.ppe import PpeInspection, PpeIssuance, PpeItem, PpeType  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import ppe_inventory as svc  # noqa: E402


def ok(cond: bool, label: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


async def main() -> int:
    async with AsyncSessionLocal() as db:
        lms = (await db.execute(select(Plant).where(Plant.code == "LMS"))).scalar_one_or_none()
        if lms is None:
            lms = (await db.execute(select(Plant))).scalars().first()
        users = (await db.execute(select(User).where(User.plantId == lms.id))).scalars().all()
        admin = next((u for u in users if (u.role or "").upper() in ("ADMIN", "SYSTEM_ADMIN", "HSE_MANAGER", "SAFETY_OFFICER")), users[0])
        recipient = next((u for u in users if u.id != admin.id), users[0])
        print(f"Plant {lms.code} | {len(users)} users | issuer={admin.name} | recipient={recipient.name}")

        # ── 1. People compliance ──
        comp = await svc.people_compliance(db, plant_id=lms.id)
        sm = comp["summary"]
        print(f"\nPeople Compliance: {sm['totalPeople']} people | "
              f"compliant={sm['compliant']} gaps={sm['gaps']} critical={sm['criticalGaps']}")
        ok(sm["totalPeople"] > 0, "compliance returns people")
        ok(sm["compliant"] + sm["gaps"] + sm["criticalGaps"] == sm["totalPeople"], "summary adds up")
        sample = comp["people"][0]
        print(f"  e.g. {sample['name']} ({sample['role']}) -> {sample['overall']}: "
              + ", ".join(f"{r['ppeTypeName']}={r['status']}" for r in sample["requirements"]))

        # ── 2. Lifecycle E2E ──
        print("\nLifecycle E2E (commission -> issue -> return -> inspect -> retire):")
        helmet = (await db.execute(select(PpeType).where(PpeType.code == "HELMET-IS3521"))).scalar_one()
        items = await svc.commission_items(
            db, plant_id=lms.id, ppe_type_id=helmet.id, quantity=1,
            manufacturer="VERIFY", model="E2E", batch_lot_number="E2E",
            manufacture_date=svc._utcnow(), purchase_date=None, storage_location="E2E",
            actor_id=admin.id,
        )
        item = items[0]
        ok(item.status == "in_stock", f"commissioned -> in_stock ({item.itemNumber})")
        ok(item.serviceLifeEndDate is not None, "service life end computed")

        iss = await svc.issue_item(db, item_id=item.id, to_user_id=recipient.id, by_user_id=admin.id)
        await db.refresh(item)
        ok(item.status == "issued" and item.currentHolderUserId == recipient.id, "issued -> held by recipient")

        await svc.return_item(db, issuance_id=iss.id, by_user_id=admin.id, condition_at_return="good")
        await db.refresh(item)
        ok(item.status == "in_stock" and item.currentHolderUserId is None, "returned -> back in stock")

        insp = await svc.record_inspection(db, item_id=item.id, inspector_user_id=admin.id, overall_result="pass")
        await db.refresh(item)
        ok(item.lastInspectedAt is not None and item.status == "in_stock", "inspected pass -> in service, next due set")

        await svc.retire_item(db, item_id=item.id, actor_id=admin.id, reason="E2E verification cleanup")
        await db.refresh(item)
        ok(item.status == "retired", "retired -> retired")
        # commission + issue + return + retire = 4 (a pass inspection that keeps
        # an in-stock item in stock is intentionally not a status change).
        ok(len(item.stateHistory) >= 4, f"audit trail captured {len(item.stateHistory)} transitions")

        # ── cleanup throwaway rows ──
        await db.execute(delete(PpeInspection).where(PpeInspection.ppeItemId == item.id))
        await db.execute(delete(PpeIssuance).where(PpeIssuance.ppeItemId == item.id))
        await db.execute(delete(PpeItem).where(PpeItem.id == item.id))
        await db.commit()
        print("  cleaned up throwaway E2E item")

    print("\nAll PPE service checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
