"""Tool: find_similar_incidents.

Searches CLOSED incidents that share characteristics with the one the
agent is analysing. The model uses these to surface patterns ("similar
incidents at sister plants had isolation-verification as root cause")
and to verify hypotheses ("does our hypothesis match the proven RCs
of comparable past events?").

Field mapping note: the brief's example referenced fields like
`incidentNumber`, `incidentType`, `hazardCategory`, `rcaMethod`,
`equipmentCategoryId`. The real schema uses `number`, `type`, no
hazardCategory column (it's on NearMiss only), `rootCauseMethod`, and
free-text `equipment.category`. We adapt the parameter surface to
what's actually queryable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.equipment import Equipment
from app.models.incident import (
    Incident,
    IncidentCapa,
    IncidentEquipment,
    IncidentStatus,
    IncidentType,
)
from app.models.plant import Plant


DEFINITION: dict[str, Any] = {
    "name": "find_similar_incidents",
    "description": (
        "Search past CLOSED incidents that share characteristics with the "
        "current incident. Returns each match with its incident number, "
        "type, severity, plant, the RCA method that was used, the root "
        "causes that were identified, and the average effectiveness rating "
        "of its CAPAs. Use this early in your analysis to find patterns. "
        "Only returns incidents whose investigation has been completed — "
        "in-flight investigations are excluded so you don't anchor on "
        "unverified hypotheses."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "incidentType": {
                "type": "string",
                "description": (
                    "Type code of the incident family to match against. One of: "
                    "FIRST_AID, MTC, RWC, LTI, FATALITY, PROPERTY_DAMAGE, "
                    "ENVIRONMENTAL, FIRE, PROCESS_SAFETY, HIPO_NEAR_MISS."
                ),
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional keywords to match against the incident description "
                    "and root-cause summary (case-insensitive substring match). "
                    "Use 2-5 distinctive words from the current incident — e.g. "
                    "['gearbox', 'isolation', 'maintenance']. Avoid generic words "
                    "('worker', 'plant') which match too broadly."
                ),
            },
            "equipmentCategory": {
                "type": "string",
                "description": (
                    "Optional Equipment.category match (e.g. 'Kiln', 'Mill', "
                    "'Mobile Equipment'). Filters to incidents whose involved "
                    "equipment was in this category. Free-text exact match."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max rows to return. Defaults to 5, hard cap 10.",
                "minimum": 1,
                "maximum": 10,
            },
        },
        "required": ["incidentType"],
    },
}


async def handle(
    input: dict[str, Any],  # noqa: A002
    *,
    db: AsyncSession,
    source_record_id: str,
    source_module: str,
) -> list[dict[str, Any]]:
    """Return matching incidents as a JSON-serialisable list."""
    incident_type = input["incidentType"]
    # Validate against the enum so a typo from the model surfaces as a
    # tool error instead of silently matching nothing.
    valid_types = {t.value for t in IncidentType}
    if incident_type not in valid_types:
        raise ValueError(
            f"Unknown incidentType {incident_type!r}. Valid values: {sorted(valid_types)}"
        )

    keywords = input.get("keywords") or []
    equipment_category = input.get("equipmentCategory")
    limit = min(int(input.get("limit", 5)), 10)

    stmt = (
        select(Incident)
        .where(Incident.status == IncidentStatus.CLOSED)
        .where(Incident.type == IncidentType(incident_type))
        # Exclude the source incident itself if the model passes its own number.
        .where(Incident.id != source_record_id)
        .options(
            selectinload(Incident.equipmentInvolved).selectinload(
                IncidentEquipment.incident
            ),
        )
    )

    if equipment_category:
        # Subquery: incident IDs whose equipmentInvolved.equipment.category
        # matches. Using EXISTS keeps the main query single-row-per-incident.
        equipment_subq = (
            select(IncidentEquipment.incidentId)
            .join(Equipment, Equipment.id == IncidentEquipment.equipmentId)
            .where(Equipment.category == equipment_category)
        )
        stmt = stmt.where(Incident.id.in_(equipment_subq))

    if keywords:
        # OR across keywords; each keyword can match description OR
        # rootCauseSummary. Postgres ILIKE for case-insensitive substring.
        keyword_clauses = []
        for kw in keywords:
            pattern = f"%{kw}%"
            keyword_clauses.append(
                or_(
                    Incident.description.ilike(pattern),
                    Incident.initialDescription.ilike(pattern),
                    Incident.rootCauseSummary.ilike(pattern),
                )
            )
        stmt = stmt.where(or_(*keyword_clauses))

    # Sort by most-recently-occurred so the model anchors on recent
    # patterns, not ancient history.
    stmt = stmt.order_by(Incident.occurredAt.desc().nulls_last(), Incident.date.desc()).limit(limit)

    rows = (await db.execute(stmt)).scalars().unique().all()

    if not rows:
        return []

    # Pull plant names + capa effectiveness in a follow-up batch query.
    # Keeps the main query simple and lets us aggregate capas in Python.
    plant_ids = {r.plantId for r in rows}
    plants_by_id: dict[str, str] = dict(
        (await db.execute(select(Plant.id, Plant.name).where(Plant.id.in_(plant_ids)))).all()
    )

    incident_ids = [r.id for r in rows]
    capas = (
        (
            await db.execute(
                select(IncidentCapa.incidentId, IncidentCapa.effectivenessRating).where(
                    IncidentCapa.incidentId.in_(incident_ids)
                )
            )
        )
        .all()
    )
    # incidentId -> list of effectiveness ratings (filter None)
    capas_by_incident: dict[str, list[int]] = {}
    capa_counts: dict[str, int] = {}
    for incident_id, rating in capas:
        capa_counts[incident_id] = capa_counts.get(incident_id, 0) + 1
        if rating is not None:
            capas_by_incident.setdefault(incident_id, []).append(int(rating))

    results: list[dict[str, Any]] = []
    for r in rows:
        ratings = capas_by_incident.get(r.id, [])
        avg_eff = round(sum(ratings) / len(ratings), 2) if ratings else None
        occurred = r.occurredAt or r.date
        description = r.initialDescription or r.description or ""
        results.append(
            {
                "incidentNumber": r.number,
                "incidentType": _enum_value(r.type),
                "severity": r.severity,
                "occurredAt": occurred.isoformat() if isinstance(occurred, datetime) else None,
                "plantName": plants_by_id.get(r.plantId),
                "descriptionPreview": description[:300],
                "rootCauseMethod": r.rootCauseMethod,
                "rootCauseSummary": r.rootCauseSummary,
                "rootCauses": r.rootCauses or [],
                "capaCount": capa_counts.get(r.id, 0),
                "averageCapaEffectiveness": avg_eff,
            }
        )
    return results


def _enum_value(v: Any) -> str | None:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)
