"""Tool: get_equipment_history.

Returns the operational history of a specific piece of equipment: prior
incidents, near misses, recent inspections + their outcomes, and the
cached lastInspectionDate / nextInspectionDue. The model uses this to
test hypotheses about equipment-level root causes ("this gearbox has
failed three times in eighteen months").

The agent must pass an equipmentId — typically pulled from the
inputContext (incident.equipmentInvolved). For Commit 1, the context
builder is generic; Commit 3 will give the RCA agent richer context
that includes the involved equipment IDs by default.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.equipment import Equipment, Inspection
from app.models.incident import Incident, IncidentEquipment
from app.models.near_miss import NearMiss


DEFINITION: dict[str, Any] = {
    "name": "get_equipment_history",
    "description": (
        "Get the operational history of a specific equipment item: prior "
        "incidents and near misses it was involved in, recent inspections "
        "with their outcomes, and current inspection status (last done / "
        "next due). Use this when equipment is central to the incident's "
        "hypothesised cause — e.g. to check whether the equipment was "
        "overdue for inspection, had a history of similar failures, or "
        "was flagged in near misses that weren't acted on."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "equipmentId": {
                "type": "string",
                "description": (
                    "Equipment.id of the item to inspect. Get this from the "
                    "incident's equipmentInvolved list in the input context."
                ),
            },
            "lookbackDays": {
                "type": "integer",
                "description": (
                    "Days of history to pull. Default 730 (2 years), max 1825 "
                    "(5 years). Inspections and near misses are filtered by "
                    "this window; cached last/next inspection dates are always "
                    "returned regardless of window."
                ),
                "minimum": 30,
                "maximum": 1825,
            },
        },
        "required": ["equipmentId"],
    },
}


async def handle(
    input: dict[str, Any],  # noqa: A002
    *,
    db: AsyncSession,
    source_record_id: str,
    source_module: str,
) -> dict[str, Any]:
    equipment_id = input["equipmentId"]
    lookback = min(int(input.get("lookbackDays", 730)), 1825)
    window_start = datetime.now(timezone.utc) - timedelta(days=lookback)

    equipment = await db.get(Equipment, equipment_id)
    if equipment is None:
        return {"note": f"Equipment {equipment_id!r} not found", "equipment": None}

    # Recent inspections (most recent N, completed only)
    insp_stmt = (
        select(Inspection)
        .where(Inspection.equipmentId == equipment_id)
        .where(Inspection.completedDate.is_not(None))
        .where(Inspection.completedDate >= window_start)
        .order_by(Inspection.completedDate.desc())
        .limit(10)
    )
    inspections = (await db.execute(insp_stmt)).scalars().all()

    # Near misses involving this equipment
    nm_stmt = (
        select(NearMiss)
        .where(NearMiss.equipmentId == equipment_id)
        .where(NearMiss.date >= window_start)
        .order_by(NearMiss.date.desc())
        .limit(10)
    )
    near_misses = (await db.execute(nm_stmt)).scalars().all()

    # Incidents involving this equipment (via IncidentEquipment join)
    inc_stmt = (
        select(Incident)
        .join(IncidentEquipment, IncidentEquipment.incidentId == Incident.id)
        .where(IncidentEquipment.equipmentId == equipment_id)
        .where(Incident.id != source_record_id)
        .order_by(Incident.date.desc())
        .limit(10)
    )
    incidents = (await db.execute(inc_stmt)).scalars().unique().all()

    return {
        "equipment": {
            "id": equipment.id,
            "code": equipment.code,
            "name": equipment.name,
            "category": equipment.category,
            "subCategory": equipment.subCategory,
            "criticality": equipment.criticality,
            "make": equipment.make,
            "modelNumber": equipment.modelNumber,
            "manufacturer": equipment.manufacturer,
            "commissioningDate": _iso(equipment.commissioningDate),
            "active": equipment.active,
            "lastInspectionDate": _iso(equipment.lastInspectionDate),
            "nextInspectionDue": _iso(equipment.nextInspectionDue),
            "inspectionStatus": _inspection_status(equipment),
        },
        "searchWindowDays": lookback,
        "recentInspections": [
            {
                "inspectionNumber": i.number,
                "completedDate": _iso(i.completedDate),
                "result": i.result,
                "status": _enum_value(i.status),
                "followUpRequired": i.followUpRequired,
                "observationsPreview": (i.observations or "")[:200],
            }
            for i in inspections
        ],
        "recentNearMisses": [
            {
                "nearMissNumber": nm.number,
                "date": _iso(nm.date),
                "potentialSeverity": _enum_value(nm.potentialSeverity),
                "hazardCategory": nm.hazardCategory,
                "descriptionPreview": (nm.description or "")[:200],
            }
            for nm in near_misses
        ],
        "priorIncidents": [
            {
                "incidentNumber": inc.number,
                "incidentType": _enum_value(inc.type),
                "severity": inc.severity,
                "date": _iso(inc.occurredAt or inc.date),
                "rootCauseSummary": inc.rootCauseSummary,
                "descriptionPreview": (inc.initialDescription or inc.description or "")[:200],
            }
            for inc in incidents
        ],
    }


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


def _enum_value(v: Any) -> str | None:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)


def _inspection_status(equipment: Equipment) -> str:
    """Compute a human-readable inspection status from the cached dates."""
    if equipment.nextInspectionDue is None:
        return "NOT_SCHEDULED"
    now = datetime.now(timezone.utc)
    due = equipment.nextInspectionDue
    if due.tzinfo is None:
        # Treat naive datetimes as UTC for comparison
        due = due.replace(tzinfo=timezone.utc)
    if due < now:
        return "OVERDUE"
    if due < now + timedelta(days=14):
        return "DUE_SOON"
    return "ON_SCHEDULE"
