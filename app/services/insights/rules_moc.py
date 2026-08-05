"""Management of Change insight rules (spec §2.8) — deterministic.

Bar:
  * overdue_escalation — open MOCs past their target completion date, named by
    number and days-overdue.
  * cluster — the single highest-value cross-module link available: an active
    MOC touching a process/area that ALSO carries an unmitigated CRITICAL entry
    in the Combined Risk Register (HIRA/EAI). Matched on shared significant
    keywords (≥2), so a boiler-feed-water change surfaces the boiler HIRA/EAI
    critical to cross-check before approval.
Row signals:
  * next_best_action "Stalled in draft" — a Major/Critical change sitting in
    draft past a threshold.

Every number traces to a counted field; no model calls. (The status-label
mapping the spec flagged is already fixed in the frontend `_meta.ts`.)
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.eai import EaiEntry, EaiStudy
from app.models.hira import HiraEntry, HiraStudy
from app.models.moc import ChangeRequest
from app.schemas.insights import Insight, Signal
from app.services.insights.common import age_days, as_naive, confidence_for, keywords, now_naive, refs_str
from app.services.insights.templates import fill

_TERMINAL = {
    "closed_successful", "closed_aborted", "closed_rejected",
    "withdrawn", "expired", "rolled_back", "closed",
}
_SERIOUS_CLASS = {"major", "critical"}
_DRAFT_STALE_DAYS = 14
_MIN_KEYWORD_OVERLAP = 2


async def compute_moc(
    db: AsyncSession,
    *,
    plant: str | None,
    date_from: Any = None,
    date_to: Any = None,
) -> tuple[list[Insight], list[Signal], int]:
    now = now_naive().replace(microsecond=0)

    stmt = (
        select(
            ChangeRequest.id,
            ChangeRequest.number,
            ChangeRequest.title,
            ChangeRequest.description,
            ChangeRequest.classification,
            ChangeRequest.status,
            ChangeRequest.plantId,
            ChangeRequest.targetCompletionDate,
            ChangeRequest.updatedAt,
            ChangeRequest.affectedProcesses,
            ChangeRequest.affectedLocations,
            ChangeRequest.hazardCategories,
        )
        .order_by(ChangeRequest.updatedAt.desc())
        .limit(500)
    )
    if plant:
        stmt = stmt.where(ChangeRequest.plantId == plant)
    rows = (await db.execute(stmt)).all()
    record_count = len(rows)
    if not rows:
        return [], [], 0

    active = [r for r in rows if (r.status or "") not in _TERMINAL]

    bar: list[Insight] = []
    overdue = _overdue_insight(active, now)
    if overdue:
        bar.append(overdue)
    cluster = await _critical_risk_cluster_insight(db, active, plant)
    if cluster:
        bar.append(cluster)

    signals = _row_signals(active, now)
    return bar, signals, record_count


def _overdue_insight(active: list[Any], now: Any) -> Insight | None:
    overdue: list[tuple[Any, int]] = []
    for r in active:
        d = as_naive(r.targetCompletionDate)
        if d is not None and d < now:
            overdue.append((r, (now - d).days))
    if not overdue:
        return None
    overdue.sort(key=lambda rd: rd[1], reverse=True)
    worst_row, worst_days = overdue[0]
    refs = [r.number for r, _ in overdue]
    return Insight(
        id="moc:overdue:past-target",
        kind="overdue_escalation",
        severity="high" if worst_days >= 30 else "watch",
        headline=fill("moc.overdue", count=len(overdue), worst_ref=worst_row.number, worst_days=worst_days),
        evidence=fill("moc.overdue.evidence", count=len(overdue), refs=refs_str(refs)),
        recordRefs=refs,
        suggestedAction="Push the overdue changes to completion or re-baseline their target dates.",
        confidence=confidence_for(len(overdue)),
    )


async def _critical_risk_cluster_insight(
    db: AsyncSession, active: list[Any], plant: str | None
) -> Insight | None:
    """Active MOCs whose keywords overlap an unmitigated CRITICAL HIRA/EAI risk
    on the same plant — the cross-module link to check before approving."""
    if not active:
        return None
    crit_bags = await _critical_risk_keyword_bags(db, plant)
    if not crit_bags:
        return None

    matched_refs: list[str] = []
    example_risk = ""
    for r in active:
        moc_bag = set(keywords(r.title, r.description, r.affectedProcesses, r.affectedLocations, r.hazardCategories))
        if not moc_bag:
            continue
        for risk_ref, risk_bag in crit_bags:
            if len(moc_bag & risk_bag) >= _MIN_KEYWORD_OVERLAP:
                matched_refs.append(r.number)
                if not example_risk:
                    example_risk = risk_ref
                break
    if not matched_refs:
        return None
    return Insight(
        id="moc:cluster:touches-critical-risk",
        kind="cluster",
        severity="high",
        headline=fill("moc.cluster.critical", count=len(matched_refs)),
        evidence=fill(
            "moc.cluster.critical.evidence",
            count=len(matched_refs),
            risk=example_risk,
            refs=refs_str(matched_refs),
        ),
        recordRefs=matched_refs,
        suggestedAction="Cross-reference these changes against the linked critical risk before approving them.",
        confidence=confidence_for(len(matched_refs)),
    )


async def _critical_risk_keyword_bags(db: AsyncSession, plant: str | None) -> list[tuple[str, set[str]]]:
    bags: list[tuple[str, set[str]]] = []

    hira_stmt = (
        select(HiraStudy.number, HiraEntry.sequenceNumber, HiraEntry.activityDescription)
        .join(HiraStudy, HiraStudy.id == HiraEntry.studyId)
        .where(HiraEntry.isCurrentVersion.is_(True))
        .where((HiraEntry.initialRiskLevel == "CRITICAL") | (HiraEntry.residualRiskLevel == "CRITICAL"))
    )
    eai_stmt = (
        select(EaiStudy.number, EaiEntry.sequenceNumber, EaiEntry.activityDescription)
        .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
        .where(EaiEntry.isCurrentVersion.is_(True))
        .where((EaiEntry.initialImpactLevel == "CRITICAL") | (EaiEntry.residualImpactLevel == "CRITICAL"))
    )
    if plant:
        hira_stmt = hira_stmt.where(HiraStudy.plantId == plant)
        eai_stmt = eai_stmt.where(EaiStudy.plantId == plant)

    for number, seq, activity in (await db.execute(hira_stmt)).all():
        bags.append((f"{number}#{seq}", set(keywords(activity))))
    for number, seq, activity in (await db.execute(eai_stmt)).all():
        bags.append((f"{number}#{seq}", set(keywords(activity))))
    return [(ref, bag) for ref, bag in bags if bag]


def _row_signals(active: list[Any], now: Any) -> list[Signal]:
    out: list[Signal] = []
    for r in active:
        cls = (r.classification or "").lower()
        if (r.status or "") == "draft" and cls in _SERIOUS_CLASS and (age_days(r.updatedAt) or 0) > _DRAFT_STALE_DAYS:
            days = age_days(r.updatedAt) or 0
            out.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.number,
                    kind="next_best_action",
                    severity="high" if cls == "critical" else "watch",
                    label=fill("signal.stalled_draft.label"),
                    evidence=fill("signal.stalled_draft.evidence", ref=r.number, cls=cls, days=days),
                    suggestedAction="Submit or withdraw this stalled change — a major change shouldn't linger in draft.",
                )
            )
    return out
