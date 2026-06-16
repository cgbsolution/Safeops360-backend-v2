"""Single source of truth for the PTW–FLRA gate. Direct port of
`src/lib/ptw/flra-gate.ts`. The workflow engine and the receiver-step UI both
key off these helpers — keep behaviour identical to the Node version.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.flra import FLRA, FLRACrewSignature, FLRAStatus
from app.models.permit import Permit
from app.models.user import User


@dataclass
class FlraGateStatus:
    ok: bool
    reason: str | None = None
    active_flra_id: str | None = None
    active_flra_number: str | None = None
    flra_status: Literal["IN_PROGRESS", "COMPLETED", "SUPERSEDED", "CANCELLED"] | None = None
    signed_count: int | None = None
    total_crew: int | None = None
    unsigned_names: list[str] | None = None


async def get_flra_gate_status(db: AsyncSession, permit_id: str) -> FlraGateStatus:
    """Pure read of DB state — no side effects. Safe from server components."""
    stmt = (
        select(FLRA)
        .where(FLRA.permitId == permit_id)
        .where(FLRA.status.in_([FLRAStatus.IN_PROGRESS, FLRAStatus.COMPLETED]))
        .order_by(FLRA.createdAt.desc())
        .options(selectinload(FLRA.crewSignatures).selectinload(FLRACrewSignature.user) if False else selectinload(FLRA.crewSignatures))
        .limit(1)
    )
    result = await db.execute(stmt)
    flra = result.scalar_one_or_none()
    if flra is None:
        return FlraGateStatus(
            ok=False,
            reason=(
                "A completed FLRA is required before this permit can become ACTIVE. "
                "All crew members must sign the FLRA at the worksite."
            ),
        )

    # Resolve names for the unsigned rows
    user_ids = [s.userId for s in flra.crewSignatures]
    users_by_id: dict[str, User] = {}
    if user_ids:
        u_stmt = select(User).where(User.id.in_(user_ids))
        u_res = await db.execute(u_stmt)
        users_by_id = {u.id: u for u in u_res.scalars().all()}

    total_crew = len(flra.crewSignatures)
    signed_count = sum(1 for s in flra.crewSignatures if s.signed)
    unsigned_names = [users_by_id[s.userId].name for s in flra.crewSignatures if not s.signed and s.userId in users_by_id]

    if flra.status == FLRAStatus.COMPLETED:
        return FlraGateStatus(
            ok=True,
            active_flra_id=flra.id,
            active_flra_number=flra.number,
            flra_status="COMPLETED",
            signed_count=signed_count,
            total_crew=total_crew,
        )

    if total_crew == 0:
        return FlraGateStatus(
            ok=False,
            active_flra_id=flra.id,
            active_flra_number=flra.number,
            flra_status="IN_PROGRESS",
            reason=f"FLRA {flra.number} has no crew sign-off rows. Add crew members and sign before activation.",
            signed_count=0,
            total_crew=0,
        )

    if signed_count < total_crew:
        return FlraGateStatus(
            ok=False,
            active_flra_id=flra.id,
            active_flra_number=flra.number,
            flra_status="IN_PROGRESS",
            reason=f"FLRA {flra.number} is awaiting sign-off from: {', '.join(unsigned_names)}.",
            signed_count=signed_count,
            total_crew=total_crew,
            unsigned_names=unsigned_names,
        )

    # All signed but status is still IN_PROGRESS — race condition tolerated.
    return FlraGateStatus(
        ok=True,
        active_flra_id=flra.id,
        active_flra_number=flra.number,
        flra_status="IN_PROGRESS",
        signed_count=signed_count,
        total_crew=total_crew,
    )


async def maybe_complete_flra(db: AsyncSession, flra_id: str) -> bool:
    """Idempotent — flips IN_PROGRESS → COMPLETED when all rows signed."""
    flra = await db.get(FLRA, flra_id)
    if flra is None or flra.status != FLRAStatus.IN_PROGRESS:
        return False
    sigs_stmt = select(FLRACrewSignature).where(FLRACrewSignature.flraId == flra_id)
    sigs = (await db.execute(sigs_stmt)).scalars().all()
    if not sigs or any(not s.signed for s in sigs):
        return False
    flra.status = FLRAStatus.COMPLETED
    flra.completedAt = datetime.now(timezone.utc)
    await db.flush()
    return True


async def resolve_crew_for_flra(
    db: AsyncSession,
    permit_id: str | None,
    fallback_team_member_ids: list[str],
) -> list[str]:
    """Returns the user-ids that should get FLRACrewSignature rows on creation."""
    if permit_id:
        permit = await db.get(Permit, permit_id, options=[selectinload(Permit.workCrew)])
        if permit:
            if permit.workCrew:
                return list({c.userId for c in permit.workCrew})
            if permit.receiverId:
                return [permit.receiverId]
    return list(dict.fromkeys(fallback_team_member_ids))
