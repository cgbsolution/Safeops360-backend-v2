"""Seed the Weekly Insight Engine demo arc (spec §11).

Backdates weekly InsightSnapshot rows for the Safety Observations pilot so a
prospect actually SEES escalating / persistent / meta — states a fresh tenant
can't reach for weeks. Deliberate narrative on the hot-work-at-IMU identity:

    new (11) → persistent (11) → escalating (19) → escalating (23)
             → meta (25, 0 closures, 2 CAPAs raised, 0 verified)

Uses the REAL top concentration cluster (real plant / category / rail payload) so
the numbers look authentic, then fabricates the week-over-week arc + the meta.
Idempotent: clears this tenant/module's snapshots for the seeded identities first.

Run from safeops_360_bakend (requires the table — run create_insight_snapshot_table first):

    python -m scripts.seed_weekly_insights
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from app.core.db import AsyncSessionLocal
from app.models.insight_snapshot import InsightSnapshot
from app.services.insights.weekly.context import load_context
from app.services.insights.weekly.engine import week_of_monday
from app.services.insights.weekly.generators import gen_bottleneck, gen_concentration, gen_duplicate_cluster
from app.services.insights.weekly.types import ScoreConfig

TENANT = "default"
MODULE = "safety_observation"

# (weeks_ago, state, number, score, consecEsc, wasHero, weeksRunning)
ARC = [
    (4, "new", 11, 62.0, 0, True, 1),
    (3, "persistent", 11, 63.0, 0, False, 2),
    (2, "escalating", 19, 73.0, 1, True, 3),
    (1, "escalating", 23, 81.0, 2, True, 4),
]


def _naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _scale_display(payload: dict, number: int) -> dict:
    d = dict(payload.get("display") or {})
    d["number"] = number
    return {"display": d, "rail": payload.get("rail") or {}}


async def _run() -> int:
    now = _naive_now()
    week0 = week_of_monday(now)
    cfg = ScoreConfig()

    async with AsyncSessionLocal() as db:
        ctx = await load_context(db, module=MODULE, plant=None, now=now, week_of=week0)
        conc = gen_concentration(ctx, cfg)
        bott = gen_bottleneck(ctx, cfg)
        dup = gen_duplicate_cluster(ctx, cfg)
        if not conc:
            print("No concentration cluster in live data — cannot seed the arc. Add unsafe hot-work observations first.")
            return 1
        top = max(conc, key=lambda c: c.magnitude)
        conc_key = top.identityKey
        conc_payload = top.payload()
        meta_key = f"meta:{conc_key}"

        keys = [conc_key, meta_key]
        if bott:
            keys.append(bott[0].identityKey)
        if dup:
            keys.append(dup[0].identityKey)

        await db.execute(
            delete(InsightSnapshot).where(
                InsightSnapshot.tenantId == TENANT,
                InsightSnapshot.module == MODULE,
                InsightSnapshot.identityKey.in_(keys),
            )
        )

        first_seen = week0 - timedelta(weeks=4)
        rows: list[InsightSnapshot] = []

        # ── backdated arc (weeks -4 … -1) on the concentration identity ──
        for weeks_ago, state, number, score, esc, was_hero, running in ARC:
            wk = week0 - timedelta(weeks=weeks_ago)
            rows.append(
                InsightSnapshot(
                    tenantId=TENANT, module=MODULE, identityKey=conc_key, type="concentration",
                    weekOf=wk, computedAt=wk, score=score,
                    scoreComponents={"magnitude": number, "seriousness": 80, "velocity": 40, "ageing": 30, "ownershipDecay": 40},
                    lifecycleState=state, consecutiveWeeksSurfaced=running, consecutiveEscalations=esc,
                    firstSeenWeek=first_seen, lastHeroWeek=(wk if was_hero else None),
                    payload=_scale_display(conc_payload, number), recordIds=top.recordIds,
                    wasHero=was_hero, rowPosition=None,
                )
            )

        # ── week 0: meta wins the hero; concentration forced to persistent row ──
        rows.append(
            InsightSnapshot(
                tenantId=TENANT, module=MODULE, identityKey=meta_key, type="meta_response_failure",
                weekOf=week0, computedAt=now, score=95.0,
                scoreComponents={"inheritedScore": 87, "metaPremium": 8},
                lifecycleState="escalating", consecutiveWeeksSurfaced=5, consecutiveEscalations=3,
                firstSeenWeek=first_seen, lastHeroWeek=week0,
                payload={
                    "display": {
                        "number": 3, "numberLabel": "weeks escalating",
                        "headline": "The response to hot work at IMU isn't working",
                        "delta": "3 weeks escalating, no closures", "deltaTone": "up_bad",
                        "qualifier": "response gap",
                        "actionLabel": "Open CAPA review", "actionHref": "/capa",
                    },
                    "rail": {
                        "kind": "meta_response_failure", "railTitle": "Escalation with no resolution",
                        "bars": [
                            {"label": "wk 1", "value": 11, "emphasis": False},
                            {"label": "wk 2", "value": 19, "emphasis": False},
                            {"label": "wk 3", "value": 25, "emphasis": True},
                        ],
                        "stats": [
                            {"value": "0", "label": "closed in window", "tone": "bad"},
                            {"value": "2", "label": "CAPAs raised", "tone": "neutral"},
                            {"value": "0", "label": "verified", "tone": "bad"},
                        ],
                        "closing": "This has worsened for weeks without a single verified closure — the process, not the hazard, is the finding now.",
                    },
                },
                recordIds=top.recordIds, wasHero=True, rowPosition=None,
            )
        )
        rows.append(
            InsightSnapshot(
                tenantId=TENANT, module=MODULE, identityKey=conc_key, type="concentration",
                weekOf=week0, computedAt=now, score=87.0,
                scoreComponents={"magnitude": 25, "seriousness": 82, "velocity": 60, "ageing": 35, "ownershipDecay": 42},
                lifecycleState="persistent", consecutiveWeeksSurfaced=5, consecutiveEscalations=0,
                firstSeenWeek=first_seen, lastHeroWeek=week0 - timedelta(weeks=1),
                payload=_scale_display(conc_payload, 25), recordIds=top.recordIds,
                wasHero=False, rowPosition=1,
            )
        )
        if bott:
            b = bott[0]
            rows.append(
                InsightSnapshot(
                    tenantId=TENANT, module=MODULE, identityKey=b.identityKey, type="bottleneck",
                    weekOf=week0, computedAt=now, score=70.0,
                    scoreComponents={**b.scoreComponents, "magnitude": b.magnitude, "velocity": 40},
                    lifecycleState="new", consecutiveWeeksSurfaced=1, consecutiveEscalations=0,
                    firstSeenWeek=week0, lastHeroWeek=None,
                    payload=b.payload(), recordIds=b.recordIds, wasHero=False, rowPosition=0,
                )
            )
        if dup:
            d = dup[0]
            rows.append(
                InsightSnapshot(
                    tenantId=TENANT, module=MODULE, identityKey=d.identityKey, type="duplicate_cluster",
                    weekOf=week0, computedAt=now, score=40.0,
                    scoreComponents={**d.scoreComponents, "magnitude": d.magnitude, "velocity": 40},
                    lifecycleState="new", consecutiveWeeksSurfaced=1, consecutiveEscalations=0,
                    firstSeenWeek=week0, lastHeroWeek=None,
                    payload=d.payload(), recordIds=d.recordIds, wasHero=False, rowPosition=2,
                )
            )

        db.add_all(rows)
        await db.commit()
        print(f"Seeded {len(rows)} snapshots · hero=meta_response_failure · concentration identity={conc_key}")
        return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
