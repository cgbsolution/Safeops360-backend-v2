"""Tool: find_concurrent_permits.

Returns permits at the same plant whose validity windows overlap the
source permit's window AND that are in an in-flight status. Used by the
PermitRiskReviewerAgent to reason about SIMOPS conflicts across the full
set of active permits — the multi-signal pattern rules pairs cannot
catch.

The PTW context builder already provides a snapshot of these under
`context.activePermitsInRadius` at invocation start. This tool exists for
when the agent wants to re-query with a tighter filter (specific
permit type, specific area) or refresh during the loop.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permit import Permit, PermitStatus, PermitType


DEFINITION: dict[str, Any] = {
    "name": "find_concurrent_permits",
    "description": (
        "Find permits at the same plant whose validity windows overlap with "
        "the source permit and that are in an in-flight status "
        "(ISSUER_APPROVED, SAFETY_APPROVED, PLANT_HEAD_APPROVED, ACTIVE, "
        "SUSPENDED). Use to look for SIMOPS conflicts, shared resources, or "
        "cumulative-load patterns the rules engine cannot catch. Optionally "
        "filter by permit type or restrict to the same area."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "permitType": {
                "type": "string",
                "description": (
                    "Optional filter on permit type. One of HOT_WORK, "
                    "CONFINED_SPACE, WORK_AT_HEIGHT, EXCAVATION, "
                    "ELECTRICAL_LOTO, GENERAL_COLD."
                ),
            },
            "sameAreaOnly": {
                "type": "boolean",
                "description": (
                    "When true, only return permits in the SAME areaId as the "
                    "source permit. Defaults to false (whole plant)."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max rows to return. Default 10, hard cap 25.",
                "minimum": 1,
                "maximum": 25,
            },
        },
        "required": [],
    },
}


_LIVE_STATUSES: tuple[PermitStatus, ...] = (
    # Closed-loop states
    PermitStatus.APPROVED,
    PermitStatus.ISSUED,
    PermitStatus.ACTIVE,
    PermitStatus.SUSPENDED,
    # Deprecated pre-rebuild intermediates (kept for old rows)
    PermitStatus.ISSUER_APPROVED,
    PermitStatus.SAFETY_APPROVED,
    PermitStatus.PLANT_HEAD_APPROVED,
)


async def handle(
    input: dict[str, Any],  # noqa: A002
    *,
    db: AsyncSession,
    source_record_id: str,
    source_module: str,
) -> dict[str, Any]:
    if source_module != "PTW":
        raise ValueError(
            f"find_concurrent_permits expects source_module=PTW, got {source_module!r}"
        )

    permit = await db.get(Permit, source_record_id)
    if permit is None:
        return {"permits": [], "note": "Source permit not found"}

    limit = min(int(input.get("limit", 10)), 25)
    same_area = bool(input.get("sameAreaOnly", False))

    stmt = (
        select(Permit)
        .where(Permit.plantId == permit.plantId)
        .where(Permit.id != permit.id)
        .where(Permit.status.in_(list(_LIVE_STATUSES)))
        # Overlap test: theirStart <= ourEnd AND theirEnd >= ourStart
        .where(Permit.validFrom <= permit.validTo)
        .where(Permit.validTo >= permit.validFrom)
    )

    if permit_type_raw := input.get("permitType"):
        try:
            permit_type = PermitType(permit_type_raw)
        except ValueError as e:
            raise ValueError(
                f"Unknown permitType {permit_type_raw!r}. Valid: "
                f"{sorted(t.value for t in PermitType)}"
            ) from e
        stmt = stmt.where(Permit.type == permit_type)

    if same_area and permit.areaId:
        stmt = stmt.where(Permit.areaId == permit.areaId)

    stmt = stmt.order_by(Permit.validFrom.asc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()

    return {
        "sourcePermit": {
            "permitNumber": permit.number,
            "plantId": permit.plantId,
            "areaId": permit.areaId,
            "validFrom": _iso(permit.validFrom),
            "validTo": _iso(permit.validTo),
        },
        "permits": [
            {
                "permitNumber": p.number,
                "type": _enum(p.type),
                "status": _enum(p.status),
                "sameArea": (p.areaId is not None and p.areaId == permit.areaId),
                "areaId": p.areaId,
                "location": p.location,
                "scopeOfWorkPreview": (p.scopeOfWork or "")[:240],
                "validFrom": _iso(p.validFrom),
                "validTo": _iso(p.validTo),
                "contractorName": p.contractorName,
                "fireWatchRequired": p.fireWatchRequired,
                "gasTestRequired": p.gasTestRequired,
            }
            for p in rows
        ],
    }


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


def _enum(v: Any) -> str | None:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)
