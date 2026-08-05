"""Audit-trail API (P1-1) — AL-01 viewer, AL-02 my-activity, chain verification,
regulator export. Always-on core infrastructure (not licence-gated).

Read access is gated on AUDIT_COMPLIANCE.READ (compliance/auditor roles) and the
list is plant-scoped via QueryScope, so an auditor sees only their plants' trail.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.db import get_db
from app.models.audit_log import AuditLog
from app.models.user import User
from app.services import audit_log as svc
from app.services.access_scope import build_query_scope
from app.services.permissions import can

router = APIRouter(prefix="/api/audit-trail", tags=["audit-trail"])

_READ_PERM = "AUDIT_COMPLIANCE.READ"


async def user_name_map(db: AsyncSession, ids) -> dict[str, str]:
    real = [i for i in set(ids) if i and not str(i).startswith("SYSTEM")]
    if not real:
        return {}
    rows = (await db.execute(select(User.id, User.name).where(User.id.in_(real)))).all()
    return {r[0]: r[1] for r in rows}


async def _require_read(db: AsyncSession, user: User) -> None:
    res = await can(db, user.id, _READ_PERM)
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Audit-trail access requires a compliance/audit role.")


def _row(r: AuditLog, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": r.id, "sequenceNo": r.sequenceNo, "entityType": r.entityType, "entityId": r.entityId,
        "entityCode": r.entityCode, "action": r.action, "plantId": r.plantId,
        "actorId": r.actorId, "actorName": names.get(r.actorId or "", r.actorId), "actorType": r.actorType,
        "actorIp": r.actorIp, "timestamp": r.timestamp.isoformat() if r.timestamp else None,
        "before": r.before, "after": r.after, "changedFields": r.changedFields, "reason": r.reason,
        "correlationId": r.correlationId, "previousEntryHash": r.previousEntryHash, "entryHash": r.entryHash,
    }


@router.get("/log")
async def list_entries(
    entityType: str | None = Query(None),
    entityId: str | None = Query(None),
    actorId: str | None = Query(None),
    action: str | None = Query(None),
    plantId: str | None = Query(None),
    fromDate: datetime | None = Query(None),
    toDate: datetime | None = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """AL-01 — filterable audit log, plant-scoped to the actor."""
    await _require_read(db, user)
    scope = await build_query_scope(db, user.id, _READ_PERM)
    stmt = scope.apply(select(AuditLog), AuditLog)
    if entityType:
        stmt = stmt.where(AuditLog.entityType == entityType)
    if entityId:
        stmt = stmt.where(AuditLog.entityId == entityId)
    if actorId:
        stmt = stmt.where(AuditLog.actorId == actorId)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if plantId:
        stmt = stmt.where(AuditLog.plantId == plantId)
    if fromDate:
        stmt = stmt.where(AuditLog.timestamp >= fromDate)
    if toDate:
        stmt = stmt.where(AuditLog.timestamp <= toDate)
    rows = (await db.execute(stmt.order_by(AuditLog.timestamp.desc()).limit(limit))).scalars().all()
    names = await user_name_map(db, [r.actorId for r in rows if r.actorId])
    return {"entries": [_row(r, names) for r in rows], "total": len(rows)}


@router.get("/log/entity/{entity_type}/{entity_id}")
async def entity_timeline(
    entity_type: str, entity_id: str,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Per-entity timeline + chain-integrity status (every change to one record)."""
    await _require_read(db, user)
    rows = (
        await db.execute(
            select(AuditLog).where(AuditLog.entityType == entity_type).where(AuditLog.entityId == entity_id)
            .order_by(AuditLog.sequenceNo)
        )
    ).scalars().all()
    names = await user_name_map(db, [r.actorId for r in rows if r.actorId])
    integrity = await svc.verify_chain(db, entity_type, entity_id)
    return {"entityType": entity_type, "entityId": entity_id, "entries": [_row(r, names) for r in rows], "integrity": integrity}


@router.post("/log/verify/{entity_type}/{entity_id}")
async def verify(
    entity_type: str, entity_id: str,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Run the hash-chain verifier for one entity (tamper detection)."""
    await _require_read(db, user)
    return await svc.verify_chain(db, entity_type, entity_id)


@router.get("/my-activity")
async def my_activity(
    limit: int = Query(50, ge=1, le=500),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """AL-02 — the current user's own recent actions."""
    rows = (
        await db.execute(
            select(AuditLog).where(AuditLog.actorId == user.id).order_by(AuditLog.timestamp.desc()).limit(limit)
        )
    ).scalars().all()
    return {"entries": [_row(r, {user.id: user.name}) for r in rows], "total": len(rows)}


@router.get("/log/export.csv")
async def export_csv(
    entityType: str | None = Query(None),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """Regulator export — CSV with per-entry chain-verification status."""
    await _require_read(db, user)
    scope = await build_query_scope(db, user.id, _READ_PERM)
    stmt = scope.apply(select(AuditLog), AuditLog)
    if entityType:
        stmt = stmt.where(AuditLog.entityType == entityType)
    rows = (await db.execute(stmt.order_by(AuditLog.entityType, AuditLog.entityId, AuditLog.sequenceNo).limit(5000))).scalars().all()
    # per-entity chain check (cache verdicts)
    verdicts: dict[tuple[str, str], dict] = {}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "entityType", "entityId", "entityCode", "seq", "action", "actorId", "actorType", "actorIp", "reason", "changedFields", "chainStatus"])
    for r in rows:
        key = (r.entityType, r.entityId)
        if key not in verdicts:
            verdicts[key] = await svc.verify_chain(db, r.entityType, r.entityId)
        v = verdicts[key]
        chain = "INTACT" if v["intact"] else f"CHAIN BROKEN @seq {v['brokenAtSequence']}"
        w.writerow([
            r.timestamp.isoformat() if r.timestamp else "", r.entityType, r.entityId, r.entityCode or "",
            r.sequenceNo, r.action, r.actorId or "", r.actorType, r.actorIp or "", r.reason or "",
            ",".join(r.changedFields or []), chain,
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-trail.csv"},
    )
