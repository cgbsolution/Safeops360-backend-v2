"""QA full-module sweep — PPE-01 lifecycle + API endpoints.

Senior-QA edge-case harness for the WHOLE PPE module (not just the PTW gate).
The lifecycle service functions commit internally, so this harness patches
`db.commit` to `db.flush` on the test session — every write stays inside one
transaction and the final rollback leaves the database untouched.

Each check asserts CORRECT behaviour; a FAIL line is a candidate defect.

Run from the backend root:
    $env:PYTHONIOENCODING="utf-8"; .venv/Scripts/python.exe scripts/qa_ppe_module_full.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.core.db import AsyncSessionLocal, engine
from app.models._base import gen_id
from app.models.plant import Plant
from app.models.ppe import PpeIssuance, PpeItem, PpeType
from app.models.user import Role, User, UserRole
from app.routers import ppe as ppe_router
from app.services import ppe_inventory as svc

NOW = datetime.now(timezone.utc)
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


def _uid() -> str:
    return uuid4().hex[:10]


async def main() -> None:  # noqa: PLR0915
    async with AsyncSessionLocal() as db:
        db.commit = db.flush  # type: ignore[method-assign]  # keep everything in-transaction

        plants = (await db.execute(select(Plant.id).limit(2))).scalars().all()
        plant, plant_other = plants[0], plants[1]

        hse = (
            await db.execute(
                select(User)
                .join(UserRole, UserRole.userId == User.id)
                .join(Role, Role.id == UserRole.roleId)
                .where(Role.code.in_(["HSE_MANAGER", "SYSTEM_ADMIN", "ADMIN"]))
                .limit(1)
            )
        ).scalars().first()
        worker = (
            await db.execute(
                select(User)
                .join(UserRole, UserRole.userId == User.id)
                .join(Role, Role.id == UserRole.roleId)
                .where(Role.code == "WORKER")
                .limit(1)
            )
        ).scalars().first()

        qa_type = PpeType(
            id=gen_id(), code=f"QA-FULL-{_uid()}", name="QA Full Harness Type",
            category="qa_cat", subcategory="qa_sub", serviceLifeYears=2,
            inspectionSchedule=[{"inspection_type": "pre_use", "interval_days": None},
                                {"inspection_type": "periodic", "interval_days": 30}],
            applicableStandards=[], controlsHazards=[], enablesPermitTypes=[],
            requiredForAreas=[], requiredTrainingPrograms=[], regulatoryReferences=[],
        )
        db.add(qa_type)
        u_a = User(id=gen_id(), email=f"qa-{_uid()}@qa.local", name="QA Holder A",
                   passwordHash="x", role="QA_ROLE", plantId=plant)
        u_b = User(id=gen_id(), email=f"qa-{_uid()}@qa.local", name="QA OtherPlant B",
                   passwordHash="x", role="QA_ROLE", plantId=plant_other)
        db.add_all([u_a, u_b])
        await db.flush()

        print("── Phase B: lifecycle service edge cases ──")

        # B1 commission
        items = await svc.commission_items(
            db, plant_id=plant, ppe_type_id=qa_type.id, quantity=3,
            manufacturer="QA", model="M1", batch_lot_number="QA-LOT-1",
            manufacture_date=NOW - timedelta(days=30), purchase_date=None,
            storage_location="QA Store", actor_id=hse.id,
        )
        check("B1 commission creates in-stock items", len(items) == 3 and all(i.status == "in_stock" for i in items))
        check("B1b item numbers unique", len({i.itemNumber for i in items}) == 3)
        check("B1c next inspection from smallest interval (30d)",
              all(i.nextInspectionDueDate and abs(((svc._aware(i.nextInspectionDueDate)) - NOW).days - 30) <= 1 for i in items))
        check("B1d service life = mfg + 2y",
              abs((svc._aware(items[0].serviceLifeEndDate) - (NOW - timedelta(days=30))).days - 730) <= 2)
        it1, it2, it3 = items

        # B2 issue happy path
        iss1 = await svc.issue_item(db, item_id=it1.id, to_user_id=u_a.id, by_user_id=hse.id)
        check("B2 issue → item issued + holder + active issuance",
              it1.status == "issued" and it1.currentHolderUserId == u_a.id and iss1.status == "active")

        # B3 issue an already-issued item → clean error
        try:
            await svc.issue_item(db, item_id=it1.id, to_user_id=u_a.id, by_user_id=hse.id)
            check("B3 double-issue rejected", False, "no error raised")
        except ValueError:
            check("B3 double-issue rejected", True)

        # B4 issue a blocked (expired) item → clean error
        it2.serviceLifeEndDate = NOW - timedelta(days=1)
        try:
            await svc.issue_item(db, item_id=it2.id, to_user_id=u_a.id, by_user_id=hse.id)
            check("B4 issuing expired item rejected", False, "no error raised")
        except ValueError:
            check("B4 issuing expired item rejected", True)
        it2.serviceLifeEndDate = NOW + timedelta(days=700)  # restore

        # B5 cross-plant issue: item @ plant A → user of plant B
        try:
            cross = await svc.issue_item(db, item_id=it2.id, to_user_id=u_b.id, by_user_id=hse.id)
            check("B5 cross-plant issue rejected", False,
                  f"ALLOWED — issuance {cross.issuanceNumber} to user of another plant")
            await svc.return_item(db, issuance_id=cross.id, by_user_id=hse.id)
        except ValueError:
            check("B5 cross-plant issue rejected", True)

        # B6 return good → back to stock
        ret = await svc.return_item(db, issuance_id=iss1.id, by_user_id=hse.id, condition_at_return="good")
        check("B6 return good → in_stock, holder cleared, issuance closed",
              it1.status == "in_stock" and it1.currentHolderUserId is None and ret.status == "returned")

        # B7 return damaged → quarantine + post-return inspection
        iss2 = await svc.issue_item(db, item_id=it1.id, to_user_id=u_a.id, by_user_id=hse.id)
        ret2 = await svc.return_item(db, issuance_id=iss2.id, by_user_id=hse.id, condition_at_return="damaged")
        check("B7 damaged return → quarantined + inspection required",
              it1.status == "quarantined" and ret2.status == "damaged_return" and ret2.postReturnInspectionRequired)

        # B8 double return → clean error
        try:
            await svc.return_item(db, issuance_id=iss2.id, by_user_id=hse.id)
            check("B8 double return rejected", False, "no error raised")
        except ValueError:
            check("B8 double return rejected", True)

        # B9 inspect pass on quarantined item → back to service, due date advanced
        insp = await svc.record_inspection(db, item_id=it1.id, inspector_user_id=hse.id,
                                           overall_result="pass")
        check("B9 inspect pass → returned to service, next due ~30d",
              it1.status == "in_stock"
              and abs((svc._aware(it1.nextInspectionDueDate) - NOW).days - 30) <= 1
              and insp.overallResult == "pass")

        # B10 inspect fail with critical defect → quarantine + CAPA flag
        insp2 = await svc.record_inspection(
            db, item_id=it3.id, inspector_user_id=hse.id, overall_result="fail",
            defects_found=[{"defect_description": "torn webbing", "severity": "critical"}],
        )
        check("B10 critical-defect fail → quarantined + capaSpawned",
              it3.status == "quarantined" and insp2.capaSpawned is True)

        # B11 recalled_to_store outcome on an ISSUED item — state consistency
        insp_fix = await svc.record_inspection(db, item_id=it3.id, inspector_user_id=hse.id,
                                               overall_result="pass")  # back to stock
        iss3 = await svc.issue_item(db, item_id=it3.id, to_user_id=u_a.id, by_user_id=hse.id)
        await svc.record_inspection(db, item_id=it3.id, inspector_user_id=hse.id,
                                    overall_result="pass", item_status_after="recalled_to_store")
        await db.refresh(iss3)
        check("B11 recalled_to_store on issued item → holder cleared + issuance closed",
              it3.status == "in_stock"
              and it3.currentHolderUserId is None
              and it3.currentIssuanceId is None
              and iss3.status != "active",
              detail=f"status={it3.status} holder={it3.currentHolderUserId} issuance={iss3.status}")

        # B11b consequence: the item can be double-issued while the first
        # issuance is still active
        if it3.status == "in_stock" and iss3.status == "active":
            iss4 = await svc.issue_item(db, item_id=it3.id, to_user_id=u_b.id, by_user_id=hse.id)
            both_active = iss3.status == "active" and iss4.status == "active"
            check("B11b no dangling double-issuance", not both_active,
                  detail="two ACTIVE issuances for one physical item")
        else:
            check("B11b no dangling double-issuance", True)

        # B12 contradictory inspection: result=fail but returned_to_service
        fresh = await svc.commission_items(
            db, plant_id=plant, ppe_type_id=qa_type.id, quantity=1,
            manufacturer="QA", model="M1", batch_lot_number="QA-LOT-2",
            manufacture_date=NOW - timedelta(days=5), purchase_date=None,
            storage_location="QA Store", actor_id=hse.id,
        )
        it4 = fresh[0]
        try:
            await svc.record_inspection(db, item_id=it4.id, inspector_user_id=hse.id,
                                        overall_result="fail", item_status_after="returned_to_service")
            check("B12 fail + returned_to_service contradiction rejected",
                  it4.status != "in_stock" or it4.condition != "good",
                  detail=f"accepted: failed item back in service (status={it4.status}, condition={it4.condition})")
        except ValueError:
            check("B12 fail + returned_to_service contradiction rejected", True)

        # B13 retire an issued item closes its issuance
        iss5 = await svc.issue_item(db, item_id=it1.id, to_user_id=u_a.id, by_user_id=hse.id)
        await svc.retire_item(db, item_id=it1.id, actor_id=hse.id, reason="QA retire")
        await db.refresh(iss5)
        check("B13 retire issued item → retired + issuance closed",
              it1.status == "retired" and iss5.status == "returned" and it1.currentHolderUserId is None)

        # B14 retire twice is idempotent
        again = await svc.retire_item(db, item_id=it1.id, actor_id=hse.id, reason="QA twice")
        check("B14 retire is idempotent", again.status == "retired")

        # B15 failed inspection on an ISSUED item withdraws it: quarantined,
        # holder cleared, issuance closed (sibling of the recalled_to_store case)
        fresh3 = await svc.commission_items(
            db, plant_id=plant, ppe_type_id=qa_type.id, quantity=1,
            manufacturer="QA", model="M1", batch_lot_number="QA-LOT-3",
            manufacture_date=NOW - timedelta(days=5), purchase_date=None,
            storage_location="QA Store", actor_id=hse.id,
        )
        it5 = fresh3[0]
        iss6 = await svc.issue_item(db, item_id=it5.id, to_user_id=u_a.id, by_user_id=hse.id)
        await svc.record_inspection(db, item_id=it5.id, inspector_user_id=hse.id,
                                    overall_result="fail",
                                    defects_found=[{"defect_description": "cracked", "severity": "major"}])
        await db.refresh(iss6)
        check("B15 fail on issued item → quarantined + issuance closed + holder cleared",
              it5.status == "quarantined" and iss6.status != "active" and it5.currentHolderUserId is None,
              detail=f"status={it5.status} issuance={iss6.status} holder={it5.currentHolderUserId}")

        print("\n── Phase C: API endpoint behaviour ──")

        # C1 dashboard
        d = await ppe_router.dashboard(plantId=plant, user=hse, db=db)
        check("C1 dashboard keys + counts", set(d["cards"]) >= {
            "itemsInService", "inspectionOverdue", "approachingServiceLife",
            "complianceGaps", "activeRecalls", "underRepairQuarantine", "overdueReturns",
        } and d["totalItems"] >= 4)

        # C2 items list + filters
        li = await ppe_router.list_items(plantId=plant, status_filter=None, category=None,
                                         typeCode=qa_type.code, holderId=None, overdueOnly=False,
                                         user=hse, db=db)
        # 3 from B1 + 1 from B12 + 1 from B15 = 5 of this type before C9 commissions more
        check("C2 items filter by typeCode", li["count"] == 5)
        li2 = await ppe_router.list_items(plantId=plant, status_filter="retired", category=None,
                                          typeCode=qa_type.code, holderId=None, overdueOnly=False,
                                          user=hse, db=db)
        check("C2b items filter by status", li2["count"] == 1)
        li3 = await ppe_router.list_items(plantId=plant, status_filter=None, category="no_such_cat",
                                          typeCode=None, holderId=None, overdueOnly=False,
                                          user=hse, db=db)
        check("C2c unknown category → empty, no crash", li3["count"] == 0)

        # C3 issuances list
        lz = await ppe_router.list_issuances(plantId=plant, status_filter="active", overdueOnly=False,
                                             user=hse, db=db)
        check("C3 issuances list responds", "issuances" in lz)

        # C4 inspections due buckets
        due = await ppe_router.inspections_due(plantId=plant, user=hse, db=db)
        check("C4 inspections due buckets", set(due["counts"]) == {"overdue", "this_week", "this_month", "upcoming"})

        # C5 catalog detail 404
        try:
            await ppe_router.catalog_detail("NO-SUCH-CODE", plantId=plant, user=hse, db=db)
            check("C5 catalog detail 404 for unknown code", False, "no exception")
        except HTTPException as e:
            check("C5 catalog detail 404 for unknown code", e.status_code == 404)

        # C6 item detail route
        det = await ppe_router.get_item(item_id=it3.id, user=hse, db=db)
        check("C6 item detail has audit trail", len(det["stateHistory"]) >= 3 and det["type"] is not None)

        # C7 CSV report
        csv_resp = await ppe_router.people_compliance_csv(plantId=plant, user=hse, db=db)
        check("C7 CSV report renders", csv_resp.media_type.startswith("text/csv"))

        # C9 commission with an old manufacture date → warning surfaced
        comm = await ppe_router.commission(
            ppe_router.CommissionBody(
                plantId=plant, ppeTypeId=qa_type.id, quantity=1,
                manufacturer="QA", batchLotNumber="QA-LOT-OLD",
                manufactureDate=NOW - timedelta(days=4000),
            ),
            user=hse, db=db,
        )
        check("C9 born-expired commission returns warning",
              bool(comm.get("warning")), detail=str(comm.get("warning")))

        # C10 RBAC: SUPERVISOR can now issue (spec §3)
        from app.services.permissions import PermissionContext, can
        sup = (
            await db.execute(
                select(User).join(UserRole, UserRole.userId == User.id)
                .join(Role, Role.id == UserRole.roleId)
                .where(Role.code == "SUPERVISOR").limit(1)
            )
        ).scalars().first()
        if sup:
            r = await can(db, sup.id, "PPE.ISSUE", PermissionContext())
            check("C10 SUPERVISOR holds PPE.ISSUE", r.allowed, detail=str(r.reason))
        else:
            print("  SKIP  C10 (no SUPERVISOR user)")

        # C8 RBAC: plain WORKER on dashboard (OWN_RECORDS scope, no record ctx)
        if worker:
            try:
                await ppe_router.dashboard(plantId=plant, user=worker, db=db)
                check("C8 worker can read PPE dashboard (spec: worker self-view)", True)
            except HTTPException as e:
                check("C8 worker can read PPE dashboard (spec: worker self-view)", False,
                      f"403 — worker role gets '{e.detail}'")
        else:
            print("  SKIP  C8 (no WORKER user)")

        await db.rollback()
        print("rolled back — nothing persisted")

    await engine.dispose()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED (candidate defects):", *FAIL, sep="\n  - ")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
