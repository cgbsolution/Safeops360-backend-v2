"""Safety Observation insight rules (spec §2.2) — deterministic.

Bar:
  * cluster — open unsafe acts/conditions concentrating in one category at a
    plant (≥3 sharing category), e.g. electrical unsafe acts up at a plant.
  * duplicate — near-identical descriptions in the same plant/area logged within
    48h (fuzzy token overlap). Catches copy-paste / test-data rows for cleanup.
  * bottleneck (Row-Level Insight Layer Part 6) — the workflow step holding open
    observations the longest on average, with the count currently stuck there.
Row signals (0-N per record now — the Row-Level Insight Layer pushes intelligence
to row level, so a row can carry several chips; the frontend SignalChipGroup
collapses the overflow into "+N"):
  * repeat_location — this (plant · area · category) has ≥3 occurrences in the
    trailing 90 days (recurring hazard at one spot). {type:'repeat_location'}.
  * stale_step — the record's current workflow step has run > 1.5× the average
    dwell for its (category · step) across the dataset. {type:'stale_step'}.
  * duplicate "Likely duplicate" — the later member of a duplicate pair.
  * anomaly "Check severity" — HIGH/CRITICAL severity on a one-line, vague
    description (flags for a second look; never overrides the human severity).
  * next_best_action "Escalate" — sat OPEN/ASSIGNED past a fixed review SLA.

Deferred (documented follow-up, not built): severity_escalation_history — flag a
(plant · area · category) that has had ≥2 prior records whose initial severity
was raised during workflow. The Observation table carries a single `severity`
column (no initial/history), so the only source is AuditLog.changedFields. It is
implementable there (pre-aggregated, no N+1) but is inert on the current dataset
— no severity-edit history exists yet — so it would demo as blank. See
AI_INSIGHTS_ROW_LAYER_DECISIONS.md.

Every count traces to a field counted below; no model calls. Clustering,
duplicate detection, repeat counting and step-dwell math are plain Python.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import String, cast, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.observation import Observation
from app.models.workflow import WorkflowHistory, WorkflowInstance
from app.schemas.insights import Insight, Signal
from app.services.insights.common import (
    age_days,
    as_naive,
    confidence_for,
    keywords,
    now_naive,
    refs_str,
)
from app.services.insights.templates import fill

_WINDOW_DAYS = 180
_REPEAT_WINDOW_DAYS = 90  # trailing window for the repeat-location rule (spec)
_REPEAT_MIN = 3           # ≥3 in the same (plant·area·category) → recurring
_DUP_WINDOW_HOURS = 48
_DUP_JACCARD = 0.6
_ESCALATE_SLA_DAYS = 7
_VAGUE_WORD_MAX = 3       # a HIGH/CRITICAL entry described in ≤3 words is suspect
_STALE_FACTOR = 1.5      # current-step dwell > 1.5× the (cat·step) average
_STALE_MIN_SAMPLES = 3   # need ≥3 completed dwells before an average is trusted
_STALE_MIN_DAYS = 2      # never flag a step that has only run a day or two
_BOTTLENECK_MIN_STUCK = 3  # a step is only a "bottleneck" with ≥3 stuck records
_MAX_CLUSTERS = 2        # keep bar room for the duplicate + bottleneck cards

# Row-signal priority — the first signal per record is the "primary" chip (the
# one the shared single-chip Map keeps for the other screens); higher first.
_SEV_RANK = {"critical": 3, "high": 2, "watch": 1, "info": 0}
_KIND_RANK = {
    "overdue_escalation": 5,  # stale_step
    "cluster": 4,             # repeat_location
    "next_best_action": 3,    # escalate
    "anomaly": 2,             # severity check
    "duplicate": 1,
}


def _humanize(token: str) -> str:
    return token.replace("_", " ").title()


def _slug(*parts: str) -> str:
    return ":".join(p.lower().replace(" ", "-")[:24] for p in parts if p)


async def compute_observation(
    db: AsyncSession,
    *,
    plant: str | None,
    date_from: Any = None,  # reserved — filter parity with the list view
    date_to: Any = None,
) -> tuple[list[Insight], list[Signal], int]:
    now = now_naive().replace(microsecond=0)
    window_start = now - timedelta(days=_WINDOW_DAYS)

    stmt = (
        select(
            Observation.id,
            Observation.number,
            Observation.date,
            Observation.plantId,
            Observation.areaId,
            cast(Observation.type, String).label("type"),
            cast(Observation.category, String).label("category"),
            cast(Observation.severity, String).label("severity"),
            cast(Observation.status, String).label("status"),
            Observation.description,
            Observation.createdAt,
        )
        .where(Observation.date >= window_start)
        .order_by(Observation.date.desc())
        .limit(600)
    )
    if plant:
        stmt = stmt.where(Observation.plantId == plant)
    rows = (await db.execute(stmt)).all()
    record_count = len(rows)
    if not rows:
        return [], [], 0

    plant_names: dict[str, str] = dict(
        (await db.execute(text('SELECT id, name FROM "Plant"'))).all()
    )
    area_names: dict[str, str] = dict(
        (await db.execute(text('SELECT id, name FROM "Area"'))).all()
    )

    open_rows = [r for r in rows if (r.status or "") != "CLOSED"]

    # Duplicate detection first — its result feeds both the bar card and the
    # per-row chips (so we don't double-flag).
    dup_groups = _duplicate_groups(rows)
    dup_ids = {rid for group in dup_groups for rid in group}

    # Repeat-location: (plant·area·category) combos with ≥3 in the trailing 90d.
    repeat_map = _repeat_location_map(rows, now)

    # Workflow step timing — one pair of queries, pre-aggregated (no N+1). Feeds
    # the stale-step row signal and the bottleneck bar card.
    current_step, avg_dwell = await _workflow_timing(db, rows, now)

    bar: list[Insight] = []
    bar.extend(_cluster_insights(open_rows, plant_names)[:_MAX_CLUSTERS])
    dup_card = _duplicate_insight(dup_groups, rows)
    if dup_card:
        bar.append(dup_card)
    bottleneck = _bottleneck_insight(open_rows, current_step)
    if bottleneck:
        bar.append(bottleneck)

    signals = _row_signals(
        open_rows,
        dup_ids=dup_ids,
        repeat_map=repeat_map,
        current_step=current_step,
        avg_dwell=avg_dwell,
        area_names=area_names,
        now=now,
    )
    return bar, signals, record_count


# ── Bar: category cluster (unchanged) ─────────────────────────────────────────
def _cluster_insights(open_rows: list[Any], plant_names: dict[str, str]) -> list[Insight]:
    """Open UNSAFE observations concentrating in one category at a plant."""
    unsafe = [r for r in open_rows if (r.type or "").startswith("UNSAFE")]
    by_plant: dict[str, list[Any]] = {}
    for r in unsafe:
        by_plant.setdefault(r.plantId, []).append(r)

    out: list[Insight] = []
    for plant_id, group in by_plant.items():
        if len(group) < 3:
            continue
        by_cat: dict[str, list[str]] = {}
        for r in group:
            by_cat.setdefault(r.category or "OTHER", []).append(r.number)
        cat, refs = max(by_cat.items(), key=lambda kv: len(kv[1]))
        count, total = len(refs), len(group)
        if count < 3:
            continue
        plant_label = plant_names.get(plant_id, "this plant")
        cat_label = _humanize(cat)
        out.append(
            Insight(
                id=_slug("observation", "cluster", plant_id, cat),
                kind="cluster",
                severity="high" if count >= 5 else "watch",
                headline=fill(
                    "observation.cluster.category",
                    count=count,
                    total=total,
                    category=cat_label,
                    plant=plant_label,
                ),
                evidence=fill(
                    "observation.cluster.category.evidence",
                    count=count,
                    category=cat_label,
                    plant=plant_label,
                    refs=refs_str(refs),
                ),
                recordRefs=refs,
                suggestedAction="Target a toolbox talk / focused inspection on this hazard category at this plant.",
                confidence=confidence_for(count),
            )
        )
    # Strongest clusters first, so the top-2 kept are the highest-signal ones.
    out.sort(key=lambda i: len(i.recordRefs), reverse=True)
    return out


# ── Bar: workflow bottleneck (Part 6) ─────────────────────────────────────────
def _bottleneck_insight(
    open_rows: list[Any], current_step: dict[str, tuple[str, int]]
) -> Insight | None:
    """The current workflow step holding open observations the longest on
    average — a queue the register is stuck behind. Grounded in the per-record
    days-in-current-step already computed from WorkflowHistory."""
    number_by_id = {r.id: r.number for r in open_rows}
    by_step: dict[str, list[tuple[str, int]]] = {}
    for rid, (step, days) in current_step.items():
        if rid not in number_by_id:  # only open rows in scope
            continue
        by_step.setdefault(step, []).append((number_by_id[rid], days))

    best: tuple[str, float, list[str]] | None = None  # step, avg_days, refs
    for step, members in by_step.items():
        if len(members) < _BOTTLENECK_MIN_STUCK:
            continue
        avg = sum(d for _, d in members) / len(members)
        if best is None or avg > best[1]:
            best = (step, avg, [ref for ref, _ in members])

    if best is None:
        return None
    step, avg_days, refs = best
    count = len(refs)
    return Insight(
        id="observation:bottleneck:step",
        kind="overdue_escalation",
        # A slow queue with real backlog is worth attention; keep it restrained.
        severity="high" if avg_days >= 5 and count >= 5 else "watch",
        headline=fill("observation.bottleneck", step=step, avg=round(avg_days, 1), count=count),
        evidence=fill(
            "observation.bottleneck.evidence",
            step=step,
            avg=round(avg_days, 1),
            count=count,
            refs=refs_str(refs),
        ),
        recordRefs=refs,
        suggestedAction=f"Rebalance or escalate the {step} queue — it is the slowest hop in the observation lifecycle.",
        confidence=confidence_for(count),
    )


# ── Duplicate detection (unchanged) ───────────────────────────────────────────
def _token_set(text_value: str | None) -> set[str]:
    return set(keywords(text_value or ""))


def _duplicate_groups(rows: list[Any]) -> list[list[str]]:
    """Groups of record-ids that look like duplicates: same plant+area, logged
    within 48h, with high token overlap in the description."""
    # Bucket by (plant, area) to keep the O(n²) comparison local and cheap.
    buckets: dict[tuple[str, str], list[Any]] = {}
    for r in rows:
        buckets.setdefault((r.plantId, r.areaId or ""), []).append(r)

    groups: list[list[str]] = []
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        toks = {r.id: _token_set(r.description) for r in bucket}
        used: set[str] = set()
        for i, a in enumerate(bucket):
            if a.id in used:
                continue
            group = [a.id]
            for b in bucket[i + 1 :]:
                if b.id in used:
                    continue
                da, db_ = as_naive(a.date), as_naive(b.date)
                if da is None or db_ is None:
                    continue
                if abs((da - db_).total_seconds()) > _DUP_WINDOW_HOURS * 3600:
                    continue
                ta, tb = toks[a.id], toks[b.id]
                if not ta or not tb:
                    # Both effectively empty/vague descriptions in the same
                    # place within 48h — treat as duplicate candidates too.
                    if not ta and not tb:
                        group.append(b.id)
                        used.add(b.id)
                    continue
                inter = len(ta & tb)
                union = len(ta | tb)
                if union and inter / union >= _DUP_JACCARD:
                    group.append(b.id)
                    used.add(b.id)
            if len(group) > 1:
                used.update(group)
                groups.append(group)
    return groups


def _duplicate_insight(dup_groups: list[list[str]], rows: list[Any]) -> Insight | None:
    if not dup_groups:
        return None
    by_id = {r.id: r for r in rows}
    # Refs = every record involved in any duplicate group.
    refs = [by_id[rid].number for group in dup_groups for rid in group if rid in by_id]
    if len(refs) < 2:
        return None
    n_groups = len(dup_groups)
    return Insight(
        id="observation:duplicate:near-identical",
        kind="duplicate",
        severity="watch",
        headline=fill("observation.duplicate", groups=n_groups, records=len(refs)),
        evidence=fill("observation.duplicate.evidence", records=len(refs), refs=refs_str(refs)),
        recordRefs=refs,
        suggestedAction="Review these near-identical entries — merge or delete the duplicates to keep the register clean.",
        confidence=confidence_for(len(refs)),
    )


# ── Repeat-location (Part 1 · repeatLocation) ─────────────────────────────────
def _repeat_location_map(rows: list[Any], now: Any) -> dict[str, tuple[int, str, str, str]]:
    """recordId → (count, plantId, areaId, category) for every record whose
    (plant·area·category) combo has ≥`_REPEAT_MIN` occurrences in the trailing
    90 days. Counts ALL records in the window (open + closed) — recurrence is
    about the spot, not the open backlog — but only OPEN records get a chip."""
    window_start = now - timedelta(days=_REPEAT_WINDOW_DAYS)
    combos: dict[tuple[str, str, str], list[str]] = {}
    for r in rows:
        d = as_naive(r.date)
        if d is None or d < window_start:
            continue
        key = (r.plantId or "", r.areaId or "", r.category or "OTHER")
        combos.setdefault(key, []).append(r.id)

    out: dict[str, tuple[int, str, str, str]] = {}
    for (plant_id, area_id, category), ids in combos.items():
        if len(ids) < _REPEAT_MIN:
            continue
        for rid in ids:
            out[rid] = (len(ids), plant_id, area_id, category)
    return out


# ── Workflow step timing (Part 1 · staleInWorkflow + Part 6 bottleneck) ───────
async def _workflow_timing(
    db: AsyncSession, rows: list[Any], now: Any
) -> tuple[dict[str, tuple[str, int]], dict[tuple[str, str], tuple[float, int]]]:
    """Returns:
      * current_step: recordId → (currentStepName, days_in_step) for open
        instances (days = now − when the record entered its current step).
      * avg_dwell: (category, stepName) → (avg_days, sample_count) for COMPLETED
        step hops across the dataset — the baseline the stale rule compares to.

    Two queries (instances, then their history), aggregated in Python. Bounded to
    the observation ids already in scope, so it never becomes an N+1."""
    obs_by_id = {r.id: r for r in rows}
    ids = list(obs_by_id.keys())
    empty: tuple[dict, dict] = ({}, {})
    if not ids:
        return empty

    inst_rows = (
        await db.execute(
            select(
                WorkflowInstance.id,
                WorkflowInstance.recordId,
                WorkflowInstance.currentStepName,
                WorkflowInstance.initiatedAt,
                WorkflowInstance.status,
            ).where(
                WorkflowInstance.module == "OBSERVATION",
                WorkflowInstance.recordId.in_(ids),
            )
        )
    ).all()
    if not inst_rows:
        return empty

    inst_ids = [i.id for i in inst_rows]
    hist_rows = (
        await db.execute(
            select(
                WorkflowHistory.instanceId,
                WorkflowHistory.stepName,
                WorkflowHistory.performedAt,
            )
            .where(WorkflowHistory.instanceId.in_(inst_ids))
            .order_by(WorkflowHistory.performedAt)
        )
    ).all()
    hist_by_inst: dict[str, list[Any]] = {}
    for h in hist_rows:
        hist_by_inst.setdefault(h.instanceId, []).append(h)

    current_step: dict[str, tuple[str, int]] = {}
    dwell_samples: dict[tuple[str, str], list[float]] = {}
    for inst in inst_rows:
        obs = obs_by_id.get(inst.recordId)
        if obs is None:
            continue
        cat = obs.category or "OTHER"
        entered = as_naive(inst.initiatedAt)
        for h in hist_by_inst.get(inst.id, []):
            pa = as_naive(h.performedAt)
            if entered is not None and pa is not None and pa >= entered:
                # Dwell in h.stepName = time from entering it (the previous
                # transition, or initiation) to this action completing it.
                dwell_samples.setdefault((cat, h.stepName), []).append(
                    (pa - entered).total_seconds() / 86400
                )
            if pa is not None:
                entered = pa
        # Current open step: entered at the last history event (or initiation).
        obs_open = (obs.status or "") != "CLOSED"
        if obs_open and (inst.status or "") == "IN_PROGRESS" and inst.currentStepName and entered is not None:
            days = int((now - entered).total_seconds() // 86400)
            current_step[inst.recordId] = (inst.currentStepName, max(days, 0))

    avg_dwell = {
        key: (sum(v) / len(v), len(v)) for key, v in dwell_samples.items() if v
    }
    return current_step, avg_dwell


# ── Row signals (0-N per record) ──────────────────────────────────────────────
def _row_signals(
    open_rows: list[Any],
    *,
    dup_ids: set[str],
    repeat_map: dict[str, tuple[int, str, str, str]],
    current_step: dict[str, tuple[str, int]],
    avg_dwell: dict[tuple[str, str], tuple[float, int]],
    area_names: dict[str, str],
    now: Any,
) -> list[Signal]:
    out: list[Signal] = []
    for r in open_rows:
        sev = (r.severity or "").upper()
        word_count = len((r.description or "").split())
        per_record: list[Signal] = []

        # 1) repeat_location — recurring hazard at this exact spot.
        rep = repeat_map.get(r.id)
        if rep:
            count, _plant_id, area_id, category = rep
            area_label = area_names.get(area_id or "", "this area")
            per_record.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.number,
                    kind="cluster",
                    severity="high" if count >= 5 else "watch",
                    label=fill("signal.repeat_location.label", count=count),
                    evidence=fill(
                        "signal.repeat_location.evidence",
                        count=count,
                        area=area_label,
                        category=_humanize(category),
                    ),
                    suggestedAction="Treat as a recurring hazard — a one-off fix here clearly isn't holding.",
                    # Click-to-filter: narrow the list to this same spot+hazard.
                    filterHref=f"?cat={category}&area={area_id}",
                )
            )

        # 2) stale_step — current step running well past its usual dwell.
        cur = current_step.get(r.id)
        if cur:
            step, days = cur
            avg_entry = avg_dwell.get((r.category or "OTHER", step))
            if (
                avg_entry
                and avg_entry[1] >= _STALE_MIN_SAMPLES
                and days >= _STALE_MIN_DAYS
                and days > _STALE_FACTOR * avg_entry[0]
            ):
                avg_days = round(avg_entry[0], 1)
                per_record.append(
                    Signal(
                        recordId=r.id,
                        recordRef=r.number,
                        kind="overdue_escalation",
                        severity="high" if days > 2 * avg_entry[0] else "watch",
                        label=fill("signal.stale_step.label", days=days),
                        evidence=fill(
                            "signal.stale_step.evidence",
                            ref=r.number,
                            days=days,
                            step=step,
                            avg=avg_days,
                        ),
                        suggestedAction=f"Chase the {step} step — it is running well past its usual time.",
                    )
                )

        # 3) duplicate — later member of a near-identical pair.
        if r.id in dup_ids:
            per_record.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.number,
                    kind="duplicate",
                    severity="watch",
                    label=fill("signal.duplicate.label"),
                    evidence=fill("signal.duplicate.evidence", ref=r.number),
                    suggestedAction="Confirm against the matching entry and remove the duplicate.",
                    filterHref="?insight=observation:duplicate:near-identical",
                )
            )

        # 4) anomaly — thin description on a HIGH/CRITICAL entry.
        if sev in {"HIGH", "CRITICAL"} and word_count <= _VAGUE_WORD_MAX:
            per_record.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.number,
                    kind="anomaly",
                    severity="watch",
                    label=fill("signal.severity_mismatch.label"),
                    evidence=fill("signal.severity_mismatch.evidence", ref=r.number, severity=sev),
                    suggestedAction="Add detail or re-check the severity — the description is too thin to justify it.",
                )
            )

        # 5) escalate — sat open past the fixed review SLA.
        if (r.status or "") in {"OPEN", "ASSIGNED"} and (age_days(r.createdAt) or 0) > _ESCALATE_SLA_DAYS:
            days = age_days(r.createdAt) or 0
            per_record.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.number,
                    kind="next_best_action",
                    severity="watch",
                    label=fill("signal.escalate.label"),
                    evidence=fill("signal.escalate.evidence", ref=r.number, days=days),
                    suggestedAction="Escalate to the section head to close the review.",
                )
            )

        # Highest-priority chip first, so the shared single-chip Map (used by the
        # other list screens) keeps the most important one for a record.
        per_record.sort(
            key=lambda s: (_SEV_RANK.get(s.severity, 0), _KIND_RANK.get(s.kind, 0)),
            reverse=True,
        )
        out.extend(per_record)
    return out
