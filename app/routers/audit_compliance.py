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
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.services import audit_assignment as assignment
from app.services import audit_compliance as svc
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
    get_accessible_plants_for,
    permission_scopes,
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

# How many checkpoints the PDF register prints. Every ordinary audit (10-100
# checkpoints) is far under this and prints in full. The cap exists only for the
# 1,500-checkpoint scale case, where a complete register with every iteration
# thread would run to hundreds of pages and take minutes to render. When it
# bites, the PDF says so on the page — it never stops silently.
_PDF_REGISTER_CAP = 600


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


def _auditor_record(audit) -> dict[str, Any]:
    """Record context for auditor actions. Includes the lead/creator plus the
    co-auditors as `teamMembers` so a co-auditor with OWN_RECORDS-scoped EXECUTE
    is permitted on the audit they're assigned to (per-discipline auditor scope
    is applied in the conduct UI). Tolerates legacy flat + structured coAuditors."""
    team = []
    for c in (audit.coAuditors or []):
        uid = c.get("userId") if isinstance(c, dict) else c
        if uid:
            team.append({"userId": uid})
    return {
        "leadAuditorUserId": audit.leadAuditorUserId,
        "createdByUserId": audit.createdByUserId,
        "teamMembers": team,
    }


async def _party_filter_for(db: AsyncSession, user: User) -> str | None:
    """User id to narrow the register by, or None for a plant-wide reader.

    Only when every AUDIT_COMPLIANCE.READ grant the user holds is OWN_RECORDS
    — i.e. they are an auditee, not a plant-wide reader — does the register
    narrow to the engagements they are party to. Any OWN_PLANT / ALL_PLANTS
    grant keeps the full plant register."""
    scopes = await permission_scopes(db, user.id, "AUDIT_COMPLIANCE.READ")
    return user.id if scopes and scopes <= {"OWN_RECORDS"} else None


def _reader_record(audit) -> dict[str, Any]:
    """Record context for READ on one audit — every party to the engagement.

    The auditee-class roles (SUPERVISOR / DEPARTMENT_HEAD / SAFETY_OFFICER)
    hold AUDIT_COMPLIANCE.READ at OWN_RECORDS scope. `can()` can only satisfy
    an OWN_RECORDS grant when the caller passes BOTH `record_id` and `record`:
    with `record_id` set and `record` None it falls straight through to a
    deny. The detail endpoints used to pass `record_id` alone, so an auditee
    got 403 on every audit — which the Next.js detail page renders as a bare
    404.

    Everyone goes into `teamMembers`, deliberately. `_matches_own_records`
    only inspects a fixed `_OWNER_FIELDS` list plus the crew fields, and
    `leadAuditorUserId` / `plantManagerUserId` are in neither — a record dict
    keyed on those names can never match, which is why they are flattened
    into the crew list here rather than passed as their own keys."""
    return {"teamMembers": [{"userId": uid} for uid in svc.audit_party_ids(audit)]}


# ─────────────────────────────────────────────────────────────────────
# Request bodies
# ─────────────────────────────────────────────────────────────────────


class AuditeeAssignment(BaseModel):
    userId: str
    responsibleCategories: list[str] = []


class CoAuditorAssignment(BaseModel):
    userId: str
    disciplineIds: list[str] = []


class CreateAuditBody(BaseModel):
    plantId: str
    title: str = Field(min_length=4)
    # ── Audit subject (WP-45) ──────────────────────────────────────────
    # `plantId` above is ALWAYS the owning plant. On a supplier audit it is
    # the site that holds the vendor relationship, not the audited premises —
    # see the note in `services.audit_compliance.create_audit`.
    subjectType: Literal["OWN_SITE", "VENDOR"] = "OWN_SITE"
    vendorProfileId: str | None = None
    vendorSiteRef: str | None = None
    supplierContactName: str | None = None
    supplierContactEmail: str | None = None
    templateId: str | None = None
    industryCode: str | None = None
    auditType: str | None = None
    scopeDepartments: list[str] = []
    scopeAreas: list[str] = []
    scopeDescription: str = ""
    # Discipline scope (audit-lifecycle v2). Empty = full library.
    selectedDisciplineIds: list[str] = []
    scopePresetUsed: str | None = None  # FULL | FIRE_FOCUSED | SA8000_ISO45001 | WORKER_WELFARE | CUSTOM
    scheduledDate: datetime
    scheduledStartTime: str = "09:00"
    estimatedDurationHours: float = Field(2, gt=0, le=24)
    leadAuditorUserId: str | None = None
    # Co-auditors: structured [{userId, disciplineIds}] (per-discipline auditor
    # scope) — legacy flat ["userId"] still accepted (treated as all-disciplines).
    coAuditors: list[CoAuditorAssignment | str] = []
    auditees: list[AuditeeAssignment] = []
    plantManagerUserId: str | None = None
    openingRemarks: str = ""


class AddDisciplinesBody(BaseModel):
    disciplineIds: list[str] = Field(min_length=1)


class AddCheckpointBody(BaseModel):
    disciplineId: str
    disciplineName: str = ""
    question: str = Field(min_length=4)
    severity: str = "major"  # critical | major | minor | observation
    guidance: str = ""
    standardClauseRef: str = ""
    requirementReference: str = ""
    evidenceRequiredOnFail: bool = False
    assignedOwnerId: str | None = None
    promoteToTemplate: bool = False


class TemplateCustomCheckpointBody(BaseModel):
    disciplineId: str
    disciplineName: str = ""
    question: str = Field(min_length=4)
    severity: str = "major"
    guidance: str = ""
    standardClauseRef: str = ""
    requirementReference: str = ""
    evidenceRequiredOnFail: bool = False


class AllocateBody(BaseModel):
    ownerId: str | None = None  # null = unassign
    checkpointIds: list[str] = []  # specific instances (per-row / bulk)
    disciplineId: str | None = None  # whole-discipline assign


class TransitionBody(BaseModel):
    action: str  # AUDITEE_RESPOND | ACCEPT | REQUEST_MORE_INFO | RAISE_CAPA | ESCALATE | PM_ACCEPT | PM_RAISE_CAPA | PM_SEND_BACK | REOPEN
    comment: str = ""
    evidenceIds: list[str] = []
    photos: list[dict[str, Any]] = []
    actionTaken: str = ""
    actionDate: str | None = None
    estimatedClosureDate: str | None = None


# action -> required permission. The router also sets the record context per role
# so OWN_RECORDS scoping can apply.
_TRANSITION_PERM = {
    "AUDITEE_RESPOND": "AUDIT_COMPLIANCE.UPDATE",
    "ACCEPT": "AUDIT_COMPLIANCE.EXECUTE",
    "REQUEST_MORE_INFO": "AUDIT_COMPLIANCE.EXECUTE",
    "RAISE_CAPA": "AUDIT_COMPLIANCE.EXECUTE",
    "ESCALATE": "AUDIT_COMPLIANCE.EXECUTE",
    "REOPEN": "AUDIT_COMPLIANCE.EXECUTE",
    "PM_ACCEPT": "AUDIT_COMPLIANCE.APPROVE",
    "PM_RAISE_CAPA": "AUDIT_COMPLIANCE.APPROVE",
    "PM_SEND_BACK": "AUDIT_COMPLIANCE.APPROVE",
}


class SaveResponseBody(BaseModel):
    checkpointCode: str
    value: Literal["pass", "partial", "fail", "na", "yes", "no"] | None = None
    numericValue: float | None = None
    selectedOptions: list[str] | None = None
    textObservation: str = ""
    auditorNotes: str = ""
    photos: list[dict[str, Any]] = []
    evidenceLinks: list[dict[str, Any]] = []


class BulkResponseBody(BaseModel):
    value: Literal["pass", "na"]
    checkpointIds: list[str] = []
    disciplineId: str | None = None
    onlyUnanswered: bool = True


class AuditeeRespondBody(BaseModel):
    checkpointCode: str
    responseText: str = ""
    actionTaken: str = ""
    actionDate: str | None = None
    estimatedClosureDate: str | None = None
    photos: list[dict[str, Any]] = []


class PmReviewBody(BaseModel):
    checkpointCode: str
    decision: str  # accepted | rejected
    comments: str = ""


class CloseBody(BaseModel):
    closingRemarks: str = ""


class SignOff(BaseModel):
    role: str
    userId: str


class GenerateReportBody(BaseModel):
    reportType: str  # INTERIM | FINAL
    signOffs: list[SignOff] = []


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
    subjectType: Literal["OWN_SITE", "VENDOR"] | None = Query(
        None, description="Filter by audit subject. Omit for the unfiltered register."
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "AUDIT_COMPLIANCE.READ")
    # P1-2: permission-specific scope (fail-closed). The module-agnostic helper
    # returned None=all as soon as the user held ANY ALL_PLANTS grant, leaking
    # other plants' audits into the list.
    plants = await get_accessible_plants_for(db, user.id, "AUDIT_COMPLIANCE.READ")
    audits = await svc.list_audits(
        db, accessible_plants=plants, subject_type=subjectType,
        party_user_id=await _party_filter_for(db, user),
    )
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


class ImportLibraryBody(BaseModel):
    industryCode: str = Field(min_length=2)
    industryName: str = ""
    version: str = "2026.1"
    categories: list[dict[str, Any]]


@router.post("/library/import", status_code=status.HTTP_201_CREATED)
async def import_library(
    body: ImportLibraryBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Bulk create/replace a per-industry checkpoint library (the audit-flow
    source). Enables ≈1500-checkpoint authoring by import."""
    await _require(db, user, "AUDIT_COMPLIANCE.CREATE")
    try:
        return await svc.import_library(db, user=user, payload=body.model_dump())
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.get("/library/{industry_code}")
async def get_library(
    industry_code: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "AUDIT_COMPLIANCE.READ")
    data = await svc.get_library(db, industry_code)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Library not found")
    return data


@router.post("/templates/{template_id}/custom-checkpoints", status_code=status.HTTP_201_CREATED)
async def add_template_custom_checkpoint(
    template_id: str,
    body: TemplateCustomCheckpointBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Template-level custom checkpoint (A-08a) — forks a new template version.
    Lead-Auditor-class action (AUDIT_COMPLIANCE.CREATE)."""
    await _require(db, user, "AUDIT_COMPLIANCE.CREATE")
    try:
        return await svc.add_template_custom_checkpoint(db, user=user, template_id=template_id, payload=body.model_dump())
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.get("/dashboard/programme")
async def programme_dashboard(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "AUDIT_COMPLIANCE.READ")
    plants = await get_accessible_plants_for(db, user.id, "AUDIT_COMPLIANCE.READ")
    return await svc.programme_dashboard(
        db, accessible_plants=plants, party_user_id=await _party_filter_for(db, user)
    )


@router.get("/my-checkpoints")
async def my_checkpoints(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Auditee transparency (A-06) — every checkpoint assigned to me, all states."""
    await _require(db, user, "AUDIT_COMPLIANCE.READ")
    plants = await get_accessible_plants(db, user.id)
    return await svc.my_assigned_checkpoints(db, user=user, accessible_plants=plants)


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


@router.get("/assignable-users")
async def assignable_users(
    plantId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Who may fill each audit role at this plant, per RBAC scope.

    Separate from /users on purpose: /users stays the unfiltered directory the
    detail screen needs to render names for people already on an audit (and for
    historic assignments made before a permission was revoked). This endpoint
    is the assignment surface, and it is narrow by construction.
    """
    await _require(db, user, "AUDIT_COMPLIANCE.READ")
    return await assignment.assignable_users(db, plant_id=plantId)


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
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.READ", plant_id=audit.plantId,
                   record=_reader_record(audit), record_id=audit.id)
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
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.READ", plant_id=audit.plantId,
                   record=_reader_record(audit), record_id=audit.id)
    data = await svc.audit_dashboard(db, audit_id)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audit not found")
    return data


@router.get("/{audit_id}/finalizability")
async def get_finalizability(
    audit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Whether the audit can be finalized (every checkpoint terminal) + blockers."""
    audit = await svc._load_audit(db, audit_id)
    if audit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audit not found")
    await _require(db, user, "AUDIT_COMPLIANCE.READ", plant_id=audit.plantId,
                   record=_reader_record(audit), record_id=audit.id)
    return await svc._finalizability_db(db, audit)


@router.get("/{audit_id}/checkpoints")
async def list_checkpoints(
    audit_id: str,
    disciplineId: str | None = Query(None),
    workflowState: str | None = Query(None),
    assessmentStatus: str | None = Query(None),
    value: str | None = Query(None, description="pass|partial|fail|na|unanswered"),
    criticality: str | None = Query(None),
    q: str | None = Query(None),
    assignedAuditorId: str | None = Query(None),
    mine: bool = Query(False, description="only checkpoints assigned to me (auditor)"),
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Paginated, filterable checkpoint slice — the scalable replacement for
    walking the full `responses` array (1500-checkpoint support)."""
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.READ", plant_id=audit.plantId,
                   record=_reader_record(audit), record_id=audit.id)
    auditor_filter = user.id if mine else assignedAuditorId
    try:
        return await svc.list_checkpoints(
            db, audit_id=audit_id, discipline_id=disciplineId, workflow_state=workflowState,
            assessment_status=assessmentStatus, value=value, criticality=criticality,
            q=q, assigned_auditor_id=auditor_filter, cursor=cursor, limit=limit,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.get("/{audit_id}/checkpoints/{checkpoint_id}/interactions")
async def get_checkpoint_interactions(
    audit_id: str,
    checkpoint_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The iteration thread for ONE checkpoint, loaded on demand (lazy)."""
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.READ", plant_id=audit.plantId,
                   record=_reader_record(audit), record_id=audit.id)
    try:
        return await svc.get_checkpoint_interactions(db, audit_id=audit_id, checkpoint_id=checkpoint_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e


@router.get("/{audit_id}/reports")
async def list_reports(
    audit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.READ", plant_id=audit.plantId,
                   record=_reader_record(audit), record_id=audit.id)
    return {"reports": await svc.list_reports(db, audit_id)}


@router.post("/{audit_id}/reports", status_code=status.HTTP_201_CREATED)
async def generate_report(
    audit_id: str,
    body: GenerateReportBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Generate an Interim (EXPORT) or Final (CLOSE) report — immutable snapshot."""
    audit = await _load_or_404(db, audit_id)
    perm = "AUDIT_COMPLIANCE.CLOSE" if (body.reportType or "").upper() == "FINAL" else "AUDIT_COMPLIANCE.EXPORT"
    await _require(db, user, perm, plant_id=audit.plantId,
                   record={"leadAuditorUserId": audit.leadAuditorUserId,
                           "plantManagerUserId": audit.plantManagerUserId,
                           "createdByUserId": audit.createdByUserId},
                   record_id=audit.id)
    try:
        return await svc.generate_report(
            db, user=user, audit_id=audit_id, report_type=body.reportType,
            sign_offs=[s.model_dump() for s in body.signOffs] or None,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.get("/reports/{report_id}")
async def get_report(
    report_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    data = await svc.get_report(db, report_id)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    # Scope to the report's plant (siteId == audit.plantId) so a report isn't
    # readable cross-plant.
    _report_audit = await svc._load_audit(db, data["auditId"])
    await _require(db, user, "AUDIT_COMPLIANCE.READ", plant_id=data["siteId"],
                   record=_reader_record(_report_audit) if _report_audit else None,
                   record_id=data["auditId"])
    return data


@router.get("/reports/{report_id}/register")
async def get_report_register(
    report_id: str,
    disciplineId: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Paginated full checkpoint register for a FINAL report (served lazily, not
    stored in the immutable snapshot)."""
    try:
        data = await svc.list_report_register(db, report_id=report_id, discipline_id=disciplineId, cursor=cursor, limit=limit)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    _report_audit = await svc._load_audit(db, data["auditId"])
    await _require(db, user, "AUDIT_COMPLIANCE.READ", plant_id=data["siteId"],
                   record=_reader_record(_report_audit) if _report_audit else None,
                   record_id=data["auditId"])
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
    # SCHEDULE, not CREATE: raising an audit is deliberately a narrower right
    # than authoring checkpoint content. CREATE still gates library import and
    # template custom-checkpoints, which audit roles keep.
    await _require(db, user, "AUDIT_COMPLIANCE.SCHEDULE", plant_id=body.plantId)
    data = body.model_dump()
    data["auditees"] = [a if isinstance(a, dict) else a.model_dump() for a in body.auditees]
    # Every seat must be filled by someone who holds the permission that seat's
    # workflow actions require. The picker is already filtered to these people;
    # this is the gate that makes it true for a hand-rolled request too.
    try:
        await assignment.assert_assignable(
            db,
            plant_id=body.plantId,
            assignments={
                "leadAuditor": [body.leadAuditorUserId] if body.leadAuditorUserId else [],
                "coAuditor": [c if isinstance(c, str) else c.userId for c in body.coAuditors],
                "plantManager": [body.plantManagerUserId] if body.plantManagerUserId else [],
                "auditee": [a.userId if not isinstance(a, dict) else a["userId"] for a in body.auditees],
            },
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    try:
        audit = await svc.create_audit(db, user=user, data=data)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    await db.refresh(audit)
    return {"id": audit.id, "auditNumber": audit.auditNumber, "totalCheckpoints": audit.totalCheckpoints}


@router.post("/{audit_id}/disciplines")
async def add_disciplines(
    audit_id: str,
    body: AddDisciplinesBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Materialize additional disciplines into a running audit (before finalization)."""
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.UPDATE", plant_id=audit.plantId,
                   record=_auditor_record(audit),
                   record_id=audit.id)
    try:
        return await svc.add_disciplines(db, user=user, audit_id=audit_id, discipline_ids=body.disciplineIds)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/{audit_id}/allocate")
async def allocate_checkpoints(
    audit_id: str,
    body: AllocateBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Plant Head / Lead Auditor allocates checkpoints to owners (A-04)."""
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.UPDATE", plant_id=audit.plantId,
                   record={"leadAuditorUserId": audit.leadAuditorUserId,
                           "plantManagerUserId": audit.plantManagerUserId,
                           "createdByUserId": audit.createdByUserId},
                   record_id=audit.id)
    # Allocating a checkpoint makes someone the responding auditee for it, so
    # it needs the same eligibility check as naming an auditee up front.
    # ownerId=None is an unassign — nothing to validate.
    if body.ownerId:
        try:
            await assignment.assert_assignable(
                db, plant_id=audit.plantId, assignments={"auditee": [body.ownerId]}
            )
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    try:
        return await svc.allocate_checkpoints(
            db, user=user, audit_id=audit_id, owner_id=body.ownerId,
            checkpoint_ids=body.checkpointIds, discipline_id=body.disciplineId,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/{audit_id}/checkpoints", status_code=status.HTTP_201_CREATED)
async def add_adhoc_checkpoint(
    audit_id: str,
    body: AddCheckpointBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Auditor adds an ad-hoc custom checkpoint to this audit (carousel "+")."""
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.EXECUTE", plant_id=audit.plantId,
                   record=_auditor_record(audit),
                   record_id=audit.id)
    try:
        return await svc.add_adhoc_checkpoint(db, user=user, audit_id=audit_id, payload=body.model_dump())
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/{audit_id}/responses")
async def save_response(
    audit_id: str,
    body: SaveResponseBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.EXECUTE", plant_id=audit.plantId,
                   record=_auditor_record(audit),
                   record_id=audit.id)
    try:
        # exclude_unset → only the fields the client actually sent are merged,
        # so an observation-only save never wipes a previously-saved value.
        return await svc.save_response(db, user=user, audit_id=audit_id, payload=body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/{audit_id}/responses/bulk")
async def bulk_save_response(
    audit_id: str,
    body: BulkResponseBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Mark a set / whole-discipline as pass|na in one call (large-audit fast
    path). Never clobbers fail/partial verdicts or in-flight findings."""
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.EXECUTE", plant_id=audit.plantId,
                   record=_auditor_record(audit),
                   record_id=audit.id)
    try:
        return await svc.bulk_save_response(
            db, user=user, audit_id=audit_id, value=body.value,
            checkpoint_ids=body.checkpointIds, discipline_id=body.disciplineId,
            only_unanswered=body.onlyUnanswered,
        )
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
                   record=_auditor_record(audit),
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


@router.post("/{audit_id}/pm-review")
async def pm_review(
    audit_id: str,
    body: PmReviewBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.APPROVE", plant_id=audit.plantId,
                   record={"plantManagerUserId": audit.plantManagerUserId}, record_id=audit.id)
    try:
        return await svc.pm_review(db, user=user, audit_id=audit_id, payload=body.model_dump())
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/{audit_id}/checkpoints/{checkpoint_id}/transition")
async def transition_checkpoint(
    audit_id: str,
    checkpoint_id: str,
    body: TransitionBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Iteration state-machine action (A-05). RBAC is action-dependent."""
    audit = await _load_or_404(db, audit_id)
    perm = _TRANSITION_PERM.get(body.action)
    if perm is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown action '{body.action}'")
    if perm == "AUDIT_COMPLIANCE.UPDATE":  # auditee responding
        record = {"routedToUserId": user.id}
    elif perm == "AUDIT_COMPLIANCE.APPROVE":  # plant manager deciding
        record = {"plantManagerUserId": audit.plantManagerUserId}
    else:  # auditor actions
        record = _auditor_record(audit)
    await _require(db, user, perm, plant_id=audit.plantId, record=record, record_id=audit.id)
    try:
        return await svc.transition_checkpoint(
            db, user=user, audit_id=audit_id, checkpoint_id=checkpoint_id, action=body.action, payload=body.model_dump(),
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/{audit_id}/close")
async def close_audit(
    audit_id: str,
    body: CloseBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    audit = await _load_or_404(db, audit_id)
    await _require(db, user, "AUDIT_COMPLIANCE.CLOSE", plant_id=audit.plantId,
                   record={"plantManagerUserId": audit.plantManagerUserId,
                           "leadAuditorUserId": audit.leadAuditorUserId}, record_id=audit.id)
    try:
        return await svc.close_audit(db, user=user, audit_id=audit_id, closing_remarks=body.closingRemarks)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


# ── P2-9 Audit report PDF (fpdf2) ────────────────────────────────────────────
@router.get("/reports/{report_id}/pdf")
async def audit_report_pdf(report_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Generate the branded PDF for an audit report (cover, INTERIM watermark,
    sections, sign-off). Sets pdfAttachmentId to mark it generated."""
    from fastapi.responses import StreamingResponse
    from app.models.audit_compliance import AuditReport
    from app.services.report_pdf import render_audit_report_pdf

    rep = await db.get(AuditReport, report_id)
    if rep is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    await _require(db, user, "AUDIT_COMPLIANCE.READ")
    by_name = (await db.get(User, rep.generatedById)).name if rep.generatedById else "—"

    # The full checkpoint register, paged in here rather than read from the
    # snapshot: it is deliberately not stored there (a 1,500-checkpoint audit
    # would bloat every read of the report row), which is why the PDF used to
    # ship with findings only and no record of what was assessed and passed.
    register: list[dict[str, Any]] = []
    truncated = 0
    cursor: str | None = None
    while len(register) < _PDF_REGISTER_CAP:
        page = await svc.list_report_register(
            db, report_id=report_id, cursor=cursor, limit=200
        )
        if not page:
            break
        register.extend(page.get("register") or [])
        cursor = page.get("nextCursor")
        if not cursor:
            break
    if len(register) > _PDF_REGISTER_CAP:
        truncated = len(register) - _PDF_REGISTER_CAP
        register = register[:_PDF_REGISTER_CAP]
    elif cursor:
        # More rows exist beyond the cap; count them so the PDF can say so
        # rather than stopping without explanation.
        total = 0
        probe = await svc.list_report_register(db, report_id=report_id, limit=1)
        if probe:
            total = probe.get("total") or 0
        truncated = max(0, total - len(register))

    # Resolve every id the register carries — owners and interaction actors —
    # so the PDF prints names, never raw cuids.
    uids: set[str] = set()
    for cp in register:
        if cp.get("ownerId"):
            uids.add(cp["ownerId"])
        for it in cp.get("interactions") or []:
            if it.get("actorId"):
                uids.add(it["actorId"])
    user_names: dict[str, str] = {}
    if uids:
        rows = (await db.execute(select(User.id, User.name).where(User.id.in_(uids)))).all()
        user_names = {r[0]: r[1] for r in rows}

    pdf_bytes = render_audit_report_pdf(
        svc._report_to_dict(rep), by_name,
        register=register, user_names=user_names, register_truncated=truncated,
    )
    if not rep.pdfAttachmentId:
        rep.pdfAttachmentId = f"generated:{report_id}"
        await db.commit()
    import io
    return StreamingResponse(
        io.BytesIO(pdf_bytes), media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{rep.reportCode}.pdf"'},
    )
