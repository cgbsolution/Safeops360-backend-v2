"""Shared Evidence Attachment router (Stream B §5). Mounts at /api/evidence.

One generic two-phase Supabase signed-URL upload flow for every attachable
entity, driven by the `evidence_registry` (entityType → model + permission
codes + plant resolver + allowed categories). Adding a new attachable module is
a single registry entry — the endpoints below don't change.

Endpoints (all keyed by /{entity_type}/{entity_id}):
  POST   …                      two-phase upload (init → complete)
  GET    …                      list current (non-deleted) files
  GET    …/{id}/versions        the full version chain for an id's slot
  GET    …/{id}/download        mint a short-lived signed download URL
  DELETE …/{id}                 soft-delete (deletedAt), preserving the row
  GET    /{entity_type}/counts  per-id current-file counts for list badges

The browser never sees the service-role key — it PUTs straight to the signed
URL; this API only mints URLs + records metadata (same contract as the existing
per-module attachment routers).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.attachment import Attachment
from app.models.user import User
from app.schemas.attachment import DOCUMENT_CATEGORIES, AttachmentOut, EvidenceInit
from app.services.evidence_registry import EntitySpec, get_spec, supported_entities
from app.services.permissions import PermissionContext, can
from app.services.storage import (
    build_evidence_storage_path,
    create_signed_download_url,
    create_signed_upload_url,
    is_storage_configured,
)

router = APIRouter(prefix="/api/evidence", tags=["evidence"])

MAX_FILE_SIZE = 25 * 1024 * 1024
ALLOWED_MIME = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
    "text/csv",
    "text/plain",
}


def _spec_or_404(entity_type: str) -> EntitySpec:
    spec = get_spec(entity_type)
    if spec is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Unknown attachable entity '{entity_type}'. Registered: {', '.join(supported_entities())}",
        )
    return spec


def _storage_guard() -> None:
    if not is_storage_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Supabase Storage isn't configured. Set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.",
        )


async def _resolve_parent(db: AsyncSession, spec: EntitySpec, entity_id: str) -> tuple[Any, str | None]:
    row = await db.get(spec.model, entity_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{spec.label} not found")
    plant_id = getattr(row, spec.plant_attr) if spec.plant_attr else None
    return row, plant_id


async def _require(
    db: AsyncSession, user: User, code: str, *, plant_id: str | None, record_id: str | None,
    record: dict | None = None,
) -> None:
    res = await can(db, user.id, code, PermissionContext(plant_id=plant_id, record_id=record_id, record=record))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


def _validate_init(spec: EntitySpec, init: EvidenceInit) -> None:
    if init.category not in spec.categories:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Invalid category for {spec.label}. Allowed: {', '.join(sorted(spec.categories))}",
        )
    if init.documentCategory is not None and init.documentCategory not in DOCUMENT_CATEGORIES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Invalid documentCategory. Allowed: {', '.join(sorted(DOCUMENT_CATEGORIES))}",
        )
    if init.fileSize > MAX_FILE_SIZE:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"File exceeds the {MAX_FILE_SIZE // 1024 // 1024} MB limit."
        )
    if init.mimeType not in ALLOWED_MIME:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"MIME type {init.mimeType} is not allowed.")


# ═══════════════════════════════════════════════════════════════════════════
#  Upload (two-phase)
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/{entity_type}/{entity_id}")
async def upload(
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    spec = _spec_or_404(entity_type)
    _, plant_id = await _resolve_parent(db, spec, entity_id)
    await _require(db, user, spec.write_perm, plant_id=plant_id, record_id=entity_id)
    _storage_guard()

    phase = payload.get("phase")
    if phase == "init":
        try:
            init = EvidenceInit(**payload)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid init body: {e}") from e
        _validate_init(spec, init)
        storage_path = build_evidence_storage_path(
            entity_type=entity_type, entity_id=entity_id, category=init.category, file_name=init.fileName
        )
        try:
            signed = create_signed_upload_url(storage_path)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Storage init failed: {e}") from e
        att = Attachment(
            entityType=entity_type,
            entityId=entity_id,
            category=init.category,
            documentCategory=init.documentCategory,
            fileName=init.fileName,
            storagePath=storage_path,
            fileSize=init.fileSize,
            mimeType=init.mimeType,
            caption=init.caption,
            slotKey=init.slotKey,
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
        att = await db.get(Attachment, attachment_id)
        if att is None or att.entityType != entity_type or att.entityId != entity_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found for this entity")
        if payload.get("caption") is not None:
            att.caption = payload.get("caption")
        # Versioning: on completing a slotted upload, supersede prior current
        # files in the same slot (keeps them queryable; never a silent overwrite).
        if att.slotKey:
            others = (
                await db.execute(
                    select(Attachment)
                    .where(Attachment.entityType == entity_type)
                    .where(Attachment.entityId == entity_id)
                    .where(Attachment.slotKey == att.slotKey)
                    .where(Attachment.id != att.id)
                    .where(Attachment.deletedAt.is_(None))
                    .order_by(Attachment.version.desc())
                )
            ).scalars().all()
            if others:
                att.version = (others[0].version or 1) + 1
                att.supersedesId = others[0].id
                for o in others:
                    o.isCurrent = False
        await db.flush()
        return {"ok": True, "version": att.version}

    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown phase")


# ═══════════════════════════════════════════════════════════════════════════
#  Read
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/{entity_type}/counts")
async def counts(
    entity_type: str,
    ids: str = Query(..., description="Comma-separated entity ids"),
    user: User = Depends(get_current_user),  # noqa: ARG001 — auth gate only
    db: AsyncSession = Depends(get_db),
) -> dict[str, dict[str, int]]:
    """Current (non-deleted) file count per entity id — powers the list-row
    paperclip badge (spec §5.2). Auth-gated; ids are already scoped to what the
    caller can see on the list."""
    _spec_or_404(entity_type)
    id_list = [i for i in (ids.split(",") if ids else []) if i]
    if not id_list:
        return {"counts": {}}
    rows = (
        await db.execute(
            select(Attachment.entityId, func.count())
            .where(Attachment.entityType == entity_type)
            .where(Attachment.entityId.in_(id_list))
            .where(Attachment.isCurrent.is_(True))
            .where(Attachment.deletedAt.is_(None))
            .group_by(Attachment.entityId)
        )
    ).all()
    return {"counts": {eid: n for eid, n in rows}}


@router.get("/{entity_type}/{entity_id}")
async def list_attachments(
    entity_type: str,
    entity_id: str,
    include_superseded: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    spec = _spec_or_404(entity_type)
    _, plant_id = await _resolve_parent(db, spec, entity_id)
    await _require(db, user, spec.read_perm, plant_id=plant_id, record_id=entity_id)
    q = (
        select(Attachment)
        .where(Attachment.entityType == entity_type)
        .where(Attachment.entityId == entity_id)
        .where(Attachment.deletedAt.is_(None))
        .order_by(Attachment.uploadedAt.desc())
    )
    if not include_superseded:
        q = q.where(Attachment.isCurrent.is_(True))
    rows = (await db.execute(q)).scalars().all()
    return {"items": [AttachmentOut.model_validate(r) for r in rows]}


@router.get("/{entity_type}/{entity_id}/{attachment_id}/download")
async def download(
    entity_type: str,
    entity_id: str,
    attachment_id: str,
    inline: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    spec = _spec_or_404(entity_type)
    att = await db.get(Attachment, attachment_id)
    if att is None or att.entityType != entity_type or att.entityId != entity_id or att.deletedAt is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    _, plant_id = await _resolve_parent(db, spec, entity_id)
    is_uploader = att.uploadedById == user.id
    res = await can(
        db, user.id, spec.read_perm, PermissionContext(plant_id=plant_id, record_id=entity_id)
    )
    if not res.allowed and not is_uploader:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")
    url = create_signed_download_url(
        att.storagePath, expires_in_sec=300, download=None if inline else att.fileName
    )
    return {"url": url}


@router.delete("/{entity_type}/{entity_id}/{attachment_id}")
async def delete(
    entity_type: str,
    entity_id: str,
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    spec = _spec_or_404(entity_type)
    att = await db.get(Attachment, attachment_id)
    if att is None or att.entityType != entity_type or att.entityId != entity_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    _, plant_id = await _resolve_parent(db, spec, entity_id)
    await _require(
        db, user, spec.write_perm, plant_id=plant_id, record_id=entity_id,
        record={"uploadedById": att.uploadedById},
    )
    att.deletedAt = datetime.now(timezone.utc)
    att.isCurrent = False
    await db.flush()
    return {"ok": True}
