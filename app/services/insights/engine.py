"""Insight engine orchestration — dispatch, confidence/severity gating, caching.

`compute()` is the single entry point the router calls. It routes to the
module's rule function, applies the shared gates (bar suppression on thin data,
ranking by confidence×severity), and serves from the 15-minute cache. No rule
does I/O beyond the DB; nothing here reaches the network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.insights import Insight, InsightResponse, Signal
from app.services.insights import cache
from app.services.insights.common import MIN_RECORDS
from app.services.insights.rules_capa import compute_capa
from app.services.insights.rules_combined_risk import compute_combined_risk
from app.services.insights.rules_eai import compute_eai
from app.services.insights.rules_hira import compute_hira
from app.services.insights.rules_incident import compute_incident
from app.services.insights.rules_moc import compute_moc
from app.services.insights.rules_nearmiss import compute_nearmiss
from app.services.insights.rules_observation import compute_observation

# module key → rule fn. One entry per wired list screen (spec §2). The module
# keys mirror the frontend `fetchInsights(<key>)` calls exactly.
_RuleFn = Callable[..., Awaitable[tuple[list[Insight], list[Signal], int]]]
_RULES: dict[str, _RuleFn] = {
    "incident": compute_incident,
    "nearmiss": compute_nearmiss,
    "observation": compute_observation,
    "hira": compute_hira,
    "eai": compute_eai,
    "combined-risk": compute_combined_risk,
    "capa": compute_capa,
    "moc": compute_moc,
}
SUPPORTED_MODULES = tuple(_RULES.keys())

# Rank order for trimming to the top 3 bar cards: severity first, then
# confidence. Low-confidence cards are NOT hidden — they rank last and the UI
# de-emphasises them (spec §1.2).
_SEV_RANK = {"critical": 3, "high": 2, "watch": 1, "info": 0}
_CONF_RANK = {"high": 2, "medium": 1, "low": 0}


def _rank(i: Insight) -> tuple[int, int]:
    return (_SEV_RANK.get(i.severity, 0), _CONF_RANK.get(i.confidence, 0))


async def compute(
    db: AsyncSession,
    module: str,
    *,
    plant: str | None = None,
    date_from: Any = None,
    date_to: Any = None,
) -> InsightResponse:
    if module not in _RULES:
        raise KeyError(module)

    filters = {"from": date_from, "to": date_to}
    key = cache.make_key(module, plant, filters)
    hit = cache.get(key)
    if hit is not None:
        return hit.model_copy(update={"cached": True})

    bar, signals, record_count = await _RULES[module](
        db, plant=plant, date_from=date_from, date_to=date_to
    )

    # Training & Competency Engine cross-link — surface training auto-assigned
    # FROM this module's records on the Incident / Near Miss / Observation bars
    # (spec: training-driven signals appear on those screens). Additive + guarded:
    # a failure here can never break the host screen's own insights.
    if module in ("incident", "nearmiss", "observation"):
        try:
            from app.services.insights.rules_training_cross import training_cross_bar

            bar = list(bar) + await training_cross_bar(db, module_key=module, plant=plant)
        except Exception:  # noqa: BLE001
            pass

    # Thin-data suppression: below the floor, show nothing rather than a
    # low-confidence card on too few records (spec §1.4). Signals are dropped
    # too — there is no meaningful intelligence under the floor.
    suppressed = record_count < MIN_RECORDS
    if suppressed:
        bar, signals = [], []

    # Rank by severity×confidence, keep the top 3 (spec §1.2).
    bar = sorted(bar, key=_rank, reverse=True)[:3]

    resp = InsightResponse(
        module=module,
        plant=plant,
        generatedAt=datetime.now(timezone.utc),
        bar=bar,
        signals=signals,
        recordCount=record_count,
        suppressed=suppressed,
        reason="insufficient_records" if suppressed else None,
        cached=False,
    )
    cache.put(key, resp)
    return resp
