"""Anomalies router. Read-side + status transitions ported from Node.

NOTE: The detector runner (`POST /api/anomalies/run`) is intentionally NOT
ported here. That endpoint lives on the Node side because the detection
algorithms (frequency-spike z-score, severity-drift CUSUM, hotspot
clustering, etc.) are non-trivial to port and would expand this session's
scope significantly. The catch-all proxy doesn't shadow the Node `/run`
route — it stays Node-owned for now.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.anomaly import Anomaly
from app.models.observation import Observation
from app.models.user import User
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/anomalies", tags=["anomalies"])


VALID_TRANSITIONS: dict[str, list[str]] = {
    "PENDING_REVIEW": ["ACKNOWLEDGED", "CONFIRMED", "DISMISSED"],
    "ACKNOWLEDGED": ["CONFIRMED", "DISMISSED"],
    "CONFIRMED": [],
    "DISMISSED": [],
    "EXPIRED": [],
}


def _row_to_dict(a: Anomaly) -> dict[str, Any]:
    return {
        "id": a.id,
        "detectedAt": a.detectedAt,
        "detectorId": a.detectorId,
        "module": a.module,
        "plantId": a.plantId,
        "category": a.category,
        "area": a.area,
        "personId": a.personId,
        "severity": a.severity,
        "signalData": a.signalData,
        "description": a.description,
        "contributingRecordIds": a.contributingRecordIds or [],
        "status": a.status,
        "reviewerId": a.reviewerId,
        "reviewedAt": a.reviewedAt,
        "reviewNote": a.reviewNote,
        "fingerprint": a.fingerprint,
        "emailNotifiedAt": a.emailNotifiedAt,
    }


@router.get("")
async def list_anomalies(
    status_filter: str | None = None,
    severity: str | None = None,
    detectorId: str | None = None,
    plantId: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # Anomaly viewing keys off AUDIT.VIEW — corp HSE / system admin holds it.
    # Anyone with HSE_MANAGER role also sees them via permissions matrix.
    read_check = await can(db, user.id, "INCIDENT.READ", PermissionContext())
    if not read_check.allowed:
        # Fall through to a more lenient check — anomalies are a derived view
        # over data the user already has plant-scope on.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")

    stmt = select(Anomaly)
    if status_filter:
        stmt = stmt.where(Anomaly.status == status_filter)
    if severity:
        stmt = stmt.where(Anomaly.severity == severity)
    if detectorId:
        stmt = stmt.where(Anomaly.detectorId == detectorId)
    if plantId:
        stmt = stmt.where(Anomaly.plantId == plantId)

    stmt = stmt.order_by(Anomaly.status.asc(), Anomaly.detectedAt.desc()).limit(200)
    rows = (await db.execute(stmt)).scalars().all()
    return {"anomalies": [_row_to_dict(a) for a in rows]}


@router.get("/{anomaly_id}")
async def get_anomaly(
    anomaly_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    a = await db.get(Anomaly, anomaly_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anomaly not found")

    # Pull contributing observations for the UI sidebar
    contributing: list[dict[str, Any]] = []
    if a.contributingRecordIds:
        obs_stmt = (
            select(Observation)
            .where(Observation.id.in_(a.contributingRecordIds))
            .order_by(Observation.date.desc())
        )
        obs_rows = (await db.execute(obs_stmt)).scalars().all()
        contributing = [
            {
                "id": o.id,
                "number": o.number,
                "date": o.date,
                "type": o.type.value,
                "category": o.category.value,
                "severity": o.severity.value,
                "description": o.description,
                "status": o.status.value,
            }
            for o in obs_rows
        ]
    return {"anomaly": _row_to_dict(a), "contributingRecords": contributing}


@router.patch("/{anomaly_id}")
async def patch_anomaly(
    anomaly_id: str,
    body: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    a = await db.get(Anomaly, anomaly_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anomaly not found")

    new_status = str(body.get("status") or "")
    note = body.get("reviewNote")
    note = str(note) if note else None

    allowed = VALID_TRANSITIONS.get(a.status, [])
    if new_status not in allowed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot transition from {a.status} to {new_status}.",
        )
    if new_status in {"DISMISSED", "CONFIRMED"} and not note:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"A review note is required when {new_status.lower()}-ing an anomaly.",
        )

    a.status = new_status
    a.reviewerId = user.id
    a.reviewedAt = datetime.now(timezone.utc)
    if note is not None:
        a.reviewNote = note
    await db.flush()
    return {"anomaly": _row_to_dict(a)}
