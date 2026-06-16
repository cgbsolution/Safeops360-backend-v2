"""SCI router — Safety Culture Index (SCI-01). Mounts at /api/sci.

SSO-only: every endpoint requires an authenticated SafeOps360 session (the
shared get_current_user dep). The module is a pure consumer — it reads the
verified-event ledger, never writes back to source modules. Phase-1 slice:
My Score, leaderboard (individual + department rollup), and the backfill sync.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.sci import SciLedgerEntry
from app.models.user import User
from app.services.sci_insight import worker_insight
from app.services.sci_scoring import sync_all

router = APIRouter(prefix="/api/sci", tags=["sci"])

_MODULE_LABEL = {
    "SAFETY_OBS": "Safety Observation", "NEAR_MISS": "Near Miss", "FLRA": "FLRA",
    "PTW": "Permit to Work", "INCIDENT": "Incident", "TRAINING": "Training",
    "INSPECTION": "Inspection", "CAPA": "CAPA", "HIRA": "HIRA", "KAIZEN_WALL": "Kaizen Wall",
}


async def _totals_by_user(db: AsyncSession, plant_id: str) -> list[tuple[str, int, int]]:
    """(userId, totalPoints, entryCount) for a plant, points desc."""
    rows = (
        await db.execute(
            select(SciLedgerEntry.userId, func.sum(SciLedgerEntry.finalPoints), func.count(SciLedgerEntry.id))
            .where(SciLedgerEntry.plantId == plant_id)
            .where(SciLedgerEntry.isVoided.is_(False))
            .group_by(SciLedgerEntry.userId)
            .order_by(func.sum(SciLedgerEntry.finalPoints).desc())
        )
    ).all()
    return [(uid, int(pts or 0), int(n or 0)) for uid, pts, n in rows]


@router.get("/leaderboard")
async def leaderboard(
    plantId: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    totals = await _totals_by_user(db, plantId)
    user_ids = [t[0] for t in totals]
    names = {
        u.id: (u.name, u.department, u.role)
        for u in ((await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all() if user_ids else [])
    }
    board = []
    for rank, (uid, pts, n) in enumerate(totals[:limit], start=1):
        nm, dept, role = names.get(uid, (uid, None, None))
        board.append({"rank": rank, "userId": uid, "name": nm, "department": dept, "role": role, "points": pts, "contributions": n, "isMe": uid == user.id})

    # Department rollup.
    dept_totals: dict[str, dict] = {}
    for uid, pts, n in totals:
        d = (names.get(uid, (None, None, None))[1]) or "Unassigned"
        agg = dept_totals.setdefault(d, {"department": d, "points": 0, "people": 0})
        agg["points"] += pts
        agg["people"] += 1
    departments = sorted(dept_totals.values(), key=lambda x: x["points"], reverse=True)
    for i, d in enumerate(departments, start=1):
        d["rank"] = i

    return {
        "plantId": plantId,
        "totalContributors": len(totals),
        "totalPoints": sum(t[1] for t in totals),
        "individuals": board,
        "departments": departments,
    }


@router.get("/my-score")
async def my_score(
    plantId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    entries = (
        await db.execute(
            select(SciLedgerEntry)
            .where(SciLedgerEntry.plantId == plantId)
            .where(SciLedgerEntry.userId == user.id)
            .where(SciLedgerEntry.isVoided.is_(False))
            .order_by(SciLedgerEntry.createdAt.desc())
        )
    ).scalars().all()
    total = sum(e.finalPoints for e in entries)

    by_module: dict[str, dict] = {}
    for e in entries:
        agg = by_module.setdefault(e.sourceModule, {"module": e.sourceModule, "label": _MODULE_LABEL.get(e.sourceModule, e.sourceModule), "points": 0, "count": 0})
        agg["points"] += e.finalPoints
        agg["count"] += 1

    # Rank within plant.
    totals = await _totals_by_user(db, plantId)
    rank = next((i for i, (uid, _, _) in enumerate(totals, start=1) if uid == user.id), None)

    # WIA-01 personalised insight cards.
    insights = await worker_insight(db, user_id=user.id, plant_id=plantId)

    return {
        "userId": user.id,
        "name": user.name,
        "totalPoints": total,
        "rank": rank,
        "totalContributors": len(totals),
        "insights": insights,
        "breakdown": sorted(by_module.values(), key=lambda x: x["points"], reverse=True),
        "recent": [
            {
                "eventType": e.eventType, "module": e.sourceModule, "moduleLabel": _MODULE_LABEL.get(e.sourceModule, e.sourceModule),
                "basePoints": e.basePoints, "multiplier": e.multiplier, "finalPoints": e.finalPoints,
                "sourceTransactionId": e.sourceTransactionId, "scoringPeriod": e.scoringPeriod,
                "createdAt": e.createdAt.isoformat() if e.createdAt else None,
            }
            for e in entries[:30]
        ],
    }


@router.post("/sync")
async def sync(
    plantId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Back-fill the SCI ledger for a plant from verified events."""
    return await sync_all(db, plant_id=plantId, actor="SYSTEM")
