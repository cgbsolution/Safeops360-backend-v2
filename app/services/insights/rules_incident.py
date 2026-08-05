"""Incident Investigation insight rules (spec §2.1) — deterministic.

Bar:
  * cluster — open incidents at a plant sharing a root-cause keyword (≥3).
  * overdue_escalation — investigations stalled >30d, oldest named.
Row signals (0-1 per record):
  * predictive_risk "No CAPA linked" — an open investigation with 0 CAPAs
    (0% CAPA linkage is itself the red flag, spec §2.1).
  * next_best_action "RCA overdue" — sat in investigation >14d.

Every number in every headline traces to a counted field below. No model calls.
This layer complements the record-level incident-intelligence services
(incident_similarity / severityDetail); it aggregates them to the LIST screen.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident, IncidentCapa
from app.schemas.insights import Insight, Signal
from app.services.insights.common import (
    age_days,
    confidence_for,
    keywords,
    now_naive,
    refs_str,
)
from app.services.insights.templates import fill

_OPEN_STATUSES = {"REPORTED", "INVESTIGATION", "CAPA_ASSIGNED"}
_STALLED_DAYS = 30
_RCA_OVERDUE_DAYS = 14
_WINDOW_DAYS = 550  # ~18 months, bounds the scan


def _slug(*parts: str) -> str:
    return ":".join(p.lower().replace(" ", "-")[:24] for p in parts if p)


async def compute_incident(
    db: AsyncSession,
    *,
    plant: str | None,
    date_from: Any = None,  # reserved (filter parity with the list view)
    date_to: Any = None,
) -> tuple[list[Insight], list[Signal], int]:
    cutoff = now_naive().replace(microsecond=0)
    from datetime import timedelta

    window_start = cutoff - timedelta(days=_WINDOW_DAYS)

    stmt = (
        select(
            Incident.id,
            Incident.number,
            cast(Incident.type, String).label("type"),
            cast(Incident.status, String).label("status"),
            Incident.severity,
            Incident.plantId,
            Incident.immediateCauses,
            Incident.rootCauses,
            Incident.description,
            Incident.triggeredCapaIds,
            Incident.updatedAt,
            Incident.createdAt,
        )
        .where(Incident.isDeleted.is_(False))
        .where(Incident.date >= window_start)
        .order_by(Incident.date.desc())
        .limit(600)
    )
    if plant:
        stmt = stmt.where(Incident.plantId == plant)
    rows = (await db.execute(stmt)).all()
    record_count = len(rows)
    if not rows:
        return [], [], 0

    # CAPA linkage per incident (child rows) — one round-trip.
    inc_ids = [r.id for r in rows]
    capa_counts: dict[str, int] = {}
    for iid, n in (
        await db.execute(
            select(IncidentCapa.incidentId, func.count())
            .where(IncidentCapa.incidentId.in_(inc_ids))
            .group_by(IncidentCapa.incidentId)
        )
    ).all():
        capa_counts[iid] = n

    plant_names: dict[str, str] = dict(
        (await db.execute(text('SELECT id, name FROM "Plant"'))).all()
    )

    open_rows = [r for r in rows if (r.status or "") in _OPEN_STATUSES]

    bar: list[Insight] = []
    bar.extend(_cluster_insights(open_rows, plant_names))
    overdue = _overdue_insight(open_rows)
    if overdue:
        bar.append(overdue)

    signals = _row_signals(open_rows, capa_counts)
    return bar, signals, record_count


def _capa_linked(row: Any, capa_counts: dict[str, int]) -> bool:
    return capa_counts.get(row.id, 0) > 0 or bool(row.triggeredCapaIds)


def _cluster_insights(open_rows: list[Any], plant_names: dict[str, str]) -> list[Insight]:
    by_plant: dict[str, list[Any]] = {}
    for r in open_rows:
        by_plant.setdefault(r.plantId, []).append(r)

    out: list[Insight] = []
    for plant_id, group in by_plant.items():
        if len(group) < 3:
            continue
        # incidents-per-keyword (distinct incidents, not token frequency).
        # Grounded ONLY in the structured cause arrays — the free-text
        # `description` is deliberately excluded: it narrates lost-time ("off
        # work 5 days") so generic duration words (days/weeks/hours) used to win
        # the frequency count and render "share a cause pattern: days" (the §2
        # slot-fill bug). A cause cluster must key off a real cause term, not
        # incidental narrative. `common._STOPWORDS` also now drops those units.
        token_refs: dict[str, list[str]] = {}
        for r in group:
            for tok in set(keywords(r.immediateCauses, r.rootCauses)):
                token_refs.setdefault(tok, []).append(r.number)
        candidates = sorted(
            ((tok, refs) for tok, refs in token_refs.items() if len(refs) >= 3),
            key=lambda kv: (-len(kv[1]), kv[0]),
        )
        if not candidates:
            continue
        keyword, refs = candidates[0]
        plant_label = plant_names.get(plant_id, "this plant")
        count, total = len(refs), len(group)
        # Fatal/serious-injury potential (PSI/SIF) — an LTI/fatality-class or
        # CRITICAL-severity member makes this cluster a leader-level signal.
        ref_set = set(refs)
        serious = any(
            ((r.type or "") in {"LTI", "FATALITY"}) or ((r.severity or "").upper() == "CRITICAL")
            for r in group
            if r.number in ref_set
        )
        out.append(
            Insight(
                id=_slug("incident", "cluster", plant_id, keyword),
                kind="cluster",
                severity="high" if (count >= 4 or serious) else "watch",
                headline=fill(
                    "incident.cluster.rootcause",
                    count=count,
                    total=total,
                    keyword=keyword,
                    plant=plant_label,
                ),
                evidence=fill(
                    "incident.cluster.rootcause.evidence",
                    count=count,
                    plant=plant_label,
                    keyword=keyword,
                    refs=refs_str(refs),
                ),
                recordRefs=refs,
                suggestedAction="Group these under one investigation theme and check for a common control gap.",
                confidence=confidence_for(count),
                seriousPotential=serious,
            )
        )
    return out


def _overdue_insight(open_rows: list[Any]) -> Insight | None:
    stalled = [
        (r, age_days(r.updatedAt))
        for r in open_rows
        if (age_days(r.updatedAt) or 0) > _STALLED_DAYS
    ]
    if not stalled:
        return None
    stalled.sort(key=lambda ra: ra[1] or 0, reverse=True)
    refs = [r.number for r, _ in stalled]
    oldest_row, oldest_days = stalled[0]
    count = len(stalled)
    return Insight(
        id=_slug("incident", "overdue", "stalled"),
        kind="overdue_escalation",
        severity="high" if (oldest_days or 0) >= 60 else "watch",
        headline=fill(
            "incident.overdue.stalled",
            count=count,
            days=_STALLED_DAYS,
            oldest_ref=oldest_row.number,
            oldest_days=oldest_days,
        ),
        evidence=fill(
            "incident.overdue.stalled.evidence",
            count=count,
            days=_STALLED_DAYS,
            refs=refs_str(refs),
        ),
        recordRefs=refs,
        suggestedAction=fill(
            "incident.overdue.stalled.action",
            oldest_ref=oldest_row.number,
            oldest_days=oldest_days,
        ),
        confidence=confidence_for(count),
    )


def _row_signals(open_rows: list[Any], capa_counts: dict[str, int]) -> list[Signal]:
    out: list[Signal] = []
    for r in open_rows:
        sev = (r.severity or "").upper()
        no_capa = not _capa_linked(r, capa_counts) and (r.status or "") in {
            "INVESTIGATION",
            "CAPA_ASSIGNED",
        }
        rca_age = age_days(r.updatedAt) if (r.status or "") == "INVESTIGATION" else None
        rca_overdue = rca_age is not None and rca_age > _RCA_OVERDUE_DAYS

        # One signal per row — the higher-severity finding wins.
        if no_capa and sev in {"HIGH", "CRITICAL"}:
            out.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.number,
                    kind="predictive_risk",
                    severity="high",
                    label=fill("signal.no_capa.label"),
                    evidence=fill("signal.no_capa.evidence", ref=r.number),
                    suggestedAction="Raise a CAPA against the identified cause.",
                )
            )
        elif rca_overdue:
            out.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.number,
                    kind="next_best_action",
                    severity="watch",
                    label=fill("signal.rca_overdue.label"),
                    evidence=fill("signal.rca_overdue.evidence", ref=r.number, days=rca_age),
                    suggestedAction=fill("signal.rca_overdue.action"),
                )
            )
        elif no_capa:
            out.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.number,
                    kind="predictive_risk",
                    severity="watch",
                    label=fill("signal.no_capa.label"),
                    evidence=fill("signal.no_capa.evidence", ref=r.number),
                    suggestedAction="Raise a CAPA against the identified cause.",
                )
            )
    return out
