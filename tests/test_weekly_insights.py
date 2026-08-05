"""Weekly Insight Engine — pure lifecycle/scoring unit tests (no DB).

Covers the acceptance items that don't need the 362-record dataset: state
transitions, hero pick + tie-break, meta promotion, the scoring weights, and the
"rail never restates the left" contract shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.services.insights.weekly import lifecycle, scoring
from app.services.insights.weekly.lifecycle import LiveInsight
from app.services.insights.weekly.types import CandidateInsight, ScoreConfig

WEEK = datetime(2026, 7, 20)


def _cand(key: str, type_: str = "concentration") -> CandidateInsight:
    return CandidateInsight(
        type=type_, identityKey=key, recordIds=["a", "b"], magnitude=10.0,
        scoreComponents={}, number=10, numberLabel="records", headline=f"H {key}",
    )


@dataclass
class _Prior:
    score: float
    lifecycleState: str = "new"
    consecutiveEscalations: int = 0
    consecutiveWeeksSurfaced: int = 1
    firstSeenWeek: Any = WEEK - timedelta(weeks=2)
    lastHeroWeek: Any = None
    wasHero: bool = False
    payload: Any = None
    type: str = "concentration"


def _live(key: str, score: float, type_: str = "concentration") -> LiveInsight:
    return LiveInsight(candidate=_cand(key, type_), score=score)


# ── scoring ─────────────────────────────────────────────────────────────────
def test_finalize_weighted_sum():
    s = scoring.finalize(ScoreConfig(), {"seriousness": 80, "velocity": 40, "ageing": 30, "ownershipDecay": 40})
    assert s == 52.0  # .35*80 + .25*40 + .20*30 + .20*40


def test_velocity_growth_and_new_baseline():
    assert scoring.velocity(23, 19) == 40.0   # +4 → 40
    assert scoring.velocity(30, 19) == 100.0  # +11 clamps to 100
    assert scoring.velocity(10, None) == 40.0  # no history → neutral baseline


# ── lifecycle state machine (§6) ────────────────────────────────────────────
def test_new_when_no_prior():
    lives = [_live("k", 70)]
    lifecycle.evaluate(lives, {}, WEEK)
    assert lives[0].state == "new"


def test_escalating_persistent_resolving_bands():
    prior = {"k": _Prior(score=70)}  # band = max(10.5, 5) = 10.5
    esc = _live("k", 82); lifecycle.evaluate([esc], prior, WEEK); assert esc.state == "escalating"
    flat = _live("k", 74); lifecycle.evaluate([flat], prior, WEEK); assert flat.state == "persistent"
    res = _live("k", 55); lifecycle.evaluate([res], prior, WEEK); assert res.state == "resolving"


# ── hero pick + tie-break (§6) ──────────────────────────────────────────────
def test_hero_is_highest_new_or_escalating_over_floor():
    a = _live("a", 65); a.state = "new"
    b = _live("b", 82); b.state = "escalating"
    c = _live("c", 90); c.state = "persistent"  # not eligible (persistent)
    hero = lifecycle.pick_hero([a, b, c])
    assert hero is b


def test_tie_break_escalating_over_new():
    a = _live("a", 80); a.state = "new"
    b = _live("b", 80); b.state = "escalating"
    hero = lifecycle.pick_hero([a, b])
    assert hero is b


def test_below_floor_is_never_hero():
    a = _live("a", 55); a.state = "new"
    assert lifecycle.pick_hero([a]) is None


# ── meta promotion (§7) ─────────────────────────────────────────────────────
def test_three_escalations_promotes_meta_and_demotes_original():
    li = _live("concentration:plant=P|cat=HOT_WORK", 85)
    li.state = "escalating"
    li.consecutiveEscalations = 3
    extra = lifecycle.promote_meta([li], WEEK)
    assert len(extra) == 1
    meta = extra[0]
    assert meta.candidate.type == "meta_response_failure"
    assert meta.score > li.score            # inherits + premium → wins the slot
    assert li.state == "persistent"          # original forced down
    assert meta.candidate.actionHref == "/capa"  # routes to CAPA review (§7)


# ── secondary row slot order by TYPE, not rank (§9) ─────────────────────────
def test_row_slot_order_by_type():
    hero = _live("hero", 90, "meta_response_failure"); hero.state = "escalating"; hero.wasHero = True
    conc = _live("c", 80, "concentration")
    bott = _live("b", 70, "bottleneck")
    dup = _live("d", 40, "duplicate_cluster")
    row = lifecycle.assign_row([hero, conc, bott, dup], hero, WEEK)
    by_slot = {li.rowPosition: li.candidate.type for li in row}
    assert by_slot[0] == "bottleneck"        # slot 0 operational
    assert by_slot[1] == "concentration"     # slot 1 concentration
    assert by_slot[2] == "duplicate_cluster" # slot 2 data quality
    assert hero.key not in {li.key for li in row}  # hero never in the row
