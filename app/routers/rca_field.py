"""Guided RCA field-input flow (build spec 1.3).

An RCA owner requests structured cause input from field technicians; technicians
respond through the same low-literacy guided pattern (cascading cause-library
picker, max 3 levels, voice note per level, control-library suggestions).
Contributions land as RcaFieldInput rows grouped by fishbone category; the RCA
owner promotes any one to an official RcaIdentifiedCause with provenance kept.

Shares the /api/erm/rca prefix with rca.py (the ptw + ptw_active precedent).
Mounted UNGATED with the capture router (CAPTURE dev-licence situation).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.capture import CaptureTaxonomy, RcaFieldInput, RcaFieldRequest
from app.models.rca import RcaIdentifiedCause, RootCauseAnalysis, RootCauseCategory, RootCauseSubCause
from app.models.user import User
from app.services import capture as cap
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/erm/rca", tags=["erm-rca-field"])

# fishbone (capture cause library) → ERM RootCauseCategory code
FISHBONE_TO_ERM_CATEGORY = {
    "EQUIPMENT": "TECH",
    "PERSON": "PEOPLE",
    "PROCESS": "PROC",
    "ENVIRONMENT": "EXTERNAL",
    "MATERIAL": "THIRD_PARTY",
    "MANAGEMENT": "GOV",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _require(db: AsyncSession, user: User, perm: str, plant_id: str | None = None) -> None:
    res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


# ── Officer: request field input ──────────────────────────────────────────────
class FieldRequestCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    contextSummary: str = Field(default="", max_length=2000)
    hazardCategoryCode: str | None = None  # scopes the technician's cause picker
    technicianIds: list[str] = Field(default_factory=list)
    dueAt: datetime | None = None


@router.post("/{rca_id}/field-requests", status_code=status.HTTP_201_CREATED)
async def create_field_request(
    rca_id: str,
    body: FieldRequestCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rca = await db.get(RootCauseAnalysis, rca_id)
    if rca is None or rca.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "RCA not found")
    await _require(db, user, "RCA.TAG", plant_id=rca.plantId)

    req = RcaFieldRequest(
        rcaId=rca_id,
        requestedById=user.id,
        plantId=rca.plantId,
        contextSummary=body.contextSummary or (rca.title or ""),
        hazardCategoryCode=body.hazardCategoryCode,
        technicianIds=body.technicianIds,
        dueAt=body.dueAt,
    )
    db.add(req)
    await db.flush()

    # in-app notification (+ email) to each selected technician — spec 1.3
    from app.services.erm_notifications import create_notification

    for tech_id in body.technicianIds[:50]:
        await create_notification(
            db,
            user_id=tech_id,
            type="RCA_FIELD_INPUT_REQUESTED",
            title=f"Your input needed: {rca.rcaCode}",
            body=(req.contextSummary or rca.title or "A safety officer needs your help understanding what happened.")[:280],
            severity="INFO",
            entity_type="RcaFieldRequest",
            entity_id=req.id,
            link_url=f"/capture/rca/{req.id}",
            send_mail=False,
        )

    await db.commit()
    await db.refresh(req)
    return {"id": req.id, "rcaId": rca_id, "status": req.status, "notified": len(body.technicianIds)}


# ── Technician: my open requests ──────────────────────────────────────────────
@router.get("/field-requests/mine")
async def my_field_requests(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(RcaFieldRequest)
            .where(RcaFieldRequest.status == "OPEN")
            .where(RcaFieldRequest.isDeleted.is_(False))
            .order_by(RcaFieldRequest.createdAt.desc())
        )
    ).scalars().all()
    mine = [r for r in rows if user.id in (r.technicianIds or [])]
    # which have I already answered?
    answered = set()
    if mine:
        rid_list = [r.id for r in mine]
        ans = (
            await db.execute(
                select(RcaFieldInput.requestId)
                .where(RcaFieldInput.requestId.in_(rid_list))
                .where(RcaFieldInput.contributorId == user.id)
            )
        ).scalars().all()
        answered = set(ans)
    return {
        "items": [
            {
                "id": r.id,
                "rcaId": r.rcaId,
                "contextSummary": r.contextSummary,
                "hazardCategoryCode": r.hazardCategoryCode,
                "dueAt": r.dueAt.isoformat() if r.dueAt else None,
                "answered": r.id in answered,
                "createdAt": r.createdAt.isoformat() if r.createdAt else None,
            }
            for r in mine
        ]
    }


# ── Technician: request detail + the scoped cause/control pickers ────────────
@router.get("/field-requests/{request_id}")
async def get_field_request(
    request_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    req = await db.get(RcaFieldRequest, request_id)
    if req is None or req.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    is_target = user.id in (req.technicianIds or [])
    if not is_target:
        await _require(db, user, "RCA.TAG", plant_id=req.plantId)

    causes = (
        await db.execute(
            select(CaptureTaxonomy)
            .where(CaptureTaxonomy.kind == "CAUSE")
            .where(CaptureTaxonomy.active.is_(True))
            .order_by(CaptureTaxonomy.level, CaptureTaxonomy.sortWeight)
        )
    ).scalars().all()
    controls = (
        await db.execute(
            select(CaptureTaxonomy)
            .where(CaptureTaxonomy.kind == "CONTROL")
            .where(CaptureTaxonomy.active.is_(True))
            .order_by(CaptureTaxonomy.level, CaptureTaxonomy.sortWeight)
        )
    ).scalars().all()

    rca = await db.get(RootCauseAnalysis, req.rcaId)
    return {
        "id": req.id,
        "rcaId": req.rcaId,
        "rcaCode": rca.rcaCode if rca else None,
        "contextSummary": req.contextSummary,
        "hazardCategoryCode": req.hazardCategoryCode,
        "status": req.status,
        "causeLibrary": [cap.taxonomy_out(n) for n in causes],
        "controlLibrary": [cap.taxonomy_out(n) for n in controls],
    }


# ── Technician: respond ───────────────────────────────────────────────────────
class CausePathNode(BaseModel):
    model_config = ConfigDict(extra="ignore")
    level: int
    nodeId: str | None = None
    code: str | None = None
    label: str | None = None
    voiceStoragePath: str | None = None
    voiceLangCode: str | None = None


class FieldInputRespond(BaseModel):
    model_config = ConfigDict(extra="ignore")
    anonymous: bool = False
    fishboneCategory: str | None = None
    causePath: list[CausePathNode] = Field(default_factory=list)
    controlSuggestionIds: list[str] = Field(default_factory=list)
    note: str | None = None
    transcriptOriginal: str | None = None
    voiceLangCode: str | None = None


@router.post("/field-requests/{request_id}/respond", status_code=status.HTTP_201_CREATED)
async def respond_to_request(
    request_id: str,
    body: FieldInputRespond,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    req = await db.get(RcaFieldRequest, request_id)
    if req is None or req.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    if req.status != "OPEN":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This request is closed")
    if user.id not in (req.technicianIds or []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You were not asked to contribute to this request")

    # derive fishbone from the top of the tapped path when not given
    fishbone = body.fishboneCategory
    if fishbone is None and body.causePath:
        top_code = body.causePath[0].code
        if top_code:
            node = await cap.resolve_code(db, "CAUSE", top_code)
            fishbone = node.fishboneCategory if node else None

    field_input = RcaFieldInput(
        requestId=req.id,
        rcaId=req.rcaId,
        contributorId=None if body.anonymous else user.id,
        isAnonymous=body.anonymous,
        anonHash=cap.anon_hash(user.id) if body.anonymous else None,
        fishboneCategory=fishbone,
        causePath=[n.model_dump(exclude_none=True) for n in body.causePath],
        controlSuggestionIds=body.controlSuggestionIds,
        note=body.note,
        transcriptOriginal=(body.transcriptOriginal or "").strip() or None,
        voiceLangCode=body.voiceLangCode,
    )
    db.add(field_input)
    await db.flush()

    # nudge the RCA owner that input arrived
    from app.services.erm_notifications import create_notification

    rca = await db.get(RootCauseAnalysis, req.rcaId)
    await create_notification(
        db,
        user_id=req.requestedById,
        type="RCA_FIELD_INPUT_RECEIVED",
        title=f"Field input received on {rca.rcaCode if rca else 'RCA'}",
        body="A technician contributed a cause suggestion.",
        severity="INFO",
        entity_type="RootCauseAnalysis",
        entity_id=req.rcaId,
        link_url=f"/erm/rca/{req.rcaId}",
        send_mail=False,
    )
    await db.commit()
    await db.refresh(field_input)
    return {"id": field_input.id, "rcaId": req.rcaId, "ok": True}


# ── Officer: view contributions grouped by fishbone ──────────────────────────
async def _input_out(db: AsyncSession, fi: RcaFieldInput) -> dict[str, Any]:
    contributor = None
    if not fi.isAnonymous and fi.contributorId:
        u = await db.get(User, fi.contributorId)
        contributor = {"id": u.id, "name": u.name, "designation": u.designation} if u else None
    return {
        "id": fi.id,
        "fishboneCategory": fi.fishboneCategory,
        "causePath": fi.causePath or [],
        "controlSuggestionIds": fi.controlSuggestionIds or [],
        "note": fi.note,
        "transcriptOriginal": fi.transcriptOriginal,
        "transcriptEnglish": fi.transcriptEnglish,
        "isAnonymous": fi.isAnonymous,
        "contributor": contributor,
        "promotedCauseId": fi.promotedCauseId,
        "createdAt": fi.createdAt.isoformat() if fi.createdAt else None,
    }


@router.get("/{rca_id}/field-inputs")
async def list_field_inputs(
    rca_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rca = await db.get(RootCauseAnalysis, rca_id)
    if rca is None or rca.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "RCA not found")
    await _require(db, user, "RCA.READ", plant_id=rca.plantId)
    rows = (
        await db.execute(
            select(RcaFieldInput).where(RcaFieldInput.rcaId == rca_id).order_by(RcaFieldInput.createdAt.asc())
        )
    ).scalars().all()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for fi in rows:
        grouped.setdefault(fi.fishboneCategory or "UNKNOWN", []).append(await _input_out(db, fi))
    return {"total": len(rows), "byFishbone": grouped}


# ── Officer: promote a field input to an official cause ──────────────────────
class PromoteBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    subCauseId: str | None = None  # officer override; auto-resolved from fishbone otherwise
    causalRole: str = "CONTRIBUTING"  # ROOT | CONTRIBUTING | DIRECT
    confidence: str = "POSSIBLE"  # CONFIRMED | PROBABLE | POSSIBLE


@router.post("/field-inputs/{input_id}/promote", status_code=status.HTTP_201_CREATED)
async def promote_field_input(
    input_id: str,
    body: PromoteBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    fi = await db.get(RcaFieldInput, input_id)
    if fi is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Field input not found")
    rca = await db.get(RootCauseAnalysis, fi.rcaId)
    if rca is None or rca.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "RCA not found")
    await _require(db, user, "RCA.TAG", plant_id=rca.plantId)
    if fi.promotedCauseId:
        return {"ok": True, "replayed": True, "causeId": fi.promotedCauseId}

    # resolve an ERM sub-cause: officer override → fishbone-mapped category → any
    sub: RootCauseSubCause | None = None
    if body.subCauseId:
        sub = await db.get(RootCauseSubCause, body.subCauseId)
    if sub is None:
        erm_code = FISHBONE_TO_ERM_CATEGORY.get(fi.fishboneCategory or "", None)
        if erm_code:
            cat = (
                await db.execute(select(RootCauseCategory).where(RootCauseCategory.code == erm_code))
            ).scalar_one_or_none()
            if cat is not None:
                sub = (
                    await db.execute(
                        select(RootCauseSubCause)
                        .where(RootCauseSubCause.categoryId == cat.id)
                        .where(RootCauseSubCause.isDeleted.is_(False))
                        .order_by(RootCauseSubCause.name)
                        .limit(1)
                    )
                ).scalar_one_or_none()
    if sub is None:  # last resort — any active sub-cause
        sub = (
            await db.execute(
                select(RootCauseSubCause).where(RootCauseSubCause.isDeleted.is_(False)).limit(1)
            )
        ).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No cause taxonomy available — seed the RCA taxonomy first")

    # human-readable provenance: the technician's tapped path + attribution
    path_labels = " → ".join(n.get("label") or n.get("code") or "" for n in (fi.causePath or []) if n.get("label") or n.get("code"))
    who = "anonymous field technician" if fi.isAnonymous else "field technician"
    desc = f"Field-contributed ({who}): {path_labels}".strip()
    if fi.transcriptEnglish or fi.transcriptOriginal:
        desc += f' — "{fi.transcriptEnglish or fi.transcriptOriginal}"'

    cause = RcaIdentifiedCause(
        rcaId=rca.id,
        subCauseId=sub.id,
        enterpriseCategoryId=sub.categoryId,
        causalRole=body.causalRole,
        description=desc[:2000],
        confidence=body.confidence,
        createdBy=user.id,
    )
    db.add(cause)
    await db.flush()

    fi.promotedCauseId = cause.id
    fi.promotedById = user.id
    fi.promotedAt = _now()
    await db.commit()
    return {"ok": True, "causeId": cause.id, "subCauseId": sub.id}
