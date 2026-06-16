"""Tool: find_related_observations.

Surfaces observations that may have been "missed warnings" — unsafe
acts/conditions reported in the same area/category in the days/weeks
preceding the incident. The hindsight test: was someone trying to tell
us this was about to happen?

Scoped to the same plant by default; agent can widen via plantScope.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident
from app.models.near_miss import NearMiss
from app.models.observation import Observation, ObservationCategory


DEFINITION: dict[str, Any] = {
    "name": "find_related_observations",
    "description": (
        "Find safety observations reported in the same area or category in the "
        "window leading up to the incident. Use this to identify 'missed warnings' "
        "— unsafe acts or conditions that were flagged but not acted on. Defaults "
        "to a 90-day lookback window and the source incident's plant. Returns up "
        "to 10 most recent observations."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": (
                    "Optional category filter. One of: PPE, HOUSEKEEPING, "
                    "WORK_AT_HEIGHT, HOT_WORK, MOBILE_EQUIPMENT, ELECTRICAL, "
                    "MATERIAL_HANDLING, CONFINED_SPACE, CHEMICAL_HANDLING, "
                    "EMERGENCY, OTHER."
                ),
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional keywords to match against the observation description "
                    "(case-insensitive substring). Use 2-5 distinctive words."
                ),
            },
            "lookbackDays": {
                "type": "integer",
                "description": "Days before the incident to look back. Default 90, max 365.",
                "minimum": 1,
                "maximum": 365,
            },
            "plantScope": {
                "type": "string",
                "description": (
                    "How wide to cast the net for plants. 'SAME_PLANT' (default) "
                    "matches only the source incident's plant. 'ALL_PLANTS' looks "
                    "across the whole organisation — use sparingly, it generates "
                    "noise."
                ),
                "enum": ["SAME_PLANT", "ALL_PLANTS"],
            },
        },
        "required": [],
    },
}


async def handle(
    input: dict[str, Any],  # noqa: A002
    *,
    db: AsyncSession,
    source_record_id: str,
    source_module: str,
) -> dict[str, Any]:
    """Return matching observations + the search parameters used so the
    model can be transparent about what window it examined."""
    if source_module != "INCIDENT":
        # The "incident window" framing only applies when the source is
        # an incident. For other modules, surface the limitation rather
        # than silently anchor on the wrong time pivot.
        raise ValueError(
            f"find_related_observations is incident-centric; called with source_module={source_module}"
        )

    incident = await db.get(Incident, source_record_id)
    if incident is None:
        return {"observations": [], "note": "Source incident not found"}

    lookback_days = min(int(input.get("lookbackDays", 90)), 365)
    plant_scope = input.get("plantScope", "SAME_PLANT")
    keywords = input.get("keywords") or []
    category = input.get("category")

    anchor = incident.occurredAt or incident.date
    if anchor is None:
        anchor = datetime.now(timezone.utc)
    window_start = anchor - timedelta(days=lookback_days)

    stmt = (
        select(Observation)
        .where(Observation.date >= window_start)
        .where(Observation.date <= anchor)
    )
    if plant_scope == "SAME_PLANT":
        stmt = stmt.where(Observation.plantId == incident.plantId)

    if category:
        valid = {c.value for c in ObservationCategory}
        if category not in valid:
            raise ValueError(f"Unknown category {category!r}. Valid: {sorted(valid)}")
        stmt = stmt.where(Observation.category == ObservationCategory(category))

    if keywords:
        clauses = []
        for kw in keywords:
            pattern = f"%{kw}%"
            clauses.append(Observation.description.ilike(pattern))
        stmt = stmt.where(or_(*clauses))

    stmt = stmt.order_by(Observation.date.desc()).limit(10)
    rows = (await db.execute(stmt)).scalars().all()

    return {
        "searchWindowDays": lookback_days,
        "anchorDate": anchor.isoformat() if isinstance(anchor, datetime) else None,
        "plantScope": plant_scope,
        "observations": [
            {
                "observationNumber": r.number,
                "date": r.date.isoformat() if isinstance(r.date, datetime) else None,
                "type": _enum_value(r.type),
                "category": _enum_value(r.category),
                "severity": _enum_value(r.severity),
                "status": _enum_value(r.status),
                "descriptionPreview": (r.description or "")[:240],
                "immediateAction": r.immediateAction,
                "riskScore": r.riskScore,
            }
            for r in rows
        ],
    }


def _enum_value(v: Any) -> str | None:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)
