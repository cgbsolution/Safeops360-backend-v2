"""Tool: get_active_permits_at_time.

Returns Permits that were ACTIVE (or in any approved-and-not-yet-closed
status) at a specific timestamp and plant. Used to answer: "what work
was being performed at the time of the incident, and was this incident
within a permitted activity vs. unauthorised work?"

A permit is considered active in the time window if:
  • validFrom <= ts < validTo (or closedAt if earlier)
  • status was ACTIVE or PLANT_HEAD_APPROVED (post-approval, pre-close)

We can't perfectly reconstruct historical status (would need an audit
trail). Approximation: the row's CURRENT status + the validity window.
For a recent incident this is accurate; for an incident from months
ago, the permit may have been closed since — that's surfaced via
closedAt.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident
from app.models.permit import Permit, PermitStatus


DEFINITION: dict[str, Any] = {
    "name": "get_active_permits_at_time",
    "description": (
        "Return Permit records that were in their validity window at a given "
        "timestamp at a given plant. Used to check whether the incident occurred "
        "during permitted work (and which permits), or during unauthorised work "
        "(no covering permit found). Defaults to the source incident's plant + "
        "occurredAt. Returns permit number, type, scope, validity window, and "
        "current status."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "asOfDate": {
                "type": "string",
                "description": (
                    "ISO datetime to evaluate. Defaults to the source incident's "
                    "occurredAt (or date if occurredAt is null)."
                ),
            },
            "plantId": {
                "type": "string",
                "description": (
                    "Plant.id to scope to. Defaults to the source incident's "
                    "plantId. Pass a different value to check sister-plant "
                    "permits in cross-plant investigations."
                ),
            },
            "permitType": {
                "type": "string",
                "description": (
                    "Optional filter on Permit.type (e.g. HOT_WORK, CONFINED_SPACE, "
                    "WORK_AT_HEIGHT, LOTO, EXCAVATION, GENERAL). Useful when you "
                    "have a hypothesis about the kind of work being done."
                ),
            },
        },
        "required": [],
    },
}


# Statuses where the permit covers active work. Pre-approval (DRAFT,
# AWAITING_*) and post-completion (CLOSED, EXPIRED, REJECTED) are
# excluded — only "approved, in validity window" counts as covering.
_ACTIVE_STATUSES = ("ACTIVE", "PLANT_HEAD_APPROVED", "SUSPENDED")


async def handle(
    input: dict[str, Any],  # noqa: A002
    *,
    db: AsyncSession,
    source_record_id: str,
    source_module: str,
) -> dict[str, Any]:
    # Resolve the anchor timestamp and plant
    as_of: datetime | None = None
    plant_id: str | None = input.get("plantId")

    if source_module == "INCIDENT":
        incident = await db.get(Incident, source_record_id)
        if incident is not None:
            as_of = incident.occurredAt or incident.date
            plant_id = plant_id or incident.plantId

    if raw := input.get("asOfDate"):
        try:
            as_of = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError(f"Invalid asOfDate {raw!r}: {e}") from e

    if as_of is None or plant_id is None:
        return {
            "note": (
                "Cannot resolve anchor timestamp + plant for this invocation; "
                "supply asOfDate and plantId explicitly."
            ),
            "permits": [],
        }

    stmt = (
        select(Permit)
        .where(Permit.plantId == plant_id)
        .where(Permit.validFrom <= as_of)
        # Either still within window OR closed AFTER the anchor (i.e.
        # was active at the time even if closed since).
        .where(or_(Permit.validTo >= as_of, Permit.validTo.is_(None)))
        # Also exclude permits that were already closed before as_of.
        .where(or_(Permit.closedAt.is_(None), Permit.closedAt >= as_of))
        .where(Permit.status.in_([PermitStatus(s) for s in _ACTIVE_STATUSES] + [PermitStatus.CLOSED]))
    )

    if permit_type := input.get("permitType"):
        stmt = stmt.where(Permit.type == permit_type)

    stmt = stmt.order_by(Permit.validFrom.desc()).limit(20)
    rows = (await db.execute(stmt)).scalars().all()

    return {
        "asOfDate": as_of.isoformat() if isinstance(as_of, datetime) else None,
        "plantId": plant_id,
        "permits": [
            {
                "permitNumber": p.number,
                "type": _enum_value(p.type),
                "status": _enum_value(p.status),
                "location": p.location,
                "scopeOfWork": (p.scopeOfWork or "")[:300],
                "validFrom": _iso(p.validFrom),
                "validTo": _iso(p.validTo),
                "closedAt": _iso(p.closedAt),
                "isolationsRequired": (p.isolationsRequired or "")[:200] if p.isolationsRequired else None,
                "gasTestRequired": p.gasTestRequired,
                "gasTestResult": p.gasTestResult,
                "fireWatchRequired": p.fireWatchRequired,
                "contractorName": p.contractorName,
                "suspendedAt": _iso(p.suspendedAt),
                "suspendedReason": p.suspendedReason,
            }
            for p in rows
        ],
        "_note": (
            "Permit status is current. A permit shown as CLOSED here was "
            "active during the incident window but has since been closed. "
            "Check validFrom/validTo against asOfDate to confirm."
        ),
    }


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


def _enum_value(v: Any) -> str | None:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)
