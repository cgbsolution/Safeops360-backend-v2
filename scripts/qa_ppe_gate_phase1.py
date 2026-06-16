"""QA Phase 1 — PTW PPE gate engine unit tests (PPE-01 Pass 2).

Self-contained: all fixtures (QA users, QA PPE types, items, issuances,
requirement profiles) are created in ONE session with flush only and rolled
back at the end — nothing is ever committed, no demo data is touched.

Run from the backend root:
    $env:PYTHONIOENCODING="utf-8"; .venv/Scripts/python.exe scripts/qa_ppe_gate_phase1.py
Exit code 0 = all green.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select

from app.core.db import AsyncSessionLocal, engine
from app.models._base import gen_id
from app.models.plant import Plant
from app.models.ppe import PpeIssuance, PpeItem, PpeRequirementProfile, PpeType
from app.models.user import User
from app.services.ppe_gate import check_ppe_for_crew, get_permit_type_ppe
from app.services.ppe_inventory import ALL_ROLES

NOW = datetime.now(timezone.utc)
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def _uid() -> str:
    return uuid4().hex[:10]


async def main() -> None:  # noqa: PLR0915
    async with AsyncSessionLocal() as db:
        plants = (await db.execute(select(Plant.id).limit(2))).scalars().all()
        if len(plants) < 2:
            print("Need 2 plants in DB for plant-scoping test")
            sys.exit(2)
        plant, other_plant = plants[0], plants[1]

        # ─── Fixture factories (flush-only, rolled back at end) ───
        def qa_type(code: str, *, cat: str, sub: str, enables: list[str] | None = None) -> PpeType:
            t = PpeType(
                id=gen_id(),
                code=code, name=code.replace("QA-", "QA ").title(),
                category=cat, subcategory=sub,
                enablesPermitTypes=enables or [],
                applicableStandards=[], controlsHazards=[], requiredForAreas=[],
                inspectionSchedule=[], requiredTrainingPrograms=[], regulatoryReferences=[],
            )
            db.add(t)
            return t

        def qa_user(role: str) -> User:
            u = User(
                id=gen_id(),
                email=f"qa-{_uid()}@qa.local", name=f"QA {role} {_uid()[:4]}",
                passwordHash="x", role=role, plantId=plant,
            )
            db.add(u)
            return u

        def qa_item(
            t: PpeType, *, status: str = "issued", condition: str = "good",
            life_days: int = 365, insp_due_days: int | None = None,
            recall: bool = False, item_plant: str | None = None,
        ) -> PpeItem:
            it = PpeItem(
                id=gen_id(),
                itemNumber=f"QA-ITEM-{_uid()}", serialNumber=f"QA-SN-{_uid()}",
                ppeTypeId=t.id, ppeTypeCode=t.code, ppeTypeName=t.name,
                manufactureDate=NOW - timedelta(days=30),
                plantId=item_plant or plant, status=status, condition=condition,
                commissionedAt=NOW - timedelta(days=30),
                serviceLifeEndDate=NOW + timedelta(days=life_days),
                nextInspectionDueDate=(NOW + timedelta(days=insp_due_days)) if insp_due_days is not None else None,
                batchUnderRecall=recall, stateHistory=[], versionNumber=1,
            )
            db.add(it)
            return it

        def qa_iss(u: User, it: PpeItem, *, acknowledged: bool = True, iss_plant: str | None = None) -> PpeIssuance:
            i = PpeIssuance(
                issuanceNumber=f"QA-ISS-{_uid()}",
                ppeItemId=it.id, ppeTypeCode=it.ppeTypeCode, ppeTypeName=it.ppeTypeName,
                serialNumber=it.serialNumber,
                issuedToUserId=u.id, issuedToName=u.name,
                issuedByUserId=u.id, issuedByName="QA",
                recipientAcknowledged=acknowledged,
                recipientAcknowledgedAt=NOW if acknowledged else None,
                status="active", plantId=iss_plant or (it.plantId),
            )
            db.add(i)
            return i

        def qa_profile(scope_id: str, reqs: list[dict]) -> PpeRequirementProfile:
            p = PpeRequirementProfile(
                plantId=plant, scopeType="role", scopeId=scope_id,
                scopeName=f"QA {scope_id}", requiredPpe=reqs, isActive=True,
            )
            db.add(p)
            return p

        async def gate(u: User, permit_type: str | None = None):
            res = await check_ppe_for_crew(
                db, plant_id=plant, user_ids=[u.id], permit_type_code=permit_type
            )
            return res[u.id]

        # ─── Shared fixtures ───
        helm_a = qa_type("QA-HELM-A", cat="qa_head", sub="qa_helmet", enables=["qa_permit"])
        helm_b = qa_type("QA-HELM-B", cat="qa_head", sub="qa_helmet", enables=["qa_permit"])
        boots = qa_type("QA-BOOTS", cat="qa_foot", sub="qa_boots", enables=["qa_permit"])
        glove = qa_type("QA-GLOVE", cat="qa_hand", sub="qa_glove")
        vest = qa_type("QA-VEST", cat="qa_vis", sub="qa_vest")
        await db.flush()
        qa_profile("QA_ROLE", [
            {"ppe_type_code": "QA-GLOVE", "ppe_type_name": "QA Glove", "requirement_level": "mandatory"},
            {"ppe_type_code": "QA-VEST", "ppe_type_name": "QA Vest", "requirement_level": "recommended"},
        ])
        qa_profile("QA_ROLE_HELM", [
            {"ppe_type_code": "QA-HELM-A", "ppe_type_name": "QA Helm A", "requirement_level": "mandatory"},
        ])
        await db.flush()

        print("── Phase 1: gate engine unit tests ──")

        # T1 no requirements at all
        u = qa_user("QA_NOREQ"); await db.flush()
        r = await gate(u)
        check("T1 no-reqs → ok", r.ok and not r.blockers and not r.warnings)

        # T2 mandatory not issued
        u = qa_user("QA_ROLE"); await db.flush()
        r = await gate(u)
        check("T2 mandatory missing → NOT_ISSUED",
              not r.ok and any(b.code == "NOT_ISSUED" and b.ppeTypeCode == "QA-GLOVE" for b in r.blockers))
        check("T3 recommended missing → warn only",
              any(w.code == "RECOMMENDED_MISSING" for w in r.warnings)
              and not any(b.ppeTypeCode == "QA-VEST" for b in r.blockers))

        # T4 unacknowledged
        u = qa_user("QA_ROLE")
        qa_iss(u, qa_item(glove), acknowledged=False); await db.flush()
        r = await gate(u)
        check("T4 unacknowledged → NOT_ACKNOWLEDGED",
              not r.ok and any(b.code == "NOT_ACKNOWLEDGED" for b in r.blockers))

        # T5 valid holding
        u = qa_user("QA_ROLE")
        qa_iss(u, qa_item(glove)); await db.flush()
        r = await gate(u)
        check("T5 valid holding → ok+satisfied", r.ok and "QA-GLOVE" in r.satisfied)

        # T6 service life exceeded
        u = qa_user("QA_ROLE")
        qa_iss(u, qa_item(glove, life_days=-5)); await db.flush()
        r = await gate(u)
        check("T6 service life exceeded → ITEM_INVALID",
              not r.ok and any(b.code == "ITEM_INVALID" and "service life" in b.message for b in r.blockers))

        # T7 inspection overdue
        u = qa_user("QA_ROLE")
        qa_iss(u, qa_item(glove, insp_due_days=-10)); await db.flush()
        r = await gate(u)
        check("T7 inspection overdue → ITEM_INVALID",
              not r.ok and any("inspection overdue" in b.message for b in r.blockers))

        # T8 batch recall
        u = qa_user("QA_ROLE")
        qa_iss(u, qa_item(glove, recall=True)); await db.flush()
        r = await gate(u)
        check("T8 batch recall → ITEM_INVALID",
              not r.ok and any("recall" in b.message for b in r.blockers))

        # T9 unserviceable condition
        u = qa_user("QA_ROLE")
        qa_iss(u, qa_item(glove, condition="unserviceable")); await db.flush()
        r = await gate(u)
        check("T9 unserviceable → ITEM_INVALID",
              not r.ok and any("unserviceable" in b.message for b in r.blockers))

        # T10 service life ending soon → warn but ok
        u = qa_user("QA_ROLE")
        qa_iss(u, qa_item(glove, life_days=30)); await db.flush()
        r = await gate(u)
        check("T10 life ending soon → ok + ITEM_WARNING",
              r.ok and any(w.code == "ITEM_WARNING" for w in r.warnings))

        # T11 duplicate holdings: expired inserted FIRST, valid second → must pass
        u = qa_user("QA_ROLE")
        qa_iss(u, qa_item(glove, life_days=-5))
        await db.flush()
        qa_iss(u, qa_item(glove, life_days=365)); await db.flush()
        r = await gate(u)
        check("T11 dup holdings (expired first, valid second) → ok", r.ok,
              detail=f"blockers={[b.message for b in r.blockers]}")

        # T12 variant group satisfied by either member
        u = qa_user("QA_NOREQ")
        qa_iss(u, qa_item(helm_b))
        qa_iss(u, qa_item(boots)); await db.flush()
        r = await gate(u, "QA_PERMIT")
        check("T12 variant group: holds B only → ok", r.ok,
              detail=f"blockers={[b.message for b in r.blockers]}")

        # T13 variant group: neither member → ONE blocker with joint label
        u = qa_user("QA_NOREQ")
        qa_iss(u, qa_item(boots)); await db.flush()
        r = await gate(u, "QA_PERMIT")
        helm_blockers = [b for b in r.blockers if "Helm" in b.message]
        check("T13 group missing → single joint blocker",
              not r.ok and len(helm_blockers) == 1 and "/" in helm_blockers[0].ppeTypeName,
              detail=f"blockers={[b.message for b in r.blockers]}")

        # T14 group: one unacknowledged, other missing → NOT_ACKNOWLEDGED
        u = qa_user("QA_NOREQ")
        qa_iss(u, qa_item(helm_a), acknowledged=False)
        qa_iss(u, qa_item(boots)); await db.flush()
        r = await gate(u, "QA_PERMIT")
        check("T14 group held-unacknowledged → NOT_ACKNOWLEDGED",
              not r.ok and any(b.code == "NOT_ACKNOWLEDGED" for b in r.blockers))

        # T15 profile covers a group member → group skipped (no duplicate blocker)
        u = qa_user("QA_ROLE_HELM"); await db.flush()
        r = await gate(u, "QA_PERMIT")
        helm_blockers = [b for b in r.blockers if "Helm" in b.message]
        check("T15 profile overrides group → exactly 1 helmet blocker",
              len(helm_blockers) == 1 and helm_blockers[0].ppeTypeCode == "QA-HELM-A",
              detail=f"helm blockers={[b.message for b in helm_blockers]}")

        # T16 plant scoping: issuance at another plant doesn't count
        u = qa_user("QA_ROLE")
        qa_iss(u, qa_item(glove, item_plant=other_plant), iss_plant=other_plant); await db.flush()
        r = await gate(u)
        check("T16 other-plant issuance → NOT_ISSUED",
              not r.ok and any(b.code == "NOT_ISSUED" for b in r.blockers))

        # T17 ELECTRICAL_LOTO alias resolves against the real catalog
        loto_types = await get_permit_type_ppe(db, "ELECTRICAL_LOTO")
        check("T17 ELECTRICAL_LOTO → catalog 'electrical' types",
              any(t.code == "GLOVES-ELEC-LV" for t in loto_types),
              detail=f"got={[t.code for t in loto_types]}")

        # T18 unknown user id
        res = await check_ppe_for_crew(db, plant_id=plant, user_ids=["nope-" + _uid()])
        bad = list(res.values())[0]
        check("T18 unknown user → USER_NOT_FOUND",
              not bad.ok and bad.blockers and bad.blockers[0].code == "USER_NOT_FOUND")

        # T19 *ALL* base profile applies to every role (added late so it
        # doesn't pollute earlier tests)
        qa_profile(ALL_ROLES, [
            {"ppe_type_code": "QA-VEST", "ppe_type_name": "QA Vest", "requirement_level": "mandatory"},
        ]); await db.flush()
        u = qa_user("QA_NOREQ"); await db.flush()
        r = await gate(u)
        check("T19 ALL-roles base profile applies",
              not r.ok and any(b.ppeTypeCode == "QA-VEST" for b in r.blockers))

        # T20 dup holdings: unacknowledged first, acknowledged-valid second → ok
        u = qa_user("QA_ROLE")
        qa_iss(u, qa_item(glove), acknowledged=False)
        await db.flush()
        qa_iss(u, qa_item(glove)); await db.flush()
        # also satisfy the new ALL-roles vest requirement
        qa_iss(u, qa_item(vest)); await db.flush()
        r = await gate(u)
        check("T20 dup holdings (unack first, valid second) → ok", r.ok,
              detail=f"blockers={[b.message for b in r.blockers]}")

        await db.rollback()
        print("rolled back — nothing persisted")

    await engine.dispose()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
