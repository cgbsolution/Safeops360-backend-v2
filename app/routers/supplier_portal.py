"""Supplier portal — the only UNAUTHENTICATED router in the product.

Every endpoint here is reached with an opaque token instead of a session, so
each one re-derives its scope from the token row. Three properties hold, and
they are the reason this router is separate from every other:

  1. **No `get_current_user`.** A supplier has no seat. Adding auth here would
     defeat the purpose; adding it *partially* would be worse.
  2. **Scope comes from the token, never the request.** The audit id is read off
     the token row. A caller cannot name a different audit, and a write is
     re-validated against the token's audit before it is accepted.
  3. **Every attempt is logged, and failures say nothing useful.** Expired,
     revoked and never-existed all answer 404 with one message. The precise
     outcome goes to `SupplierPortalAccessLog`, where it helps us and not a
     caller probing tokens.

Mounted at /api/supplier-portal.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.services import supplier_portal as portal
from app.services import vendors as vendor_svc
from app.services.storage import (
    create_signed_download_url,
    create_signed_upload_url,
    is_storage_configured,
)

router = APIRouter(prefix="/api/supplier-portal", tags=["supplier-portal"])

# Evidence a supplier can send: photographs of a remediated condition, or a
# document (policy, wage register extract, training record). Same allow-list as
# the internal audit photo upload, and the same 10 MB ceiling.
_ALLOWED_MIME = {
    "image/jpeg", "image/png", "image/webp", "image/heic",
    "application/pdf",
}
_MAX_BYTES = 10 * 1024 * 1024

# One generic failure message for every token problem — see property 3 above.
_TOKEN_FAIL = "This link is not valid. It may have expired or been withdrawn."


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


async def _resolve(
    db: AsyncSession, token: str, request: Request, *, action: str, is_write: bool = False
):
    try:
        return await portal.resolve_token(
            db, token, action=action, is_write=is_write,
            ip=_client_ip(request), user_agent=request.headers.get("user-agent"),
        )
    except portal.PortalError as e:
        raise HTTPException(
            e.status_code,
            "Too many requests. Please wait a few minutes and try again."
            if e.status_code == 429
            else _TOKEN_FAIL,
        ) from e


@router.get("/{token}")
async def view(
    token: str, request: Request, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """The supplier's view of one audit: their non-conformances and what they
    have already sent us. Nothing else exists as far as this endpoint knows."""
    row = await _resolve(db, token, request, action="VIEW")
    try:
        payload = await portal.portal_view(db, row)
    except portal.PortalError as e:
        raise HTTPException(e.status_code, _TOKEN_FAIL) from e

    # The vendor's own name, through the boundary like every other vendor read.
    v = await vendor_svc.get_vendor(db, row.vendorProfileId)
    payload["supplier"]["legalName"] = v.legalName if v else None
    return payload


class CommentBody(BaseModel):
    checkpointResponseId: str
    body: str = Field(min_length=1, max_length=8000)


@router.post("/{token}/comment", status_code=status.HTTP_201_CREATED)
async def add_comment(
    token: str, body: CommentBody, request: Request, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    row = await _resolve(db, token, request, action="COMMENT", is_write=True)
    try:
        sub = await portal.add_comment(
            db, row, checkpoint_response_id=body.checkpointResponseId, body=body.body
        )
    except portal.PortalError as e:
        raise HTTPException(e.status_code, _TOKEN_FAIL if e.status_code == 404 else "Empty comment") from e
    return {"id": sub.id, "submittedAt": sub.submittedAt.isoformat() if sub.submittedAt else None}


class UploadUrlBody(BaseModel):
    checkpointResponseId: str
    fileName: str = Field(min_length=1, max_length=200)
    mimeType: str
    fileSize: int = Field(gt=0, le=_MAX_BYTES)


@router.post("/{token}/upload-url")
async def upload_url(
    token: str, body: UploadUrlBody, request: Request, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Signed, single-use upload target under a portal-scoped path.

    The path embeds the token row id, so supplier uploads are segregated from
    internal audit evidence in storage as well as in the database.
    """
    row = await _resolve(db, token, request, action="UPLOAD_URL", is_write=True)
    if body.mimeType not in _ALLOWED_MIME:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Please upload a photo (JPG, PNG, WEBP, HEIC) or a PDF.",
        )
    if not is_storage_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "File upload is not available.")

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", body.fileName)[:80] or "evidence"
    path = f"supplier-portal/{row.id}/{secrets.token_hex(4)}-{safe}"
    try:
        signed = create_signed_upload_url(path)
    except RuntimeError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e
    return {"storagePath": path, **signed}


class EvidenceBody(BaseModel):
    checkpointResponseId: str
    fileName: str = Field(min_length=1, max_length=200)
    storagePath: str
    mimeType: str | None = None
    fileSize: int | None = Field(None, gt=0, le=_MAX_BYTES)
    caption: str = Field("", max_length=2000)


@router.post("/{token}/evidence", status_code=status.HTTP_201_CREATED)
async def add_evidence(
    token: str, body: EvidenceBody, request: Request, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    row = await _resolve(db, token, request, action="EVIDENCE", is_write=True)
    # The path must be the one we issued for THIS token. Without this check a
    # supplier could attach any object in the bucket — including internal audit
    # evidence — to their own finding.
    if not body.storagePath.startswith(f"supplier-portal/{row.id}/"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unrecognised upload reference.")
    try:
        sub = await portal.add_evidence(
            db, row,
            checkpoint_response_id=body.checkpointResponseId,
            file_name=body.fileName,
            storage_path=body.storagePath,
            file_size=body.fileSize,
            mime_type=body.mimeType,
            caption=body.caption,
        )
    except portal.PortalError as e:
        raise HTTPException(e.status_code, _TOKEN_FAIL) from e
    return {"id": sub.id, "fileName": sub.fileName}


@router.get("/{token}/evidence/{submission_id}/url")
async def evidence_url(
    token: str, submission_id: str, request: Request, db: AsyncSession = Depends(get_db)
) -> dict[str, str]:
    """Let the supplier re-open a file they themselves sent. Scoped to their own
    submissions on their own audit — nothing else is reachable."""
    row = await _resolve(db, token, request, action="VIEW")
    subs = await portal.submissions_for_audit(db, row.auditId)
    match = next(
        (s for s in subs if s["id"] == submission_id and s.get("storagePath")), None
    )
    if match is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    try:
        return {"url": create_signed_download_url(match["storagePath"], 300)}
    except RuntimeError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e
