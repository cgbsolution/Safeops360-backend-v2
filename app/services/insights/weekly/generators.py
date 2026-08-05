"""Weekly Insight Engine — the five generators (spec §3).

Each returns CandidateInsight[]. Left-panel fields carry the claim; the rail
(bars + 3-stat footer + closing sentence, spec §4) always shows a DIFFERENT cut
than the left states. Score components seriousness/ageing/ownershipDecay are
computed here; velocity is filled by the engine from prior-week snapshots (§5).

All deterministic — clustering, dwell math, token-Jaccard; no model, no network.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.services.insights.common import keywords
from app.services.insights.weekly import scoring
from app.services.insights.weekly.context import GeneratorContext, ObsRec
from app.services.insights.weekly.types import (
    HIGH_ENERGY_SUBCATEGORIES,
    CandidateInsight,
    LabelledBar,
    RailStat,
    ScoreConfig,
    roll_up_bars,
)

_HIGH_ENERGY = {"HOT_WORK", "CONFINED_SPACE", "ELECTRICAL", "WORK_AT_HEIGHT", "CHEMICAL_HANDLING", "PROCESS_SAFETY", "LIFTING"}


def _high_energy_qualifier(category: str, recs: list) -> str | None:
    """The 'high-energy' badge on a concentration cluster.

    Legacy categories name an energy class outright, so the category alone
    decides. STOP categories are behavioural — "Positions of People" spans
    contact-with-current and overexertion alike — so for those the exposure only
    shows at the sub-category level, and the badge is worded to match: it says
    the cluster CONTAINS high-energy exposures rather than claiming the whole
    category is one. Without this, every at-risk record filed after the taxonomy
    change would silently lose the qualifier.
    """
    if category in _HIGH_ENERGY:
        return "high-energy category"
    hits = sum(1 for r in recs if (getattr(r, "subCategoryCode", None) or "") in HIGH_ENERGY_SUBCATEGORIES)
    if hits:
        return f"{hits} high-energy exposure{'s' if hits != 1 else ''}"
    return None
_DUP_WINDOW_HOURS = 48
_DUP_JACCARD = 0.6


def _humanize(token: str) -> str:
    return token.replace("_", " ").title()


def _age_days(ctx: GeneratorContext, r: ObsRec) -> int:
    if r.date is None:
        return 0
    return max(0, int((ctx.now - r.date).total_seconds() // 86400))


def _ownership(records: list[ObsRec]) -> tuple[int, int, int]:
    """(unassigned, topOwner records, others) — a clean partition summing to len."""
    owned = [r for r in records if r.ownerId]
    by_owner: dict[str, int] = {}
    for r in owned:
        by_owner[r.ownerId] = by_owner.get(r.ownerId, 0) + 1
    top = max(by_owner.values()) if by_owner else 0
    return len(records) - len(owned), top, len(owned) - top


def _delta_vs_prior_90(ctx: GeneratorContext, scoped: list[ObsRec]) -> int:
    cut = ctx.now - timedelta(days=90)
    recent = sum(1 for r in scoped if r.date and r.date >= cut)
    prior = sum(1 for r in scoped if r.date and r.date < cut)
    return recent - prior


# ── 3.1 concentration ────────────────────────────────────────────────────────
def gen_concentration(ctx: GeneratorContext, cfg: ScoreConfig) -> list[CandidateInsight]:
    open_unsafe = [r for r in ctx.open_records if r.is_unsafe]
    by_plant_open: dict[str, int] = {}
    for r in ctx.open_records:
        by_plant_open[r.plantId] = by_plant_open.get(r.plantId, 0) + 1

    clusters: dict[tuple[str, str], list[ObsRec]] = {}
    for r in open_unsafe:
        clusters.setdefault((r.plantId, r.category), []).append(r)

    out: list[CandidateInsight] = []
    for (plant_id, category), recs in clusters.items():
        floor = max(5, round(0.08 * by_plant_open.get(plant_id, 0)))
        if len(recs) < floor:
            continue
        out.append(_concentration_candidate(ctx, cfg, plant_id, category, recs))
    return out


def _concentration_candidate(
    ctx: GeneratorContext, cfg: ScoreConfig, plant_id: str, category: str, recs: list[ObsRec]
) -> CandidateInsight:
    count = len(recs)
    # area breakdown (a DIFFERENT cut from the left's count)
    by_area: dict[str, int] = {}
    for r in recs:
        by_area[ctx.area_name(r.areaId)] = by_area.get(ctx.area_name(r.areaId), 0) + 1
    bars = roll_up_bars(
        sorted((LabelledBar(a, n) for a, n in by_area.items()), key=lambda b: b.value, reverse=True)
    )
    dominant_area = max(by_area, key=by_area.get) if by_area else None

    unassigned, top_owner, others = _ownership(recs)
    unowned_ages = [_age_days(ctx, r) for r in recs if not r.ownerId]
    avg_unowned = (sum(unowned_ages) / len(unowned_ages)) if unowned_ages else 0.0
    oldest = max((_age_days(ctx, r) for r in recs), default=0)

    scoped_all = [r for r in ctx.records if r.plantId == plant_id and r.category == category and r.is_unsafe]
    delta = _delta_vs_prior_90(ctx, scoped_all)

    comps = {
        "seriousness": scoring.seriousness(cfg, category, dominant_area),
        "ageing": scoring.ageing(oldest, ctx.category_median_closure.get(category)),
        "ownershipDecay": scoring.ownership_decay(unassigned / count if count else 0.0, avg_unowned),
    }
    return CandidateInsight(
        type="concentration",
        identityKey=f"concentration:plant={plant_id}|cat={category}",
        recordIds=[r.id for r in recs],
        magnitude=float(count),
        scoreComponents=comps,
        number=count,
        numberLabel="records",
        headline=f"Open unsafe {_humanize(category).lower()} observations concentrated at {ctx.plant_name(plant_id)}",
        delta=f"+{delta} vs prior 90d" if delta > 0 else None,
        deltaTone="up_bad" if delta > 0 else "neutral",
        qualifier=_high_energy_qualifier(category, recs),
        actionLabel="Show me these records",
        actionHref=f"?cat={category}",
        railTitle="Where inside the unit",
        bars=bars,
        stats=[
            RailStat(str(unassigned), "unassigned", "bad" if unassigned else "neutral"),
            RailStat(str(top_owner), "one owner"),
            RailStat(str(others), "others"),
        ],
        closing=f"Oldest is {oldest} days open.",
    )


# ── 3.2 bottleneck ───────────────────────────────────────────────────────────
def gen_bottleneck(ctx: GeneratorContext, cfg: ScoreConfig) -> list[CandidateInsight]:
    by_step: dict[str, list[tuple[str, int]]] = {}
    rec_by_id = {r.id: r for r in ctx.records}
    for rid, (step, days) in ctx.current_step.items():
        by_step.setdefault(step, []).append((rid, days))
    module_avg = (sum(ctx.step_norm.values()) / len(ctx.step_norm)) if ctx.step_norm else 0.0

    out: list[CandidateInsight] = []
    for step, members in by_step.items():
        if len(members) < 5:
            continue
        avg = sum(d for _, d in members) / len(members)
        norm = ctx.step_norm.get(step) or module_avg or 1.0
        if avg < 1.5 * norm:
            continue
        recs = [rec_by_id[rid] for rid, _ in members if rid in rec_by_id]
        out.append(_bottleneck_candidate(ctx, cfg, step, members, recs, avg))
    return out


def _bottleneck_candidate(
    ctx: GeneratorContext, cfg: ScoreConfig, step: str, members: list[tuple[str, int]], recs: list[ObsRec], avg: float
) -> CandidateInsight:
    count = len(members)
    # ageing bands (a DIFFERENT cut from "avg Nd, M stuck")
    bands = {"0–7d": 0, "8–14d": 0, "15–30d": 0, "30d+": 0}
    for _, d in members:
        key = "0–7d" if d <= 7 else "8–14d" if d <= 14 else "15–30d" if d <= 30 else "30d+"
        bands[key] += 1
    bars = [LabelledBar(k, v, emphasis=(k == "30d+")) for k, v in bands.items() if v]

    plants = len({r.plantId for r in recs})
    high_sev = sum(1 for r in recs if (r.severity or "").upper() in {"HIGH", "CRITICAL"})
    actors = len({r.ownerId for r in recs if r.ownerId})
    oldest = max((d for _, d in members), default=0)

    dominant_cat = max({r.category for r in recs}, key=lambda c: sum(1 for r in recs if r.category == c)) if recs else None
    comps = {
        "seriousness": scoring.seriousness(cfg, dominant_cat, None) if dominant_cat else 40.0,
        "ageing": scoring.ageing(float(oldest), ctx.category_median_closure.get(dominant_cat or "OTHER")),
        "ownershipDecay": scoring.ownership_decay(1.0 if actors == 0 else 0.2, float(oldest)),
    }
    return CandidateInsight(
        type="bottleneck",
        identityKey=f"bottleneck:step={step}",
        recordIds=[r.id for r in recs],
        magnitude=float(count),
        scoreComponents=comps,
        number=count,
        numberLabel="stuck",
        headline=f"{step} is the slowest hop — {round(avg, 1)}d on average",
        delta=None,
        deltaTone="neutral",
        qualifier="workflow queue",
        actionLabel="Show the stuck records",
        actionHref="",
        railTitle="How long they've waited",
        bars=bars,
        stats=[
            RailStat(str(plants), "plant" if plants == 1 else "plants"),
            RailStat(str(high_sev), "high severity", "bad" if high_sev else "neutral"),
            RailStat(str(actors), "owners"),
        ],
        closing=f"Clear this step first — it holds {round(avg, 1)}d against a {round(ctx.step_norm.get(step, avg), 1)}d norm.",
    )


# ── 3.3 reporting_drop ───────────────────────────────────────────────────────
def gen_reporting_drop(ctx: GeneratorContext, cfg: ScoreConfig) -> list[CandidateInsight]:
    """Submissions last 30d ≥40% below the plant's trailing 6-month mean.
    Suppressed unless ≥6 months of history exist (spec §3.3, §14)."""
    earliest = min((r.date for r in ctx.records if r.date), default=None)
    if earliest is None or (ctx.now - earliest).days < 180:
        return []  # not enough baseline — suppress cleanly, never a short baseline

    out: list[CandidateInsight] = []
    plants = {r.plantId for r in ctx.records}
    for plant_id in plants:
        precs = [r for r in ctx.records if r.plantId == plant_id and r.date]
        # 6 trailing 30-day buckets (oldest→newest) for the baseline mean
        buckets = [0] * 6
        for r in precs:
            days_ago = (ctx.now - r.date).days
            if 30 <= days_ago < 210:
                idx = min(5, (days_ago - 30) // 30)
                buckets[idx] += 1
        prior_mean = sum(buckets) / 6.0
        last30 = sum(1 for r in precs if (ctx.now - r.date).days < 30)
        if prior_mean < 3 or last30 > 0.6 * prior_mean:
            continue
        drop_pct = round((1 - (last30 / prior_mean)) * 100) if prior_mean else 0
        active_reporters = len({r.ownerId for r in precs if r.ownerId and (ctx.now - r.date).days < 30})
        recent_areas = {r.areaId for r in precs if (ctx.now - r.date).days < 30}
        prior_areas = {r.areaId for r in precs if 30 <= (ctx.now - r.date).days < 210}
        silent_areas = len(prior_areas - recent_areas)
        comps = {"seriousness": 55.0, "ageing": 20.0, "ownershipDecay": 30.0}
        out.append(
            CandidateInsight(
                type="reporting_drop",
                identityKey=f"reporting_drop:plant={plant_id}",
                recordIds=[],
                magnitude=float(drop_pct),
                scoreComponents=comps,
                number=drop_pct,
                numberLabel="% below normal",
                headline=f"Observation reporting has fallen at {ctx.plant_name(plant_id)}",
                delta=f"{last30} in 30d vs {round(prior_mean)}/mo",
                deltaTone="up_bad",
                qualifier="signal from records that don't exist",
                actionLabel="Review reporting coverage",
                actionHref="",
                railTitle="Monthly submissions",
                bars=[LabelledBar(f"m-{6 - i}", v, emphasis=(i == 5)) for i, v in enumerate(buckets)],
                stats=[
                    RailStat(str(silent_areas), "silent areas", "bad" if silent_areas else "neutral"),
                    RailStat(str(active_reporters), "reporters"),
                    RailStat(str(round(prior_mean)), "6-mo mean"),
                ],
                closing=f"{silent_areas} areas that used to report went quiet this month.",
            )
        )
    return out


# ── 3.4 duplicate_cluster ────────────────────────────────────────────────────
def gen_duplicate_cluster(ctx: GeneratorContext, cfg: ScoreConfig) -> list[CandidateInsight]:
    groups = _duplicate_groups(ctx.records)
    if not groups:
        return []
    dup_ids = [rid for g in groups for rid in g]
    open_count = len(ctx.open_records) or 1
    pct = round(len(dup_ids) / open_count * 100)
    comps = {"seriousness": 28.0, "ageing": 0.0, "ownershipDecay": 0.0}  # data quality, low hazard
    return [
        CandidateInsight(
            type="duplicate_cluster",
            identityKey="duplicate_cluster:module=safety_observation",
            recordIds=dup_ids,
            magnitude=float(len(dup_ids)),
            scoreComponents=comps,
            number=len(groups),
            numberLabel="sets",
            headline=f"{len(groups)} sets of near-identical observations logged",
            delta=None,
            deltaTone="neutral",
            qualifier="data quality",
            actionLabel=f"Review {len(dup_ids)} records",
            actionHref="?insight=observation:duplicate:near-identical",
            railTitle="",
            bars=[],
            stats=[
                RailStat(str(len(groups)), "sets"),
                RailStat(str(len(dup_ids)), "records"),
                RailStat(f"{pct}%", "of open list"),
            ],
            closing=f"Same area, logged under 48h apart, near-identical text — merging clears roughly {pct}% of the open list.",
        )
    ]


def _duplicate_groups(records: list[ObsRec]) -> list[list[str]]:
    buckets: dict[tuple[str, str], list[ObsRec]] = {}
    for r in records:
        buckets.setdefault((r.plantId, r.areaId or ""), []).append(r)
    groups: list[list[str]] = []
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        toks = {r.id: set(keywords(r.number)) for r in bucket}  # description not in ctx; number/token proxy
        used: set[str] = set()
        for i, a in enumerate(bucket):
            if a.id in used:
                continue
            group = [a.id]
            for b in bucket[i + 1:]:
                if b.id in used or a.date is None or b.date is None:
                    continue
                if abs((a.date - b.date).total_seconds()) > _DUP_WINDOW_HOURS * 3600:
                    continue
                ta, tb = toks[a.id], toks[b.id]
                if a.category == b.category and a.areaId == b.areaId:
                    inter, union = len(ta & tb), len(ta | tb) or 1
                    if inter / union >= _DUP_JACCARD or (not ta and not tb):
                        group.append(b.id)
                        used.add(b.id)
            if len(group) > 1:
                used.update(group)
                groups.append(group)
    return groups


# ── 3.5 recurrence ───────────────────────────────────────────────────────────
def gen_recurrence(ctx: GeneratorContext, cfg: ScoreConfig) -> list[CandidateInsight]:
    """Same area+category produced a new record within 90d of a prior one in that
    pair being CLOSED — closure isn't fixing the condition (spec §3.5)."""
    pairs: dict[tuple[str, str], list[ObsRec]] = {}
    for r in ctx.records:
        if r.areaId:
            pairs.setdefault((r.areaId, r.category), []).append(r)

    out: list[CandidateInsight] = []
    for (area_id, category), recs in pairs.items():
        closed = sorted([r for r in recs if not r.is_open and r.closedAt], key=lambda r: r.closedAt)  # type: ignore[arg-type]
        if not closed:
            continue
        recurred: list[tuple[ObsRec, int]] = []
        for nr in recs:
            if nr.date is None:
                continue
            for cr in closed:
                gap = (nr.date - cr.closedAt).days  # type: ignore[operator]
                if 0 < gap <= 90:
                    recurred.append((nr, gap))
                    break
        if len(recurred) < 2:
            continue
        gaps = sorted(g for _, g in recurred)
        median_gap = gaps[len(gaps) // 2]
        capas_raised = sum(1 for r, _ in recurred if r.capaId)
        capas_verified = sum(1 for r, _ in recurred if r.capaId and not r.is_open)
        oldest = max((_age_days(ctx, r) for r, _ in recurred), default=0)
        comps = {
            "seriousness": scoring.seriousness(cfg, category, ctx.area_name(area_id)),
            "ageing": scoring.ageing(float(oldest), ctx.category_median_closure.get(category)),
            "ownershipDecay": 30.0,
        }
        out.append(
            CandidateInsight(
                type="recurrence",
                identityKey=f"recurrence:area={area_id}|cat={category}",
                recordIds=[r.id for r, _ in recurred],
                magnitude=float(len(recurred)),
                scoreComponents=comps,
                number=len(recurred),
                numberLabel="recurred",
                headline=f"{_humanize(category)} keeps coming back at {ctx.area_name(area_id)}",
                delta=None,
                deltaTone="up_bad",
                qualifier="closure isn't holding",
                actionLabel="Show me these records",
                actionHref=f"?cat={category}&area={area_id}",
                railTitle="Recurrence vs CAPA response",
                bars=[
                    LabelledBar("recurred", len(recurred), emphasis=True),
                    LabelledBar("CAPAs raised", capas_raised),
                    LabelledBar("CAPAs verified", capas_verified),
                ],
                stats=[
                    RailStat(str(capas_raised), "CAPAs raised"),
                    RailStat(str(capas_verified), "verified", "bad" if capas_verified == 0 else "neutral"),
                    RailStat(f"{median_gap}d", "median to recur"),
                ],
                closing=f"Median {median_gap} days from close to the next occurrence — the fix isn't holding.",
            )
        )
    return out


ALL_GENERATORS = [
    gen_concentration,
    gen_bottleneck,
    gen_reporting_drop,
    gen_duplicate_cluster,
    gen_recurrence,
]


def run_generators(ctx: GeneratorContext, cfg: ScoreConfig) -> list[CandidateInsight]:
    out: list[CandidateInsight] = []
    for gen in ALL_GENERATORS:
        out.extend(gen(ctx, cfg))
    return out
