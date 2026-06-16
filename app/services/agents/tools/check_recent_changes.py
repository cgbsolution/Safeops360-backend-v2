"""Tool: check_recent_changes.

Surfaces "what changed in the area before the incident?" — the classic
MOC (Management of Change) question. SafeOps360 does not yet have a
first-class MOC module, so this tool returns the change-adjacent
signals we DO have:

  • Equipment in the same area decommissioned or commissioned recently
  • Permits closed recently (completed work that may have changed the
    physical state of the area)
  • Inspections with non-PASS results that triggered follow-up

Critically, the tool is explicit about the MOC gap rather than
pretending coverage it doesn't have. The agent's prompt instructs it
to flag this limitation to the investigator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.equipment import Equipment, Inspection
from app.models.incident import Incident
from app.models.permit import Permit, PermitStatus


DEFINITION: dict[str, Any] = {
    "name": "check_recent_changes",
    "description": (
        "Surface 'what changed in this area recently?' signals that may have "
        "contributed to the incident. SafeOps360 does NOT have a formal MOC "
        "(Management of Change) module yet — this tool aggregates the proxy "
        "signals available: equipment commissioned/decommissioned in the "
        "lookback window, permits closed in the area (completed work), and "
        "inspections with non-PASS outcomes that triggered follow-up. The "
        "result includes a flag noting the MOC gap so the agent can recommend "
        "the investigator gather change-control evidence manually."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "lookbackDays": {
                "type": "integer",
                "description": "Window before the incident. Default 90, max 365.",
                "minimum": 7,
                "maximum": 365,
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
    if source_module != "INCIDENT":
        raise ValueError(
            f"check_recent_changes is incident-centric; source_module={source_module}"
        )
    incident = await db.get(Incident, source_record_id)
    if incident is None:
        return {"note": "Source incident not found", "changes": []}

    lookback = min(int(input.get("lookbackDays", 90)), 365)
    anchor = incident.occurredAt or incident.date or datetime.now(timezone.utc)
    window_start = anchor - timedelta(days=lookback)

    # ── Equipment commissioned/decommissioned in window (same plant) ──
    eq_stmt = (
        select(Equipment)
        .where(Equipment.plantId == incident.plantId)
        .where(
            or_(
                and_(
                    Equipment.commissioningDate >= window_start,
                    Equipment.commissioningDate <= anchor,
                ),
                and_(
                    Equipment.decommissionDate >= window_start,
                    Equipment.decommissionDate <= anchor,
                ),
            )
        )
        .order_by(Equipment.updatedAt.desc())
        .limit(20)
    )
    eq_changes = (await db.execute(eq_stmt)).scalars().all()

    # ── Permits closed in window in same plant ──
    permit_stmt = (
        select(Permit)
        .where(Permit.plantId == incident.plantId)
        .where(Permit.closedAt >= window_start)
        .where(Permit.closedAt <= anchor)
        .where(Permit.status == PermitStatus.CLOSED)
        .order_by(Permit.closedAt.desc())
        .limit(10)
    )
    closed_permits = (await db.execute(permit_stmt)).scalars().all()

    # ── Inspections with non-PASS results in window, same plant ──
    insp_stmt = (
        select(Inspection)
        .where(Inspection.plantId == incident.plantId)
        .where(Inspection.completedDate >= window_start)
        .where(Inspection.completedDate <= anchor)
        # `result` is free-text in the schema; the seed convention is
        # PASS / FAIL / OBSERVATION_FOUND. We want anything that's not a
        # clean PASS.
        .where(or_(Inspection.result != "PASS", Inspection.result.is_(None)))
        .where(Inspection.followUpRequired.is_(True))
        .order_by(Inspection.completedDate.desc())
        .limit(10)
    )
    follow_up_inspections = (await db.execute(insp_stmt)).scalars().all()

    return {
        "searchWindowDays": lookback,
        "anchorDate": _iso(anchor),
        "plantId": incident.plantId,
        "_mocGapNote": (
            "SafeOps360 has no formal MOC (Management of Change) module "
            "integrated yet. This tool returns proxy signals only. Recommend "
            "the investigator manually gather change-control records (work "
            "orders, MOC approvals, vendor modifications) from operational "
            "systems outside SafeOps360."
        ),
        "equipmentChanges": [
            {
                "equipmentCode": e.code,
                "equipmentName": e.name,
                "category": e.category,
                "commissioned": _iso(e.commissioningDate),
                "decommissioned": _iso(e.decommissionDate),
                "active": e.active,
                "changeType": (
                    "DECOMMISSIONED"
                    if e.decommissionDate and e.decommissionDate >= window_start
                    else "COMMISSIONED"
                ),
            }
            for e in eq_changes
        ],
        "completedPermits": [
            {
                "permitNumber": p.number,
                "type": _enum_value(p.type),
                "location": p.location,
                "scopeOfWorkPreview": (p.scopeOfWork or "")[:240],
                "closedAt": _iso(p.closedAt),
                "contractorName": p.contractorName,
            }
            for p in closed_permits
        ],
        "inspectionFollowUps": [
            {
                "inspectionNumber": i.number,
                "completedDate": _iso(i.completedDate),
                "equipmentId": i.equipmentId,
                "result": i.result,
                "observationsPreview": (i.observations or "")[:240],
            }
            for i in follow_up_inspections
        ],
    }


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


def _enum_value(v: Any) -> str | None:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)
