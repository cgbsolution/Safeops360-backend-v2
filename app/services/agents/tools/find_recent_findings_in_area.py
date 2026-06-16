"""Tool: find_recent_findings_in_area.

Returns recent observations (SafeOps' nearest analog to inspection
findings) in the same plant or area as the source permit. Filterable by
severity, category, and lookback window. Used by the
PermitRiskReviewerAgent for the "recurring-local-issue" pattern — if the
area has open HIGH-severity observations on, say, hot work or housekeeping,
and the current permit's controls don't address them, that's a finding.

The PTW context builder pre-fetches a 30-day window of HIGH+ severity
observations. This tool lets the agent widen the window, change the
severity floor, or filter by category.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.observation import (
    Observation,
    ObservationCategory,
    Severity,
)
from app.models.permit import Permit


DEFINITION: dict[str, Any] = {
    "name": "find_recent_findings_in_area",
    "description": (
        "Find recent observations at the same plant/area as the source permit. "
        "Observations are SafeOps' analog to inspection findings — unsafe acts "
        "or conditions reported by frontline staff. Filter by severity, "
        "category, scope (area vs plant), and lookback window. Use to surface "
        "recurring local issues the current permit's controls should address."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "minSeverity": {
                "type": "string",
                "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
                "description": (
                    "Lower bound on severity. Defaults to HIGH. Set to "
                    "MEDIUM when looking for early-warning patterns."
                ),
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional Observation.category filter. One of: PPE, "
                    "HOUSEKEEPING, WORK_AT_HEIGHT, HOT_WORK, MOBILE_EQUIPMENT, "
                    "ELECTRICAL, MATERIAL_HANDLING, CONFINED_SPACE, "
                    "CHEMICAL_HANDLING, EMERGENCY, OTHER."
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["area", "plant"],
                "description": (
                    "Restrict to source permit's area only ('area') or the "
                    "whole plant ('plant'). Defaults to 'area'. Falls back to "
                    "plant scope if source permit has no areaId."
                ),
            },
            "lookbackDays": {
                "type": "integer",
                "description": "Days to look back. Default 60, max 365.",
                "minimum": 7,
                "maximum": 365,
            },
            "limit": {
                "type": "integer",
                "description": "Max rows. Default 15, hard cap 25.",
                "minimum": 1,
                "maximum": 25,
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
    if source_module != "PTW":
        raise ValueError(
            f"find_recent_findings_in_area expects source_module=PTW, got {source_module!r}"
        )

    permit = await db.get(Permit, source_record_id)
    if permit is None:
        return {"findings": [], "note": "Source permit not found"}

    min_sev_raw = (input.get("minSeverity") or "HIGH").upper()
    if min_sev_raw not in _SEVERITY_RANK:
        raise ValueError(
            f"Unknown minSeverity {min_sev_raw!r}. Valid: "
            f"{sorted(_SEVERITY_RANK.keys())}"
        )
    allowed_severities = [
        Severity(s)
        for s, rank in _SEVERITY_RANK.items()
        if rank >= _SEVERITY_RANK[min_sev_raw]
    ]

    lookback_days = min(int(input.get("lookbackDays", 60)), 365)
    window_start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    limit = min(int(input.get("limit", 15)), 25)

    scope = (input.get("scope") or "area").lower()
    if scope == "area" and not permit.areaId:
        scope = "plant"

    stmt = (
        select(Observation)
        .where(Observation.plantId == permit.plantId)
        .where(Observation.date >= window_start)
        .where(Observation.severity.in_(allowed_severities))
    )
    if scope == "area":
        stmt = stmt.where(Observation.areaId == permit.areaId)

    if category_raw := input.get("category"):
        try:
            category = ObservationCategory(category_raw)
        except ValueError as e:
            raise ValueError(
                f"Unknown category {category_raw!r}. Valid: "
                f"{sorted(c.value for c in ObservationCategory)}"
            ) from e
        stmt = stmt.where(Observation.category == category)

    stmt = stmt.order_by(Observation.date.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()

    return {
        "permitNumber": permit.number,
        "scope": scope,
        "minSeverity": min_sev_raw,
        "lookbackDays": lookback_days,
        "findings": [
            {
                "observationNumber": o.number,
                "date": _iso(o.date),
                "type": _enum(o.type),
                "category": _enum(o.category),
                "severity": _enum(o.severity),
                "status": _enum(o.status),
                "descriptionPreview": (o.description or "")[:240],
                "immediateActionPreview": (
                    (o.immediateAction or "")[:200] if o.immediateAction else None
                ),
                "closedAt": _iso(o.closedAt),
            }
            for o in rows
        ],
    }


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


def _enum(v: Any) -> str | None:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)
