"""Weekly Insight Engine — orchestrator (spec §1, §6, §8, §9, §10).

compute_weekly() runs one weekly pass (context → generators → score+velocity →
lifecycle → upsert). get_current_week_view() serves the read endpoint from
persisted snapshots (computing on demand only if a week was never run, so a fresh
demo shows something without waiting for the scheduler).

100% deterministic — no model, no network egress.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.insights.weekly import lifecycle, repo, scoring
from app.services.insights.weekly.context import load_context
from app.services.insights.weekly.generators import run_generators
from app.services.insights.weekly.lifecycle import LiveInsight
from app.services.insights.weekly.types import SURFACING_FLOOR, ScoreConfig

DEFAULT_TENANT = "default"


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def week_of_monday(dt: datetime) -> datetime:
    d = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return d - timedelta(days=d.weekday())  # Monday=0


async def compute_weekly(
    db: AsyncSession,
    *,
    module: str = "safety_observation",
    plant: str | None = None,
    tenant: str = DEFAULT_TENANT,
    week_of: datetime | None = None,
    cfg: ScoreConfig | None = None,
) -> dict:
    cfg = cfg or ScoreConfig()
    now = _now_naive()
    week = week_of or week_of_monday(now)

    ctx = await load_context(db, module=module, plant=plant, now=now, week_of=week)
    prior_by_key = await repo.load_prior_by_key(db, tenant=tenant, module=module, week_of=week)

    candidates = run_generators(ctx, cfg)

    lives: list[LiveInsight] = []
    for c in candidates:
        prior = prior_by_key.get(c.identityKey)
        prior_mag = None
        if prior is not None and isinstance(prior.scoreComponents, dict):
            prior_mag = prior.scoreComponents.get("magnitude")
        vel = scoring.velocity(c.magnitude, prior_mag)
        c.scoreComponents = {**c.scoreComponents, "velocity": vel, "magnitude": c.magnitude}
        score = scoring.finalize(cfg, c.scoreComponents)
        lives.append(LiveInsight(candidate=c, score=score))

    lifecycle.evaluate(lives, prior_by_key, week, floor=cfg.floor)
    lives.extend(lifecycle.promote_meta(lives, week))

    hero = lifecycle.pick_hero(lives, floor=cfg.floor)
    if hero is not None:
        hero.wasHero = True
        hero.lastHeroWeek = week
    row = lifecycle.assign_row(lives, hero, week)

    current_keys = {li.key for li in lives}
    closures = lifecycle.closure_cards(prior_by_key, current_keys, week, floor=cfg.floor)
    for i, cl in enumerate(closures[:2]):  # closure cards ride the row tail
        cl.rowPosition = len(row) + i

    all_lives = lives + closures
    written = await repo.upsert_week(
        db, tenant=tenant, module=module, week_of=week, computed_at=now, lives=all_lives
    )

    return {
        "module": module,
        "weekOf": week.isoformat(),
        "evaluated": len(candidates),
        "written": written,
        "heroType": hero.candidate.type if hero else None,
        "heroScore": round(hero.score, 1) if hero else None,
    }


# ── Read path ────────────────────────────────────────────────────────────────
def _snap_view(s: Any) -> dict:
    payload = s.payload if isinstance(s.payload, dict) else {}
    return {
        "identityKey": s.identityKey,
        "type": s.type,
        "lifecycleState": s.lifecycleState,
        "score": round(float(s.score or 0.0), 1),
        "weeksRunning": int(s.consecutiveWeeksSurfaced or 1),
        "display": payload.get("display") or {},
        "rail": payload.get("rail") or {},
    }


async def get_current_week_view(
    db: AsyncSession,
    *,
    module: str = "safety_observation",
    plant: str | None = None,
    tenant: str = DEFAULT_TENANT,
) -> dict:
    week = week_of_monday(_now_naive())
    rows = await repo.load_current_view_rows(db, tenant=tenant, module=module, week_of=week)
    if not rows:
        # Never computed this week — run once so a fresh demo isn't blank.
        await compute_weekly(db, module=module, plant=plant, tenant=tenant, week_of=week)
        await db.commit()
        rows = await repo.load_current_view_rows(db, tenant=tenant, module=module, week_of=week)

    hero_row = next((r for r in rows if r.wasHero), None)
    row_cards = sorted(
        [r for r in rows if not r.wasHero and r.rowPosition is not None],
        key=lambda r: r.rowPosition,
    )
    surplus = [r for r in rows if not r.wasHero and r.rowPosition is None]

    empty = None
    if hero_row is None:
        top = max((float(r.score or 0.0) for r in rows), default=0.0)
        empty = {
            "topScore": round(top, 1),
            "floor": SURFACING_FLOOR,
            "clustersWatched": len(rows),
        }

    return {
        "module": module,
        "weekOf": week.isoformat(),
        "hero": _snap_view(hero_row) if hero_row else None,
        "row": [_snap_view(r) for r in row_cards[:3]],
        "moreCount": max(0, len(surplus) + max(0, len(row_cards) - 3)),
        "empty": empty,
    }
