"""CAPA Management insight rules (spec §2.7) — deterministic.

Bar:
  * overdue_escalation — open CAPAs past their closure target, ranked by
    days-overdue, worst offenders named (not buried in a full list).
  * trend — CAPAs closed this month vs opened this month, reconciled in plain
    language so the tiles don't look contradictory ("0 closed vs 22 opened —
    backlog growing").
  * next_best_action — one owner holding ≥3 overdue CAPAs is a bottleneck;
    name them so the HSE manager can redistribute.
Row signals:
  * predictive_risk "Likely audit finding" — CRITICAL/HIGH severity sitting in
    ACTIONS_PLANNED (planned, not yet in progress) past a threshold.

Every number traces to a counted field; no model calls.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capa import Capa
from app.schemas.insights import Insight, Signal
from app.services.insights.common import age_days, as_naive, confidence_for, now_naive, refs_str
from app.services.insights.templates import fill

# Non-terminal states — mirrors app/services/erm_p3.py::_OPEN_CAPA exactly.
_OPEN = ("DRAFT", "SUBMITTED", "UNDER_RCA", "ACTIONS_PLANNED", "ACTIONS_IN_PROGRESS", "PENDING_VERIFICATION")
_SERIOUS = {"CRITICAL", "HIGH"}
_PLANNED_STALE_DAYS = 7
_BOTTLENECK_MIN = 3
_NEAR_BREACH_DAYS = 7  # a serious CAPA still in planning this close to target is a pre-breach signal


async def compute_capa(
    db: AsyncSession,
    *,
    plant: str | None,
    date_from: Any = None,
    date_to: Any = None,
) -> tuple[list[Insight], list[Signal], int]:
    now = now_naive().replace(microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    stmt = (
        select(
            Capa.id,
            Capa.capaNumber,
            Capa.plantId,
            Capa.severity,
            Capa.state,
            Capa.closureTargetDate,
            Capa.stateChangedAt,
            Capa.createdAt,
            Capa.primaryOwnerUserId,
        )
        .where(Capa.isDeleted.is_(False))
        .order_by(Capa.createdAt.desc())
        .limit(800)
    )
    if plant:
        stmt = stmt.where(Capa.plantId == plant)
    rows = (await db.execute(stmt)).all()
    record_count = len(rows)
    if not rows:
        return [], [], 0

    open_rows = [r for r in rows if (r.state or "") in _OPEN]

    bar: list[Insight] = []
    overdue_rows, overdue_card = _overdue_insight(open_rows, now)
    if overdue_card:
        bar.append(overdue_card)
    near_breach = _near_breach_insight(open_rows, now)
    if near_breach:
        bar.append(near_breach)
    trend = await _backlog_trend_insight(db, plant, month_start)
    if trend:
        bar.append(trend)
    bottleneck = await _bottleneck_insight(db, overdue_rows)
    if bottleneck:
        bar.append(bottleneck)

    signals = _row_signals(open_rows, now)
    return bar, signals, record_count


def _near_breach_insight(open_rows: list[Any], now: Any) -> Insight | None:
    """Predictive, not reactive (spec §2 card #4): CRITICAL/HIGH-source CAPAs
    still in ACTIONS_PLANNED (not yet started) whose closure target lands within
    the next week — flagged BEFORE the date passes, the difference between a
    sentinel and a late-notice list."""
    from datetime import timedelta

    horizon = now + timedelta(days=_NEAR_BREACH_DAYS)
    hits: list[tuple[Any, int]] = []
    for r in open_rows:
        if (r.severity or "").upper() not in _SERIOUS:
            continue
        if (r.state or "") != "ACTIONS_PLANNED":
            continue
        d = as_naive(r.closureTargetDate)
        if d is not None and now < d <= horizon:
            hits.append((r, (d - now).days))
    if not hits:
        return None
    hits.sort(key=lambda rd: rd[1])  # soonest-due first
    soonest_row, soonest_days = hits[0]
    refs = [r.capaNumber for r, _ in hits]
    return Insight(
        id="capa:predictive:near-breach",
        kind="predictive_risk",
        severity="high",
        headline=fill(
            "capa.near_breach", count=len(hits), worst_ref=soonest_row.capaNumber, days=soonest_days
        ),
        evidence=fill("capa.near_breach.evidence", count=len(hits), refs=refs_str(refs)),
        recordRefs=refs,
        suggestedAction="Move these serious CAPAs into progress now — they breach their target within the week.",
        confidence=confidence_for(len(hits)),
        seriousPotential=True,
    )


def _overdue_insight(open_rows: list[Any], now: Any) -> tuple[list[Any], Insight | None]:
    overdue: list[tuple[Any, int]] = []
    for r in open_rows:
        d = as_naive(r.closureTargetDate)
        if d is not None and d < now:
            overdue.append((r, (now - d).days))
    if not overdue:
        return [], None
    overdue.sort(key=lambda rd: rd[1], reverse=True)
    worst_row, worst_days = overdue[0]
    refs = [r.capaNumber for r, _ in overdue]
    worst_sev = (worst_row.severity or "MODERATE").title()
    any_serious = any((r.severity or "").upper() in _SERIOUS for r, _ in overdue)
    card = Insight(
        id="capa:overdue:past-target",
        kind="overdue_escalation",
        severity="critical" if (any_serious and worst_days >= 30) else "high",
        headline=fill(
            "capa.overdue",
            count=len(overdue),
            worst_ref=worst_row.capaNumber,
            worst_days=worst_days,
            severity=worst_sev,
        ),
        evidence=fill("capa.overdue.evidence", count=len(overdue), refs=refs_str(refs)),
        recordRefs=refs,
        suggestedAction="Escalate the worst-overdue CAPAs to their owners this week — these are next audit findings.",
        confidence=confidence_for(len(overdue)),
        seriousPotential=any_serious,
        overdueDays=worst_days,
    )
    return [r for r, _ in overdue], card


async def _backlog_trend_insight(db: AsyncSession, plant: str | None, month_start: Any) -> Insight | None:
    opened_stmt = (
        select(func.count()).select_from(Capa).where(Capa.isDeleted.is_(False)).where(Capa.createdAt >= month_start)
    )
    closed_stmt = (
        select(func.count()).select_from(Capa).where(Capa.isDeleted.is_(False)).where(Capa.closedAt >= month_start)
    )
    if plant:
        opened_stmt = opened_stmt.where(Capa.plantId == plant)
        closed_stmt = closed_stmt.where(Capa.plantId == plant)
    opened = int((await db.execute(opened_stmt)).scalar_one() or 0)
    closed = int((await db.execute(closed_stmt)).scalar_one() or 0)
    # Only worth surfacing when the backlog is actually growing.
    if opened < 3 or opened <= closed:
        return None
    return Insight(
        id="capa:trend:backlog",
        kind="trend",
        severity="watch",
        headline=fill("capa.backlog", closed=closed, opened=opened),
        evidence=fill("capa.backlog.evidence", closed=closed, opened=opened),
        recordRefs=[],
        suggestedAction="Prioritise closures — the open backlog is growing faster than it's being cleared this month.",
        confidence=confidence_for(opened),
    )


async def _bottleneck_insight(db: AsyncSession, overdue_rows: list[Any]) -> Insight | None:
    by_owner: dict[str, list[Any]] = {}
    for r in overdue_rows:
        if r.primaryOwnerUserId:
            by_owner.setdefault(r.primaryOwnerUserId, []).append(r)
    if not by_owner:
        return None
    owner_id, held = max(by_owner.items(), key=lambda kv: len(kv[1]))
    if len(held) < _BOTTLENECK_MIN:
        return None
    name_row = (
        await db.execute(text('SELECT name FROM "User" WHERE id = :id').bindparams(id=owner_id))
    ).first()
    owner_name = name_row[0] if name_row else "One owner"
    refs = [r.capaNumber for r in held]
    return Insight(
        id="capa:action:owner-bottleneck",
        kind="next_best_action",
        severity="high",
        headline=fill("capa.bottleneck", owner=owner_name, count=len(held)),
        evidence=fill("capa.bottleneck.evidence", owner=owner_name, count=len(held), refs=refs_str(refs)),
        recordRefs=refs,
        suggestedAction="Redistribute some of this owner's overdue CAPAs so they don't all stall behind one person.",
        confidence=confidence_for(len(held)),
    )


def _row_signals(open_rows: list[Any], now: Any) -> list[Signal]:
    out: list[Signal] = []
    for r in open_rows:
        sev = (r.severity or "").upper()
        planned_age = age_days(r.stateChangedAt) if (r.state or "") == "ACTIONS_PLANNED" else None
        if sev in _SERIOUS and planned_age is not None and planned_age > _PLANNED_STALE_DAYS:
            out.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.capaNumber,
                    kind="predictive_risk",
                    severity="high",
                    label=fill("signal.audit_finding.label"),
                    evidence=fill(
                        "signal.audit_finding.evidence", ref=r.capaNumber, severity=sev, days=planned_age
                    ),
                    suggestedAction="Move it into progress — a serious CAPA stuck in planning is a likely audit finding.",
                )
            )
    return out
