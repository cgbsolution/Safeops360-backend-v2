"""QA Phase 2 — PTW PPE gate integration tests (PPE-01 Pass 2).

Covers the activation-gate aggregation, the §9.1 gate-check endpoint
(including RBAC), the PermitCrewMember PPE-snapshot columns, and the
crew-removal rule. Same discipline as Phase 1: flush-only, rollback at the
end, nothing persisted.

Run from the backend root:
    $env:PYTHONIOENCODING="utf-8"; .venv/Scripts/python.exe scripts/qa_ppe_gate_phase2.py
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
from app.models.permit import Permit, PermitCrewMember
from app.models.ppe import PpeIssuance, PpeItem
from app.models.user import User
from app.routers.ppe import PtwGateCheckBody, ptw_gate_check
from app.services.permissions import PermissionContext, can
from app.services.ppe_gate import get_permit_type_ppe
from app.services.ptw_activation_gate import can_ptw_transition_to_active

NOW = datetime.now(timezone.utc)
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if not cond else ""))


def _uid() -> str:
    return uuid4().hex[:10]


async def main() -> None:  # noqa: PLR0915
    async with AsyncSessionLocal() as db:
        # A real permit to hang synthetic crew on (gate is read-only on it).
        permit_row = (
            await db.execute(
                select(Permit.id, Permit.number, Permit.type, Permit.plantId).limit(1)
            )
        ).first()
        if permit_row is None:
            print("No permits in DB")
            sys.exit(2)
        permit_id, number, p_type, plant = permit_row
        ptype = p_type.value if hasattr(p_type, "value") else str(p_type)
        print(f"Using permit {number} [{ptype}] @ {plant}\n")

        def qa_user(name: str) -> User:
            u = User(
                id=gen_id(), email=f"qa-{_uid()}@qa.local", name=name,
                passwordHash="x", role=f"QA_{_uid()[:6]}", plantId=plant,
            )
            db.add(u)
            return u

        def issue_type(u: User, type_row, *, life_days: int = 365, acknowledged: bool = True) -> None:
            it = PpeItem(
                id=gen_id(), itemNumber=f"QA-ITEM-{_uid()}", serialNumber=f"QA-SN-{_uid()}",
                ppeTypeId=type_row.id, ppeTypeCode=type_row.code, ppeTypeName=type_row.name,
                manufactureDate=NOW - timedelta(days=10), plantId=plant,
                status="issued", condition="good", commissionedAt=NOW - timedelta(days=10),
                serviceLifeEndDate=NOW + timedelta(days=life_days),
                stateHistory=[], versionNumber=1,
            )
            db.add(it)
            db.add(PpeIssuance(
                issuanceNumber=f"QA-ISS-{_uid()}",
                ppeItemId=it.id, ppeTypeCode=it.ppeTypeCode, ppeTypeName=it.ppeTypeName,
                serialNumber=it.serialNumber,
                issuedToUserId=u.id, issuedToName=u.name,
                issuedByUserId=u.id, issuedByName="QA",
                recipientAcknowledged=acknowledged,
                recipientAcknowledgedAt=NOW if acknowledged else None,
                status="active", plantId=plant,
            ))

        # Required PPE for this permit type, one representative per variant group.
        req_types = await get_permit_type_ppe(db, ptype)
        groups: dict[tuple[str, str], list] = {}
        for t in req_types:
            groups.setdefault((t.category, t.subcategory), []).append(t)
        one_per_group = [g[0] for g in groups.values()]
        print(f"Permit type requires {len(groups)} variant groups\n")

        print("── Phase 2: integration tests ──")

        # I1: two non-compliant crew → CREW_PPE names BOTH
        u1, u2 = qa_user("QA Crew One"), qa_user("QA Crew Two")
        await db.flush()  # users must exist before crew FK rows
        db.add(PermitCrewMember(permitId=permit_id, userId=u1.id, role="WORKER"))
        db.add(PermitCrewMember(permitId=permit_id, userId=u2.id, role="WORKER"))
        await db.flush()
        gate = await can_ptw_transition_to_active(db, permit_id)
        crew_ppe = next((b for b in gate.blockers if b.code == "CREW_PPE"), None)
        check("I1 CREW_PPE blocker emitted for non-compliant crew", crew_ppe is not None)
        check("I1b both crew named in blocker",
              crew_ppe is not None and "QA Crew One" in crew_ppe.message and "QA Crew Two" in crew_ppe.message,
              detail=(crew_ppe.message[:200] if crew_ppe else "none"))
        check("I1c gate not ok", not gate.ok)

        # I2: fully equip u1; u2 removed → no CREW_PPE at all
        for t in one_per_group:
            issue_type(u1, t)
        u2_row = (
            await db.execute(
                select(PermitCrewMember)
                .where(PermitCrewMember.permitId == permit_id)
                .where(PermitCrewMember.userId == u2.id)
            )
        ).scalar_one()
        u2_row.removedAt = NOW
        u2_row.removalReason = "QA"
        await db.flush()
        gate = await can_ptw_transition_to_active(db, permit_id)
        codes = [b.code for b in gate.blockers]
        check("I2 compliant crew + removed member → no CREW_PPE", "CREW_PPE" not in codes,
              detail=str([b.message[:120] for b in gate.blockers if b.code == "CREW_PPE"]))
        check("I2b removed member not re-checked", "QA Crew Two" not in " ".join(b.message for b in gate.blockers))

        # I3: warning-only (item near end of life) → CREW_PPE_WARN, severity WARN
        u3 = qa_user("QA Crew Warn")
        await db.flush()
        db.add(PermitCrewMember(permitId=permit_id, userId=u3.id, role="WORKER"))
        for i, t in enumerate(one_per_group):
            issue_type(u3, t, life_days=(20 if i == 0 else 365))  # first item expiring soon
        await db.flush()
        gate = await can_ptw_transition_to_active(db, permit_id)
        warn = next((b for b in gate.blockers if b.code == "CREW_PPE_WARN"), None)
        check("I3 near-expiry → CREW_PPE_WARN present", warn is not None)
        check("I3b WARN severity, not a hard block",
              warn is not None and warn.severity == "WARN"
              and not any(b.code == "CREW_PPE" for b in gate.blockers))

        # I4: §9.1 endpoint — RBAC + payload shape
        from app.models.user import Role, UserRole

        privileged = (
            await db.execute(
                select(User)
                .join(UserRole, UserRole.userId == User.id)
                .join(Role, Role.id == UserRole.roleId)
                .where(Role.code.in_(["HSE_MANAGER", "SYSTEM_ADMIN", "ADMIN", "SAFETY_OFFICER"]))
                .where(~User.email.like("qa-%"))
                .limit(10)
            )
        ).scalars().all()
        sampled = (await db.execute(select(User).where(~User.email.like("qa-%")).limit(40))).scalars().all()
        allowed_user = denied_user = None
        for u in list(privileged) + list(sampled):
            r = await can(db, u.id, "PPE.READ", PermissionContext())
            if r.allowed and allowed_user is None:
                allowed_user = u
            if not r.allowed and denied_user is None:
                denied_user = u
            if allowed_user and denied_user:
                break

        body = PtwGateCheckBody(ptwId="QA-PTW", plantId=plant, permitType=ptype, workers=[u1.id, u3.id, "ghost-user"])
        if allowed_user:
            resp = await ptw_gate_check(body, user=allowed_user, db=db)
            check("I4 gate-check: compliant worker listed CLEAR", u1.id in resp["compliantWorkers"],
                  detail=str(resp)[:200])
            check("I4b gate-check: ghost worker BLOCKED with USER_NOT_FOUND",
                  resp["gateStatus"] == "BLOCKED"
                  and any(w["workerId"] == "ghost-user" and w["gaps"][0]["reason"] == "USER_NOT_FOUND"
                          for w in resp["nonCompliantWorkers"]))
            check("I4c gate-check: u3 (warn-only) counts as compliant", u3.id in resp["compliantWorkers"])
        else:
            check("I4 gate-check with permitted user", False, "no user with PPE.READ found")
        if denied_user:
            try:
                await ptw_gate_check(body, user=denied_user, db=db)
                check("I5 gate-check RBAC 403 without PPE.READ", False, "no exception raised")
            except HTTPException as e:
                check("I5 gate-check RBAC 403 without PPE.READ", e.status_code == 403)
        else:
            print("  SKIP  I5 (every sampled user has PPE.READ)")

        # I6: snapshot columns round-trip through the live DB
        u4 = qa_user("QA Snapshot")
        await db.flush()
        db.add(PermitCrewMember(
            permitId=permit_id, userId=u4.id, role="WORKER",
            ppeValidAtIssuance=False, ppeValidationNotes="QA note: helmet not issued",
        ))
        await db.flush()
        row = (
            await db.execute(
                select(PermitCrewMember.ppeValidAtIssuance, PermitCrewMember.ppeValidationNotes)
                .where(PermitCrewMember.permitId == permit_id)
                .where(PermitCrewMember.userId == u4.id)
            )
        ).first()
        check("I6 ppeValidAtIssuance/Notes round-trip",
              row is not None and row[0] is False and "helmet" in (row[1] or ""))

        await db.rollback()
        print("rolled back — nothing persisted")

    await engine.dispose()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
