"""Weekly Insight Engine — lifecycle state machine (spec §6, §7, §9).

Compares this week's finalised scores against the most recent prior snapshot per
identityKey and assigns new/escalating/persistent/resolving. Hero = highest score
in new|escalating (tie → escalating). A 3-week escalating streak promotes a
`meta_response_failure` (§7). Closure emits one final `resolving` row card (§6).

Deliberately NOT a decay/cooldown model (§6): `escalating` is a state, so a
worsening signal structurally cannot be suppressed to keep the UI fresh.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.services.insights.weekly.types import (
    META_ESCALATION_STREAK,
    META_SCORE_PREMIUM,
    SURFACING_FLOOR,
    CandidateInsight,
    LabelledBar,
    RailStat,
)


@dataclass
class LiveInsight:
    candidate: CandidateInsight
    score: float
    state: str = "new"
    consecutiveWeeksSurfaced: int = 1
    consecutiveEscalations: int = 0
    firstSeenWeek: datetime | None = None
    lastHeroWeek: datetime | None = None
    wasHero: bool = False
    rowPosition: int | None = None

    @property
    def key(self) -> str:
        return self.candidate.identityKey


def _band(prior_score: float) -> float:
    """Material-change band = max(15% of prior, 5 absolute) (spec §6)."""
    return max(0.15 * prior_score, 5.0)


def evaluate(lives: list[LiveInsight], prior_by_key: dict[str, Any], week_of: datetime, floor: float = SURFACING_FLOOR) -> None:
    """Assign lifecycle state + streaks in place, comparing to prior snapshots."""
    for li in lives:
        prior = prior_by_key.get(li.key)
        if prior is None:
            li.state = "new"
            li.consecutiveWeeksSurfaced = 1
            li.consecutiveEscalations = 0
            li.firstSeenWeek = week_of
            li.lastHeroWeek = None
            continue

        li.firstSeenWeek = _as_dt(prior.firstSeenWeek) or week_of
        li.lastHeroWeek = _as_dt(prior.lastHeroWeek)
        li.consecutiveWeeksSurfaced = int(getattr(prior, "consecutiveWeeksSurfaced", 0) or 0) + 1
        prior_score = float(getattr(prior, "score", 0.0) or 0.0)
        band = _band(prior_score)

        if li.score < floor:
            li.state = "resolving"  # dropping out of significance
        elif li.score >= prior_score + band:
            li.state = "escalating"
        elif li.score <= prior_score - band:
            li.state = "resolving"
        else:
            li.state = "persistent"

        li.consecutiveEscalations = (
            int(getattr(prior, "consecutiveEscalations", 0) or 0) + 1 if li.state == "escalating" else 0
        )


def promote_meta(lives: list[LiveInsight], week_of: datetime) -> list[LiveInsight]:
    """3 consecutive escalating weeks → meta_response_failure; the underlying
    insight is forced to `persistent` and drops to the row (spec §7)."""
    extra: list[LiveInsight] = []
    for li in lives:
        if li.state == "escalating" and li.consecutiveEscalations >= META_ESCALATION_STREAK:
            meta = _build_meta(li, week_of)
            extra.append(meta)
            li.state = "persistent"  # forced down; the meta now carries the slot
    return extra


def _build_meta(underlying: LiveInsight, week_of: datetime) -> LiveInsight:
    u = underlying.candidate
    weeks = underlying.consecutiveEscalations
    meta_candidate = CandidateInsight(
        type="meta_response_failure",
        identityKey=f"meta:{u.identityKey}",
        recordIds=u.recordIds,
        magnitude=underlying.score,
        scoreComponents={"inheritedScore": underlying.score, "metaPremium": META_SCORE_PREMIUM},
        number=weeks,
        numberLabel="weeks escalating",
        headline=f"The response to {_short(u.headline)} isn't working",
        delta=f"{weeks} weeks escalating, no closures",
        deltaTone="up_bad",
        qualifier="response gap",
        actionLabel="Open CAPA review",
        actionHref="/capa",  # the finding has changed module (spec §7)
        railTitle="Escalation with no resolution",
        bars=[LabelledBar(f"wk {i + 1}", 1, emphasis=(i == weeks - 1)) for i in range(max(weeks, 1))],
        stats=[
            RailStat("0", "closed in window", "bad"),
            RailStat("—", "CAPAs raised"),
            RailStat("0", "verified", "bad"),
        ],
        closing="This has worsened for weeks without a closure — the process, not the hazard, is the finding now.",
    )
    return LiveInsight(
        candidate=meta_candidate,
        score=underlying.score + META_SCORE_PREMIUM,
        state="escalating",
        consecutiveEscalations=weeks,
        consecutiveWeeksSurfaced=underlying.consecutiveWeeksSurfaced,
        firstSeenWeek=underlying.firstSeenWeek,
    )


def pick_hero(lives: list[LiveInsight], floor: float = SURFACING_FLOOR) -> LiveInsight | None:
    """Highest score in new|escalating; tie → escalating outranks new (spec §6)."""
    eligible = [li for li in lives if li.state in ("new", "escalating") and li.score >= floor]
    if not eligible:
        return None
    eligible.sort(key=lambda li: (li.score, 1 if li.state == "escalating" else 0), reverse=True)
    hero = eligible[0]
    hero.wasHero = True  # lastHeroWeek is stamped to this week in the engine
    return hero


# Slot order is by TYPE, not rank (spec §9): 0=operational, 1=concentration,
# 2=data-quality. Falls through to the next-ranked candidate if a slot's natural
# occupant is the hero.
_SLOT_TYPES = [
    ("bottleneck", "recurrence", "reporting_drop", "meta_response_failure"),  # slot 0 operational
    ("concentration",),                                                        # slot 1
    ("duplicate_cluster",),                                                    # slot 2 data quality
]


def assign_row(lives: list[LiveInsight], hero: LiveInsight | None, week_of: datetime) -> list[LiveInsight]:
    used = {hero.key} if hero else set()
    row: list[LiveInsight] = []
    ranked = sorted(lives, key=lambda li: li.score, reverse=True)
    for slot, types in enumerate(_SLOT_TYPES):
        pick = next((li for li in ranked if li.key not in used and li.candidate.type in types), None)
        if pick is None:
            pick = next((li for li in ranked if li.key not in used), None)
        if pick is not None:
            pick.rowPosition = slot
            used.add(pick.key)
            row.append(pick)
    return row


def closure_cards(
    prior_by_key: dict[str, Any], current_keys: set[str], week_of: datetime, floor: float = SURFACING_FLOOR
) -> list[LiveInsight]:
    """When a prior HERO insight no longer appears (or dropped below floor), emit
    one final `resolving` row card — the only positive feedback the system gives
    (spec §6)."""
    out: list[LiveInsight] = []
    for key, prior in prior_by_key.items():
        if key in current_keys:
            continue
        if not bool(getattr(prior, "wasHero", False)):
            continue
        headline = _prior_headline(prior)
        cand = CandidateInsight(
            type=str(getattr(prior, "type", "concentration")),
            identityKey=key,
            recordIds=[],
            magnitude=0.0,
            scoreComponents={},
            number=0,
            numberLabel="cleared",
            headline=f"{_short(headline)} cleared",
            delta="resolved",
            deltaTone="down_good",
            qualifier="closed out",
            actionLabel="",
            actionHref="",
            railTitle="",
            bars=[],
            stats=[],
            closing="This signal fell below the surfacing floor after weeks on the board — resolved.",
        )
        out.append(
            LiveInsight(candidate=cand, score=0.0, state="resolving", firstSeenWeek=_as_dt(getattr(prior, "firstSeenWeek", None)) or week_of)
        )
    return out


def _short(text: str, n: int = 54) -> str:
    t = (text or "").strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _prior_headline(prior: Any) -> str:
    payload = getattr(prior, "payload", None) or {}
    if isinstance(payload, dict):
        return ((payload.get("display") or {}).get("headline")) or "This signal"
    return "This signal"


def _as_dt(v: Any) -> datetime | None:
    return v if isinstance(v, datetime) else None
