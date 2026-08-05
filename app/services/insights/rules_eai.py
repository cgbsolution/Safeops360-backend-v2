"""EAI — Environmental Register insight rules (spec §2.5) — deterministic.

The EAI list screen shows STUDIES, so every recordRef is a study `number`.
This screen is plant-scoped.

Bar:
  * overdue_escalation — compliance obligations whose next monitoring falls
    inside 30 days (or is already past), named by study. A silent miss here is
    expensive, so it is surfaced even when the tile reads 0.
  * predictive_risk — how many significant environmental aspects are live and
    across how many studies. Phrased as a present-state priority (a count that
    exists in the data), NOT a fabricated trend — spec §2.5 says suppress a
    trend claim when there is no review history to support it.
Row signals (0-1 per study, highest-priority wins):
  * overdue_escalation "Monitoring overdue" — a compliance obligation is past
    its next-monitoring date.
  * predictive_risk "Significant aspect" — holds a significant residual aspect.

Every number traces to a counted field; no model calls.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.eai import EaiComplianceObligation, EaiEntry, EaiStudy
from app.schemas.insights import Insight, Signal
from app.services.insights.common import as_naive, confidence_for, now_naive, refs_str
from app.services.insights.templates import fill

_MONITORING_DUE_DAYS = 30


async def compute_eai(
    db: AsyncSession,
    *,
    plant: str | None,
    date_from: Any = None,
    date_to: Any = None,
) -> tuple[list[Insight], list[Signal], int]:
    now = now_naive().replace(microsecond=0)

    stmt = (
        select(
            EaiStudy.id,
            EaiStudy.number,
            EaiStudy.plantId,
            EaiStudy.status,
        )
        .order_by(EaiStudy.updatedAt.desc())
        .limit(400)
    )
    if plant:
        stmt = stmt.where(EaiStudy.plantId == plant)
    rows = (await db.execute(stmt)).all()
    record_count = len(rows)
    if not rows:
        return [], [], 0

    study_ids = [r.id for r in rows]
    number_by_id = {r.id: r.number for r in rows}
    horizon = now + timedelta(days=_MONITORING_DUE_DAYS)

    # Compliance obligations due/overdue, mapped to their study.
    obligation_rows = (
        await db.execute(
            select(EaiStudy.id, EaiComplianceObligation.nextMonitoringDue)
            .select_from(EaiComplianceObligation)
            .join(EaiEntry, EaiEntry.id == EaiComplianceObligation.entryId)
            .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
            .where(EaiStudy.id.in_(study_ids))
            .where(EaiEntry.isCurrentVersion.is_(True))
        )
    ).all()
    due_study_ids: set[str] = set()
    overdue_study_ids: set[str] = set()
    for sid, next_due in obligation_rows:
        d = as_naive(next_due)
        if d is None:
            continue
        if d <= horizon:
            due_study_ids.add(sid)
        if d < now:
            overdue_study_ids.add(sid)

    # Studies holding a significant residual aspect (current version).
    sig_study_ids: set[str] = set()
    for (sid,) in (
        await db.execute(
            select(EaiEntry.studyId)
            .where(EaiEntry.studyId.in_(study_ids))
            .where(EaiEntry.isCurrentVersion.is_(True))
            .where(EaiEntry.residualSignificant.is_(True))
            .distinct()
        )
    ).all():
        sig_study_ids.add(sid)

    bar: list[Insight] = []
    obligations = _obligations_due_insight(due_study_ids, number_by_id, now, obligation_rows)
    if obligations:
        bar.append(obligations)
    significance = _significance_insight(sig_study_ids, number_by_id)
    if significance:
        bar.append(significance)

    signals = _row_signals(rows, overdue_study_ids, sig_study_ids)
    return bar, signals, record_count


def _obligations_due_insight(
    due_study_ids: set[str], number_by_id: dict[str, str], now: Any, obligation_rows: list[Any]
) -> Insight | None:
    if not due_study_ids:
        return None
    refs = sorted(number_by_id[sid] for sid in due_study_ids if sid in number_by_id)
    if not refs:
        return None
    # Count obligations (not studies) actually due, for the evidence line.
    horizon_count = sum(
        1
        for _, nd in obligation_rows
        if (d := as_naive(nd)) is not None and d <= now + timedelta(days=_MONITORING_DUE_DAYS)
    )
    return Insight(
        id="eai:overdue:monitoring-due",
        kind="overdue_escalation",
        severity="high",
        headline=fill("eai.obligation.due", obligations=horizon_count, studies=len(refs)),
        evidence=fill("eai.obligation.due.evidence", obligations=horizon_count, refs=refs_str(refs)),
        recordRefs=refs,
        suggestedAction="Book the monitoring before the deadline — a missed sampling window is a reportable lapse.",
        confidence=confidence_for(len(refs)),
    )


def _significance_insight(sig_study_ids: set[str], number_by_id: dict[str, str]) -> Insight | None:
    if len(sig_study_ids) < 2:
        return None
    refs = sorted(number_by_id[sid] for sid in sig_study_ids if sid in number_by_id)
    return Insight(
        id="eai:predictive:significant-aspects",
        kind="predictive_risk",
        severity="watch",
        headline=fill("eai.significance.count", studies=len(refs)),
        evidence=fill("eai.significance.count.evidence", studies=len(refs), refs=refs_str(refs)),
        recordRefs=refs,
        suggestedAction="Verify each significant aspect has a current control and monitoring plan.",
        confidence=confidence_for(len(refs)),
    )


def _row_signals(rows: list[Any], overdue_study_ids: set[str], sig_study_ids: set[str]) -> list[Signal]:
    out: list[Signal] = []
    for r in rows:
        if r.id in overdue_study_ids:
            out.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.number,
                    kind="overdue_escalation",
                    severity="high",
                    label=fill("signal.monitoring_overdue.label"),
                    evidence=fill("signal.monitoring_overdue.evidence", ref=r.number),
                    suggestedAction="Complete the overdue environmental monitoring for this study.",
                )
            )
        elif r.id in sig_study_ids:
            out.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.number,
                    kind="predictive_risk",
                    severity="watch",
                    label=fill("signal.significant_aspect.label"),
                    evidence=fill("signal.significant_aspect.evidence", ref=r.number),
                    suggestedAction="Confirm the significant aspect's control is documented and effective.",
                )
            )
    return out
