"""Waves 3-5 completion router - suppliers, i18n, evidence packs, preferences.

  WP-45  /suppliers/*      link an engagement to an ERM VendorProfile
  WP-46  /i18n/*           field-facing translations (Q18: en + hi)
  WP-40  /packs/*          async certification evidence pack
  WP-34  /packs (PROGRAMME_CYCLE scope)
  WP-43  /notification-preferences

Reuses the existing CAMS permission codes - no RBAC migration.
"""

from __future__ import annotations

import io
import os
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.audit_compliance import AuditCheckpointLibrary, ComplianceAudit
from app.models.cams_completion import EvidencePackJob
from app.models.supplier_portal import SupplierPortalToken
from app.models.user import User
from app.services import cams_i18n as i18n
from app.services import cams_notifications as notif
from app.services import cams_suppliers as sup
from app.services import evidence_pack as packs
from app.services import supplier_portal as portal
from app.services.notifications import send_email
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/cams-completion", tags=["cams-completion"])


async def _require(db: AsyncSession, user: User, code: str, *, plant_id=None) -> None:
    res = await can(db, user.id, code, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


# ── WP-45: suppliers ─────────────────────────────────────────────────


class SupplierLinkBody(BaseModel):
    engagementKind: Literal["AUDIT", "INSPECTION"] = "AUDIT"
    engagementId: str
    vendorProfileId: str
    vendorSiteRef: str | None = None
    supplierContactName: str | None = None
    supplierContactEmail: str | None = None


@router.post("/suppliers/link", status_code=status.HTTP_201_CREATED)
async def link_supplier(
    body: SupplierLinkBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Attach a supplier to an engagement, snapshotting its risk posture."""
    await _require(db, user, "CAMS.SCHEDULE")
    try:
        row = await sup.link_supplier(
            db,
            engagement_kind=body.engagementKind,
            engagement_id=body.engagementId,
            vendor_profile_id=body.vendorProfileId,
            vendor_site_ref=body.vendorSiteRef,
            contact_name=body.supplierContactName,
            contact_email=body.supplierContactEmail,
            actor_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.commit()
    return {"id": row.id, "ok": True}


@router.get("/suppliers/engagement")
async def supplier_for_engagement(
    engagementKind: str = Query("AUDIT"),
    engagementId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The supplier block. `linked: false` means this is an own facility."""
    await _require(db, user, "CAMS.READ")
    out = await sup.supplier_for_engagement(
        db, engagement_kind=engagementKind, engagement_id=engagementId
    )
    return out or {"linked": False}


@router.get("/suppliers/{vendor_profile_id}/history")
async def supplier_history(
    vendor_profile_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.READ")
    return await sup.supplier_audit_history(db, vendor_profile_id=vendor_profile_id)


@router.get("/suppliers/coverage/gaps")
async def supplier_gaps(
    criticality: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Vendors with no engagement on record, highest risk first.

    A critical single-source vendor that has never been audited is the
    highest-value row a coverage matrix can surface.
    """
    await _require(db, user, "CAMS.READ")
    items = await sup.unaudited_suppliers(db, criticality=criticality)
    return {"items": items, "total": len(items)}


@router.get("/suppliers/vendors")
async def list_vendors_for_picker(
    criticality: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The vendor picker's source for the scheduling wizard.

    Served from CAMS rather than pointing the wizard at `/api/erm/t3/vendors`
    so scheduling an audit needs CAMS.SCHEDULE, not a vendor-module permission
    — an audit manager who cannot edit vendor risk can still audit a vendor.
    Reaches the data through `services/vendors.py` like everything else.
    """
    await _require(db, user, "CAMS.SCHEDULE")
    from app.services import vendors as vendor_svc

    rows = await vendor_svc.list_vendors(db, criticality=criticality)
    return {"vendors": [v.as_dict() for v in rows], "total": len(rows)}


# ── WP-45 stage 2: supplier portal (internal side) ───────────────────


class IssuePortalBody(BaseModel):
    auditId: str
    contactEmail: str | None = None
    contactName: str | None = None
    ttlDays: int = Field(portal.DEFAULT_TTL_DAYS, ge=1, le=180)
    sendEmail: bool = True


@router.post("/suppliers/portal/issue", status_code=status.HTTP_201_CREATED)
async def issue_portal_access(
    body: IssuePortalBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Mint a supplier portal link for one audit and email it to the contact.

    **The raw link is returned exactly once** — only its hash is stored. That is
    deliberate (a database read must not be replayable as portal access), so the
    response is the single opportunity to copy it. Re-issuing mints a new link
    and revokes this one.
    """
    audit = await db.get(ComplianceAudit, body.auditId)
    if audit is None or audit.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audit not found")
    await _require(db, user, "CAMS.FINDING_MANAGE", plant_id=audit.plantId)

    link = await sup.supplier_for_engagement(
        db, engagement_kind="AUDIT", engagement_id=body.auditId
    )
    if link is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This is an own-facility audit — there is no supplier to give access to.",
        )
    email = (body.contactEmail or link.get("supplierContactEmail") or "").strip()
    if not email:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No supplier contact email is recorded for this audit. Add one before "
            "issuing portal access.",
        )

    issued = await portal.issue_token(
        db,
        audit_id=body.auditId,
        contact_email=email,
        contact_name=body.contactName or link.get("supplierContactName"),
        vendor_profile_id=link.get("vendorProfileId"),
        actor_id=user.id,
        ttl_days=body.ttlDays,
    )

    sent = False
    if body.sendEmail:
        base = (os.getenv("APP_PUBLIC_URL") or "").rstrip("/")
        url = f"{base}{issued.portalPath}" if base else issued.portalPath
        try:
            sent = await send_email(
                [email],
                f"Corrective actions required — audit {audit.auditNumber}",
                (
                    f"Dear {issued.token.supplierContactName or 'colleague'},\n\n"
                    f"Following the audit of your facility ({audit.auditNumber} — "
                    f"{audit.title}), the findings requiring corrective action are "
                    f"available at the link below. You can add comments and upload "
                    f"evidence against each one.\n\n{url}\n\n"
                    f"This link is personal to you and expires on "
                    f"{issued.token.expiresAt.date().isoformat()}.\n"
                ),
            )
        except Exception:  # noqa: BLE001
            sent = False
        if sent:
            issued.token.emailSentAt = datetime.now(timezone.utc)

    return {
        "tokenId": issued.token.id,
        "portalPath": issued.portalPath,
        "expiresAt": issued.token.expiresAt.isoformat(),
        "contactEmail": email,
        "emailSent": sent,
        "note": (
            "This link is shown once and is not recoverable — only a hash is stored. "
            "Re-issue to generate a new one."
        ),
    }


@router.post("/suppliers/portal/{token_id}/revoke")
async def revoke_portal_access(
    token_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = await db.get(SupplierPortalToken, token_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Portal access not found")
    audit = await db.get(ComplianceAudit, row.auditId)
    await _require(db, user, "CAMS.FINDING_MANAGE", plant_id=audit.plantId if audit else None)
    ok = await portal.revoke_token(db, token_id=token_id, actor_id=user.id)
    return {"ok": ok, "alreadyRevoked": not ok}


@router.get("/suppliers/portal/{audit_id}/submissions")
async def portal_submissions(
    audit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """What the supplier has sent, for the internal findings/CAPA view.

    Every row carries `actorType: "SUPPLIER"`, which is what lets the internal
    UI distinguish a supplier's own response from an internal owner recording
    one on their behalf.
    """
    audit = await db.get(ComplianceAudit, audit_id)
    if audit is None or audit.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audit not found")
    await _require(db, user, "CAMS.READ", plant_id=audit.plantId)
    items = await portal.submissions_for_audit(db, audit_id)
    return {"items": items, "total": len(items)}


# ── WP-46: i18n ──────────────────────────────────────────────────────


@router.get("/i18n/languages")
async def languages(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Supported field languages. Q18 answered no to Tamil/Kannada, so en + hi."""
    return {"items": i18n.list_languages(), "default": i18n.DEFAULT_LANGUAGE}


@router.get("/i18n/audits/{audit_id}/checkpoints")
async def audit_checkpoint_text(
    audit_id: str,
    language: str = Query("en"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Resolved question + guidance for a conduct screen, in one round trip.

    Batched deliberately: 1,500 checkpoints resolved one at a time would be
    1,500 queries, and the offline pack builds from this too.
    """
    audit = await db.get(ComplianceAudit, audit_id)
    if audit is None or audit.isDeleted:
        raise HTTPException(404, "Audit not found")
    await _require(db, user, "AUDIT_COMPLIANCE.READ", plant_id=audit.plantId)

    from app.models.audit_compliance import AuditCheckpointResponse

    rows = (
        await db.execute(
            select(
                AuditCheckpointResponse.checkpointCode,
                AuditCheckpointResponse.checkpointQuestion,
                AuditCheckpointResponse.guidance,
            ).where(AuditCheckpointResponse.auditId == audit_id)
        )
    ).all()
    english = {
        code: {"questionText": q or "", "guidance": g or ""} for code, q, g in rows if code
    }
    resolved = await i18n.resolve_checkpoints(
        db,
        library_code=audit.industryCode,
        checkpoint_codes=list(english),
        language=language,
        english_source=english,
    )
    fallbacks = sum(1 for v in resolved.values() if not v["question"]["isTranslated"])
    return {
        "language": i18n.normalise(language),
        "items": resolved,
        "total": len(resolved),
        "fallbackCount": fallbacks,
        # Surfaced so the conduct screen can warn BEFORE an auditor walks the
        # floor and hits English halfway through.
        "fullyTranslated": fallbacks == 0,
    }


@router.get("/i18n/libraries/{library_code}/coverage")
async def library_translation_coverage(
    library_code: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.READ")
    lib = (
        await db.execute(
            select(AuditCheckpointLibrary).where(
                AuditCheckpointLibrary.industryCode == library_code
            )
        )
    ).scalars().first()
    if lib is None:
        raise HTTPException(404, "Library not found")
    items = await i18n.coverage_for_library(
        db, library_code=library_code, total_checkpoints=lib.checkpointCount or 0
    )
    return {"libraryCode": library_code, "items": items}


class TranslationBody(BaseModel):
    libraryCode: str
    checkpointCode: str
    language: str
    questionText: str = Field(min_length=1)
    guidance: str | None = None
    source: Literal["HUMAN", "MACHINE"] = "HUMAN"
    markReviewed: bool = False


@router.post("/i18n/translations")
async def upsert_translation(
    body: TranslationBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.TEMPLATE_AUTHOR")
    try:
        row = await i18n.upsert_translation(
            db,
            library_code=body.libraryCode,
            checkpoint_code=body.checkpointCode,
            language=body.language,
            question_text=body.questionText,
            guidance=body.guidance,
            source=body.source,
            reviewed_by=user.id if body.markReviewed else None,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.commit()
    return {"id": row.id, "ok": True}


# ── WP-40 / WP-34: evidence packs ────────────────────────────────────


class PackBody(BaseModel):
    scopeKind: Literal["AUDIT", "PROGRAMME_CYCLE"] = "AUDIT"
    scopeId: str
    includeEvidencePhotos: bool = True
    includeFullRegister: bool = True


@router.post("/packs", status_code=status.HTTP_202_ACCEPTED)
async def request_pack(
    body: PackBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Queue a pack. 202, not 200 — a 1,500-checkpoint pack with 200 photos
    will not finish inside a request cycle, so the job row is the contract."""
    await _require(db, user, "CAMS.READ")
    job = EvidencePackJob(
        scopeKind=body.scopeKind,
        scopeId=body.scopeId,
        includeEvidencePhotos=body.includeEvidencePhotos,
        includeFullRegister=body.includeFullRegister,
        requestedById=user.id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return {"jobId": job.id, "status": job.status}


@router.get("/packs/{job_id}")
async def pack_status(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.READ")
    job = await db.get(EvidencePackJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    missing = [m for m in (job.manifest or []) if m.get("kind") == "MISSING"]
    return {
        "jobId": job.id,
        "scopeKind": job.scopeKind,
        "scopeId": job.scopeId,
        "status": job.status,
        "progressPct": job.progressPct,
        "currentStep": job.currentStep,
        "itemCount": job.itemCount,
        "totalBytes": job.totalBytes,
        "errorMessage": job.errorMessage,
        "requestedAt": job.requestedAt.isoformat() if job.requestedAt else None,
        "completedAt": job.completedAt.isoformat() if job.completedAt else None,
        # Surfaced, never buried: a pack that omitted 40 photos looks complete
        # unless the gap is stated.
        "missingCount": len(missing),
        "missing": missing[:20],
    }


@router.get("/packs/{job_id}/download")
async def download_pack(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Build and stream the ZIP.

    Built on demand from the frozen records rather than stored: the snapshot and
    its hash are already immutable, so re-deriving is deterministic and avoids a
    second copy of every audit going stale in object storage.
    """
    await _require(db, user, "CAMS.READ")
    job = await db.get(EvidencePackJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    try:
        if job.scopeKind == "AUDIT":
            builder = await packs.collect_audit_pack(
                db, audit_id=job.scopeId, include_photos=job.includeEvidencePhotos
            )
        else:
            builder = await packs.collect_programme_pack(db, cycle_id=job.scopeId)
    except ValueError as e:
        raise HTTPException(404, str(e))

    data, manifest = builder.seal()
    job.manifest = manifest
    job.itemCount = len(manifest)
    job.totalBytes = len(data)
    job.status = "COMPLETE"
    job.progressPct = 100
    job.completedAt = packs._utcnow()
    await db.commit()

    name = f"evidence-pack-{job.scopeKind.lower()}-{job.scopeId[:8]}.zip"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ── WP-43: notification preferences ──────────────────────────────────


@router.get("/notification-preferences")
async def get_preferences(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """All five event classes with defaults filled in.

    Always returns every class: a screen listing only saved rows is one the user
    cannot use to change anything.
    """
    items = await notif.preferences_for(db, user.id)
    return {
        "items": items,
        "frequencies": list(notif.DIGEST_FREQUENCIES),
        "default": notif.DEFAULT_FREQUENCY,
    }


class PreferenceBody(BaseModel):
    eventClass: Literal["ASSIGNMENT", "EXECUTION", "CAPA", "SIGNOFF", "PROGRAMME"]
    inAppEnabled: bool = True
    emailFrequency: Literal["IMMEDIATE", "DAILY", "WEEKLY", "OFF"] = "DAILY"


@router.put("/notification-preferences")
async def set_preference(
    body: PreferenceBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        out = await notif.set_preference(
            db,
            user_id=user.id,
            event_class_code=body.eventClass,
            in_app=body.inAppEnabled,
            email_frequency=body.emailFrequency,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.commit()
    return out
