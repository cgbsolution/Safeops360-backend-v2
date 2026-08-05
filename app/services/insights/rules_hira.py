"""HIRA Study insight rules (spec §2.4) — deterministic.

The HIRA list screen shows STUDIES, so every recordRef here is a study `number`.

Bar:
  * overdue_escalation — in-force studies whose next scheduled review falls
    inside 30 days (or is already past), named specifically.
  * cluster — the same hazard category live in studies at ≥2 plants, so review
    cycles can be synced instead of run independently (cross-plant only).
Row signals (0-1 per study, highest-priority wins):
  * anomaly "Unmitigated critical" — the study holds a current entry whose
    residual risk is still CRITICAL.
  * next_best_action "Nudge team lead" — DRAFT study with no activity >30d.

Every number traces to a counted field; no model calls.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.hira import HiraEntry, HiraEntryHazard, HiraHazard, HiraStudy
from app.schemas.insights import Insight, Signal
from app.services.insights.common import age_days, as_naive, confidence_for, now_naive, refs_str
from app.services.insights.templates import fill

_IN_FORCE = ("ACTIVE", "APPROVED")
_REVIEW_DUE_DAYS = 30
_DRAFT_STALE_DAYS = 30


def _slug(*parts: str) -> str:
    return ":".join(p.lower().replace(" ", "-")[:24] for p in parts if p)


async def compute_hira(
    db: AsyncSession,
    *,
    plant: str | None,
    date_from: Any = None,
    date_to: Any = None,
) -> tuple[list[Insight], list[Signal], int]:
    now = now_naive().replace(microsecond=0)

    stmt = (
        select(
            HiraStudy.id,
            HiraStudy.number,
            HiraStudy.plantId,
            HiraStudy.status,
            HiraStudy.nextScheduledReviewDate,
            HiraStudy.updatedAt,
        )
        .order_by(HiraStudy.updatedAt.desc())
        .limit(400)
    )
    if plant:
        stmt = stmt.where(HiraStudy.plantId == plant)
    rows = (await db.execute(stmt)).all()
    record_count = len(rows)
    if not rows:
        return [], [], 0

    plant_names: dict[str, str] = dict(
        (await db.execute(text('SELECT id, name FROM "Plant"'))).all()
    )
    study_ids = [r.id for r in rows]

    # Studies holding a current entry still at CRITICAL residual risk.
    crit_study_ids: set[str] = set()
    for (sid,) in (
        await db.execute(
            select(HiraEntry.studyId)
            .where(HiraEntry.studyId.in_(study_ids))
            .where(HiraEntry.isCurrentVersion.is_(True))
            .where(HiraEntry.residualRiskLevel == "CRITICAL")
            .distinct()
        )
    ).all():
        crit_study_ids.add(sid)

    bar: list[Insight] = []
    review = _review_due_insight(rows, now)
    if review:
        bar.append(review)
    if not plant:
        cluster = await _hazard_cluster_insight(db, study_ids, plant_names)
        if cluster:
            bar.append(cluster)

    signals = _row_signals(rows, crit_study_ids, now)
    return bar, signals, record_count


def _review_due_insight(rows: list[Any], now: Any) -> Insight | None:
    horizon = now + timedelta(days=_REVIEW_DUE_DAYS)
    due: list[tuple[Any, Any]] = []
    for r in rows:
        if (r.status or "") not in _IN_FORCE:
            continue
        d = as_naive(r.nextScheduledReviewDate)
        if d is not None and d <= horizon:
            due.append((r, d))
    if not due:
        return None
    due.sort(key=lambda rd: rd[1])  # soonest / most-overdue first
    refs = [r.number for r, _ in due]
    soonest_row, soonest_date = due[0]
    days = (soonest_date - now).days
    overdue = days < 0
    return Insight(
        id="hira:overdue:review-due",
        kind="overdue_escalation",
        severity="high" if overdue else "watch",
        headline=fill(
            "hira.review.overdue" if overdue else "hira.review.soon",
            count=len(due),
            soonest_ref=soonest_row.number,
            days=abs(days),
        ),
        evidence=fill("hira.review.evidence", count=len(due), refs=refs_str(refs)),
        recordRefs=refs,
        suggestedAction="Schedule the review team now so the study doesn't lapse out of currency.",
        confidence=confidence_for(len(due)),
    )


async def _hazard_cluster_insight(
    db: AsyncSession, study_ids: list[str], plant_names: dict[str, str]
) -> Insight | None:
    """Same hazard category live in studies across ≥2 plants."""
    rows = (
        await db.execute(
            select(
                HiraHazard.category,
                HiraStudy.plantId,
                HiraStudy.number,
            )
            .select_from(HiraEntryHazard)
            .join(HiraEntry, HiraEntry.id == HiraEntryHazard.entryId)
            .join(HiraStudy, HiraStudy.id == HiraEntry.studyId)
            .join(HiraHazard, HiraHazard.id == HiraEntryHazard.hazardId)
            .where(HiraStudy.id.in_(study_ids))
            .where(HiraStudy.status.in_(_IN_FORCE))
            .where(HiraEntry.isCurrentVersion.is_(True))
        )
    ).all()
    by_cat_plants: dict[str, set[str]] = {}
    by_cat_studies: dict[str, set[str]] = {}
    for category, plant_id, number in rows:
        by_cat_plants.setdefault(category, set()).add(plant_id)
        by_cat_studies.setdefault(category, set()).add(number)

    # Pick the category spanning the most plants (≥2).
    candidates = [(c, p) for c, p in by_cat_plants.items() if len(p) >= 2]
    if not candidates:
        return None
    category, plants = max(candidates, key=lambda cp: len(cp[1]))
    refs = sorted(by_cat_studies[category])
    cat_label = category.replace("_", " ").title()
    plant_labels = ", ".join(sorted(plant_names.get(p, p) for p in plants))
    return Insight(
        id=_slug("hira", "cluster", category),
        kind="cluster",
        severity="watch",
        headline=fill("hira.cluster.hazard", category=cat_label, plants=len(plants)),
        evidence=fill(
            "hira.cluster.hazard.evidence",
            category=cat_label,
            plant_list=plant_labels,
            refs=refs_str(refs),
        ),
        recordRefs=refs,
        suggestedAction="Sync the review cycles for this hazard so controls stay consistent across plants.",
        confidence=confidence_for(len(refs)),
    )


def _row_signals(rows: list[Any], crit_study_ids: set[str], now: Any) -> list[Signal]:
    out: list[Signal] = []
    for r in rows:
        if r.id in crit_study_ids:
            out.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.number,
                    kind="anomaly",
                    severity="high",
                    label=fill("signal.unmitigated_critical.label"),
                    evidence=fill("signal.unmitigated_critical.evidence", ref=r.number),
                    suggestedAction="Open the study and raise a CAPA against the critical-residual entry.",
                )
            )
        elif (r.status or "") == "DRAFT" and (age_days(r.updatedAt) or 0) > _DRAFT_STALE_DAYS:
            days = age_days(r.updatedAt) or 0
            out.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.number,
                    kind="next_best_action",
                    severity="watch",
                    label=fill("signal.nudge_lead.label"),
                    evidence=fill("signal.nudge_lead.evidence", ref=r.number, days=days),
                    suggestedAction="Nudge the team leader to finish and submit the draft study.",
                )
            )
    return out
