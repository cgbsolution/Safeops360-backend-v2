"""Incident similarity — the rule-based trend/pattern matcher (Slice-1 seed).

This is the minimal, synchronous, rule-based matcher that Feature 2 (AI
root-cause suggestion confidence) and Feature 5 (likelihood-of-recurrence +
the "3-in-90-days" escalation rule) both depend on. It is deliberately NOT
ML — a weighted MongoDB-style aggregation done as SQLAlchemy queries, fast
enough to run inline on save.

It is the forward-compatible foundation of the full Feature 3 trend engine:
when that ships, the scored output here can be blended with embedding
similarity without changing these call sites.

Scoring weights (per spec):
  • same equipmentId OR equipmentCategory   weight 3
  • same area / specificLocation            weight 2
  • overlapping immediate-cause tokens       weight 2
  • same incidentType                        weight 1
  • within trailing 12 months                weight 1
Raw max = 9 → normalised to 0-100. Matches below a 40-point floor are dropped.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.equipment import Equipment
from app.models.incident import Incident, IncidentEquipment, IncidentStatus

# Raw weights and the normalisation denominator.
_W_EQUIPMENT = 3
_W_AREA = 2
_W_CAUSE = 2
_W_TYPE = 1
_W_RECENCY = 1
_MAX_RAW = _W_EQUIPMENT + _W_AREA + _W_CAUSE + _W_TYPE + _W_RECENCY  # 9

MATCH_FLOOR = 40  # points, per spec
_CANDIDATE_CAP = 400  # bound the scan before Python scoring

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tokens(*values: Any) -> set[str]:
    """Normalised token set from free-text cause fields (list or scalar)."""
    out: set[str] = set()
    for v in values:
        if not v:
            continue
        items = v if isinstance(v, (list, tuple)) else [v]
        for item in items:
            if not item:
                continue
            out.update(t for t in _TOKEN_RE.findall(str(item).lower()) if len(t) > 2)
    return out


async def _equipment_profiles(
    db: AsyncSession, incident_ids: list[str]
) -> dict[str, dict[str, set[str]]]:
    """Map incidentId → {equipmentIds, categories} in one round-trip."""
    profiles: dict[str, dict[str, set[str]]] = {
        iid: {"equipmentIds": set(), "categories": set()} for iid in incident_ids
    }
    if not incident_ids:
        return profiles
    rows = (
        await db.execute(
            select(IncidentEquipment.incidentId, IncidentEquipment.equipmentId, Equipment.category)
            .join(Equipment, Equipment.id == IncidentEquipment.equipmentId, isouter=True)
            .where(IncidentEquipment.incidentId.in_(incident_ids))
        )
    ).all()
    for incident_id, equipment_id, category in rows:
        prof = profiles.setdefault(incident_id, {"equipmentIds": set(), "categories": set()})
        if equipment_id:
            prof["equipmentIds"].add(equipment_id)
        if category:
            prof["categories"].add(category)
    return profiles


async def similar_incidents(
    db: AsyncSession,
    incident: Incident,
    *,
    limit: int = 10,
    floor: int = MATCH_FLOOR,
    only_closed: bool = True,
    window_months: int = 12,
) -> list[dict[str, Any]]:
    """Return the top `limit` incidents most similar to `incident`, each with a
    normalised 0-100 `score` (>= `floor`), sorted descending.

    `only_closed=True` (the default, used by the AI suggestion) restricts to
    CLOSED incidents that already have structured cause data to learn from.
    """
    cutoff = _now() - timedelta(days=int(window_months * 30.4))

    stmt = (
        select(Incident)
        .where(Incident.id != incident.id)
        .where(Incident.deletedAt.is_(None))
        .where(Incident.date >= cutoff)
        .order_by(Incident.date.desc())
        .limit(_CANDIDATE_CAP)
    )
    if only_closed:
        stmt = stmt.where(Incident.status == IncidentStatus.CLOSED)
    candidates = (await db.execute(stmt)).scalars().all()
    if not candidates:
        return []

    profiles = await _equipment_profiles(db, [incident.id, *[c.id for c in candidates]])
    cur = profiles.get(incident.id, {"equipmentIds": set(), "categories": set()})
    cur_causes = _tokens(incident.immediateCauses, incident.immediateCause)
    cur_date = incident.date

    results: list[dict[str, Any]] = []
    for cand in candidates:
        raw = 0
        shared: list[str] = []
        prof = profiles.get(cand.id, {"equipmentIds": set(), "categories": set()})

        if (cur["equipmentIds"] & prof["equipmentIds"]) or (cur["categories"] & prof["categories"]):
            raw += _W_EQUIPMENT
            shared.append("equipment")
        if incident.areaId and cand.areaId and incident.areaId == cand.areaId:
            raw += _W_AREA
            shared.append("area")
        if cur_causes and (cur_causes & _tokens(cand.immediateCauses, cand.immediateCause)):
            raw += _W_CAUSE
            shared.append("immediate cause")
        if incident.type == cand.type:
            raw += _W_TYPE
            shared.append("type")
        if cur_date and cand.date and abs((cur_date - cand.date).days) <= 365:
            raw += _W_RECENCY

        if raw == 0:
            continue
        score = round(raw * 100 / _MAX_RAW)
        if score < floor:
            continue
        results.append(
            {
                "incidentId": cand.id,
                "number": cand.number,
                "score": score,
                "sharedFactors": shared,
                "type": cand.type.value if cand.type else None,
                "severity": cand.severity,
                "date": cand.date.isoformat() if cand.date else None,
                "status": cand.status.value if cand.status else None,
                # For AI learning + confidence: the retrieved root causes.
                "rootCauses": list(cand.rootCauses or []),
            }
        )

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


async def incidents_sharing_equipment(
    db: AsyncSession,
    *,
    categories: set[str],
    equipment_ids: set[str],
    plant_id: str | None,
    window_days: int = 90,
    exclude_incident_id: str | None = None,
) -> list[str]:
    """Distinct incident ids in the trailing `window_days` that involve the same
    equipment category (or specific equipment), scoped to the same plant. Powers
    Feature 5's recurrence count + the Feature 3→5 auto-recalc fan-out."""
    if not categories and not equipment_ids:
        return []
    cutoff = _now() - timedelta(days=window_days)

    stmt = (
        select(IncidentEquipment.incidentId)
        .join(Incident, Incident.id == IncidentEquipment.incidentId)
        .join(Equipment, Equipment.id == IncidentEquipment.equipmentId, isouter=True)
        .where(Incident.deletedAt.is_(None))
        .where(Incident.date >= cutoff)
    )
    if plant_id:
        stmt = stmt.where(Incident.plantId == plant_id)
    if exclude_incident_id:
        stmt = stmt.where(IncidentEquipment.incidentId != exclude_incident_id)

    conds = []
    if categories:
        conds.append(Equipment.category.in_(list(categories)))
    if equipment_ids:
        conds.append(IncidentEquipment.equipmentId.in_(list(equipment_ids)))
    if conds:
        from sqlalchemy import or_

        stmt = stmt.where(or_(*conds))

    rows = (await db.execute(stmt)).scalars().all()
    return sorted(set(rows))


async def recurrence_count(
    db: AsyncSession,
    *,
    categories: set[str],
    equipment_ids: set[str],
    plant_id: str | None,
    window_days: int = 90,
    exclude_incident_id: str | None = None,
) -> int:
    """Count of distinct incidents sharing equipment in the trailing window.
    Powers the "3+ in the same equipment category within 90 days" escalation."""
    ids = await incidents_sharing_equipment(
        db,
        categories=categories,
        equipment_ids=equipment_ids,
        plant_id=plant_id,
        window_days=window_days,
        exclude_incident_id=exclude_incident_id,
    )
    return len(ids)


async def equipment_footprint(db: AsyncSession, incident_id: str) -> dict[str, set[str]]:
    """Convenience: {equipmentIds, categories} for one incident."""
    profiles = await _equipment_profiles(db, [incident_id])
    return profiles.get(incident_id, {"equipmentIds": set(), "categories": set()})
