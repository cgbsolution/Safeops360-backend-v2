"""Observations router. Vertical slice that demonstrates the full pattern
the other modules will follow:

  • authorize() at the top of every handler
  • plant-scope filter on list queries (via get_accessible_plants)
  • workflow engine kicked off on create
  • permission service consulted for both module action AND scope

This file is the template for porting the remaining 7 operational modules.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.observation import Observation, ObservationAttachment, ObservationStatus
from app.models.plant import Plant
from app.models.user import User
from app.models.workflow import WorkflowTask
from app.schemas.observation import (
    ObservationCreate,
    ObservationListResponse,
    ObservationOut,
    ObservationUpdate,
)
from app.services import workflow_engine
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
)
from app.services.storage import (
    build_storage_path,
    create_signed_download_url,
    create_signed_upload_url,
    is_storage_configured,
)

router = APIRouter(prefix="/api/observations", tags=["observations"])


async def _is_workflow_actor(db: AsyncSession, user_id: str, observation_id: str) -> bool:
    """True if the caller has any WorkflowTask (pending or completed) for
    this observation. Workflow assignees need to read the record's
    attachments to do their job, even when their role's OBSERVATION.READ
    scope is OWN_RECORDS and they aren't the observer/responsible person."""
    stmt = (
        select(WorkflowTask.id)
        .where(WorkflowTask.module == "OBSERVATION")
        .where(WorkflowTask.recordId == observation_id)
        .where(WorkflowTask.assignedToId == user_id)
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def _has_uploaded_attachment(db: AsyncSession, user_id: str, observation_id: str) -> bool:
    """True if the caller uploaded at least one (non-deleted) attachment to
    this observation. Whoever contributes evidence must always be able to see
    it back in the gallery, even without an OBSERVATION.READ grant."""
    stmt = (
        select(ObservationAttachment.id)
        .where(ObservationAttachment.observationId == observation_id)
        .where(ObservationAttachment.uploadedById == user_id)
        .where(ObservationAttachment.deletedAt.is_(None))
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


@router.get("", response_model=ObservationListResponse)
async def list_observations(
    status_filter: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ObservationListResponse:
    """List observations the caller can see. Plant-scoped server-side."""
    read_check = await can(db, user.id, "OBSERVATION.READ", PermissionContext())
    if not read_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, read_check.reason or "Access denied")

    accessible_plants = await get_accessible_plants(db, user.id)

    stmt = select(Observation)
    # Apply plant scope. None ⇒ ALL_PLANTS (no filter). Empty list ⇒ no rows.
    if accessible_plants is None:
        pass
    elif len(accessible_plants) == 0:
        return ObservationListResponse(items=[], total=0)
    else:
        stmt = stmt.where(Observation.plantId.in_(accessible_plants))

    # OWN_RECORDS users (e.g. Workers) only see records they're attached to.
    # We detect this by absence of OWN_PLANT/OWN_DEPARTMENT/ALL_PLANTS scopes.
    # The check is rough — refine later when needed.
    if accessible_plants is not None and read_check.matched_scope == "OWN_RECORDS":
        stmt = stmt.where(
            (Observation.observerId == user.id) | (Observation.responsiblePersonId == user.id)
        )

    if status_filter:
        try:
            stmt = stmt.where(Observation.status == ObservationStatus(status_filter))
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid status: {status_filter}") from e

    stmt = stmt.order_by(Observation.date.desc()).limit(100)
    rows = (await db.execute(stmt)).scalars().all()
    return ObservationListResponse(items=[ObservationOut.model_validate(r) for r in rows], total=len(rows))


@router.post("", response_model=ObservationOut, status_code=status.HTTP_201_CREATED)
async def create_observation(
    payload: ObservationCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ObservationOut:
    await require_permission_with_context(
        "OBSERVATION.CREATE", user, db, plant_id=payload.plantId
    )

    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")

    # Compare on date only — sidesteps the offset-naive vs offset-aware
    # datetime mismatch you get when the form sends a bare YYYY-MM-DD that
    # Pydantic parses as a naive datetime.
    if payload.targetDate is not None:
        target_d = payload.targetDate.date() if hasattr(payload.targetDate, "date") else payload.targetDate
        if target_d < datetime.now(timezone.utc).date():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Target closure date cannot be in the past.")

    # Number generation — same pattern as Node side: count existing rows + 1
    count_stmt = select(func.count()).select_from(Observation).where(Observation.plantId == payload.plantId)
    last = (await db.execute(count_stmt)).scalar_one()
    number = f"SO-{payload.date.year}-{plant.code}-{last + 1:04d}"

    obs = Observation(
        number=number,
        date=payload.date,
        type=payload.type,
        category=payload.category,
        severity=payload.severity,
        plantId=payload.plantId,
        areaId=payload.areaId,
        observerId=user.id,
        responsiblePersonId=payload.responsiblePersonId,
        description=payload.description,
        immediateAction=payload.immediateAction,
        targetDate=payload.targetDate,
        status=ObservationStatus.OPEN,
    )
    db.add(obs)
    await db.flush()

    # Kick off workflow. Best-effort — workflow init failures must NOT
    # poison the main transaction (otherwise the Observation INSERT, even
    # though already flushed, gets rolled back at commit time → 500).
    # Wrap in a SAVEPOINT so a flush failure in the engine rolls back
    # only the engine's partial work, leaving the outer transaction
    # consistent.
    import sys
    import traceback

    try:
        async with db.begin_nested():
            await workflow_engine.initiate(
                db,
                module="OBSERVATION",
                record_id=obs.id,
                record_number=obs.number,
                record_title=obs.description[:120],
                record_data={
                    "type": obs.type.value,
                    "severity": obs.severity.value,
                    "plantId": obs.plantId,
                    "observerId": obs.observerId,
                    "responsiblePersonId": obs.responsiblePersonId,
                },
                initiator_id=user.id,
                plant_id=obs.plantId,
            )
    except Exception as e:  # noqa: BLE001
        print(f"Observation workflow init failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    # TriageAgent — run on submission. Best-effort, never blocks creation.
    # Output is appended to closureTriggers with ruleId="rule_triage_on_submit".
    # Same SAVEPOINT pattern: a write failure here (e.g. column missing,
    # transient DB error) rolls back only this block.
    try:
        async with db.begin_nested():
            from app.services.ai.agents.triage import run_triage

            triage = await run_triage(
                observation={
                    "type": obs.type.value,
                    "category": obs.category.value,
                    "severity": obs.severity.value,
                    "description": obs.description,
                    "immediateAction": obs.immediateAction,
                }
            )
            if triage is not None:
                entry = {
                    "ruleId": "rule_triage_on_submit",
                    "ruleName": "Triage (AI)",
                    "fired": not triage.get("skipped", False),
                    "reason": triage.get("rationale") or triage.get("reason") or "",
                    "spawnedRecordType": "AI_TRIAGE",
                    "data": triage,
                }
                existing = obs.closureTriggers or []
                if not isinstance(existing, list):
                    existing = []
                obs.closureTriggers = [entry, *existing]
                await db.flush()
    except Exception as e:  # noqa: BLE001
        print(f"TriageAgent failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    # Final refresh before serialising. The savepoint flushes above
    # (workflow init, TriageAgent's UPDATE on closureTriggers) leave
    # `obs` with expired attributes — even with expire_on_commit=False
    # SQLAlchemy can mark fields stale after a write. Reading any of
    # those (e.g. updatedAt) inside Pydantic's sync validator triggers
    # MissingGreenlet because the lazy load needs an async context.
    # Refreshing here loads everything in one round-trip.
    await db.refresh(obs)
    return ObservationOut.model_validate(obs)


@router.get("/{observation_id}", response_model=ObservationOut)
async def get_observation(
    observation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ObservationOut:
    obs = await db.get(Observation, observation_id)
    if obs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Observation not found")

    record_dict = {
        "observerId": obs.observerId,
        "responsiblePersonId": obs.responsiblePersonId,
    }
    result = await can(
        db,
        user.id,
        "OBSERVATION.READ",
        PermissionContext(record_id=obs.id, plant_id=obs.plantId, record=record_dict),
    )
    if not result.allowed and not await _is_workflow_actor(db, user.id, observation_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    return ObservationOut.model_validate(obs)


@router.patch("/{observation_id}", response_model=ObservationOut)
async def update_observation(
    observation_id: str,
    payload: ObservationUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ObservationOut:
    obs = await db.get(Observation, observation_id)
    if obs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Observation not found")

    record_dict = {
        "observerId": obs.observerId,
        "responsiblePersonId": obs.responsiblePersonId,
    }
    perm_code = "OBSERVATION.CLOSE" if payload.status == ObservationStatus.CLOSED else "OBSERVATION.UPDATE"
    result = await can(
        db,
        user.id,
        perm_code,
        PermissionContext(record_id=obs.id, plant_id=obs.plantId, record=record_dict),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    if payload.status is not None:
        obs.status = payload.status
        if payload.status == ObservationStatus.CLOSED:
            obs.closedAt = datetime.now(timezone.utc)
    if payload.closingRemark is not None:
        obs.closingRemark = payload.closingRemark
    await db.flush()
    await db.refresh(obs)
    return ObservationOut.model_validate(obs)


@router.delete("/{observation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_observation(
    observation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hard-delete an observation. Per the RBAC matrix, only HSE_MANAGER
    (own plant), CORPORATE_HSE (all plants), and SYSTEM_ADMIN (all plants)
    have OBSERVATION.DELETE — the permission service enforces the scope.
    Cascades remove the workflow instance, tasks, history, and any
    attachments via DB foreign keys (ondelete=CASCADE)."""
    obs = await db.get(Observation, observation_id)
    if obs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Observation not found")
    record_dict = {
        "observerId": obs.observerId,
        "responsiblePersonId": obs.responsiblePersonId,
    }
    result = await can(
        db,
        user.id,
        "OBSERVATION.DELETE",
        PermissionContext(record_id=obs.id, plant_id=obs.plantId, record=record_dict),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    # Drop the workflow instance + downstream rows first. Prisma's
    # WorkflowInstance has ondelete CASCADE on its FKs to history/tasks,
    # but Observation doesn't FK into WorkflowInstance — we have to
    # delete it ourselves.
    from app.models.workflow import WorkflowInstance

    inst_rows = (
        await db.execute(
            select(WorkflowInstance).where(
                WorkflowInstance.module == "OBSERVATION",
                WorkflowInstance.recordId == observation_id,
            )
        )
    ).scalars().all()
    for inst in inst_rows:
        await db.delete(inst)

    # Soft-delete attachments instead of hard-delete so the storage
    # objects (Supabase) can be reaped later by a cleanup job.
    att_rows = (
        await db.execute(
            select(ObservationAttachment).where(
                ObservationAttachment.observationId == observation_id,
                ObservationAttachment.deletedAt.is_(None),
            )
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    for att in att_rows:
        att.deletedAt = now

    await db.delete(obs)
    await db.flush()
    return None


# ─── Attachments ─────────────────────────────────────────────────────────
# Same two-phase upload pattern as IncidentAttachment — see that router for
# the design notes (init → direct PUT to Supabase signed URL → complete).

VALID_OBS_CATEGORIES = {"INITIAL_PHOTO", "ACTION_EVIDENCE", "VERIFICATION_PHOTO", "DOCUMENT"}
ALLOWED_OBS_MIME = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic",
    "video/mp4", "video/quicktime",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv", "text/plain",
}
MAX_OBS_FILE_SIZE = 50 * 1024 * 1024


def _attachment_to_dict(a: ObservationAttachment) -> dict[str, Any]:
    # Frontend (attachment-gallery.tsx) reads `att.uploadedBy.id` — emit the
    # nested user object, not just the flat id, otherwise it crashes with
    # "Cannot read properties of undefined".
    uploaded_by: dict[str, Any] | None = None
    if a.uploadedBy is not None:
        uploaded_by = {
            "id": a.uploadedBy.id,
            "name": a.uploadedBy.name,
            "designation": a.uploadedBy.designation,
        }
    return {
        "id": a.id,
        "observationId": a.observationId,
        "category": a.category,
        "fileName": a.fileName,
        "fileSize": a.fileSize,
        "mimeType": a.mimeType,
        "caption": a.caption,
        "exifData": a.exifData,
        "uploadedAt": a.uploadedAt,
        "uploadedById": a.uploadedById,
        "uploadedBy": uploaded_by,
    }


@router.get("/{observation_id}/attachments")
async def list_attachments(
    observation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    obs = await db.get(Observation, observation_id)
    if obs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Observation not found")
    record = {"observerId": obs.observerId, "responsiblePersonId": obs.responsiblePersonId}
    result = await can(
        db, user.id, "OBSERVATION.READ",
        PermissionContext(record_id=obs.id, plant_id=obs.plantId, record=record),
    )
    # The observer and anyone who uploaded evidence here can always see the
    # gallery, even without an OBSERVATION.READ grant — so an uploader never
    # loses sight of their own contribution.
    if (
        not result.allowed
        and obs.observerId != user.id
        and not await _is_workflow_actor(db, user.id, observation_id)
        and not await _has_uploaded_attachment(db, user.id, observation_id)
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    rows = (
        await db.execute(
            select(ObservationAttachment)
            .options(selectinload(ObservationAttachment.uploadedBy))
            .where(ObservationAttachment.observationId == observation_id)
            .where(ObservationAttachment.deletedAt.is_(None))
            .order_by(ObservationAttachment.uploadedAt.desc())
        )
    ).scalars().all()
    return {"items": [_attachment_to_dict(r) for r in rows]}


@router.post("/{observation_id}/attachments")
async def upload_attachment(
    observation_id: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    obs = await db.get(Observation, observation_id)
    if obs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Observation not found")
    record = {"observerId": obs.observerId, "responsiblePersonId": obs.responsiblePersonId}
    result = await can(
        db, user.id, "OBSERVATION.UPDATE",
        PermissionContext(record_id=obs.id, plant_id=obs.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    if not is_storage_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Supabase Storage isn't configured. Set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.",
        )

    phase = payload.get("phase")
    if phase == "init":
        category = str(payload.get("category") or "")
        file_name = str(payload.get("fileName") or "").strip()
        file_size = int(payload.get("fileSize") or 0)
        mime_type = str(payload.get("mimeType") or "")
        if not file_name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "File name is required")
        if category not in VALID_OBS_CATEGORIES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid category. Must be one of: {', '.join(VALID_OBS_CATEGORIES)}")
        if file_size <= 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "File size must be a positive number")
        if file_size > MAX_OBS_FILE_SIZE:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"File size exceeds the {MAX_OBS_FILE_SIZE // 1024 // 1024} MB limit.")
        if mime_type not in ALLOWED_OBS_MIME:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"MIME type {mime_type} is not allowed.")

        # Reuse incident-style storage path layout, namespaced by "observations"
        from app.services.storage import build_storage_path
        storage_path = build_storage_path(incident_id=observation_id, category=category, file_name=file_name)
        # Override the prefix from "incidents/" to "observations/" — the helper
        # always writes incidents/ but the bucket is shared and we want a clear
        # namespace per module. Done via simple replace.
        if storage_path.startswith("incidents/"):
            storage_path = "observations/" + storage_path[len("incidents/"):]
        try:
            signed = create_signed_upload_url(storage_path)
        except Exception as e:  # noqa: BLE001
            # Bubble up a helpful error so the UI can show what's wrong
            # (bucket missing, wrong key, RLS denial, etc.) instead of a
            # generic "Init failed".
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"Storage upload init failed: {e}",
            ) from e

        att = ObservationAttachment(
            observationId=observation_id,
            category=category,
            fileName=file_name,
            storagePath=storage_path,
            fileSize=file_size,
            mimeType=mime_type,
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
        att = await db.get(ObservationAttachment, attachment_id)
        if att is None or att.observationId != observation_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found for this observation")
        att.caption = payload.get("caption")
        att.exifData = payload.get("exifData")
        await db.flush()
        return {"ok": True}

    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown phase")


@router.delete("/{observation_id}/attachments/{attachment_id}")
async def delete_attachment(
    observation_id: str,
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    att = await db.get(ObservationAttachment, attachment_id)
    if att is None or att.observationId != observation_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    obs = await db.get(Observation, observation_id)
    # The uploader can always remove their own attachment — covers the
    # action-owner-uploaded-by-mistake / rework-rejection cases. Falls
    # through to the standard RBAC check otherwise.
    is_uploader = att.uploadedById == user.id
    if not is_uploader:
        record = {
            "observerId": obs.observerId if obs else None,
            "responsiblePersonId": obs.responsiblePersonId if obs else None,
            "uploadedById": att.uploadedById,
        }
        result = await can(
            db, user.id, "OBSERVATION.UPDATE",
            PermissionContext(record_id=att.id, plant_id=obs.plantId if obs else None, record=record),
        )
        if not result.allowed and not await _is_workflow_actor(db, user.id, observation_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    att.deletedAt = datetime.now(timezone.utc)
    await db.flush()
    return {"ok": True}


@router.get("/{observation_id}/attachments/{attachment_id}/download")
async def download_attachment(
    observation_id: str,
    attachment_id: str,
    inline: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    att = await db.get(ObservationAttachment, attachment_id)
    if att is None or att.observationId != observation_id or att.deletedAt is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    obs = await db.get(Observation, observation_id)
    # The uploader can always view their own file — mirrors delete_attachment's
    # is_uploader bypass so the person who uploaded a photo can preview it even
    # without an OBSERVATION.READ grant.
    is_uploader = att.uploadedById == user.id
    record = {
        "observerId": obs.observerId if obs else None,
        "responsiblePersonId": obs.responsiblePersonId if obs else None,
        "uploadedById": att.uploadedById,
    }
    result = await can(
        db, user.id, "OBSERVATION.READ",
        PermissionContext(record_id=obs.id if obs else None, plant_id=obs.plantId if obs else None, record=record),
    )
    if not result.allowed and not is_uploader and not await _is_workflow_actor(db, user.id, observation_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    url = create_signed_download_url(
        att.storagePath,
        expires_in_sec=300,
        download=None if inline else att.fileName,
    )
    return {"url": url}
