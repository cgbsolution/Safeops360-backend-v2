"""Audit & Compliance Management — API router (prefix /api/audit-compliance).

Industry-checklist audits: schedule -> conduct (partial-save) -> auditee
response -> plant-manager review -> close, plus programme + per-audit
dashboards. Every endpoint is RBAC-gated via `can()` on the AUDIT_COMPLIANCE
module. The service flushes; the get_db dependency commits at request end.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.services import audit_compliance as svc
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
)
from app.services.storage import (
    create_signed_download_url,
    create_signed_upload_url,
    delete_storage_object,
    is_storage_configured,
)

# Photo upload: images + PDF, 10 MB cap. Photos live inline in each
# checkpoint response's JSONB; the binary goes to Supabase Storage under an
# audit-compliance/ prefix in the shared attachments bucket.
_ALLOWED_PHOTO_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/gif", "application/pdf"}


def _audit_photo_path(audit_id: str | None, checkpoint_code: str | None, file_name: str) -> str:
    safe = re.sub(r"[\\/]", "_", file_name)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", safe)[:80] or "photo"
    seg = re.sub(r"[^a-z0-9._-]", "_", (checkpoint_code or "general").lower())[:40]
    short = secrets.token_hex(4)
    return f"audit-compliance/{audit_id or 'unassigned'}/{seg}/{short}-{safe}"

router = APIRouter(prefix="/api/audit-compliance", tags=["audit-compliance"])


# ─────────────────────────────────────────────────────────────────────
# Permission helpers
# ─────────────────────────────────────────────────────────────────────


async def _require(db: AsyncSession, user: User, code: str, *, plant_id: str | None = None,
                   record: dict | None = None, record_id: str | None = None) -> None:
    res = await can(
        db, user.id, code,
        PermissionContext(plant_id=plant_id, record=record, record_id=record_id),
    )
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


async def _load_or_404(db: AsyncSession, audit_id: str):
    audit = await svc._load_audit(db, audit_id)
    if audit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audit not found")
    return audit


# ─────────────────────────────────────────────────────────────────────
# Request bodies
# ─────────────────────────────────────────────────────────────────────


class AuditeeAssignment(BaseModel):
    userId: str
    responsibleCategories: list[str] = []


class CreateAuditBody(BaseModel):
    plantId: str
    title: str = Field(min_length=4)
    templateId: str | None = None
    industryCode: str | None = None
    auditType: str | None = None
    scopeDepartments: list[str] = []
    scopeAreas: list[str] = []
    scopeDescription: str = ""
    scheduledDate: datetime
    scheduledStartTime: str = "09:00"
    estimatedDurationHours: float = 2
    leadAuditorUserId: str | None = None
    coAuditors: list[str] = []
    auditees: list[AuditeeAssignment] = []
    plantManagerUserId: str | None = None
    openingRemarks: str = ""


class SaveResponseBody(BaseModel):
    checkpointCode: str
    value: str | None = None  # pass | partial | fail | na | yes | no | null
    numericValue: float | None = None
    selectedOptions: list[str] | None = None
    textObservation: str = ""
    auditorNotes: str = ""
    photos: list[dict[str, Any]] = []
    evidenceLinks: list[dict[str, Any]] = []


class AuditeeRespondBody(BaseModel):
    checkpointCode: str
    responseText: str = ""
    actionTaken: str = ""
    actionDate: str | None = None
    estimatedClosureDate: str | None = None
    photos: list[dict[str, Any]] = []


class DispatchBody(BaseModel):
    """Plant Head assigns a responsible auditee per discipline, then dispatches."""
    assignments: list[AuditeeAssignment] = []


class AuditorReviewBody(BaseModel):
    checkpointCode: str
    decision: str  # accepted | rejected
    comments: str = ""


class AcceptBody(BaseModel):
    decision: str = "accepted"  # accepted | rejected
    comments: str = ""
    closingRemarks: str = ""


class CustomCheckpointBody(BaseModel):
    categoryId: str = "CUSTOM"
    categoryName: str = "Custom checkpoints"
    categoryColor: str = "#6d28d9"
    checkpointQuestion: str = Field(min_length=4)
    guidance: str = ""
    requirementReference: str = ""
    standard: str = ""
    criticality: str = "major"  # critical | major | minor | informational
    responseType: str = "pass_partial_fail"
    requiresPhotoOnFail: bool = False
    autoTriggerCapaOnFail: bool = False
    capaSeverity: str | None = None


class UploadUrlBody(BaseModel):
    fileName: str
    contentType: str | None = None
    auditId: str | None = None
    checkpointCode: str | None = None


class ViewUrlBody(BaseModel):
    storagePath: str


# ─────────────────────────────────────────────────────────────────────
# Reference + list + dashboards (specific paths before /{id})
# ─────────────────────────────────────────────────────────────────────


@router.get("")
async def list_audits(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "AUDIT_COMPLIANCE.READ")
    plants = await get_accessible_plants(db, user.id)
    audits = await svc.list_audits(db, accessible_plants=plants)
    return {"audits": audits}


@router.get("/templates")
async def list_templates(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "AUDIT_COMPLIANCE.READ")
    return {"templates": await svc.list_templates(db)}


@router.get("/library")
async def list_library(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "AUDIT_COMPLIANCE.READ")
    return {"libraries": await svc.list_libraries(db)}


@router.get("/dashboard/programme")
async def programme_dashboard(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "AUDIT_COMPLIANCE.READ")
    plants = await get_accessible_plants(db, user.id)
    return await svc.programme_dashboard(db, accessible_plants=plants)


@router.get("/users")
async def plant_users(
    plantId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Users at a plant — populates the schedule wizard's auditor/auditee pickers."""
    await _require(db, user, "AUDIT_COMPLIANCE.READ")
    rows = (
        await db.execute(
            select(User).where(User.plantId == plantId).order_by(User.name)
        )
    ).scalars().all()
    return {
        "users": [
            {"id": u.id, "name": u.name, "role": u.role, "department": u.department or ""}
            for u in rows
        ]
    }


@router.post("/upload-url")
async def upload_url(
    body: UploadUrlBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Mint a short-lived signed URL the browser PUTs the photo bytes to.
    The service-role key never reaches the browser."""
    await _require(db, user, "AUDIT_COMPLIANCE.READ")
    if not is_storage_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Supabase Storage isn't configured (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY).")
    if body.contentType and body.contentType not in _ALLOWED_PHOTO_MIME:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported file type: {body.contentType}")
    path = _audit_photo_path(body.auditId, body.checkpointCode, body.fileName)
    try:
        signed = create_signed_upload_url(path)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Storage upload init failed: {e}") from e
    return {"storagePath": path, "uploadUrl": signed["uploadUrl"], "token": signed["token"]}


@router.post("/view-url")
async def view_url(
    body: ViewUrlBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Signed download URL for a stored photo (7-day window)."""
    await _require(db, user, "AUDIT_COMPLIANCE.READ")
    if not is_storage_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Supabase Storage isn't configured.")
    try:
        url = create_signed_download_url(body.storagePath, expires_in_sec=7 * 86400)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Could not sign photo: {e}") from e
    return {"url": url}


@router.post("/delete-photo")
async def delete_photo(
    body: ViewUrlBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Remove a photo object from storage (so a removed/replaced photo isn't
    left orphaned). Best-effort — the caller also drops it from the response."""
    await _require(db, user, "AUDIT_COMPLIANCE.READ")
    if not body.storagePath or not body.storagePath.startswith("audit-compliance/"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid storage path")
    try:
        delete_storage_object(body.storagePath)
    except Exception as e:  # noqa: BLE001
        # Non-fatal — the record-level removal still succeeds.
        return {"ok": False, "warning": str(e)[:140]}
    return {"ok": True}


@router.get("/{audit_id}")
async def get_audit(
    audit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "AUDIT_COMPLIANCE.READ")
    data = await svc.get_audit(db, audit_id)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audit not found")
    return data


@router.get("/{audit_id}/dashboard")
async def get_audit_dashboard(
    audit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "AUDIT_COMPLIANCE.READ")
    data = await svc.audit_dashboard(db, audit_id)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audit not found")
    return data


# ─────────────────────────────────────────────────────────────────────
# Mutations
# ─────────────────────────────────────────────────────────────────────


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_audit(
    body: CreateAuditBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "AUDIT_COMPLIANCE.CREATE", plant_id=body.plantId)
    data = body.model_dump()
    data["auditees"] = [a if isinstance(a, dict) else a.model_dump() for a in body.auditees]
    try:
        audit = await svc.create_audit(db, user=user, data=data)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    await db.refresh(audit)
    return {"id": audit.id, "auditNumber": audit.auditNumber, "totalCheckpoints": audit.totalCheckpoints}


@router.post("/{audit_id}/responses")
async def save_response(
    audit_id: str,
    body: SaveResponseBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.EXECUTE", plant_id=audit.plantId,
                   record={"leadAuditorUserId": audit.leadAuditorUserId, "createdByUserId": audit.createdByUserId},
                   record_id=audit.id)
    try:
        # exclude_unset → only the fields the client actually sent are merged,
        # so an observation-only save never wipes a previously-saved value.
        return await svc.save_response(db, user=user, audit_id=audit_id, payload=body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/{audit_id}/submit")
async def submit_audit(
    audit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.EXECUTE", plant_id=audit.plantId,
                   record={"leadAuditorUserId": audit.leadAuditorUserId, "createdByUserId": audit.createdByUserId},
                   record_id=audit.id)
    try:
        return await svc.submit_audit(db, user=user, audit_id=audit_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/{audit_id}/auditee-respond")
async def auditee_respond(
    audit_id: str,
    body: AuditeeRespondBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.UPDATE", plant_id=audit.plantId,
                   record={"routedToUserId": user.id}, record_id=audit.id)
    try:
        return await svc.auditee_respond(db, user=user, audit_id=audit_id, payload=body.model_dump())
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/{audit_id}/custom-checkpoint", status_code=status.HTTP_201_CREATED)
async def add_custom_checkpoint(
    audit_id: str,
    body: CustomCheckpointBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Auditor adds an ad-hoc checkpoint while conducting the audit."""
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.EXECUTE", plant_id=audit.plantId,
                   record={"leadAuditorUserId": audit.leadAuditorUserId, "createdByUserId": audit.createdByUserId},
                   record_id=audit.id)
    try:
        return await svc.add_custom_checkpoint(db, user=user, audit_id=audit_id, payload=body.model_dump())
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/{audit_id}/dispatch")
async def dispatch(
    audit_id: str,
    body: DispatchBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Plant Head assigns auditees per discipline and dispatches the findings."""
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.APPROVE", plant_id=audit.plantId,
                   record={"plantManagerUserId": audit.plantManagerUserId}, record_id=audit.id)
    assignments = [a.model_dump() for a in body.assignments]
    try:
        return await svc.plant_head_dispatch(db, user=user, audit_id=audit_id, assignments=assignments)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/{audit_id}/auditor-review")
async def auditor_review(
    audit_id: str,
    body: AuditorReviewBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Auditor verifies an auditee response (accept / reject)."""
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.EXECUTE", plant_id=audit.plantId,
                   record={"leadAuditorUserId": audit.leadAuditorUserId, "createdByUserId": audit.createdByUserId},
                   record_id=audit.id)
    try:
        return await svc.auditor_review(db, user=user, audit_id=audit_id, payload=body.model_dump())
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/{audit_id}/request-acceptance")
async def request_acceptance(
    audit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Auditor sends the final report to the Plant Head for acceptance."""
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.EXECUTE", plant_id=audit.plantId,
                   record={"leadAuditorUserId": audit.leadAuditorUserId, "createdByUserId": audit.createdByUserId},
                   record_id=audit.id)
    try:
        return await svc.request_acceptance(db, user=user, audit_id=audit_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/{audit_id}/accept")
async def accept_audit(
    audit_id: str,
    body: AcceptBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Plant Head's final acceptance (accept -> closed, reject -> back to auditor)."""
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.CLOSE", plant_id=audit.plantId,
                   record={"plantManagerUserId": audit.plantManagerUserId,
                           "leadAuditorUserId": audit.leadAuditorUserId}, record_id=audit.id)
    try:
        return await svc.plant_head_accept(
            db, user=user, audit_id=audit_id,
            decision=body.decision, comments=body.comments, closing_remarks=body.closingRemarks,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
