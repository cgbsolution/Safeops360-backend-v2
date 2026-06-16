"""Tool: find_related_near_misses.

Near misses are the gold-standard leading indicator. If a near miss
reported a similar hazard, with similar equipment, in similar conditions
— and we didn't act on it — that's a damning finding. This tool helps
the agent find that link.

Unlike observations (Tool 2), near misses carry potentialSeverity
(what could have happened) and hazardCategory (which Incident does not
have). Both are powerful filters.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident
from app.models.near_miss import NearMiss
from app.models.observation import Severity


DEFINITION: dict[str, Any] = {
    "name": "find_related_near_misses",
    "description": (
        "Find near misses that may have foreshadowed the incident. Returns "
        "near misses from the same plant in the lookback window, filtered "
        "optionally by hazardCategory, equipmentId, or keywords. Each result "
        "includes the potentialSeverity rating — a near miss with CRITICAL "
        "potential severity that was reported but not acted on is a strong "
        "systemic finding."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "hazardCategory": {
                "type": "string",
                "description": (
                    "Optional hazard category match against NearMiss.hazardCategory "
                    "(free-text master code, e.g. 'MECHANICAL', 'ELECTRICAL', "
                    "'CHEMICAL'). Exact match."
                ),
            },
            "equipmentId": {
                "type": "string",
                "description": (
                    "Optional equipment ID to match against NearMiss.equipmentId. "
                    "When set, only near misses involving this exact equipment "
                    "are returned."
                ),
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional keywords to match against the near miss description "
                    "(case-insensitive substring). Use 2-5 distinctive words."
                ),
            },
            "minPotentialSeverity": {
                "type": "string",
                "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
                "description": (
                    "Lower bound on potentialSeverity. Default is no filter. "
                    "Set to 'HIGH' or 'CRITICAL' to focus on near misses with "
                    "real risk."
                ),
            },
            "lookbackDays": {
                "type": "integer",
                "description": "Days before the incident to look back. Default 180, max 730.",
                "minimum": 1,
                "maximum": 730,
            },
        },
        "required": [],
    },
}


_SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


async def handle(
    input: dict[str, Any],  # noqa: A002
    *,
    db: AsyncSession,
    source_record_id: str,
    source_module: str,
) -> dict[str, Any]:
    if source_module != "INCIDENT":
        raise ValueError(
            f"find_related_near_misses is incident-centric; called with source_module={source_module}"
        )

    incident = await db.get(Incident, source_record_id)
    if incident is None:
        return {"nearMisses": [], "note": "Source incident not found"}

    lookback_days = min(int(input.get("lookbackDays", 180)), 730)
    anchor = incident.occurredAt or incident.date or datetime.now(timezone.utc)
    window_start = anchor - timedelta(days=lookback_days)

    stmt = (
        select(NearMiss)
        .where(NearMiss.plantId == incident.plantId)
        .where(NearMiss.date >= window_start)
        .where(NearMiss.date <= anchor)
    )

    if hazard := input.get("hazardCategory"):
        stmt = stmt.where(NearMiss.hazardCategory == hazard)

    if equipment_id := input.get("equipmentId"):
        stmt = stmt.where(NearMiss.equipmentId == equipment_id)

    if keywords := input.get("keywords"):
        clauses = [NearMiss.description.ilike(f"%{kw}%") for kw in keywords]
        stmt = stmt.where(or_(*clauses))

    if min_sev := input.get("minPotentialSeverity"):
        if min_sev not in _SEVERITY_RANK:
            raise ValueError(f"Unknown minPotentialSeverity {min_sev!r}")
        # Severity is stored as the enum value; we want all values at or
        # above the threshold. Easiest: explicit list.
        allowed = [s for s, rank in _SEVERITY_RANK.items() if rank >= _SEVERITY_RANK[min_sev]]
        stmt = stmt.where(NearMiss.potentialSeverity.in_([Severity(s) for s in allowed]))

    stmt = stmt.order_by(NearMiss.date.desc()).limit(10)
    rows = (await db.execute(stmt)).scalars().all()

    return {
        "searchWindowDays": lookback_days,
        "anchorDate": anchor.isoformat() if isinstance(anchor, datetime) else None,
        "plantId": incident.plantId,
        "nearMisses": [
            {
                "nearMissNumber": r.number,
                "date": r.date.isoformat() if isinstance(r.date, datetime) else None,
                "potentialSeverity": _enum_value(r.potentialSeverity),
                "hazardCategory": r.hazardCategory,
                "energySource": r.energySource,
                "equipmentId": r.equipmentId,
                "activity": r.activityBeingPerformed or r.activity,
                "descriptionPreview": (r.description or "")[:240],
                "immediateAction": r.immediateAction,
            }
            for r in rows
        ],
    }


def _enum_value(v: Any) -> str | None:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)
