"""Combined Risk Register insight rules (spec §2.6) — deterministic.

This screen unions HIRA + EAI current-version entries (same shape as
GET /api/risk-register/combined) and is the natural home for cross-module
correlation. Each recordRef is the register row identity `NUMBER#SEQ`
(study number + entry sequence), matching what the list renders. It is
plant-scoped.

Bar:
  * anomaly — entries whose INITIAL risk was CRITICAL but whose residual is
    reduced to HIGH/MODERATE with NO linked CAPA. The mitigation may be real
    but undocumented — surface it to verify, don't assume.
  * cluster — an area hosting both HIRA and EAI entries, so a change flagged in
    MOC touching that area can be cross-referenced here.
Row signals:
  * anomaly "Not active-tracked" — a CRITICAL-initial entry still in DRAFT /
    APPROVED (not yet ACTIVE).

Every number traces to a counted field; no model calls.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.eai import EaiEntry, EaiEntryRecommendedControl, EaiStudy
from app.models.hira import HiraCapa, HiraEntry, HiraEntryRecommendedControl, HiraStudy
from app.schemas.insights import Insight, Signal
from app.services.insights.common import confidence_for, refs_str
from app.services.insights.templates import fill

_REDUCED_RESIDUAL = {"HIGH", "MODERATE", "MEDIUM"}


def _slug(*parts: str) -> str:
    return ":".join(p.lower().replace(" ", "-")[:24] for p in parts if p)


class _Row:
    __slots__ = ("id", "ref", "type", "area_id", "initial", "residual", "status", "has_capa")

    def __init__(self, id, ref, type_, area_id, initial, residual, status, has_capa):
        self.id = id
        self.ref = ref
        self.type = type_
        self.area_id = area_id
        self.initial = (initial or "").upper()
        self.residual = (residual or "").upper()
        self.status = status or ""
        self.has_capa = has_capa


async def compute_combined_risk(
    db: AsyncSession,
    *,
    plant: str | None,
    date_from: Any = None,
    date_to: Any = None,
) -> tuple[list[Insight], list[Signal], int]:
    hira_stmt = (
        select(
            HiraEntry.id,
            HiraEntry.sequenceNumber,
            HiraEntry.areaId,
            HiraEntry.initialRiskLevel,
            HiraEntry.residualRiskLevel,
            HiraEntry.status,
            HiraStudy.number,
        )
        .join(HiraStudy, HiraStudy.id == HiraEntry.studyId)
        .where(HiraEntry.isCurrentVersion.is_(True))
    )
    eai_stmt = (
        select(
            EaiEntry.id,
            EaiEntry.sequenceNumber,
            EaiEntry.areaId,
            EaiEntry.initialImpactLevel,
            EaiEntry.residualImpactLevel,
            EaiEntry.status,
            EaiStudy.number,
        )
        .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
        .where(EaiEntry.isCurrentVersion.is_(True))
    )
    if plant:
        hira_stmt = hira_stmt.where(HiraStudy.plantId == plant)
        eai_stmt = eai_stmt.where(EaiStudy.plantId == plant)

    hira_rows = (await db.execute(hira_stmt)).all()
    eai_rows = (await db.execute(eai_stmt)).all()

    hira_ids = [r.id for r in hira_rows]
    eai_ids = [r.id for r in eai_rows]

    capa_linked = await _capa_linked_ids(db, hira_ids, eai_ids)

    rows: list[_Row] = []
    for r in hira_rows:
        rows.append(
            _Row(r.id, f"{r.number}#{r.sequenceNumber}", "HIRA", r.areaId,
                 r.initialRiskLevel, r.residualRiskLevel, r.status, r.id in capa_linked)
        )
    for r in eai_rows:
        rows.append(
            _Row(r.id, f"{r.number}#{r.sequenceNumber}", "EAI", r.areaId,
                 r.initialImpactLevel, r.residualImpactLevel, r.status, r.id in capa_linked)
        )
    record_count = len(rows)
    if not rows:
        return [], [], 0

    bar: list[Insight] = []
    not_tracked = _not_active_tracked_insight(rows)
    if not_tracked:
        bar.append(not_tracked)
    anomaly = _reduced_no_capa_insight(rows)
    if anomaly:
        bar.append(anomaly)
    cluster = await _area_cluster_insight(db, rows)
    if cluster:
        bar.append(cluster)

    signals = _row_signals(rows)
    return bar, signals, record_count


def _not_active_tracked_insight(rows: list[_Row]) -> Insight | None:
    """Bar-level promotion of the row 'Not active-tracked' signal (spec §2 card
    #3): CRITICAL-initial register entries still in DRAFT/APPROVED, not ACTIVE.
    A critical risk that no one is tracking is a leading, pre-incident gap."""
    hits = [r for r in rows if r.initial == "CRITICAL" and r.status != "ACTIVE"]
    if not hits:
        return None
    refs = [r.ref for r in hits]
    return Insight(
        id="combined-risk:anomaly:not-active-tracked",
        kind="anomaly",
        severity="high" if len(hits) >= 2 else "watch",
        headline=fill("combined.not_active_tracked", count=len(hits)),
        evidence=fill("combined.not_active_tracked.evidence", count=len(hits), refs=refs_str(refs)),
        recordRefs=refs,
        suggestedAction="Move these critical-initial risks to ACTIVE so they are tracked and reviewed.",
        confidence=confidence_for(len(hits)),
        seriousPotential=True,
    )


async def _capa_linked_ids(db: AsyncSession, hira_ids: list[str], eai_ids: list[str]) -> set[str]:
    linked: set[str] = set()
    if hira_ids:
        for (eid,) in (
            await db.execute(
                select(HiraCapa.entryId).where(HiraCapa.entryId.in_(hira_ids)).distinct()
            )
        ).all():
            linked.add(eid)
        for (eid,) in (
            await db.execute(
                select(HiraEntryRecommendedControl.entryId)
                .where(HiraEntryRecommendedControl.entryId.in_(hira_ids))
                .where(HiraEntryRecommendedControl.capaId.isnot(None))
                .distinct()
            )
        ).all():
            linked.add(eid)
    if eai_ids:
        for (eid,) in (
            await db.execute(
                select(EaiEntryRecommendedControl.entryId)
                .where(EaiEntryRecommendedControl.entryId.in_(eai_ids))
                .where(EaiEntryRecommendedControl.capaId.isnot(None))
                .distinct()
            )
        ).all():
            linked.add(eid)
    return linked


def _reduced_no_capa_insight(rows: list[_Row]) -> Insight | None:
    hits = [
        r
        for r in rows
        if r.initial == "CRITICAL" and r.residual in _REDUCED_RESIDUAL and not r.has_capa
    ]
    if not hits:
        return None
    refs = [r.ref for r in hits]
    return Insight(
        id="combined-risk:anomaly:reduced-no-capa",
        kind="anomaly",
        severity="high" if len(hits) >= 3 else "watch",
        headline=fill("combined.reduced_no_capa", count=len(hits)),
        evidence=fill("combined.reduced_no_capa.evidence", count=len(hits), refs=refs_str(refs)),
        recordRefs=refs,
        suggestedAction="Verify each mitigation is documented with a linked CAPA, not just assumed on the register.",
        confidence=confidence_for(len(hits)),
    )


async def _area_cluster_insight(db: AsyncSession, rows: list[_Row]) -> Insight | None:
    """An area carrying both HIRA and EAI risks — the cross-domain link a MOC
    touching that area should reference."""
    by_area: dict[str, list[_Row]] = {}
    for r in rows:
        if r.area_id:
            by_area.setdefault(r.area_id, []).append(r)
    # Areas with entries from BOTH modules.
    cross = [
        (area_id, members)
        for area_id, members in by_area.items()
        if {m.type for m in members} == {"HIRA", "EAI"}
    ]
    if not cross:
        return None
    area_id, members = max(cross, key=lambda am: len(am[1]))
    area_names: dict[str, str] = dict(
        (await db.execute(text('SELECT id, name FROM "Area"'))).all()
    )
    area_label = area_names.get(area_id, "one area")
    refs = [m.ref for m in members]
    return Insight(
        id=_slug("combined-risk", "cluster", area_id),
        kind="cluster",
        severity="watch",
        headline=fill("combined.area_cluster", count=len(members), area=area_label),
        evidence=fill("combined.area_cluster.evidence", area=area_label, refs=refs_str(refs)),
        recordRefs=refs,
        suggestedAction="When a MOC touches this area, cross-check these HIRA and EAI risks together.",
        confidence=confidence_for(len(members)),
    )


def _row_signals(rows: list[_Row]) -> list[Signal]:
    out: list[Signal] = []
    for r in rows:
        if r.initial == "CRITICAL" and r.status != "ACTIVE":
            out.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.ref,
                    kind="anomaly",
                    severity="high",
                    label=fill("signal.not_active.label"),
                    evidence=fill("signal.not_active.evidence", ref=r.ref, status=r.status or "draft"),
                    suggestedAction="Move this critical risk to ACTIVE so it is tracked and reviewed.",
                )
            )
    return out
