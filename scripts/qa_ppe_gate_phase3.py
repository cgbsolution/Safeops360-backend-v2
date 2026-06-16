"""QA Phase 3 — Python ↔ TypeScript gate parity (PPE-01 Pass 2).

The TS SSR mirror (src/lib/ptw/activation-gate.ts) renders the blockers the
Python service enforces — they must agree. The TS side reads committed data
via Prisma, so this phase COMMITS a clearly-tagged QA bundle (users with
emails qa-parity-*, items/issuances QAP-*, profile scopeId QA_PARITY, crew
rows on one permit), and `cleanup` removes every trace.

Usage (backend root):
    python scripts/qa_ppe_gate_phase3.py setup    → prints PERMIT_ID, writes qa_parity_expected.json
    python scripts/qa_ppe_gate_phase3.py cleanup  → deletes the bundle
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select

from app.core.db import AsyncSessionLocal, engine
from app.models._base import gen_id
from app.models.permit import Permit, PermitCrewMember
from app.models.ppe import PpeIssuance, PpeItem, PpeRequirementProfile, PpeType
from app.models.user import User
from app.services.ptw_activation_gate import can_ptw_transition_to_active

NOW = datetime.now(timezone.utc)
OUT = Path(__file__).parent / "qa_parity_expected.json"


def _uid() -> str:
    return uuid4().hex[:10]


async def setup() -> None:
    async with AsyncSessionLocal() as db:
        permit_id, number, p_type, plant = (
            await db.execute(
                select(Permit.id, Permit.number, Permit.type, Permit.plantId).limit(1)
            )
        ).first()
        ptype = p_type.value if hasattr(p_type, "value") else str(p_type)

        types = {
            t.code: t
            for t in (
                await db.execute(
                    select(PpeType).where(
                        PpeType.code.in_(["GOGGLES-CHEM", "HELMET-IS3521", "COVERALL-FR", "GLOVES-HEAT"])
                    )
                )
            ).scalars().all()
        }

        def user(name: str) -> User:
            u = User(
                id=gen_id(), email=f"qa-parity-{_uid()}@qa.local", name=name,
                passwordHash="x", role="QA_PARITY", plantId=plant,
            )
            db.add(u)
            return u

        def issue(u: User, code: str, *, life_days: int = 365, acknowledged: bool = True) -> None:
            t = types[code]
            it = PpeItem(
                id=gen_id(), itemNumber=f"QAP-ITEM-{_uid()}", serialNumber=f"QAP-SN-{_uid()}",
                ppeTypeId=t.id, ppeTypeCode=t.code, ppeTypeName=t.name,
                manufactureDate=NOW - timedelta(days=10), plantId=plant,
                status="issued", condition="good", commissionedAt=NOW - timedelta(days=10),
                serviceLifeEndDate=NOW + timedelta(days=life_days),
                stateHistory=[], versionNumber=1,
            )
            db.add(it)
            db.add(PpeIssuance(
                issuanceNumber=f"QAP-ISS-{_uid()}",
                ppeItemId=it.id, ppeTypeCode=t.code, ppeTypeName=t.name,
                serialNumber=it.serialNumber,
                issuedToUserId=u.id, issuedToName=u.name,
                issuedByUserId=u.id, issuedByName="QA",
                recipientAcknowledged=acknowledged,
                recipientAcknowledgedAt=NOW if acknowledged else None,
                status="active", plantId=plant,
            ))

        u1 = user("QA Parity Bare")
        u2 = user("QA Parity Mixed")
        await db.flush()
        db.add(PpeRequirementProfile(
            plantId=plant, scopeType="role", scopeId="QA_PARITY",
            scopeName="QA Parity Role",
            requiredPpe=[
                {"ppe_type_code": "GOGGLES-CHEM", "ppe_type_name": "Chemical Splash Goggles", "requirement_level": "mandatory"},
            ],
            isActive=True,
        ))
        issue(u2, "GOGGLES-CHEM")                          # valid
        issue(u2, "HELMET-IS3521")                         # valid (satisfies a permit group)
        issue(u2, "COVERALL-FR", life_days=-5)             # expired
        issue(u2, "GLOVES-HEAT", acknowledged=False)       # unacknowledged
        db.add(PermitCrewMember(permitId=permit_id, userId=u1.id, role="WORKER"))
        db.add(PermitCrewMember(permitId=permit_id, userId=u2.id, role="WORKER"))
        await db.commit()

        gate = await can_ptw_transition_to_active(db, permit_id)
        OUT.write_text(json.dumps({
            "permitId": permit_id, "number": number, "type": ptype,
            "crewPpeIssues": gate.crew_ppe_issues,
            "crewPpeWarnings": gate.crew_ppe_warnings,
        }, indent=2), encoding="utf-8")
        print(permit_id)
    await engine.dispose()


async def cleanup() -> None:
    async with AsyncSessionLocal() as db:
        qa_users = (
            await db.execute(select(User.id).where(User.email.like("qa-parity-%")))
        ).scalars().all()
        if qa_users:
            await db.execute(delete(PermitCrewMember).where(PermitCrewMember.userId.in_(qa_users)))
        await db.execute(delete(PpeIssuance).where(PpeIssuance.issuanceNumber.like("QAP-ISS-%")))
        await db.execute(delete(PpeItem).where(PpeItem.itemNumber.like("QAP-ITEM-%")))
        await db.execute(delete(PpeRequirementProfile).where(PpeRequirementProfile.scopeId == "QA_PARITY"))
        if qa_users:
            await db.execute(delete(User).where(User.id.in_(qa_users)))
        await db.commit()
        print(f"cleaned: {len(qa_users)} users + bundle")
    await engine.dispose()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "setup":
        asyncio.run(setup())
    elif cmd == "cleanup":
        asyncio.run(cleanup())
    else:
        print("usage: qa_ppe_gate_phase3.py setup|cleanup")
        sys.exit(2)
