"""Verify the PTW PPE gate (PPE-01 Pass 2) against live data.

Read-only. For a handful of recent permits: run the full activation gate and
show whether the new PPE check contributes blockers/warnings; then run the
raw crew PPE check for the same crews so the numbers can be eyeballed.

Run from the backend root:
    .venv/Scripts/python.exe scripts/verify_ptw_ppe_gate.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.db import AsyncSessionLocal, engine
from app.models.permit import Permit, PermitCrewMember
from app.services.ppe_gate import check_ppe_for_crew
from app.services.ptw_activation_gate import can_ptw_transition_to_active


async def main() -> None:
    async with AsyncSessionLocal() as db:
        # ids only — pre-loading Permit rows here would poison the identity
        # map and defeat the gate's selectinload (lazy-load greenlet error).
        # Only permits that actually have crew exercise the PPE path.
        rows = (
            await db.execute(
                select(Permit.id, Permit.number, Permit.type, Permit.status, Permit.plantId)
                .where(
                    Permit.id.in_(
                        select(PermitCrewMember.permitId).where(
                            PermitCrewMember.removedAt.is_(None)
                        )
                    )
                )
                .order_by(Permit.createdAt.desc())
                .limit(5)
            )
        ).all()
        print(f"Checking {len(rows)} most recent permits\n")
        for permit_id, number, p_type, p_status, plant_id in rows:
            ptype = p_type.value if hasattr(p_type, "value") else str(p_type)
            gate = await can_ptw_transition_to_active(db, permit_id)
            print(f"── {number} [{ptype}] status={p_status.value if hasattr(p_status, 'value') else p_status}")
            print(f"   gate ok={gate.ok}  blockers={[b.code for b in gate.blockers]}")
            for line in gate.crew_ppe_issues:
                print(f"   PPE BLOCK  → {line}")
            for line in gate.crew_ppe_warnings:
                print(f"   PPE WARN   → {line}")

            crew = (
                await db.execute(
                    select(PermitCrewMember)
                    .where(PermitCrewMember.permitId == permit_id)
                    .where(PermitCrewMember.removedAt.is_(None))
                )
            ).scalars().all()
            if crew:
                results = await check_ppe_for_crew(
                    db,
                    plant_id=plant_id,
                    user_ids=[c.userId for c in crew],
                    permit_type_code=ptype,
                )
                ok_n = sum(1 for r in results.values() if r.ok)
                print(f"   crew={len(crew)}  ppe-ok={ok_n}  ppe-blocked={len(crew) - ok_n}")
            print()

        # ─── Direct crew check against the PPE demo population ───
        from app.models.user import User
        from app.services.ppe_inventory import people_compliance

        plant_row = (
            await db.execute(
                select(User.plantId)
                .where(User.plantId.isnot(None))
                .group_by(User.plantId)
                .limit(1)
            )
        ).first()
        if plant_row:
            plant_id = plant_row[0]
            comp = await people_compliance(db, plant_id=plant_id)
            print(
                f"People Compliance @ {plant_id}: "
                f"{comp['summary']['compliant']} compliant / "
                f"{comp['summary']['gaps']} gaps / "
                f"{comp['summary']['criticalGaps']} critical"
            )
            # One person from each bucket through the gate engine, with the
            # permit-type requirement layered on (harness enables WORK_AT_HEIGHT).
            sample: dict[str, dict] = {}
            for person in comp["people"]:
                sample.setdefault(person["overall"], person)
            user_ids = [p["userId"] for p in sample.values()]
            results = await check_ppe_for_crew(
                db,
                plant_id=plant_id,
                user_ids=user_ids,
                permit_type_code="WORK_AT_HEIGHT",
            )
            for overall, person in sample.items():
                r = results[person["userId"]]
                print(f"\n   [{overall}] {person['name']} ({person['role']}) → gate ok={r.ok}")
                for b in r.blockers:
                    print(f"      BLOCK {b.code}: {b.message}")
                for w in r.warnings:
                    print(f"      WARN  {w.code}: {w.message}")
                if r.satisfied:
                    print(f"      satisfied: {', '.join(r.satisfied)}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
