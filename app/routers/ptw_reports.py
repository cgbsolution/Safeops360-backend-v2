"""PTW reporting endpoints.

  GET /api/ptw/{permit_id}/report   — close-out PDF (full evidence timeline,
                                      approvals, gas/isolation logs, outcome,
                                      handback, closure; PROVISIONAL watermark
                                      until the permit is CLOSED)
  GET /api/ptw/export/register      — permit register XLSX (plant-scoped)
"""

from __future__ import annotations

import io
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.audit_log import AuditLog
from app.models.permit import (
    Permit,
    PermitActionEvidence,
    PermitApproval,
    PermitAttachment,
    PermitExtension,
    PermitGasTestReading,
    PermitIsolation,
    PermitSuspension,
)
from app.models.plant import Plant
from app.models.user import User
from app.services.permissions import PermissionContext, can, get_accessible_plants
from app.services.ptw_report import build_register_xlsx, render_ptw_closeout_pdf

router = APIRouter(prefix="/api/ptw", tags=["ptw-reports"])


async def _names(db: AsyncSession, ids: set[str | None]) -> dict[str, str]:
    clean = {i for i in ids if i}
    if not clean:
        return {}
    rows = (await db.execute(select(User).where(User.id.in_(clean)))).scalars().all()
    return {u.id: u.name for u in rows}


@router.get("/export/register")
async def export_register(
    include_archived: bool = True,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Permit register XLSX — every permit the caller can see (plant-scoped),
    archived rows included by default so the register is the audit-complete
    view."""
    read_check = await can(db, user.id, "PTW.READ", PermissionContext())
    if not read_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, read_check.reason or "Access denied")

    plants = await get_accessible_plants(db, user.id)
    stmt = select(Permit).options(
        selectinload(Permit.suspensions),
        selectinload(Permit.extensions),
        selectinload(Permit.gasTestReadings),
    )
    if plants is not None:
        if not plants:
            stmt = stmt.where(Permit.id.is_(None))  # empty result
        else:
            stmt = stmt.where(Permit.plantId.in_(plants))
    if not include_archived:
        stmt = stmt.where(Permit.isArchived.is_(False))
    permits = (
        await db.execute(stmt.order_by(Permit.createdAt.desc()).limit(2000))
    ).scalars().all()

    plant_rows = (await db.execute(select(Plant))).scalars().all()
    plant_names = {p.id: p.name for p in plant_rows}
    user_names = await _names(
        db,
        {p.originatorId for p in permits}
        | {p.issuerId for p in permits}
        | {p.receiverId for p in permits},
    )

    rows: list[dict[str, Any]] = []
    for p in permits:
        rows.append({
            "number": p.number,
            "type": p.type.value if hasattr(p.type, "value") else str(p.type),
            "status": p.status.value if hasattr(p.status, "value") else str(p.status),
            "outcome": p.outcome.value if p.outcome is not None and hasattr(p.outcome, "value") else p.outcome,
            "plantName": plant_names.get(p.plantId, p.plantId),
            "location": p.location,
            "scopeOfWork": p.scopeOfWork,
            "validFrom": p.validFrom,
            "validTo": p.validTo,
            "originatorName": user_names.get(p.originatorId, "—"),
            "issuerName": user_names.get(p.issuerId, "—"),
            "receiverName": user_names.get(p.receiverId, "—"),
            "contractorName": p.contractorName,
            "flraRequired": bool(p.flraRequired),
            "issuedAt": p.issuedAt,
            "activatedAt": p.activatedAt,
            "workCompletedAt": p.workCompletedAt,
            "siteVerifiedAt": p.siteVerifiedAt,
            "closedAt": p.closedAt,
            "closingRemark": p.closingRemark,
            "suspensionCount": len(p.suspensions or []),
            "extensionCount": len(p.extensions or []),
            "exceedanceCount": sum(1 for g in (p.gasTestReadings or []) if g.isExceedance),
            "isArchived": bool(p.isArchived),
        })

    xlsx = build_register_xlsx(rows)
    return StreamingResponse(
        io.BytesIO(xlsx),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="ptw-register.xlsx"'},
    )


@router.get("/{permit_id}/report")
async def closeout_report(
    permit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    permit = await db.get(
        Permit,
        permit_id,
        options=[
            selectinload(Permit.workCrew),
            selectinload(Permit.isolations),
            selectinload(Permit.gasTestReadings),
            selectinload(Permit.approvals),
            selectinload(Permit.suspensions),
            selectinload(Permit.extensions),
            selectinload(Permit.actionEvidence).selectinload(PermitActionEvidence.photos),
        ],
    )
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
    result = await can(
        db, user.id, "PTW.READ",
        PermissionContext(
            record_id=permit.id,
            plant_id=permit.plantId,
            record={
                "originatorId": permit.originatorId,
                "issuerId": permit.issuerId,
                "receiverId": permit.receiverId,
            },
        ),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    plant = await db.get(Plant, permit.plantId)

    # Bulk-resolve every actor name the report mentions.
    ids: set[str | None] = {
        permit.originatorId, permit.issuerId, permit.receiverId,
        permit.workCompletedById, permit.siteVerifiedById, permit.closedById,
        permit.cancelledById,
    }
    ids |= {c.userId for c in permit.workCrew}
    ids |= {a.approverId for a in permit.approvals}
    ids |= {s.suspendedById for s in permit.suspensions}
    ids |= {e.requestedById for e in permit.extensions} | {e.approvedById for e in permit.extensions}
    ids |= {g.recordedById for g in permit.gasTestReadings}
    ids |= {i.isolationVerifiedById for i in permit.isolations} | {i.restoredById for i in permit.isolations}
    ids |= {ev.actorId for ev in permit.actionEvidence}
    names = await _names(db, ids)

    def n(uid: str | None) -> str:
        return names.get(uid, "—") if uid else "—"

    # Latest hash-chain entry for the integrity stamp.
    latest_hash = (
        await db.execute(
            select(AuditLog.entryHash)
            .where(AuditLog.entityType == "Permit", AuditLog.entityId == permit_id)
            .order_by(AuditLog.sequenceNo.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    # Best-effort: pull evidence photo bytes from storage for inline render.
    photo_bytes_by_evidence: dict[str, list[bytes]] = {}
    try:
        from app.services.storage import download_object, is_storage_configured

        if is_storage_configured():
            for ev in permit.actionEvidence:
                blobs: list[bytes] = []
                for att in (ev.photos or [])[:3]:
                    if att.deletedAt is not None or not att.mimeType.startswith("image/"):
                        continue
                    try:
                        blobs.append(download_object(att.storagePath))
                    except Exception:  # noqa: BLE001 — photo fetch never kills the report
                        continue
                if blobs:
                    photo_bytes_by_evidence[ev.id] = blobs
    except Exception:  # noqa: BLE001
        pass

    evidence_sorted = sorted(permit.actionEvidence, key=lambda e: (e.capturedAt is None, e.capturedAt))
    data: dict[str, Any] = {
        "latestAuditHash": latest_hash,
        "permit": {
            "number": permit.number,
            "type": permit.type.value if hasattr(permit.type, "value") else str(permit.type),
            "status": permit.status.value if hasattr(permit.status, "value") else str(permit.status),
            "outcome": permit.outcome.value if permit.outcome is not None and hasattr(permit.outcome, "value") else permit.outcome,
            # Printed on the close-out PDF and the register XLSX.
            "plantName": plant.name if plant else "Unknown site",
            "location": permit.location,
            "specificLocation": permit.specificLocation,
            "scopeOfWork": permit.scopeOfWork,
            "validFrom": permit.validFrom,
            "validTo": permit.validTo,
            "originatorName": n(permit.originatorId),
            "issuerName": n(permit.issuerId),
            "receiverName": n(permit.receiverId),
            "contractorName": permit.contractorName,
            "flraRequired": bool(permit.flraRequired),
            "gpsLatitude": permit.gpsLatitude,
            "gpsLongitude": permit.gpsLongitude,
            "workCompletedAt": permit.workCompletedAt,
            "workCompletedByName": n(permit.workCompletedById),
            "returnNotes": permit.returnNotes,
            "siteVerifiedAt": permit.siteVerifiedAt,
            "siteVerifiedByName": n(permit.siteVerifiedById),
            "siteVerificationChecklist": permit.siteVerificationChecklist,
            "closedAt": permit.closedAt,
            "closedByName": n(permit.closedById),
            "closingRemark": permit.closingRemark,
            "cancelledAt": permit.cancelledAt,
            "cancelledByName": n(permit.cancelledById),
            "cancellationReason": permit.cancellationReason,
        },
        "crew": [
            {
                "name": n(c.userId),
                "role": c.role,
                "trainingValid": c.trainingValidAtIssuance,
                "ppeValid": c.ppeValidAtIssuance,
                "removedAt": c.removedAt,
            }
            for c in permit.workCrew
        ],
        "evidence": [
            {
                "action": ev.action.value if hasattr(ev.action, "value") else str(ev.action),
                "actorName": n(ev.actorId),
                "capturedAt": ev.capturedAt,
                "gpsLatitude": ev.gpsLatitude,
                "gpsLongitude": ev.gpsLongitude,
                "gpsAccuracyMeters": ev.gpsAccuracyMeters,
                "declarationText": ev.declarationText,
                "comments": ev.comments,
                "signatureImageBase64": ev.signatureImageBase64,
                "photoBytes": photo_bytes_by_evidence.get(ev.id, []),
            }
            for ev in evidence_sorted
        ],
        "approvals": [
            {
                "step": a.step,
                "approverName": n(a.approverId),
                "decision": a.decision,
                "decidedAt": a.decidedAt,
                "comments": a.comments,
            }
            for a in sorted(permit.approvals, key=lambda x: (x.decidedAt is None, x.decidedAt))
        ],
        "gasReadings": [
            {
                "recordedAt": g.recordedAt,
                "byName": n(g.recordedById),
                "readings": g.readings,
                "isExceedance": g.isExceedance,
                "isPreEntry": g.isPreEntry,
            }
            for g in sorted(permit.gasTestReadings, key=lambda x: (x.recordedAt is None, x.recordedAt))
        ],
        "isolations": [
            {
                "tag": i.isolationPointTag,
                "type": i.isolationType,
                "verifiedAt": i.isolationVerifiedAt,
                "verifiedByName": names.get(i.isolationVerifiedById) if i.isolationVerifiedById else None,
                "restoredAt": i.restoredAt,
                "restoredByName": names.get(i.restoredById) if i.restoredById else None,
                "lotoTag": i.lotoTagNumber,
            }
            for i in permit.isolations
        ],
        "suspensions": [
            {
                "suspendedAt": s.suspendedAt,
                "reason": s.reason,
                "reasonDetail": s.reasonDetail,
                "resumedAt": s.resumedAt,
                "reFlraRequired": s.reFlraRequired,
                "byName": n(s.suspendedById),
            }
            for s in sorted(permit.suspensions, key=lambda x: (x.suspendedAt is None, x.suspendedAt))
        ],
        "extensions": [
            {
                "requestedAt": e.requestedAt,
                "newValidTo": e.newValidTo,
                "status": e.status,
                "approverName": names.get(e.approvedById) if e.approvedById else None,
                "reason": e.reason,
            }
            for e in sorted(permit.extensions, key=lambda x: (x.requestedAt is None, x.requestedAt))
        ],
    }

    pdf_bytes = render_ptw_closeout_pdf(data)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{permit.number}-closeout.pdf"'
        },
    )
