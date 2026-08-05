"""ERM attachment endpoints — supporting documents on an EnterpriseRisk and
evidence files on a Control.

Two-phase Supabase signed-URL upload, cloned from the incident attachment flow
(app/routers/incidents.py). The browser PUTs directly to a short-lived signed
URL; this API only mints URLs and records metadata.

Permissions reuse the ERM/Control codes already enforced on risk & control
mutations:
  * Risk    → ERM.READ (read/download) / ERM.UPDATE (upload/delete)
  * Control → CONTROL.READ (read/download) / CONTROL.WRITE (upload/delete)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.erm import EnterpriseRisk
from app.models.erm_attachments import ControlAttachment, RiskAttachment
from app.models.erm_t3 import Control
from app.models.user import User
from app.schemas.erm_attachments import (
    AttachmentInit,
    ControlAttachmentOut,
    RiskAttachmentOut,
)
from app.services.permissions import PermissionContext, can
from app.services.storage import (
    build_control_storage_path,
    build_risk_storage_path,
    create_signed_download_url,
    create_signed_upload_url,
    is_storage_configured,
)

router = APIRouter(prefix="/api/erm", tags=["erm-attachments"])

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
    "application/vnd.ms-outlook",  # msg
}
RISK_VALID_CATEGORIES = {"SUPPORTING_DOC", "RISK_EVIDENCE", "ASSESSMENT_BASIS", "OTHER"}
CONTROL_VALID_CATEGORIES = {"CONTROL_EVIDENCE", "TEST_WORKPAPER", "REVIEW_EVIDENCE", "OTHER"}


async def _require(
    db: AsyncSession,
    user: User,
    code: str,
    *,
    plant_id: str | None = None,
    record: dict | None = None,
    record_id: str | None = None,
) -> None:
    res = await can(
        db, user.id, code, PermissionContext(plant_id=plant_id, record=record, record_id=record_id)
    )
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


def _storage_guard() -> None:
    if not is_storage_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Supabase Storage isn't configured. Set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.",
        )


def _validate_init(init: AttachmentInit, valid_categories: set[str]) -> None:
    if init.category not in valid_categories:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Invalid category. Must be one of: {', '.join(sorted(valid_categories))}",
        )
    if init.fileSize > MAX_FILE_SIZE:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"File size exceeds the {MAX_FILE_SIZE // 1024 // 1024} MB limit.",
        )
    if init.mimeType not in ALLOWED_MIME:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"MIME type {init.mimeType} is not allowed.")


# ═══════════════════════════════════════════════════════════════════════
#  Risk attachments
# ═══════════════════════════════════════════════════════════════════════


@router.post("/risks/{risk_id}/attachments")
async def upload_risk_attachment(
    risk_id: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    risk = await db.get(EnterpriseRisk, risk_id)
    if risk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Risk not found")
    await _require(db, user, "ERM.UPDATE", plant_id=risk.plantId, record_id=risk.id)
    _storage_guard()

    phase = payload.get("phase")
    if phase == "init":
        try:
            init = AttachmentInit(**payload)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid init body: {e}") from e
        _validate_init(init, RISK_VALID_CATEGORIES)
        storage_path = build_risk_storage_path(
            risk_id=risk_id, category=init.category, file_name=init.fileName
        )
        try:
            signed = create_signed_upload_url(storage_path)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"Storage upload init failed: {e}"
            ) from e
        att = RiskAttachment(
            riskId=risk_id,
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
        att = await db.get(RiskAttachment, attachment_id)
        if att is None or att.riskId != risk_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found for this risk")
        att.caption = payload.get("caption")
        await db.flush()
        return {"ok": True}

    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown phase")


@router.get("/risks/{risk_id}/attachments")
async def list_risk_attachments(
    risk_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    risk = await db.get(EnterpriseRisk, risk_id)
    if risk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Risk not found")
    await _require(db, user, "ERM.READ", plant_id=risk.plantId, record_id=risk.id)
    rows = (
        (
            await db.execute(
                select(RiskAttachment)
                .where(RiskAttachment.riskId == risk_id)
                .where(RiskAttachment.deletedAt.is_(None))
                .order_by(RiskAttachment.uploadedAt.desc())
            )
        )
        .scalars()
        .all()
    )
    return {"items": [RiskAttachmentOut.model_validate(r) for r in rows]}


@router.get("/risks/{risk_id}/attachments/{attachment_id}/download")
async def download_risk_attachment(
    risk_id: str,
    attachment_id: str,
    inline: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    att = await db.get(RiskAttachment, attachment_id)
    if att is None or att.riskId != risk_id or att.deletedAt is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    risk = await db.get(EnterpriseRisk, risk_id)
    # The uploader can always view their own file.
    is_uploader = att.uploadedById == user.id
    res = await can(
        db, user.id, "ERM.READ",
        PermissionContext(plant_id=risk.plantId if risk else None, record_id=risk.id if risk else None),
    )
    if not res.allowed and not is_uploader:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")
    url = create_signed_download_url(
        att.storagePath,
        expires_in_sec=300,
        download=None if inline else att.fileName,
    )
    return {"url": url}


@router.delete("/risks/{risk_id}/attachments/{attachment_id}")
async def delete_risk_attachment(
    risk_id: str,
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    att = await db.get(RiskAttachment, attachment_id)
    if att is None or att.riskId != risk_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    risk = await db.get(EnterpriseRisk, risk_id)
    await _require(
        db, user, "ERM.UPDATE",
        plant_id=risk.plantId if risk else None,
        record_id=risk.id if risk else None,
        record={"uploadedById": att.uploadedById},
    )
    att.deletedAt = datetime.now(timezone.utc)
    await db.flush()
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════
#  Control attachments
# ═══════════════════════════════════════════════════════════════════════


@router.post("/controls/{control_id}/attachments")
async def upload_control_attachment(
    control_id: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    control = await db.get(Control, control_id)
    if control is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Control not found")
    await _require(db, user, "CONTROL.WRITE", plant_id=control.siteId, record_id=control.id)
    _storage_guard()

    phase = payload.get("phase")
    if phase == "init":
        try:
            init = AttachmentInit(**payload)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid init body: {e}") from e
        _validate_init(init, CONTROL_VALID_CATEGORIES)
        storage_path = build_control_storage_path(
            control_id=control_id, category=init.category, file_name=init.fileName
        )
        try:
            signed = create_signed_upload_url(storage_path)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"Storage upload init failed: {e}"
            ) from e
        att = ControlAttachment(
            controlId=control_id,
            controlTestId=init.controlTestId,
            category=init.category,
            fileName=init.fileName,
            storagePath=storage_path,
            fileSize=init.fileSize,
            mimeType=init.mimeType,
            reviewDate=init.reviewDate,
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
        att = await db.get(ControlAttachment, attachment_id)
        if att is None or att.controlId != control_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found for this control")
        att.caption = payload.get("caption")
        await db.flush()
        return {"ok": True}

    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown phase")


@router.get("/controls/{control_id}/attachments")
async def list_control_attachments(
    control_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    control = await db.get(Control, control_id)
    if control is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Control not found")
    await _require(db, user, "CONTROL.READ", plant_id=control.siteId, record_id=control.id)
    rows = (
        (
            await db.execute(
                select(ControlAttachment)
                .where(ControlAttachment.controlId == control_id)
                .where(ControlAttachment.deletedAt.is_(None))
                .order_by(ControlAttachment.uploadedAt.desc())
            )
        )
        .scalars()
        .all()
    )
    return {"items": [ControlAttachmentOut.model_validate(r) for r in rows]}


@router.get("/controls/{control_id}/attachments/{attachment_id}/download")
async def download_control_attachment(
    control_id: str,
    attachment_id: str,
    inline: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    att = await db.get(ControlAttachment, attachment_id)
    if att is None or att.controlId != control_id or att.deletedAt is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    control = await db.get(Control, control_id)
    is_uploader = att.uploadedById == user.id
    res = await can(
        db, user.id, "CONTROL.READ",
        PermissionContext(
            plant_id=control.siteId if control else None,
            record_id=control.id if control else None,
        ),
    )
    if not res.allowed and not is_uploader:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")
    url = create_signed_download_url(
        att.storagePath,
        expires_in_sec=300,
        download=None if inline else att.fileName,
    )
    return {"url": url}


@router.delete("/controls/{control_id}/attachments/{attachment_id}")
async def delete_control_attachment(
    control_id: str,
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    att = await db.get(ControlAttachment, attachment_id)
    if att is None or att.controlId != control_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    control = await db.get(Control, control_id)
    await _require(
        db, user, "CONTROL.WRITE",
        plant_id=control.siteId if control else None,
        record_id=control.id if control else None,
        record={"uploadedById": att.uploadedById},
    )
    att.deletedAt = datetime.now(timezone.utc)
    await db.flush()
    return {"ok": True}
