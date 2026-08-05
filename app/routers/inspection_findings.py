"""Inspection Finding → universal CAPA bridge. Mounts at /api/inspection-findings.

The inspection-findings lifecycle lives on the Node/Prisma side and spawns a
lightweight per-finding `InspectionFindingCapa`. This router promotes a finding
into the platform's UNIVERSAL `Capa` engine (source INSPECTION_FINDING) so it is
tracked with SLA, escalation, the tamper-evident audit chain and the unified CAPA
dashboards — the "auto-generation of findings routed to CAPA" Raychem expects.

A distinct prefix (`/api/inspection-findings`) is used so the Next.js catch-all
proxy forwards these to FastAPI without colliding with the Node
`/api/inspections/findings/*` routes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.equipment import Inspection
from app.models.inspection_finding import InspectionFinding
from app.models.user import User
from app.services.access_scope import build_query_scope
from app.services.capa_spawn import existing_capas_for, spawn_inspection_finding_capa

router = APIRouter(prefix="/api/inspection-findings", tags=["inspection-findings"])


async def _load_finding_and_plant(db: AsyncSession, finding_id: str) -> tuple[InspectionFinding, str]:
    finding = await db.get(InspectionFinding, finding_id)
    if finding is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Finding not found")
    inspection = await db.get(Inspection, finding.inspectionId)
    if inspection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Parent inspection not found")
    return finding, inspection.plantId


@router.post("/{finding_id}/spawn-capa", status_code=status.HTTP_201_CREATED)
async def spawn_capa_for_finding(
    finding_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Promote this inspection finding into the universal CAPA register.
    Idempotent — a second call returns the existing CAPA."""
    finding, plant_id = await _load_finding_and_plant(db, finding_id)
    await require_permission_with_context(
        "INSPECTION_FINDING.UPDATE", user, db, plant_id=plant_id
    )
    result = await spawn_inspection_finding_capa(db, finding, plant_id, user.id)
    await db.commit()
    return {"findingId": finding_id, "findingNumber": finding.findingNumber, **result}


@router.get("/{finding_id}/register-capas")
async def list_register_capas(
    finding_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Universal-register CAPAs raised from this finding (for the 'Raise CAPA'
    UI to show the linked CAPA once created)."""
    finding, plant_id = await _load_finding_and_plant(db, finding_id)
    scope = await build_query_scope(db, user.id, "INSPECTION_FINDING.READ")
    if not scope.allows_plant(plant_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied for this plant")
    capas = await existing_capas_for(db, "INSPECTION_FINDING", finding_id)
    return {
        "findingId": finding_id,
        "capas": [
            {
                "id": c.id,
                "capaNumber": c.capaNumber,
                "state": c.state,
                "severity": c.severity,
                "closureTargetDate": c.closureTargetDate.isoformat() if c.closureTargetDate else None,
            }
            for c in capas
        ],
    }
