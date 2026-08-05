"""Weekly Insight Engine — shared types + scoring config (spec §2, §4, §5).

Everything here is data/config only. Weight tables are the seeded per-vertical
defaults the spec asks for (§5 "tenant config, not code constants … seed sensible
defaults per industry vertical"); a per-tenant override table is the documented
extension point — the scorer already reads a `ScoreConfig`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

INSIGHT_TYPES = (
    "concentration",
    "bottleneck",
    "reporting_drop",
    "duplicate_cluster",
    "recurrence",
    "meta_response_failure",
)

SURFACING_FLOOR = 60.0          # nothing below is hero-eligible, ever (§5)
META_SCORE_PREMIUM = 8.0        # meta inherits underlying score + this, to win (§7)
META_ESCALATION_STREAK = 3      # consecutive escalating weeks → meta (§7)


@dataclass
class ScoreWeights:
    seriousness: float = 0.35
    velocity: float = 0.25
    ageing: float = 0.20
    ownershipDecay: float = 0.20


DEFAULT_CATEGORY_RISK: dict[str, float] = {
    "CONFINED_SPACE": 0.95,
    "PROCESS_SAFETY": 0.92,
    "HOT_WORK": 0.90,
    "ELECTRICAL": 0.85,
    "WORK_AT_HEIGHT": 0.85,
    "CHEMICAL_HANDLING": 0.80,
    "LIFTING": 0.70,
    "MOBILE_EQUIPMENT": 0.70,
    "MATERIAL_HANDLING": 0.60,
    "EMERGENCY": 0.65,
    "EMERGENCY_PREP": 0.65,
    "ENVIRONMENT": 0.55,
    "PPE": 0.50,
    "ERGONOMICS": 0.45,
    "BEHAVIOUR": 0.45,
    "PROCESS": 0.60,
    "HOUSEKEEPING": 0.30,
    "OTHER": 0.40,
    "OTHERS": 0.40,
    # ─── DuPont STOP categories ───
    # At-risk observations write their STOP categoryCode into `category`, so
    # without these four the scorer would fall through to its default weight and
    # under-rank every new at-risk record. Graded by the exposure each category
    # actually describes, NOT by energy class — STOP categories are behavioural.
    # Positions of People is the line-of-fire category (struck by, caught
    # in/between, falls, contact with current) and ranks accordingly; Reactions
    # of People is a leading indicator, not an exposure, so it ranks low.
    "POSITIONS_OF_PEOPLE": 0.75,
    "PROCEDURES": 0.65,
    "TOOLS_EQUIPMENT": 0.60,
    "REACTIONS_OF_PEOPLE": 0.45,
}

# High-energy exposure under the STOP taxonomy lives at the SUB-category level,
# not the category level — "Positions of People" spans contact-with-current and
# overexertion alike. Consumers that want the high-energy qualifier on an
# at-risk record must test subCategoryCode against this set.
HIGH_ENERGY_SUBCATEGORIES: frozenset[str] = frozenset({
    "PP_CONTACT_ELECTRICAL",
    "PP_CONTACT_TEMPERATURE",
    "PP_FALLING_DIFFERENT_LEVEL",
    "PP_CAUGHT_IN_ON_BETWEEN",
    "PP_STRUCK_BY",
    "PR_PERMIT_LOTO_BYPASSED",
    "TE_GUARD_MISSING",
})

AREA_RISK_KEYWORDS: list[tuple[tuple[str, ...], float]] = [
    (("boiler", "furnace", "reactor", "vessel", "kiln", "confined"), 0.92),
    (("tank", "pit", "silo", "storage"), 0.82),
    (("electrical", "power", "substation", "switchgear", "hv", "transformer"), 0.85),
    (("height", "roof", "scaffold", "tower"), 0.80),
    (("process", "plant", "utilities", "chemical"), 0.70),
    (("finishing", "packing", "warehouse", "dispatch", "yard"), 0.55),
    (("office", "admin", "canteen", "lab", "laboratory"), 0.40),
]
_DEFAULT_AREA_RISK = 0.55


@dataclass
class ScoreConfig:
    weights: ScoreWeights = field(default_factory=ScoreWeights)
    categoryRisk: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_CATEGORY_RISK))
    floor: float = SURFACING_FLOOR


def area_risk(area_name: str | None) -> float:
    if not area_name:
        return _DEFAULT_AREA_RISK
    lname = area_name.lower()
    for keys, w in AREA_RISK_KEYWORDS:
        if any(k in lname for k in keys):
            return w
    return _DEFAULT_AREA_RISK


def category_risk(cfg: ScoreConfig, category: str | None) -> float:
    return cfg.categoryRisk.get((category or "OTHER").upper(), 0.40)


@dataclass
class LabelledBar:
    label: str
    value: float
    emphasis: bool = False

    def as_dict(self) -> dict:
        return {"label": self.label, "value": self.value, "emphasis": self.emphasis}


@dataclass
class RailStat:
    value: str
    label: str
    tone: str = "neutral"  # neutral | bad | up_bad | down_good | caution

    def as_dict(self) -> dict:
        return {"value": self.value, "label": self.label, "tone": self.tone}


@dataclass
class CandidateInsight:
    """One insight a generator emits for the current week, pre-lifecycle."""

    type: str
    identityKey: str
    recordIds: list[str]
    magnitude: float
    scoreComponents: dict[str, float]  # seriousness, ageing, ownershipDecay (0-100)

    number: float
    numberLabel: str
    headline: str
    delta: str | None = None
    deltaTone: str = "neutral"   # up_bad | up_good | down_good | neutral
    qualifier: str | None = None
    actionLabel: str = "Show me these records"
    actionHref: str = ""

    railTitle: str = ""
    bars: list[LabelledBar] = field(default_factory=list)
    stats: list[RailStat] = field(default_factory=list)
    closing: str = ""

    def payload(self) -> dict:
        return {
            "display": {
                "number": self.number,
                "numberLabel": self.numberLabel,
                "headline": self.headline,
                "delta": self.delta,
                "deltaTone": self.deltaTone,
                "qualifier": self.qualifier,
                "actionLabel": self.actionLabel,
                "actionHref": self.actionHref,
            },
            "rail": {
                "kind": self.type,
                "railTitle": self.railTitle,
                "bars": [b.as_dict() for b in self.bars],
                "stats": [s.as_dict() for s in self.stats],
                "closing": self.closing,
            },
        }


def roll_up_bars(bars: list[LabelledBar], cap: int = 4) -> list[LabelledBar]:
    """Cap at `cap` rows; roll the remainder into one 'N other areas' bar
    (spec §4). Assumes `bars` is already sorted by value desc."""
    if len(bars) <= cap:
        return bars
    kept = bars[: cap - 1]
    rest = bars[cap - 1:]
    kept.append(LabelledBar(label=f"{len(rest)} other areas", value=sum(b.value for b in rest)))
    return kept
