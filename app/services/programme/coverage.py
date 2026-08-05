"""THE coverage engine. One accessor. No stored coverage flag exists anywhere.

docs/cams/08 §4.

Coverage is **derived, never stored**. This is the direct lesson of F-29, where
four independent read paths disagreed about one fact for a month and shipped a
report reading "78.9% over 0-of-82". Every surface — matrix, gap list, Command
Centre Band 4, programme export, recommendation engine — calls
`coverage_for_cycle`, and `test_programme_coverage` asserts they agree.

Six states, and the two the brief insists on are first-class:

  COVERED_FULL     ≥ threshold of the scope unit's checkpoints assessed
  COVERED_SAMPLED  as above, but the engagement declared a sampling approach.
                   NEVER merged into green — "we sampled 8 of 40 and passed" is a
                   different assurance claim from "we verified all 40".
  PARTIAL          > 0 but < threshold. Fire Safety touched at 3 of 14 lands
                   here: amber with 3/14 on the cell, never green, never blank.
  UNCOVERED        no completed engagement in the period
  OVERDUE          uncovered AND the required window has closed
  WAIVED           the scope unit carries an approved waiver

The pure scoring core (`classify`, `aggregate_states`) takes plain numbers so it
is unit-testable with no DB — the house test style.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_compliance import ComplianceAudit
from app.models.cams import CamsEngagement
from app.models.programme import (
    AuditProgramme,
    ProgrammeCycle,
    ProgrammeScopeUnit,
    ProgrammeSlot,
    SlotScopeUnit,
)
from app.services.plant_directory import resolve_plant_names, site_label
from app.services.programme import resolver

COVERED_STATES = ("COVERED_FULL", "COVERED_SAMPLED")
ALL_STATES = (
    "COVERED_FULL",
    "COVERED_SAMPLED",
    "PARTIAL",
    "UNCOVERED",
    "OVERDUE",
    "WAIVED",
)


def _today() -> date:
    return datetime.now(timezone.utc).date()


# ─────────────────────────────────────────────────────────────────────
# Pure core
# ─────────────────────────────────────────────────────────────────────


@dataclass
class PeriodCoverage:
    periodIndex: int
    state: str
    assessed: int = 0
    total: int = 0
    engagements: list[dict[str, Any]] = field(default_factory=list)

    @property
    def pct(self) -> float | None:
        return None if not self.total else round(self.assessed / self.total * 100, 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "periodIndex": self.periodIndex,
            "state": self.state,
            "assessed": self.assessed,
            "total": self.total,
            "pct": self.pct,
            "label": f"{self.assessed}/{self.total}" if self.total else "—",
            "engagements": self.engagements,
        }


def classify(
    *,
    assessed: int,
    total: int,
    threshold_pct: float,
    sampled: bool,
    waived: bool = False,
    window_closed: bool = False,
) -> str:
    """The single definition of a coverage state. Pure.

    Order matters: a waiver wins over everything (it is a governed decision), and
    an overdue window only downgrades an *uncovered* period — a period that was
    covered late is covered, and the *lateness* is variance, reported separately.
    """
    if waived:
        return "WAIVED"
    if total <= 0 or assessed <= 0:
        return "OVERDUE" if window_closed else "UNCOVERED"
    pct = assessed / total * 100
    if pct + 1e-9 >= threshold_pct:
        return "COVERED_SAMPLED" if sampled else "COVERED_FULL"
    return "PARTIAL"


def aggregate_states(states: Iterable[str]) -> dict[str, Any]:
    """Roll period states into a scope-unit / site / cycle summary.

    `coveragePct` counts COVERED_FULL and COVERED_SAMPLED, and excludes WAIVED
    from the denominator — a waived unit is neither a success nor a gap, and
    counting it either way misstates the programme.
    """
    counts = {s: 0 for s in ALL_STATES}
    for s in states:
        counts[s] = counts.get(s, 0) + 1
    considered = sum(v for k, v in counts.items() if k != "WAIVED")
    covered = counts["COVERED_FULL"] + counts["COVERED_SAMPLED"]
    return {
        "counts": counts,
        "considered": considered,
        "covered": covered,
        "coveragePct": None if not considered else round(covered / considered * 100, 1),
        "gaps": counts["UNCOVERED"] + counts["OVERDUE"] + counts["PARTIAL"],
        "overdue": counts["OVERDUE"],
        "waived": counts["WAIVED"],
        "sampledOnly": counts["COVERED_SAMPLED"],
    }


def period_bounds(start: date, end: date, periods: int) -> list[tuple[date, date]]:
    """Split a cycle into N contiguous sub-periods (4 → quarters).

    Day-count arithmetic rather than calendar months, so a 3-year certification
    cycle splits as evenly as a financial year and the last period always ends
    exactly on `periodEnd`.
    """
    periods = max(1, periods)
    total_days = (end - start).days + 1
    out: list[tuple[date, date]] = []
    cursor = start
    for i in range(periods):
        span = total_days // periods + (1 if i < total_days % periods else 0)
        p_end = cursor + timedelta(days=span - 1)
        if i == periods - 1:
            p_end = end
        out.append((cursor, p_end))
        cursor = p_end + timedelta(days=1)
    return out


def period_index_for(bounds: list[tuple[date, date]], when: date | None) -> int | None:
    if when is None:
        return None
    for i, (s, e) in enumerate(bounds):
        if s <= when <= e:
            return i
    return None


# ─────────────────────────────────────────────────────────────────────
# The accessor
# ─────────────────────────────────────────────────────────────────────


@dataclass
class CoverageResult:
    cycleId: str
    thresholdPct: float
    periods: list[dict[str, Any]]
    scopeUnits: list[dict[str, Any]]
    summary: dict[str, Any]
    bySite: list[dict[str, Any]]
    gaps: list[dict[str, Any]]
    auditorLoad: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycleId": self.cycleId,
            "thresholdPct": self.thresholdPct,
            "periods": self.periods,
            "scopeUnits": self.scopeUnits,
            "summary": self.summary,
            "bySite": self.bySite,
            "gaps": self.gaps,
            "auditorLoad": self.auditorLoad,
        }


async def coverage_for_cycle(
    db: AsyncSession, cycle_id: str, *, as_of: date | None = None
) -> CoverageResult:
    """The ONE coverage definition. Every surface calls this.

    Do not add a second read path, and do not cache the result in a column. The
    contract test asserts the matrix endpoint, the gap endpoint and the export
    payload return identical states for a fixed cycle.
    """
    as_of = as_of or _today()

    cycle = await db.get(ProgrammeCycle, cycle_id)
    if cycle is None:
        raise ValueError("Programme cycle not found")
    programme = await db.get(AuditProgramme, cycle.programmeId)
    threshold = programme.fullCoverageThresholdPct if programme else 80.0

    bounds = period_bounds(cycle.periodStart, cycle.periodEnd, cycle.periodsPerCycle)

    units = list(
        (
            await db.execute(
                select(ProgrammeScopeUnit).where(ProgrammeScopeUnit.cycleId == cycle_id)
            )
        ).scalars().all()
    )
    slots = list(
        (
            await db.execute(select(ProgrammeSlot).where(ProgrammeSlot.cycleId == cycle_id))
        ).scalars().all()
    )
    links: list[SlotScopeUnit] = []
    if slots:
        links = list(
            (
                await db.execute(
                    select(SlotScopeUnit).where(
                        SlotScopeUnit.slotId.in_([s.id for s in slots])
                    )
                )
            ).scalars().all()
        )

    slots_by_unit: dict[str, list[ProgrammeSlot]] = {}
    slot_by_id = {s.id: s for s in slots}
    for ln in links:
        s = slot_by_id.get(ln.slotId)
        if s is not None:
            slots_by_unit.setdefault(ln.scopeUnitId, []).append(s)

    resolved = await resolver.resolve_many(
        db, [(s.engagementKind, s.engagementId) for s in slots]
    )

    # Every row this engine emits carries `siteName` next to `siteId`. The
    # matrix is a site × discipline grid, so the site axis is the one label a
    # reader cannot do without — and it was printing cuids.
    plant_names = await resolve_plant_names(db, [u.siteId for u in units])

    # ── per scope unit × period ──────────────────────────────────────
    unit_rows: list[dict[str, Any]] = []
    for u in units:
        waived = bool(u.waiverReason)
        per_period: list[PeriodCoverage] = []
        for i, (p_start, p_end) in enumerate(bounds):
            window_closed = p_end < as_of
            assessed = total = 0
            sampled = False
            engs: list[dict[str, Any]] = []

            for s in slots_by_unit.get(u.id, []):
                if s.periodIndex != i:
                    continue
                r = resolved.get(((s.engagementKind or "").upper(), s.engagementId or ""))
                if r is None or not r.isComplete:
                    continue
                a, t = r.assessedByDimension.get(u.dimensionKey, (0, 0))
                if t == 0 and len(r.assessedByDimension) == 1:
                    # Inspection engine: one bucket keyed by standard. If the slot
                    # links this unit, attribute the engagement's counts to it.
                    a, t = next(iter(r.assessedByDimension.values()))
                assessed += a
                total += t
                if s.samplingApproach and s.samplingApproach != "FULL":
                    sampled = True
                engs.append(
                    {
                        "engagementKind": r.kind,
                        "engagementId": r.id,
                        "code": r.code,
                        "status": r.status,
                        "assessed": a,
                        "total": t,
                        "scorePct": r.scorePct,
                        "samplingApproach": s.samplingApproach,
                    }
                )

            state = classify(
                assessed=assessed,
                total=total,
                threshold_pct=threshold,
                sampled=sampled,
                waived=waived,
                window_closed=window_closed,
            )
            per_period.append(
                PeriodCoverage(
                    periodIndex=i, state=state, assessed=assessed, total=total, engagements=engs
                )
            )

        agg = aggregate_states([p.state for p in per_period])
        # Required-frequency shortfall is measured against completed coverage, not
        # against slot count — a slot that never became an engagement covers
        # nothing, which is precisely what the programme is meant to expose.
        required = u.requiredPerCycle or 0
        unit_rows.append(
            {
                "scopeUnitId": u.id,
                "dimension": u.dimension,
                "dimensionKey": u.dimensionKey,
                "dimensionLabel": u.dimensionLabel or u.dimensionKey,
                "siteId": u.siteId,
                "siteName": site_label(plant_names, u.siteId),
                "riskWeight": u.riskWeight,
                "requiredPerCycle": u.requiredPerCycle,
                "rationale": u.rationale,
                # `isWaived` (bool) NOT `waived` — the aggregate below also
                # carries a `waived` COUNT, and spreading it over a same-named
                # boolean silently replaced the scope unit's own waiver flag
                # with a number. Distinct names, distinct meanings.
                "isWaived": waived,
                "waiverReason": u.waiverReason,
                "periods": [p.as_dict() for p in per_period],
                **agg,
                "shortfall": max(0, required - agg["covered"]) if required else 0,
            }
        )

    # ── site rollup ──────────────────────────────────────────────────
    by_site: dict[str | None, list[str]] = {}
    for row in unit_rows:
        for p in row["periods"]:
            by_site.setdefault(row["siteId"], []).append(p["state"])
    site_rows = [
        {"siteId": sid, "siteName": site_label(plant_names, sid), **aggregate_states(states)}
        for sid, states in by_site.items()
    ]
    site_rows.sort(key=lambda r: (r["coveragePct"] is None, r["coveragePct"] or 0))

    # ── gaps, ranked by risk weight (NOT alphabetically) ─────────────
    gaps: list[dict[str, Any]] = []
    for row in unit_rows:
        if row["isWaived"]:
            continue
        for p in row["periods"]:
            if p["state"] in ("UNCOVERED", "OVERDUE", "PARTIAL"):
                gaps.append(
                    {
                        "scopeUnitId": row["scopeUnitId"],
                        "siteId": row["siteId"],
                        "siteName": row["siteName"],
                        "dimensionKey": row["dimensionKey"],
                        "dimensionLabel": row["dimensionLabel"],
                        "periodIndex": p["periodIndex"],
                        "state": p["state"],
                        "riskWeight": row["riskWeight"],
                        "assessedLabel": p["label"],
                    }
                )
    # An uncovered weight-5 unit outranks three uncovered weight-1s.
    _sev = {"OVERDUE": 0, "UNCOVERED": 1, "PARTIAL": 2}
    gaps.sort(key=lambda g: (-g["riskWeight"], _sev.get(g["state"], 3), g["periodIndex"]))

    # ── auditor load + collision detection ───────────────────────────
    load: dict[str, dict[str, Any]] = {}
    for s in slots:
        if s.status in ("CANCELLED", "WAIVED"):
            continue
        uid = s.intendedLeadUserId
        if not uid:
            # EXTERNAL slots have no internal lead but still consume auditee-side
            # capacity; they are counted against the site, not a person.
            continue
        entry = load.setdefault(
            uid, {"userId": uid, "totalDays": 0.0, "byPeriod": {}, "slots": [], "collisions": []}
        )
        entry["totalDays"] += s.estimatedAuditorDays or 0.0
        entry["byPeriod"][s.periodIndex] = (
            entry["byPeriod"].get(s.periodIndex, 0.0) + (s.estimatedAuditorDays or 0.0)
        )
        entry["slots"].append(
            {
                "slotId": s.id,
                "slotCode": s.slotCode,
                "windowStart": s.windowStart.isoformat(),
                "windowEnd": s.windowEnd.isoformat(),
                "periodIndex": s.periodIndex,
                "days": s.estimatedAuditorDays,
            }
        )

    for entry in load.values():
        ss = sorted(entry["slots"], key=lambda x: x["windowStart"])
        for a, b in zip(ss, ss[1:]):
            if b["windowStart"] <= a["windowEnd"]:
                entry["collisions"].append(
                    {
                        "a": a["slotCode"],
                        "b": b["slotCode"],
                        "reason": f"{a['slotCode']} and {b['slotCode']} have overlapping windows",
                    }
                )
    load_rows = sorted(load.values(), key=lambda e: -e["totalDays"])

    all_states = [p["state"] for row in unit_rows for p in row["periods"]]
    summary = {
        **aggregate_states(all_states),
        "scopeUnitCount": len(units),
        "slotCount": len(slots),
        "materialisedSlotCount": sum(1 for s in slots if s.engagementId),
        "unplannedSlotCount": sum(1 for s in slots if s.origin == "UNPLANNED"),
        "externalSlotCount": sum(1 for s in slots if s.origin == "EXTERNAL"),
        "collisionCount": sum(len(e["collisions"]) for e in load_rows),
    }

    return CoverageResult(
        cycleId=cycle_id,
        thresholdPct=threshold,
        periods=[
            {"periodIndex": i, "start": s.isoformat(), "end": e.isoformat(),
             "closed": e < as_of, "label": f"P{i + 1}"}
            for i, (s, e) in enumerate(bounds)
        ],
        scopeUnits=unit_rows,
        summary=summary,
        bySite=site_rows,
        gaps=gaps,
        auditorLoad=load_rows,
    )


# ─────────────────────────────────────────────────────────────────────
# Variance — the subtraction that makes slot ≠ engagement worth modelling
# ─────────────────────────────────────────────────────────────────────


async def variance_for_cycle(db: AsyncSession, cycle_id: str) -> list[dict[str, Any]]:
    """Plan vs actual, per slot. Three variance classes (docs/cams/08 §2.2).

    Each one is a question a certification body actually asks:
      timing drift   "you planned Q2 and audited in Q4 — why?"
      scope variance "the plan covered Fire AND Electrical; the report covers Fire"
      non-execution  "why did this audit not happen?" → the amendment is the answer
    """
    slots = list(
        (
            await db.execute(
                select(ProgrammeSlot)
                .where(ProgrammeSlot.cycleId == cycle_id)
                .order_by(ProgrammeSlot.windowStart)
            )
        ).scalars().all()
    )
    resolved = await resolver.resolve_many(
        db, [(s.engagementKind, s.engagementId) for s in slots]
    )
    links: list[SlotScopeUnit] = []
    if slots:
        links = list(
            (
                await db.execute(
                    select(SlotScopeUnit).where(SlotScopeUnit.slotId.in_([s.id for s in slots]))
                )
            ).scalars().all()
        )
    planned_units: dict[str, list[str]] = {}
    for ln in links:
        planned_units.setdefault(ln.slotId, []).append(ln.scopeUnitId)

    unit_rows = {
        u.id: u
        for u in (
            await db.execute(
                select(ProgrammeScopeUnit).where(ProgrammeScopeUnit.cycleId == cycle_id)
            )
        ).scalars().all()
    }

    out: list[dict[str, Any]] = []
    for s in slots:
        r = resolved.get(((s.engagementKind or "").upper(), s.engagementId or ""))
        drift = None
        if r and r.actualDate:
            drift = (r.actualDate.date() - s.windowEnd).days

        planned_keys = [
            unit_rows[uid].dimensionKey for uid in planned_units.get(s.id, []) if uid in unit_rows
        ]
        actual_keys = sorted(r.assessedByDimension) if r else []
        missed = [k for k in planned_keys if k not in actual_keys]

        out.append(
            {
                "slotId": s.id,
                "slotCode": s.slotCode,
                "status": s.status,
                "origin": s.origin,
                "windowStart": s.windowStart.isoformat(),
                "windowEnd": s.windowEnd.isoformat(),
                "engagement": r.as_dict() if r else None,
                "timingDriftDays": drift,
                "isLate": bool(drift and drift > 0),
                "plannedScopeKeys": planned_keys,
                "actualScopeKeys": actual_keys,
                "scopeVariance": missed,
                "hasScopeVariance": bool(missed),
                "estimatedAuditorDays": s.estimatedAuditorDays,
                "actualAuditorDays": s.actualAuditorDays,
                "notExecuted": s.status in ("DEFERRED", "CANCELLED", "WAIVED"),
                "amendmentCount": s.amendmentCount,
            }
        )
    return out


__all__ = [
    "classify",
    "aggregate_states",
    "period_bounds",
    "period_index_for",
    "coverage_for_cycle",
    "variance_for_cycle",
    "CoverageResult",
    "PeriodCoverage",
    "ALL_STATES",
    "COVERED_STATES",
]
