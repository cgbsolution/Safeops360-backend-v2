"""Near Miss insight rules (spec §2.3) — deterministic.

Bar:
  * predictive_risk — critical-potential near misses uninvestigated >7d
    (the highest-value insight on this screen; these precede LTIs).
  * trend — the near-miss : LTI ratio over 12 months, in plain language.
Row signals:
  * predictive_risk "Prioritize" — critical potential, still in REPORTED.

The NM:LTI line is phrased as a ratio the data supports, never a fabricated
correlation claim (spec §2.3): it is suppressed when there are no LTIs to
divide by, rather than invented.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident
from app.models.near_miss import NearMiss
from app.schemas.insights import Insight, Signal
from app.services.insights.common import (
    age_days,
    confidence_for,
    now_naive,
    refs_str,
)
from app.services.insights.templates import fill

_UNINVESTIGATED_DAYS = 7
_WINDOW_DAYS = 366


async def compute_nearmiss(
    db: AsyncSession,
    *,
    plant: str | None,
    date_from: Any = None,
    date_to: Any = None,
) -> tuple[list[Insight], list[Signal], int]:
    now = now_naive().replace(microsecond=0)
    window_start = now - timedelta(days=_WINDOW_DAYS)

    stmt = (
        select(
            NearMiss.id,
            NearMiss.number,
            NearMiss.date,
            NearMiss.plantId,
            cast(NearMiss.potentialSeverity, String).label("severity"),
            cast(NearMiss.status, String).label("status"),
            NearMiss.createdAt,
        )
        .where(NearMiss.date >= window_start)
        .order_by(NearMiss.date.desc())
        .limit(600)
    )
    if plant:
        stmt = stmt.where(NearMiss.plantId == plant)
    rows = (await db.execute(stmt)).all()
    record_count = len(rows)
    if not rows:
        return [], [], 0

    bar: list[Insight] = []

    crit_uninv = _critical_uninvestigated(rows)
    if crit_uninv:
        bar.append(crit_uninv)

    ratio = await _nm_lti_ratio(db, plant=plant, nm_count=record_count, window_start=window_start)
    if ratio:
        bar.append(ratio)

    signals = _row_signals(rows)
    return bar, signals, record_count


def _critical_uninvestigated(rows: list[Any]) -> Insight | None:
    crit = [r for r in rows if (r.severity or "").upper() == "CRITICAL"]
    if not crit:
        return None
    stale = [
        r
        for r in crit
        if (r.status or "") == "REPORTED" and (age_days(r.createdAt) or 0) > _UNINVESTIGATED_DAYS
    ]
    if not stale:
        return None
    refs = [r.number for r in stale]
    n_stale, n_crit = len(stale), len(crit)
    return Insight(
        id="nearmiss:predictive:critical-uninvestigated",
        kind="predictive_risk",
        severity="critical" if n_stale >= 3 else "high",
        headline=fill("nearmiss.critical.uninvestigated", stale=n_stale, crit=n_crit),
        evidence=fill(
            "nearmiss.critical.uninvestigated.evidence",
            crit=n_crit,
            stale=n_stale,
            refs=refs_str(refs),
        ),
        recordRefs=refs,
        suggestedAction=fill("nearmiss.critical.uninvestigated.action"),
        confidence=confidence_for(n_crit),
        # Critical-potential near misses precede LTIs — this is the leading
        # PSI/SIF indicator the Executive Sentinel ranks highest (spec §2).
        seriousPotential=True,
    )


async def _nm_lti_ratio(
    db: AsyncSession, *, plant: str | None, nm_count: int, window_start: Any
) -> Insight | None:
    lti_stmt = (
        select(func.count())
        .select_from(Incident)
        .where(Incident.isDeleted.is_(False))
        .where(Incident.date >= window_start)
        .where(cast(Incident.type, String).in_(["LTI", "FATALITY"]))
    )
    if plant:
        lti_stmt = lti_stmt.where(Incident.plantId == plant)
    lti = int((await db.execute(lti_stmt)).scalar_one() or 0)
    # No LTI to divide by → suppress rather than fabricate a ratio (spec §2.3).
    if lti <= 0 or (nm_count + lti) < 5:
        return None
    ratio = round(nm_count / lti)
    return Insight(
        id="nearmiss:trend:nm-lti-ratio",
        kind="trend",
        severity="info",
        headline=fill("nearmiss.ratio.nm_lti", ratio=ratio, nm=nm_count, lti=lti),
        evidence=fill("nearmiss.ratio.nm_lti.evidence", nm=nm_count, lti=lti),
        recordRefs=[],
        suggestedAction="A high ratio is healthy reporting; a falling ratio means near misses are going unlogged.",
        confidence=confidence_for(nm_count + lti),
    )


def _row_signals(rows: list[Any]) -> list[Signal]:
    out: list[Signal] = []
    for r in rows:
        if (r.severity or "").upper() == "CRITICAL" and (r.status or "") == "REPORTED":
            out.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.number,
                    kind="predictive_risk",
                    severity="high",
                    label=fill("signal.prioritize.label"),
                    evidence=fill("signal.prioritize.evidence", ref=r.number),
                    suggestedAction="Move it into review before it recurs.",
                )
            )
    return out
