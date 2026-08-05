"""PTW closed-loop lifecycle endpoints.

The evidence-carrying transitions of the rebuilt permit state machine:

  POST /api/ptw/{id}/accept                       — receiver accepts (ISSUED → ACTIVE)
  POST /api/ptw/{id}/complete                     — work-completed declaration + outcome
  POST /api/ptw/{id}/handback                     — handback inspection (ex site-verify)
  POST /api/ptw/{id}/cancel                       — operational cancellation
  POST /api/ptw/{id}/archive                      — retention flag on CLOSED permits
  POST /api/ptw/{id}/isolations/{iso_id}/verify   — lock-out confirmation (was a dead end)
  GET  /api/ptw/{id}/evidence                     — evidence timeline (detail page + report)

  GET/POST/DELETE /api/ptw/{id}/attachments…      — two-phase signed-URL uploads
                                                     (mirrors the incident pattern)

Every action persists a PermitActionEvidence row (GPS + photo + signature per
the policy in app/services/ptw_evidence.py) — that is the closed loop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.flra import FLRA, FLRAStatus
from app.models.permit import (
    Permit,
    PermitActionEvidence,
    PermitAttachment,
    PermitEvidenceAction,
    PermitIsolation,
    PermitStatus,
    PermitSuspension,
)
from app.models.user import User
from app.models.workflow import (
    Action,
    InstanceStatus,
    TaskType,
    WorkflowHistory,
    WorkflowInstance,
    WorkflowTask,
)
from app.schemas.permit import (
    PERMIT_ATTACHMENT_CATEGORIES,
    AcceptRequest,
    CancelRequest,
    CompleteRequest,
    HandbackRequest,
    IsolationVerifyRequest,
    PermitAttachmentInit,
    PermitAttachmentOut,
    PtwEvidenceInput,
)
from app.services import workflow_engine
from app.services.permissions import PermissionContext, can, get_user_role_codes
from app.services.ptw_evidence import (
    EvidenceError,
    evidence_out,
    record_action_evidence,
)
from app.services.storage import (
    build_permit_storage_path,
    create_signed_download_url,
    create_signed_upload_url,
    is_storage_configured,
)
from app.services.workflow_engine import (
    OPEN_TASK_STATUSES,
    PTW_ACCEPT_STEP,
    PTW_LEGACY_RECEIVER_PREFIX,
    WorkflowError,
)

router = APIRouter(prefix="/api/ptw", tags=["ptw-lifecycle"])

ALLOWED_MIME = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic",
    "video/mp4", "video/quicktime",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv", "text/plain",
}
MAX_FILE_SIZE = 50 * 1024 * 1024

_PRIV_ROLES = {"HSE_MANAGER", "ADMIN", "SYSTEM_ADMIN"}
_HANDBACK_ROLES = {"PERMIT_ISSUER", "SAFETY_OFFICER", "PLANT_HEAD"} | _PRIV_ROLES


# ─── Helpers ───────────────────────────────────────────────────────────


async def _load_permit_or_403(
    db: AsyncSession, permit_id: str, user: User, op: str
) -> Permit:
    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
    result = await can(
        db,
        user.id,
        op,
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
    return permit


async def _evidence_or_422(
    db: AsyncSession,
    *,
    permit: Permit,
    action: PermitEvidenceAction,
    actor_id: str,
    ev: PtwEvidenceInput | None,
    comments: str | None = None,
    enforce: bool = True,
) -> PermitActionEvidence:
    try:
        return await record_action_evidence(
            db,
            permit=permit,
            action=action,
            actor_id=actor_id,
            gps_latitude=ev.gpsLatitude if ev else None,
            gps_longitude=ev.gpsLongitude if ev else None,
            gps_accuracy_meters=ev.gpsAccuracyMeters if ev else None,
            signature_image=ev.signatureImageBase64 if ev else None,
            declaration_text=ev.declarationText if ev else None,
            comments=comments,
            photo_attachment_ids=ev.photoAttachmentIds if ev else None,
            enforce=enforce,
        )
    except EvidenceError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Accept (ISSUED → ACTIVE) ──────────────────────────────────────────


@router.post("/{permit_id}/accept")
async def accept_permit(
    permit_id: str,
    payload: AcceptRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Receiver accepts the issued permit at the worksite — a first-class
    signed act (declaration + signature + GPS + onsite photo). Completes
    the workflow's receiver step; the activation gate (FLRA when required,
    crew validity, PPE, isolations, expiry) runs inside the engine."""
    # The receiver acts through their workflow EXECUTION task — the engine's
    # RBAC gate enforces PTW.EXECUTE + assignee match. Accept either UPDATE
    # or EXECUTE here so a worker-receiver without PTW.UPDATE isn't blocked
    # before the engine even sees the task.
    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
    _ctx = PermissionContext(
        record_id=permit.id,
        plant_id=permit.plantId,
        record={
            "originatorId": permit.originatorId,
            "issuerId": permit.issuerId,
            "receiverId": permit.receiverId,
        },
    )
    upd = await can(db, user.id, "PTW.UPDATE", _ctx)
    if not upd.allowed:
        exe = await can(db, user.id, "PTW.EXECUTE", _ctx)
        if not exe.allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, upd.reason or "Access denied")

    if permit.receiverId != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the named receiver can accept this permit.",
        )
    if permit.status not in (PermitStatus.ISSUED, PermitStatus.APPROVED):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Only an ISSUED permit can be accepted (current status: {permit.status.value}).",
        )

    # Locate the receiver's open acceptance task. Scalar columns only — the
    # engine reloads the task entity itself with its eager-load options.
    task_row = (
        await db.execute(
            select(WorkflowTask.id, WorkflowTask.stepName)
            .where(WorkflowTask.module == "PTW")
            .where(WorkflowTask.recordId == permit_id)
            .where(WorkflowTask.taskType == TaskType.EXECUTION.value)
            .where(WorkflowTask.status.in_(OPEN_TASK_STATUSES))
            .where(WorkflowTask.assignedToId == user.id)
            .where(
                (WorkflowTask.stepName == PTW_ACCEPT_STEP)
                | (WorkflowTask.stepName.like(f"{PTW_LEGACY_RECEIVER_PREFIX}%"))
            )
            .order_by(WorkflowTask.assignedAt.desc())
            .limit(1)
        )
    ).first()
    if task_row is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No open acceptance task found for you on this permit — the "
            "approval chain may not have completed yet.",
        )

    await _evidence_or_422(
        db,
        permit=permit,
        action=PermitEvidenceAction.ACCEPT,
        actor_id=user.id,
        ev=payload.evidence,
        comments=payload.comments,
    )

    try:
        result = await workflow_engine.submit_execution(
            db,
            task_id=task_row.id,
            user_id=user.id,
            comments=payload.comments or "Permit accepted by receiver at the worksite.",
            plant_id=permit.plantId,
            allow_ptw_accept=True,
        )
    except WorkflowError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    # The engine stamps activatedAt/activatedById when the instance advances
    # into the CLOSURE step; belt-and-braces for legacy definitions.
    if permit.activatedAt is None:
        permit.activatedAt = _now()
        permit.activatedById = user.id

    # Pin the effective FLRA on the permit (when the FLRA sub-flow ran).
    if permit.flraRequired and permit.currentActiveFlraId is None:
        flra_id = (
            await db.execute(
                select(FLRA.id)
                .where(FLRA.permitId == permit_id)
                .where(FLRA.status == FLRAStatus.COMPLETED)
                .order_by(FLRA.createdAt.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        permit.currentActiveFlraId = flra_id

    await db.flush()
    await db.refresh(permit)
    return {
        "ok": True,
        "status": permit.status.value,
        "activatedAt": permit.activatedAt.isoformat() if permit.activatedAt else None,
        "workflow": result,
    }


# ─── Work Completed declaration (ACTIVE/SUSPENDED → WORK_COMPLETED) ────


@router.post("/{permit_id}/complete")
async def declare_work_completed(
    permit_id: str,
    payload: CompleteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Receiver declares work done: structured outcome + restoration
    confirmations + narrative + evidence. Replaces the legacy Return step
    (returnedAt/returnedById stay stamped in lockstep for old read-sites)."""
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")

    if permit.receiverId is not None and permit.receiverId != user.id:
        role_codes = await get_user_role_codes(db, user.id)
        if not any(r in _PRIV_ROLES for r in role_codes):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Only the named receiver can declare work completed.",
            )
    if permit.status not in (PermitStatus.ACTIVE, PermitStatus.SUSPENDED):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot declare work completed on a {permit.status.value} permit.",
        )
    if permit.workCompletedAt is not None or permit.returnedAt is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Work has already been declared completed."
        )
    if not payload.isolationsRestored:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Confirm isolations are restored before declaring completion.",
        )
    if not payload.workAreaClean:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Confirm the work area is clean before declaring completion.",
        )

    await _evidence_or_422(
        db,
        permit=permit,
        action=PermitEvidenceAction.WORK_COMPLETED_DECLARE,
        actor_id=user.id,
        ev=payload.evidence,
        comments=payload.notes,
    )

    now = _now()

    # Restore any isolation rows still open (audit row per isolation).
    open_isolations = (
        await db.execute(
            select(PermitIsolation)
            .where(PermitIsolation.permitId == permit_id)
            .where(PermitIsolation.restoredAt.is_(None))
        )
    ).scalars().all()
    for iso in open_isolations:
        iso.restoredAt = now
        iso.restoredById = user.id

    # A permit completed while suspended: close the open suspension cycle so
    # the audit trail doesn't show a dangling suspension on a finished job.
    if permit.status == PermitStatus.SUSPENDED:
        open_susp = (
            await db.execute(
                select(PermitSuspension)
                .where(PermitSuspension.permitId == permit_id)
                .where(PermitSuspension.resumedAt.is_(None))
                .order_by(PermitSuspension.suspendedAt.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if open_susp is not None:
            open_susp.resumedAt = now
            open_susp.resumedById = user.id
            open_susp.resumptionConditions = (
                "Work Completed declared — suspension cycle closed."
            )

    permit.workCompletedAt = now
    permit.workCompletedById = user.id
    permit.outcome = payload.outcome
    # Legacy lockstep (old dashboards/read-sites use returnedAt).
    permit.returnedAt = now
    permit.returnedById = user.id
    permit.returnNotes = payload.notes
    permit.status = PermitStatus.WORK_COMPLETED
    permit.isCurrentlySuspended = False
    permit.suspendedAt = None
    permit.suspendedReason = None

    await db.flush()
    return {
        "ok": True,
        "status": permit.status.value,
        "outcome": payload.outcome.value,
        "workCompletedAt": now.isoformat(),
        "isolationsAutoRestored": len(open_isolations),
    }


# ─── Handback inspection (WORK_COMPLETED → HANDBACK_INSPECTION) ────────


@router.post("/{permit_id}/handback")
async def handback_inspection(
    permit_id: str,
    payload: HandbackRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Issuer / Safety Officer / Plant Head walks the site after the
    Work Completed declaration and records the inspection checklist +
    photos + GPS + signature. Closure approval cannot proceed without it."""
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")
    role_codes = await get_user_role_codes(db, user.id)
    if not any(r in _HANDBACK_ROLES for r in role_codes):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only Issuer / Safety Officer / Plant Head / HSE / Admin can "
            "record the handback inspection.",
        )
    if permit.workCompletedAt is None and permit.returnedAt is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Receiver must declare Work Completed before the handback inspection.",
        )
    if permit.siteVerifiedAt is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Handback inspection has already been recorded for this permit.",
        )

    failed = [k for k, v in payload.checklist.items() if not v]
    if failed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            (
                f"Checklist items not satisfied: {', '.join(failed)}. "
                "Re-walk the site or escalate to HSE before closing."
            ),
        )

    await _evidence_or_422(
        db,
        permit=permit,
        action=PermitEvidenceAction.HANDBACK_INSPECT,
        actor_id=user.id,
        ev=payload.evidence,
        comments=payload.notes,
    )

    permit.siteVerifiedAt = _now()
    permit.siteVerifiedById = user.id
    permit.siteVerificationChecklist = payload.checklist
    # Only WORK_COMPLETED moves forward; legacy in-flight permits that used
    # the old return flow keep their current status (the closure gate still
    # sees siteVerifiedAt).
    if permit.status == PermitStatus.WORK_COMPLETED:
        permit.status = PermitStatus.HANDBACK_INSPECTION
    await db.flush()
    return {
        "ok": True,
        "status": permit.status.value,
        "siteVerifiedAt": permit.siteVerifiedAt.isoformat(),
    }


# ─── Cancel ─────────────────────────────────────────────────────────────


@router.post("/{permit_id}/cancel")
async def cancel_permit(
    permit_id: str,
    payload: CancelRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Operational cancellation — the issuer/originator pulls a permit that
    hasn't started, or HSE/Admin pulls one mid-flow. Distinct from REJECTED
    (an approver's refusal during the approval chain)."""
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")

    pre_active = permit.status in (
        PermitStatus.DRAFT,
        PermitStatus.SUBMITTED,
        PermitStatus.APPROVED,
        PermitStatus.ISSUED,
    )
    mid_flow = permit.status in (PermitStatus.ACTIVE, PermitStatus.SUSPENDED)
    if not (pre_active or mid_flow):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"A {permit.status.value} permit cannot be cancelled.",
        )

    role_codes = await get_user_role_codes(db, user.id)
    is_priv = any(r in _PRIV_ROLES for r in role_codes)
    if mid_flow and not is_priv:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only HSE Manager / Admin can cancel a permit with work in progress.",
        )
    if pre_active and not (
        is_priv or user.id in (permit.originatorId, permit.issuerId)
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the originator, issuer, or HSE/Admin can cancel this permit.",
        )

    await _evidence_or_422(
        db,
        permit=permit,
        action=PermitEvidenceAction.CANCEL,
        actor_id=user.id,
        ev=payload.evidence,
        comments=payload.reason,
    )

    now = _now()
    from_status = permit.status.value
    permit.status = PermitStatus.CANCELLED
    permit.cancelledAt = now
    permit.cancelledById = user.id
    permit.cancellationReason = payload.reason
    permit.isCurrentlySuspended = False

    # Retire the workflow instance + its open tasks.
    instance = (
        await db.execute(
            select(WorkflowInstance)
            .where(WorkflowInstance.module == "PTW")
            .where(WorkflowInstance.recordId == permit_id)
        )
    ).scalar_one_or_none()
    if instance is not None and instance.status == InstanceStatus.IN_PROGRESS.value:
        instance.status = Action.CANCELLED.value
        instance.currentStepId = None
        instance.currentStepName = "Cancelled"
        instance.completedAt = now
        await workflow_engine._close_pending_tasks(db, instance_id=instance.id)
        db.add(
            WorkflowHistory(
                instanceId=instance.id,
                stepId=None,
                stepName="Cancelled",
                action=Action.CANCELLED.value,
                performedById=user.id,
                comments=payload.reason,
                fromStatus=from_status,
                toStatus=PermitStatus.CANCELLED.value,
            )
        )

    await db.flush()
    return {"ok": True, "status": PermitStatus.CANCELLED.value, "cancelledAt": now.isoformat()}


# ─── Archive ────────────────────────────────────────────────────────────


@router.post("/{permit_id}/archive")
async def archive_permit(
    permit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Retention flag layered on CLOSED — hides the permit from the default
    register view. Not a lifecycle transition; the closed record and its
    hash-chain stay untouched."""
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")
    if permit.status != PermitStatus.CLOSED:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Only CLOSED permits can be archived (current status: {permit.status.value}).",
        )
    if permit.isArchived:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Permit is already archived.")
    permit.isArchived = True
    permit.archivedAt = _now()
    await db.flush()
    return {"ok": True, "archivedAt": permit.archivedAt.isoformat()}


# ─── Isolation verification (was a functional dead end) ────────────────


@router.post("/{permit_id}/isolations/{isolation_id}/verify")
async def verify_isolation(
    permit_id: str,
    isolation_id: str,
    payload: IsolationVerifyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Confirm one isolation point is physically locked-out and tagged.
    The activation gate requires every isolation row to be verified before
    the permit can go ACTIVE — this endpoint is what sets it (previously
    NOTHING wrote isolationVerifiedAt, so permits with isolations could
    never activate through the API)."""
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")
    role_codes = await get_user_role_codes(db, user.id)
    if user.id != permit.receiverId and not any(
        r in _HANDBACK_ROLES for r in role_codes
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the receiver, Issuer, Safety Officer, Plant Head or HSE/Admin "
            "can verify isolations.",
        )
    iso = await db.get(PermitIsolation, isolation_id)
    if iso is None or iso.permitId != permit_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Isolation not found")
    if iso.isolationVerifiedAt is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Isolation already verified.")

    await _evidence_or_422(
        db,
        permit=permit,
        action=PermitEvidenceAction.ISOLATION_VERIFY,
        actor_id=user.id,
        ev=payload.evidence,
        comments=payload.notes
        or f"Isolation {iso.isolationPointTag} ({iso.isolationType}) verified.",
    )

    iso.isolationVerifiedAt = _now()
    iso.isolationVerifiedById = user.id
    await db.flush()

    remaining = (
        await db.execute(
            select(PermitIsolation.id)
            .where(PermitIsolation.permitId == permit_id)
            .where(PermitIsolation.isolationVerifiedAt.is_(None))
        )
    ).scalars().all()
    return {
        "ok": True,
        "isolationId": iso.id,
        "verifiedAt": iso.isolationVerifiedAt.isoformat(),
        "remainingUnverified": len(remaining),
    }


# ─── Evidence timeline ─────────────────────────────────────────────────


@router.get("/{permit_id}/evidence")
async def list_evidence(
    permit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _load_permit_or_403(db, permit_id, user, "PTW.READ")
    rows = (
        await db.execute(
            select(PermitActionEvidence)
            .options(selectinload(PermitActionEvidence.photos))
            .where(PermitActionEvidence.permitId == permit_id)
            .order_by(PermitActionEvidence.capturedAt.asc())
        )
    ).scalars().all()

    actor_ids = {r.actorId for r in rows}
    names: dict[str, str] = {}
    if actor_ids:
        u_rows = (
            await db.execute(select(User).where(User.id.in_(actor_ids)))
        ).scalars().all()
        names = {u.id: u.name for u in u_rows}

    items = []
    for r in rows:
        d = evidence_out(r)
        d["actorName"] = names.get(r.actorId)
        items.append(d)
    return {"items": items, "total": len(items)}


@router.get("/{permit_id}/evidence/{evidence_id}/signature")
async def get_evidence_signature(
    permit_id: str,
    evidence_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The signature image is served separately (it's a ~10 KB data URL —
    keeping it out of the list payload keeps the timeline light)."""
    await _load_permit_or_403(db, permit_id, user, "PTW.READ")
    row = await db.get(PermitActionEvidence, evidence_id)
    if row is None or row.permitId != permit_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence not found")
    return {"signatureImageBase64": row.signatureImageBase64}


# ─── Attachments (two-phase signed-URL upload — incident pattern) ──────


@router.get("/{permit_id}/attachments")
async def list_attachments(
    permit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _load_permit_or_403(db, permit_id, user, "PTW.READ")
    rows = (
        await db.execute(
            select(PermitAttachment)
            .where(PermitAttachment.permitId == permit_id)
            .where(PermitAttachment.deletedAt.is_(None))
            .order_by(PermitAttachment.uploadedAt.desc())
        )
    ).scalars().all()
    return {"items": [PermitAttachmentOut.model_validate(r) for r in rows]}


@router.post("/{permit_id}/attachments")
async def upload_attachment(
    permit_id: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # Uploaders include APPROVERS attaching onsite evidence photos — a
    # Safety Officer / Plant Head holds PTW.APPROVE but not necessarily
    # PTW.UPDATE, so accept either grant.
    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
    _ctx = PermissionContext(
        record_id=permit.id,
        plant_id=permit.plantId,
        record={
            "originatorId": permit.originatorId,
            "issuerId": permit.issuerId,
            "receiverId": permit.receiverId,
        },
    )
    upd = await can(db, user.id, "PTW.UPDATE", _ctx)
    if not upd.allowed:
        appr = await can(db, user.id, "PTW.APPROVE", _ctx)
        if not appr.allowed:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, upd.reason or "Access denied"
            )
    if not is_storage_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Supabase Storage isn't configured. Set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.",
        )

    phase = payload.get("phase")
    if phase == "init":
        try:
            init = PermitAttachmentInit(**payload)
        except Exception as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid init body: {e}") from e
        if init.category not in PERMIT_ATTACHMENT_CATEGORIES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Invalid category. Must be one of: {', '.join(sorted(PERMIT_ATTACHMENT_CATEGORIES))}",
            )
        if init.fileSize > MAX_FILE_SIZE:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"File size exceeds the {MAX_FILE_SIZE // 1024 // 1024} MB limit.",
            )
        if init.mimeType not in ALLOWED_MIME:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"MIME type {init.mimeType} is not allowed."
            )
        storage_path = build_permit_storage_path(
            permit_id=permit_id, category=init.category, file_name=init.fileName
        )
        try:
            signed = create_signed_upload_url(storage_path)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"Storage upload init failed: {e}",
            ) from e
        att = PermitAttachment(
            permitId=permit.id,
            category=init.category,
            fileName=init.fileName,
            storagePath=storage_path,
            fileSize=init.fileSize,
            mimeType=init.mimeType,
            uploadedById=user.id,
        )
        db.add(att)
        await db.flush()
        return {
            "phase": "init",
            "attachmentId": att.id,
            "storagePath": storage_path,
            "uploadUrl": signed["uploadUrl"],
            "token": signed["token"],
        }

    if phase == "complete":
        attachment_id = payload.get("attachmentId")
        if not attachment_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "attachmentId required")
        att = await db.get(PermitAttachment, attachment_id)
        if att is None or att.permitId != permit_id:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "Attachment not found for this permit"
            )
        att.caption = payload.get("caption")
        await db.flush()
        return {"ok": True, "attachmentId": att.id}

    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown phase")


@router.delete("/{permit_id}/attachments/{attachment_id}")
async def delete_attachment(
    permit_id: str,
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    att = await db.get(PermitAttachment, attachment_id)
    if att is None or att.permitId != permit_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    if att.actionEvidenceId is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This photo is linked to a signed lifecycle action and cannot be deleted.",
        )
    await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")
    att.deletedAt = _now()
    await db.flush()
    return {"ok": True}


@router.get("/{permit_id}/attachments/{attachment_id}/download")
async def download_attachment(
    permit_id: str,
    attachment_id: str,
    inline: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    att = await db.get(PermitAttachment, attachment_id)
    if att is None or att.permitId != permit_id or att.deletedAt is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    await _load_permit_or_403(db, permit_id, user, "PTW.READ")
    url = create_signed_download_url(
        att.storagePath,
        expires_in_sec=300,
        download=None if inline else att.fileName,
    )
    return {"url": url}
