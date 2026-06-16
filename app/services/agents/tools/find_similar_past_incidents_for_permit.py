"""Tool: find_similar_past_incidents_for_permit.

Returns closed past incidents whose active permit at the time was of the
same type as the source permit, OR whose activity description matches
keywords from the source permit's scope. Used by the
PermitRiskReviewerAgent for the "historical pattern synthesis" pattern —
if three or more past incidents share a root cause and this permit's
controls don't obviously address it, that's a finding worth surfacing.

Two-pass search:
  1. Same plant, same permit type, last 24 months — strongest signal.
  2. Same plant, keyword-match on activity description — fall-back.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident, IncidentStatus
from app.models.permit import Permit, PermitType


DEFINITION: dict[str, Any] = {
    "name": "find_similar_past_incidents_for_permit",
    "description": (
        "Find closed past incidents at the same plant relevant to this "
        "permit's work. Searches first by permit type (incidents whose "
        "active permit at the time matched this permit's type), then by "
        "keyword match against the activity description. Use to anchor "
        "historical-pattern findings. Returns root cause summary and root "
        "causes for each incident so you can spot recurring patterns."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "permitType": {
                "type": "string",
                "description": (
                    "Optional override on permit type to match. Defaults to "
                    "the source permit's own type. Pass a different value to "
                    "investigate cross-type patterns (e.g. confined-space "
                    "incidents on a hot-work permit you're reviewing because "
                    "scope implies tank entry)."
                ),
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional keywords to match against the incident "
                    "activity description and free-text description. Use 2-4 "
                    "distinctive words from the scope (e.g. ['gearbox', "
                    "'isolation', 'maintenance']). Avoid generic words."
                ),
            },
            "lookbackDays": {
                "type": "integer",
                "description": "Days to look back. Default 730 (24 months), max 1825.",
                "minimum": 30,
                "maximum": 1825,
            },
            "limit": {
                "type": "integer",
                "description": "Max rows to return. Default 8, hard cap 15.",
                "minimum": 1,
                "maximum": 15,
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
    if source_module != "PTW":
        raise ValueError(
            f"find_similar_past_incidents_for_permit expects source_module=PTW, "
            f"got {source_module!r}"
        )

    permit = await db.get(Permit, source_record_id)
    if permit is None:
        return {"incidents": [], "note": "Source permit not found"}

    lookback_days = min(int(input.get("lookbackDays", 730)), 1825)
    window_start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    limit = min(int(input.get("limit", 8)), 15)

    permit_type: PermitType | None
    if raw := input.get("permitType"):
        try:
            permit_type = PermitType(raw)
        except ValueError as e:
            raise ValueError(
                f"Unknown permitType {raw!r}. Valid: "
                f"{sorted(t.value for t in PermitType)}"
            ) from e
    else:
        permit_type = permit.type

    # Pass 1: same permit type
    pass1_stmt = (
        select(Incident)
        .join(Permit, Permit.id == Incident.activePermitId)
        .where(Incident.plantId == permit.plantId)
        .where(Incident.status == IncidentStatus.CLOSED)
        .where(Incident.date >= window_start)
        .where(Permit.type == permit_type)
        .order_by(Incident.date.desc())
        .limit(limit)
    )
    pass1 = (await db.execute(pass1_stmt)).scalars().all()

    incidents = list(pass1)

    # Pass 2: keyword match if we have headroom
    if len(incidents) < limit and (keywords := input.get("keywords")):
        clauses = []
        for kw in keywords:
            pattern = f"%{kw}%"
            clauses.append(
                or_(
                    Incident.activityBeingPerformed.ilike(pattern),
                    Incident.description.ilike(pattern),
                    Incident.initialDescription.ilike(pattern),
                )
            )
        existing_ids = {i.id for i in incidents}
        pass2_stmt = (
            select(Incident)
            .where(Incident.plantId == permit.plantId)
            .where(Incident.status == IncidentStatus.CLOSED)
            .where(Incident.date >= window_start)
            .where(or_(*clauses))
            .order_by(Incident.date.desc())
            .limit(limit)
        )
        pass2 = (await db.execute(pass2_stmt)).scalars().all()
        for inc in pass2:
            if inc.id in existing_ids:
                continue
            if len(incidents) >= limit:
                break
            incidents.append(inc)

    return {
        "permitNumber": permit.number,
        "matchedOnPermitType": _enum(permit_type),
        "lookbackDays": lookback_days,
        "incidents": [
            {
                "incidentNumber": i.number,
                "type": _enum(i.type),
                "severity": i.severity,
                "occurredAt": _iso(i.occurredAt or i.date),
                "activityBeingPerformed": i.activityBeingPerformed,
                "descriptionPreview": (
                    (i.initialDescription or i.description or "")[:240]
                ),
                "rootCauseSummary": (
                    (i.rootCauseSummary or "")[:300]
                    if i.rootCauseSummary
                    else None
                ),
                "rootCauseMethod": i.rootCauseMethod,
                "rootCauses": (
                    list(i.rootCauses)[:5]
                    if getattr(i, "rootCauses", None)
                    else []
                ),
            }
            for i in incidents
        ],
    }


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


def _enum(v: Any) -> str | None:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)
