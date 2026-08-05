"""Weekly Insight Engine — snapshot persistence (spec §2, §14).

Idempotent upsert per (tenant, module, identityKey, weekOf) via ON CONFLICT — a
re-run for the same weekOf UPDATES rather than duplicates (§14). Prior-week load
returns the most recent snapshot per identity for the lifecycle comparison.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.insight_snapshot import InsightSnapshot
from app.services.insights.weekly.lifecycle import LiveInsight

_RETENTION_WEEKS = 26  # keep ≥26 weeks (spec §2)


async def load_prior_by_key(
    db: AsyncSession, *, tenant: str, module: str, week_of: datetime
) -> dict[str, InsightSnapshot]:
    """Most recent snapshot per identityKey strictly before this week (§6)."""
    horizon = week_of - timedelta(weeks=_RETENTION_WEEKS)
    rows = (
        await db.execute(
            select(InsightSnapshot)
            .where(
                InsightSnapshot.tenantId == tenant,
                InsightSnapshot.module == module,
                InsightSnapshot.weekOf < week_of,
                InsightSnapshot.weekOf >= horizon,
            )
            .order_by(InsightSnapshot.weekOf.desc())
        )
    ).scalars().all()
    prior: dict[str, InsightSnapshot] = {}
    for r in rows:  # rows are weekOf-desc → first seen per key is the most recent
        prior.setdefault(r.identityKey, r)
    return prior


async def upsert_week(
    db: AsyncSession, *, tenant: str, module: str, week_of: datetime, computed_at: datetime, lives: list[LiveInsight]
) -> int:
    written = 0
    for li in lives:
        c = li.candidate
        values = {
            "tenantId": tenant,
            "module": module,
            "identityKey": c.identityKey,
            "type": c.type,
            "weekOf": week_of,
            "computedAt": computed_at,
            "score": li.score,
            "scoreComponents": c.scoreComponents,
            "lifecycleState": li.state,
            "consecutiveWeeksSurfaced": li.consecutiveWeeksSurfaced,
            "consecutiveEscalations": li.consecutiveEscalations,
            "firstSeenWeek": li.firstSeenWeek or week_of,
            "lastHeroWeek": (week_of if li.wasHero else li.lastHeroWeek),
            "payload": c.payload(),
            "recordIds": c.recordIds,
            "wasHero": li.wasHero,
            "rowPosition": li.rowPosition,
        }
        stmt = pg_insert(InsightSnapshot).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenantId", "module", "identityKey", "weekOf"],
            set_={
                "type": stmt.excluded.type,
                "computedAt": stmt.excluded.computedAt,
                "score": stmt.excluded.score,
                "scoreComponents": stmt.excluded.scoreComponents,
                "lifecycleState": stmt.excluded.lifecycleState,
                "consecutiveWeeksSurfaced": stmt.excluded.consecutiveWeeksSurfaced,
                "consecutiveEscalations": stmt.excluded.consecutiveEscalations,
                "firstSeenWeek": stmt.excluded.firstSeenWeek,
                "lastHeroWeek": stmt.excluded.lastHeroWeek,
                "payload": stmt.excluded.payload,
                "recordIds": stmt.excluded.recordIds,
                "wasHero": stmt.excluded.wasHero,
                "rowPosition": stmt.excluded.rowPosition,
                "updatedAt": computed_at,
            },
        )
        await db.execute(stmt)
        written += 1
    return written


async def load_current_view_rows(
    db: AsyncSession, *, tenant: str, module: str, week_of: datetime
) -> list[InsightSnapshot]:
    """This week's snapshots (hero + row), for the read endpoint."""
    return (
        await db.execute(
            select(InsightSnapshot)
            .where(
                InsightSnapshot.tenantId == tenant,
                InsightSnapshot.module == module,
                InsightSnapshot.weekOf == week_of,
            )
            .order_by(InsightSnapshot.score.desc())
        )
    ).scalars().all()


async def latest_week(db: AsyncSession, *, tenant: str, module: str) -> datetime | None:
    return (
        await db.execute(
            select(InsightSnapshot.weekOf)
            .where(InsightSnapshot.tenantId == tenant, InsightSnapshot.module == module)
            .order_by(InsightSnapshot.weekOf.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
