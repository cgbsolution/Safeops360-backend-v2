"""Guided Field Capture API.

Low-literacy field reporting (icon-first wizard → CaptureSubmission staging
row) + officer triage/conversion into the real Observation / Near Miss /
Incident modules. Idempotent on the client-generated UUID so offline sync
retries never duplicate (spec 1.4). Conversion calls the existing module
create handlers directly, so numbering, workflows and post-rules stay single-
sourced.

NB: like fire_safety, this router is mounted UNGATED in dev — the CAPTURE
licence module exists in the registry but the signed dev licence predates it.
Add "capture": "CAPTURE" to ROUTER_MODULE once a CAPTURE-inclusive licence is
issued.
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.capture import CaptureAttachment, CaptureSubmission, CaptureTaxonomy
from app.models.equipment import Equipment
from app.models.fire_safety import FireEquipment
from app.models.observation import ObservationTaxonomy
from app.models.plant import Area, Plant
from app.models.user import User
from app.schemas.capture import (
    CleanupTextBody,
    ConvertBody,
    DraftDescriptionBody,
    RejectBody,
    SubmissionCreate,
    SuggestCategoryBody,
    TriageBody,
)
from app.services import capture as svc
from app.services import events
from app.services.access_scope import build_query_scope
from app.services.permissions import PermissionContext, can
from app.services.storage import (
    create_signed_download_url,
    create_signed_upload_url,
    is_storage_configured,
)

router = APIRouter(prefix="/api/capture", tags=["capture"])

_CREATE = "CAPTURE.CREATE"
_READ = "CAPTURE.READ"
_TRIAGE = "CAPTURE.TRIAGE"
_UNMASK = "CAPTURE.UNMASK"

VALID_KINDS = {"PHOTO", "VIDEO", "VOICE", "DOCUMENT"}
ALLOWED_CAPTURE_MIME = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic",
    "video/mp4", "video/webm", "video/quicktime",
    "audio/webm", "audio/mp4", "audio/mpeg", "audio/ogg", "audio/wav", "audio/aac", "audio/3gpp",
    "application/pdf",
}
MAX_CAPTURE_FILE_SIZE = 60 * 1024 * 1024  # 30s video ceiling


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _require(db: AsyncSession, user: User, perm: str, plant_id: str | None = None,
                   record: dict | None = None, record_id: str | None = None) -> None:
    res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id, record=record, record_id=record_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


async def _can_view(db: AsyncSession, user: User, sub: CaptureSubmission) -> bool:
    if svc.is_owner(sub, user.id):
        return True
    res = await can(db, user.id, _READ, PermissionContext(plant_id=sub.plantId, record_id=sub.id,
                                                          record={"reporterId": sub.reporterId}))
    return res.allowed


async def _load(db: AsyncSession, submission_id: str) -> CaptureSubmission:
    sub = await db.get(CaptureSubmission, submission_id)
    if sub is None or sub.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Field report not found")
    return sub


def _fire_asset_context(asset: FireEquipment) -> dict[str, Any]:
    """The asset context a field report carries, and what a reader needs.

    `code` and `location` are the two the reporter and the triager both read —
    "FE-ACS-0011, Cutting Hall corridor" is what makes a finding actionable.
    `allottedSerialNo` is the tag actually stencilled on the cylinder, which is
    what the person standing at it can verify by eye; `type`/`subtype` are what
    let triage route a panel fault differently from a discharged extinguisher.
    """
    return {
        "id": asset.id,
        # The sticker's opaque token, so the wizard can map a scan to this asset
        # ON DEVICE. Without it a corridor scan would need a round-trip to learn
        # which cylinder it is holding, which is exactly where signal is worst.
        "qrToken": asset.qrToken,
        "code": asset.equipmentCode,
        "allottedSerialNo": asset.allottedSerialNo,
        "location": asset.location,
        "type": asset.type,
        "subtype": asset.assetSubtype,
        "plantId": asset.plantId,
    }


def _fire_asset_line(snapshot: dict[str, Any] | None) -> str | None:
    """One human line naming the asset, for records with nowhere to put an id.

    `Observation` has no asset column at all and `NearMiss.equipmentId` means an
    `Equipment` row, so neither can hold a `FireEquipment` id without creating a
    reference that resolves to nothing. The structured link stays on the
    CaptureSubmission — which is the golden-thread anchor conversion already
    records both ends of — and the converted record carries the asset in the one
    field that is guaranteed to be read: its narrative.
    """
    if not snapshot:
        return None
    code = snapshot.get("code") or snapshot.get("id")
    tag = snapshot.get("allottedSerialNo")
    where = snapshot.get("location")
    # Each part is included only when it exists. A snapshot missing its code
    # still names the location it came from; printing "Fire asset: None" into a
    # statutory record would be worse than naming one thing less.
    bits: list[str] = []
    if code:
        bits.append(f"Fire asset: {code}")
    if tag:
        bits.append(f"tag {tag}")
    if where:
        bits.append(where if bits else f"Fire asset at {where}")
    return " · ".join(bits) or None


async def _attachments(db: AsyncSession, submission_id: str) -> list[CaptureAttachment]:
    return list((
        await db.execute(
            select(CaptureAttachment)
            .where(CaptureAttachment.submissionId == submission_id)
            .where(CaptureAttachment.deletedAt.is_(None))
            .order_by(CaptureAttachment.uploadedAt.asc())
        )
    ).scalars().all())


# ── Bootstrap (wizard boot payload: my plant, areas, taxonomy version) ────────
@router.get("/bootstrap")
async def bootstrap(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, _CREATE)
    plant = await db.get(Plant, user.plantId) if user.plantId else None
    areas: list[Area] = []
    equipment: list[Equipment] = []
    fire_assets: list[FireEquipment] = []
    if plant is not None:
        areas = list((
            await db.execute(select(Area).where(Area.plantId == plant.id).order_by(Area.name.asc()))
        ).scalars().all())
        # Compact plant-scoped asset directory so a scanned QR equipment token
        # resolves to a name in the Context Banner *on-device* — the wizard
        # caches this bootstrap in IndexedDB, so QR asset resolution keeps
        # working with zero connectivity (the offline-first QR decision).
        equipment = list((
            await db.execute(
                select(Equipment)
                .where(Equipment.plantId == plant.id)
                .where(Equipment.active.is_(True))
                .order_by(Equipment.name.asc())
                .limit(1000)
            )
        ).scalars().all())
        # The same directory for fire-register assets, and for the same reason:
        # a `safeops:fire-asset:` sticker is scanned in a corridor, which is
        # where connectivity is worst. Without this the Context Banner would
        # have to round-trip to name the cylinder the reporter is standing at,
        # and would show nothing at all in a dead zone.
        #
        # A separate list rather than merged into `equipment`: the ids come from
        # a different table and the wizard must not confuse the two, which is
        # the whole reason fire assets got their own QR noun.
        fire_assets = list((
            await db.execute(
                select(FireEquipment)
                .where(FireEquipment.plantId == plant.id)
                .where(FireEquipment.isDeleted.is_(False))
                .order_by(FireEquipment.equipmentCode.asc())
                .limit(1000)
            )
        ).scalars().all())
    settings_features = _features()
    return {
        "user": {"id": user.id, "name": user.name, "plantId": user.plantId},
        "plant": {"id": plant.id, "code": plant.code, "name": plant.name} if plant else None,
        "areas": [{"id": a.id, "name": a.name} for a in areas],
        "equipment": [
            {"id": e.id, "code": e.code, "name": e.name, "location": e.location}
            for e in equipment
        ],
        "fireAssets": [_fire_asset_context(a) for a in fire_assets],
        "taxonomyVersion": await svc.taxonomy_version(db),
        "features": settings_features,
    }


@router.get("/fire-asset/by-token/{token}")
async def fire_asset_by_token(
    token: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Resolve a scanned fire sticker to its asset, for the capture wizard.

    Declared BEFORE /fire-asset/{asset_id} so "by-token" is never swallowed as
    an asset id by the path matcher.

    Accepts the current opaque token and, while legacy scanning is on, a
    pre-reprint label carrying the bare asset id — `fire_qr.resolve` decides
    which, because only the database can tell them apart.
    """
    await _require(db, user, _CREATE)
    from app.services import fire_qr as qrsvc

    asset, _how = await qrsvc.resolve(db, token)
    if asset is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "That sticker is not in the fire register.",
        )
    if user.plantId and asset.plantId != user.plantId:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "That sticker belongs to a different site.",
        )
    return _fire_asset_context(asset)


@router.get("/fire-asset/{asset_id}")
async def fire_asset_context(
    asset_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Resolve one scanned fire-asset sticker, for the wizard's Context Banner.

    The bootstrap already ships a plant-scoped fire asset directory so the
    common scan resolves on-device with no connectivity. This covers the two
    cases that directory cannot: an asset registered after the reporter's cache
    was built, and entry into the wizard by `?fireAsset=<id>` from the fire
    scan route rather than by scanning inside it.

    Gated on CAPTURE.CREATE, not FIRE.READ. The question being asked is "may
    this person file a report about the thing they just scanned", and a
    contractor holding no fire grant can still be the one who finds a
    discharged cylinder. Nothing here exposes inspection history, certificates
    or status — only what is printed on the sticker and its location.
    """
    await _require(db, user, _CREATE)
    asset = await db.get(FireEquipment, asset_id)
    if asset is None or asset.isDeleted:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "That sticker is not in the fire register.",
        )
    if user.plantId and asset.plantId != user.plantId:
        # 404 rather than 403: whether a given id exists at another site is not
        # this reporter's business, and the actionable advice is identical.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "That sticker belongs to a different site.",
        )
    return _fire_asset_context(asset)


def _features() -> dict[str, bool]:
    """Tenant feature flags: licence featureFlags override env settings
    (DECISIONS.md D13). Defaults off; everything fails soft."""
    import os

    def env_flag(name: str) -> bool:
        return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")

    flags = {
        "aiCaptureAssist": env_flag("AI_CAPTURE_ASSIST"),
        "voiceTranscription": env_flag("VOICE_TRANSCRIPTION"),
        "dailyBriefDigest": env_flag("DAILY_BRIEF_DIGEST"),
    }
    try:
        from app.licensing.state import get_state
        state = get_state()
        licence_flags = state.payload.feature_flags if state.payload else {}
        for key in flags:
            if key in licence_flags:
                flags[key] = bool(licence_flags[key])
    except Exception:  # noqa: BLE001 — licence state unavailable → env defaults
        pass
    return flags


# ── AI vision-suggest (feature-flagged, provider abstraction) ────────────────
@router.post("/ai/vision-suggest")
async def vision_suggest(
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Photo → suggested hazard category + one-line draft (spec 1.2 screen 3).
    Fails soft to {ok:false} so the wizard degrades to manual selection — the
    UI never blocks on AI. Gated by features.aiCaptureAssist."""
    await _require(db, user, _CREATE)
    if not _features()["aiCaptureAssist"]:
        return {"ok": False, "reason": "ai_disabled"}

    import base64

    lang = str(payload.get("lang") or "hi")
    mime_type = str(payload.get("mimeType") or "image/jpeg")
    try:
        image = base64.b64decode(str(payload.get("imageB64") or ""), validate=True)
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "bad_image"}
    if len(image) == 0 or len(image) > 5 * 1024 * 1024:
        return {"ok": False, "reason": "bad_image"}

    # taxonomy (HAZARD, with parent codes) for the provider prompt
    nodes = (
        await db.execute(
            select(CaptureTaxonomy)
            .where(CaptureTaxonomy.kind == "HAZARD")
            .where(CaptureTaxonomy.active.is_(True))
        )
    ).scalars().all()
    by_id = {n.id: n for n in nodes}
    taxonomy = [
        {"code": n.code, "parentCode": by_id[n.parentId].code if n.parentId and n.parentId in by_id else None,
         "labels": n.labels}
        for n in nodes
    ]

    from app.services.ai.capture_providers import get_vision_provider

    provider = get_vision_provider(enabled=True)
    suggestion = await provider.suggest(image, mime_type, taxonomy, lang)
    if suggestion is None:
        return {"ok": False, "reason": "no_suggestion", "provider": provider.name}

    # resolve suggested codes → ids so the client can confirm with one tap
    l1 = await svc.resolve_code(db, "HAZARD", suggestion["l1Code"]) if suggestion.get("l1Code") else None
    l2 = await svc.resolve_code(db, "HAZARD", suggestion["l2Code"]) if suggestion.get("l2Code") else None

    if l1 is None or suggestion["confidence"] < 0.35:
        return {"ok": False, "reason": "low_confidence", "provider": provider.name}

    # audit the AI suggestion surfaced (acceptance is audited on submit)
    from app.services.audit_log import record_event
    await record_event(
        db, entity_type="CaptureVisionSuggest", entity_id=user.id,
        action="AI_SUGGESTION", after={"l1": l1.code, "confidence": suggestion["confidence"]},
        reason="Vision-suggest surfaced",
    )
    await db.commit()

    # The model classifies against the CaptureTaxonomy hazard tree, but the
    # observation flow's Category step is the DuPont STOP taxonomy. Resolve the
    # equivalent STOP pair for BOTH axes here so the client can apply the
    # suggestion to whichever one the reporter picked — rather than shipping a
    # second copy of the mapping to the browser, which is how the original
    # shared category list drifted across surfaces in the first place.
    from app.services import observation_taxonomy as obs_tax

    stop_by_axis = {}
    for axis in ("ACT", "CONDITION"):
        cat_code, sub_code = obs_tax.stop_pair_for_hazard(l1.code, axis)
        labels = (
            await db.execute(
                select(ObservationTaxonomy)
                .where(ObservationTaxonomy.categoryCode == cat_code)
                .where(ObservationTaxonomy.subCategoryCode == sub_code)
                .where(ObservationTaxonomy.observationType == axis)
                .where(ObservationTaxonomy.isActive.is_(True))
                .limit(1)
            )
        ).scalar_one_or_none()
        if labels is not None:
            stop_by_axis[axis] = {
                "categoryCode": labels.categoryCode,
                "categoryLabel": labels.categoryLabel,
                "subCategoryCode": labels.subCategoryCode,
                "subCategoryLabel": labels.subCategoryLabel,
                "stopReferenceCode": labels.stopReferenceCode,
            }

    return {
        "ok": True,
        "provider": provider.name,
        "l1": {"id": l1.id, "code": l1.code, "labels": l1.labels, "iconKey": l1.iconKey},
        "l2": {"id": l2.id, "code": l2.code, "labels": l2.labels, "iconKey": l2.iconKey} if l2 else None,
        # Empty when the taxonomy master isn't seeded yet — the client falls
        # back to hiding the suggestion rather than offering a dead control.
        "stop": stop_by_axis,
        "description": suggestion["description"],
        "descriptionEn": suggestion["descriptionEn"],
        "confidence": suggestion["confidence"],
    }


# ── AI text assist: grammar cleanup (spec §7a) ───────────────────────────────
@router.post("/ai/cleanup-text")
async def cleanup_text_endpoint(
    body: CleanupTextBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Grammar/clarity cleanup of a voice transcript or typed note. Returns
    BOTH the original and the cleaned text so the wizard can show the diff and
    require the technician to actively accept (spec §7a — never silently
    replaces what they said). Fail-soft: ok:false → wizard keeps the original."""
    await _require(db, user, _CREATE)
    if not _features()["aiCaptureAssist"]:
        return {"ok": False, "reason": "ai_disabled", "original": body.text}

    from app.services.ai.capture_providers import cleanup_text

    cleaned = await cleanup_text(body.text, body.lang)
    if cleaned is None:
        return {"ok": False, "reason": "no_result", "original": body.text}
    return {"ok": True, "original": body.text, "cleaned": cleaned}


# ── AI text assist: text → category suggestion (spec §7b) ────────────────────
@router.post("/ai/suggest-category")
async def suggest_category_endpoint(
    body: SuggestCategoryBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Description text → suggested hazard category. Presented as a pre-selected
    'suggested' chip the technician confirms or changes (spec §7b — never
    auto-applied). Reuses the same HAZARD taxonomy as manual selection so no
    new vocabulary is invented. Fail-soft to {ok:false}."""
    await _require(db, user, _CREATE)
    if not _features()["aiCaptureAssist"]:
        return {"ok": False, "reason": "ai_disabled"}

    nodes = (
        await db.execute(
            select(CaptureTaxonomy)
            .where(CaptureTaxonomy.kind == "HAZARD")
            .where(CaptureTaxonomy.active.is_(True))
        )
    ).scalars().all()
    by_id = {n.id: n for n in nodes}
    taxonomy = [
        {"code": n.code, "parentCode": by_id[n.parentId].code if n.parentId and n.parentId in by_id else None,
         "labels": n.labels}
        for n in nodes
    ]

    from app.services.ai.capture_providers import suggest_category_from_text

    suggestion = await suggest_category_from_text(body.text, taxonomy, body.lang)
    if suggestion is None or not suggestion.get("l1Code"):
        return {"ok": False, "reason": "no_suggestion"}

    l1 = await svc.resolve_code(db, "HAZARD", suggestion["l1Code"]) if suggestion.get("l1Code") else None
    l2 = await svc.resolve_code(db, "HAZARD", suggestion["l2Code"]) if suggestion.get("l2Code") else None
    if l1 is None or suggestion["confidence"] < 0.35:
        return {"ok": False, "reason": "low_confidence"}

    return {
        "ok": True,
        "l1": {"id": l1.id, "code": l1.code, "labels": l1.labels, "iconKey": l1.iconKey},
        "l2": {"id": l2.id, "code": l2.code, "labels": l2.labels, "iconKey": l2.iconKey} if l2 else None,
        "confidence": suggestion["confidence"],
    }


# ── AI text assist: guided answers → drafted description (guided draft) ───────
@router.post("/ai/draft-description")
async def draft_description_endpoint(
    body: DraftDescriptionBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """A few guided-question answers → a drafted report description the reporter
    accepts or edits (never auto-applied). Fact-only by construction so AI can't
    invent detail. Fail-soft to {ok:false} → the wizard falls back to plain
    typing. Gated by features.aiCaptureAssist."""
    await _require(db, user, _CREATE)
    if not _features()["aiCaptureAssist"]:
        return {"ok": False, "reason": "ai_disabled"}

    from app.services.ai.capture_providers import draft_description

    draft = await draft_description(
        report_type=body.reportType,
        category_label=body.categoryLabel,
        location=body.location,
        severity=body.severity,
        answers=[{"q": a.q, "a": a.a} for a in body.answers],
        lang=body.lang,
    )
    if not draft or not draft.get("description"):
        return {"ok": False, "reason": "no_result"}
    return {"ok": True, "description": draft["description"], "descriptionEn": draft.get("descriptionEn", "")}


# ── Taxonomy (precacheable; 304 on version match) ─────────────────────────────
@router.get("/taxonomy")
async def get_taxonomy(
    kind: str | None = Query(None),
    version: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    current = await svc.taxonomy_version(db)
    if version is not None and version == current:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    stmt = select(CaptureTaxonomy).where(CaptureTaxonomy.active.is_(True))
    if kind:
        stmt = stmt.where(CaptureTaxonomy.kind == kind.upper())
    rows = (await db.execute(stmt.order_by(CaptureTaxonomy.level.asc(), CaptureTaxonomy.sortWeight.asc()))).scalars().all()
    return {"version": current, "items": [svc.taxonomy_out(n) for n in rows]}


# ── Submit (idempotent) ───────────────────────────────────────────────────────
@router.post("/submissions", status_code=status.HTTP_201_CREATED)
async def create_submission(
    payload: SubmissionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plant_id = payload.plantId or user.plantId
    if not plant_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No plant on your profile — ask your admin to assign one.")
    await require_permission_with_context(_CREATE, user, db, plant_id=plant_id)

    # Idempotency, check-first: a sync retry returns the existing record.
    existing = await svc.find_existing(db, payload.clientSubmissionId)
    if existing is not None:
        atts = await _attachments(db, existing.id)
        out = svc.submission_out(existing, viewer_is_owner=True, attachments=atts)
        out["replayed"] = True
        return out

    plant = await db.get(Plant, plant_id)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")

    area_id = payload.location.areaId
    if area_id:
        area = await db.get(Area, area_id)
        if area is None or area.plantId != plant_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Area does not belong to your plant")

    # Asset (equipment) from a scanned QR token — validate against the plant.
    # Field-first: a re-pointed or retired sticker must never BLOCK a report,
    # so an unresolvable/foreign token is dropped (the report still lands,
    # just without a linked asset) rather than 400-ing a low-literacy user.
    equipment_id = payload.location.equipmentId
    if equipment_id:
        equip = await db.get(Equipment, equipment_id)
        if equip is None or equip.plantId != plant_id or not equip.active:
            equipment_id = None

    # Fire-register asset from a `safeops:fire-asset:` sticker. Rejected rather
    # than dropped when it does not resolve — deliberately the opposite of the
    # equipment rule above, and the difference is not an inconsistency:
    #
    #   * An equipment token arrives from a sticker the reporter may have
    #     scanned incidentally while aiming at an area code. Losing the link
    #     costs a cross-reference.
    #   * A fire-asset token is only ever present because someone deliberately
    #     scanned a cylinder or panel to report a finding ON IT. A report that
    #     silently loses that link is a fire finding filed against nothing —
    #     it reads as a generic area observation and no one re-inspects the
    #     asset. Dropping it quietly is the more expensive failure.
    #
    # The wizard resolves the sticker at scan time (GET fire-asset/{id} below)
    # and makes the reporter choose, so reaching this 400 means the asset was
    # deleted or re-plumbed between the scan and the sync — genuinely rare, and
    # worth an explicit message rather than a quiet downgrade.
    fire_asset_id = payload.location.fireAssetId
    fire_snapshot: dict[str, Any] | None = None
    if fire_asset_id:
        asset = await db.get(FireEquipment, fire_asset_id)
        if asset is None or asset.isDeleted:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "That fire asset is no longer in the register, so this report cannot be "
                "linked to it. Submit again without the scan, or ask your supervisor to "
                "check whether the sticker is still valid.",
            )
        if asset.plantId != plant_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "That fire asset belongs to a different site from the one you are "
                "reporting against.",
            )
        fire_snapshot = _fire_asset_context(asset)

    # Category: ids preferred; stable codes resolved (alias-aware) for
    # offline clients with a stale taxonomy cache.
    l1: CaptureTaxonomy | None = None
    l2: CaptureTaxonomy | None = None
    cat = payload.category
    if cat is not None:
        if cat.l1Id:
            l1 = await db.get(CaptureTaxonomy, cat.l1Id)
        elif cat.l1Code:
            l1 = await svc.resolve_code(db, "HAZARD", cat.l1Code)
        if cat.l2Id:
            l2 = await db.get(CaptureTaxonomy, cat.l2Id)
        elif cat.l2Code:
            l2 = await svc.resolve_code(db, "HAZARD", cat.l2Code)

    snapshot: dict[str, Any] | None = None
    if l1 is not None or l2 is not None:
        snapshot = {
            "l1": {"code": l1.code, "labels": l1.labels, "iconKey": l1.iconKey} if l1 else None,
            "l2": {"code": l2.code, "labels": l2.labels, "iconKey": l2.iconKey} if l2 else None,
        }

    # ── DuPont STOP classification (observation flow) ──
    # The observation flow's Category step is driven by the act/condition axis,
    # so it sends STOP codes instead of CaptureTaxonomy hazard nodes. Both the
    # axis and the pair live in categorySnapshot — no column, and the snapshot
    # is already the thing every downstream reader (alert rules, description
    # synthesis, conversion) consults.
    stop_axis = svc.capture_axis(payload.type, payload.observationKind)
    if stop_axis is not None:
        from app.services import observation_taxonomy as obs_tax

        stop_cat = (payload.category.stopCategoryCode if payload.category else None) or None
        stop_sub = (payload.category.stopSubCategoryCode if payload.category else None) or None
        stop_entry: dict[str, Any] = {"axis": stop_axis}

        if stop_cat and stop_sub:
            # Same gate the observation endpoints use, so a hand-rolled client
            # can't stage a mismatched pair that only fails later at conversion
            # — when the reporter is long gone and a triager gets the 400.
            row = (
                await db.execute(
                    select(ObservationTaxonomy)
                    .where(ObservationTaxonomy.categoryCode == stop_cat)
                    .where(ObservationTaxonomy.subCategoryCode == stop_sub)
                    .where(ObservationTaxonomy.observationType == stop_axis)
                    .where(ObservationTaxonomy.isActive.is_(True))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"'{stop_cat} / {stop_sub}' is not a valid category for an unsafe "
                    f"{stop_axis.lower()}.",
                )
            stop_entry |= {
                "categoryCode": row.categoryCode,
                "subCategoryCode": row.subCategoryCode,
                "stopReferenceCode": row.stopReferenceCode,
            }
            # Mirror into the l1/l2 slots so `categorySnapshot->l1->code` alert
            # rules and synth_description keep working unchanged for this flow.
            snapshot = snapshot or {"l1": None, "l2": None}
            snapshot["l1"] = {"code": row.categoryCode, "labels": {"en": row.categoryLabel}, "iconKey": None}
            snapshot["l2"] = {"code": row.subCategoryCode, "labels": {"en": row.subCategoryLabel}, "iconKey": None}
        elif stop_cat or stop_sub:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Pick both a category and a sub-category, or neither.",
            )

        # Recorded even when the reporter skipped the category step — the axis
        # alone is what stops the conversion defaulting everything to an act.
        snapshot = snapshot or {"l1": None, "l2": None}
        snapshot["stop"] = stop_entry

    number = await svc.next_fld_number(db, plant.code, plant.id)
    voice = payload.voice
    meta = payload.capture

    sub = CaptureSubmission(
        number=number,
        clientSubmissionId=payload.clientSubmissionId,
        type=payload.type,
        reporterId=None if payload.anonymous else user.id,
        isAnonymous=payload.anonymous,
        anonHash=svc.anon_hash(user.id) if payload.anonymous else None,
        plantId=plant_id,
        areaId=area_id,
        mapPinX=payload.location.mapPinX,
        mapPinY=payload.location.mapPinY,
        equipmentId=equipment_id,
        fireAssetId=fire_asset_id,
        fireAssetSnapshot=fire_snapshot,
        qrScanned=payload.location.qrScanned,
        categoryL1Id=l1.id if l1 else None,
        categoryL2Id=l2.id if l2 else None,
        categorySnapshot=snapshot,
        aiSuggested=bool(cat and cat.aiSuggested),
        aiConfidence=cat.aiConfidence if cat else None,
        severitySelfReported=payload.severity,
        description=(payload.description or "").strip() or None,
        voiceLangCode=voice.langCode if voice else None,
        transcriptOriginal=(voice.transcriptOriginal or "").strip() or None if voice else None,
        transcriptionStatus="device" if (voice and voice.transcriptOriginal) else "none",
        tapCount=meta.tapCount if meta else None,
        durationMs=meta.durationMs if meta else None,
        wasOffline=bool(meta and meta.offline),
        appVersion=meta.appVersion if meta else None,
        deviceLang=meta.deviceLang if meta else None,
        taxonomyVersion=payload.taxonomyVersion,
        createdAtClient=payload.createdAtClient,
    )
    db.add(sub)
    try:
        # SAVEPOINT so a unique-index race (two concurrent sync retries)
        # rolls back only the insert, not the whole session.
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        replay = await svc.find_existing(db, payload.clientSubmissionId)
        if replay is None:
            raise
        out = svc.submission_out(replay, viewer_is_owner=True, attachments=[])
        out["replayed"] = True
        return out

    # Domain event — same transaction (outbox).
    events.emit(
        db,
        event_type=events.CAPTURE_SUBMITTED,
        entity_type="CaptureSubmission",
        entity_id=sub.id,
        entity_ref=sub.number,
        site_id=plant_id,
        actor_id=None if payload.anonymous else user.id,
        payload={
            "type": sub.type,
            "severity": sub.severitySelfReported,
            "categoryL1": l1.code if l1 else None,
            "areaId": area_id,
            "offline": sub.wasOffline,
        },
    )

    # "Your safety officer has been notified" — best-effort, never blocks.
    try:
        async with db.begin_nested():
            from app.services.erm_notifications import _users_with_role, create_notification

            officers = await _users_with_role(db, "SAFETY_OFFICER", plant_id)
            label = (snapshot or {}).get("l1") or {}
            cat_en = (label.get("labels") or {}).get("en") or "Field report"
            for officer in officers[:10]:
                await create_notification(
                    db,
                    user_id=officer.id,
                    type="FIELD_REPORT_SUBMITTED",
                    title=f"{sub.number}: {cat_en} ({sub.severitySelfReported})",
                    body=f"New field report at {plant.name} — {sub.type.replace('_', ' ')}.",
                    severity="WARNING" if sub.severitySelfReported == "high" else "INFO",
                    entity_type="CaptureSubmission",
                    entity_id=sub.id,
                    link_url=f"/field-reports/{sub.id}",
                    send_mail=sub.severitySelfReported == "high",
                )
    except Exception as e:  # noqa: BLE001
        print(f"Capture notify failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    await db.commit()
    await db.refresh(sub)
    out = svc.submission_out(sub, viewer_is_owner=True, attachments=[])
    out["replayed"] = False
    return out


# ── My reports (reporter history — includes own anonymous reports) ───────────
@router.get("/submissions/mine")
async def my_submissions(
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, _CREATE)
    my_hash = svc.anon_hash(user.id)
    stmt = (
        select(CaptureSubmission)
        .where(CaptureSubmission.isDeleted.is_(False))
        .where((CaptureSubmission.reporterId == user.id) | (CaptureSubmission.anonHash == my_hash))
        .order_by(CaptureSubmission.createdAt.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {"items": [svc.submission_out(s, viewer_is_owner=True) for s in rows], "total": len(rows)}


# ── Triage queue (officers) ───────────────────────────────────────────────────
@router.get("/submissions")
async def list_submissions(
    status_filter: str | None = Query(None, alias="status"),
    type_filter: str | None = Query(None, alias="type"),
    severity: str | None = Query(None),
    plant_id: str | None = Query(None, alias="plantId"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, _READ)
    scope = await build_query_scope(db, user.id, _READ)
    stmt = scope.apply(
        select(CaptureSubmission).where(CaptureSubmission.isDeleted.is_(False)), CaptureSubmission
    )
    if plant_id:
        stmt = stmt.where(CaptureSubmission.plantId == plant_id)
    if status_filter:
        stmt = stmt.where(CaptureSubmission.status == status_filter)
    if type_filter:
        stmt = stmt.where(CaptureSubmission.type == type_filter)
    if severity:
        stmt = stmt.where(CaptureSubmission.severitySelfReported == severity)
    rows = (
        await db.execute(stmt.order_by(CaptureSubmission.createdAt.desc()).offset(offset).limit(limit))
    ).scalars().all()
    return {"items": [svc.submission_out(s) for s in rows], "total": len(rows), "offset": offset}


@router.get("/submissions/{submission_id}")
async def get_submission(
    submission_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    sub = await _load(db, submission_id)
    if not await _can_view(db, user, sub):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    atts = await _attachments(db, sub.id)
    return svc.submission_out(sub, viewer_is_owner=svc.is_owner(sub, user.id), attachments=atts)


# ── Officer triage: map onto the 5x5 (technician never sees a matrix) ────────
@router.post("/submissions/{submission_id}/triage")
async def triage_submission(
    submission_id: str,
    body: TriageBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    sub = await _load(db, submission_id)
    await _require(db, user, _TRIAGE, plant_id=sub.plantId)
    if sub.status in ("converted", "rejected", "closed"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Cannot triage a {sub.status} report")

    sub.hiraLikelihood = body.hiraLikelihood
    sub.hiraSeverity = body.hiraSeverity
    sub.riskScore = body.hiraLikelihood * body.hiraSeverity
    sub.riskLevel = svc.risk_level_for(sub.riskScore)
    sub.triageNote = body.note
    sub.triagedById = user.id
    sub.triagedAt = _now()
    sub.status = "triaged"

    if sub.riskLevel in ("HIGH", "CRITICAL"):
        events.emit(
            db,
            event_type=events.OBSERVATION_TRIAGED_HIGH,
            entity_type="CaptureSubmission",
            entity_id=sub.id,
            entity_ref=sub.number,
            site_id=sub.plantId,
            actor_id=user.id,
            payload={
                "riskLevel": sub.riskLevel,
                "riskScore": sub.riskScore,
                "categoryL1": ((sub.categorySnapshot or {}).get("l1") or {}).get("code"),
                "areaId": sub.areaId,
                "type": sub.type,
            },
        )

    await db.commit()
    await db.refresh(sub)
    return svc.submission_out(sub, attachments=await _attachments(db, sub.id))


# ── Convert into the real module record (golden-thread entry) ─────────────────
@router.post("/submissions/{submission_id}/convert")
async def convert_submission(
    submission_id: str,
    body: ConvertBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    sub = await _load(db, submission_id)
    await _require(db, user, _TRIAGE, plant_id=sub.plantId)
    if sub.convertedEntityId:
        return {
            "ok": True, "replayed": True,
            "entityType": sub.convertedEntityType, "entityId": sub.convertedEntityId,
        }

    from pydantic import ValidationError

    description = (body.description or "").strip() or svc.synth_description(sub)
    # The scanned fire asset, named up front. Prepended rather than appended
    # because the converted record's description is what an inspector reads
    # first, and "which cylinder" is the question that decides whether anyone
    # walks back out to it. Guarded on the text so a triager who already typed
    # the code does not get it twice.
    fire_line = _fire_asset_line(sub.fireAssetSnapshot)
    if fire_line:
        code = (sub.fireAssetSnapshot or {}).get("code") or ""
        if not code or code.lower() not in description.lower():
            description = f"{fire_line}. {description}".strip()
    # At-risk observations (UNSAFE_ACT / UNSAFE_CONDITION) are gated at >=50
    # chars by the BBS quality rule (bbs_quality.validate_quality). A terse
    # officer note — or a capture where the worker typed nothing — would 400 on
    # convert. Enrich it with the captured context instead of rejecting it.
    if len(description) < 50:
        snap = sub.categorySnapshot or {}
        l1 = ((snap.get("l1") or {}).get("labels") or {}).get("en")
        l2 = ((snap.get("l2") or {}).get("labels") or {}).get("en")
        extras: list[str] = []
        if l1 and l1.lower() not in description.lower():
            extras.append(f"Hazard: {l1}{' — ' + l2 if l2 else ''}.")
        if sub.severitySelfReported:
            extras.append(f"Reporter-assessed severity: {sub.severitySelfReported}.")
        transcript = sub.transcriptEnglish or sub.transcriptOriginal
        if transcript and transcript.strip() and transcript.strip() not in description:
            extras.append(f'Reporter said: "{transcript.strip()}".')
        extras.append(f"Captured via guided field capture ({sub.number}); see the field report for evidence.")
        description = " ".join([description, *extras]).strip()
    if len(description) < 10:
        description = description + " (field report)"
    module_severity = (
        svc.RISK_LEVEL_TO_MODULE.get(sub.riskLevel or "", None)
        or svc.SELF_SEVERITY_TO_MODULE[sub.severitySelfReported]
    )
    when = sub.createdAtClient or sub.createdAt or _now()

    entity_type: str
    entity_id: str
    entity_ref: str

    if body.target == "observation":
        from app.models.observation import ObservationAttachment
        from app.routers.observations import create_observation
        from app.schemas.observation import ObservationCreate

        from app.services import observation_taxonomy as obs_tax

        # The axis comes from what the reporter actually chose on the Kind step,
        # not from the submission type. `sub.type` is "observation" for BOTH
        # kinds, so the old `type in (...)` test silently made every capture
        # observation an UNSAFE_ACT — the Kind chip only ever reached the
        # description text.
        axis = svc.axis_from_snapshot(sub)
        obs_type = "UNSAFE_CONDITION" if axis == "CONDITION" else "UNSAFE_ACT"

        # A converted field report is always at-risk, so it must resolve to a
        # real STOP pair. Preference order: what the triager typed on this call
        # > what the reporter picked in the wizard > a mapping from the legacy
        # CaptureTaxonomy hazard (older submissions, and the non-observation
        # flows that still classify by hazard).
        picked_cat, picked_sub = svc.stop_pair_from_snapshot(sub)
        l1_code = ((sub.categorySnapshot or {}).get("l1") or {}).get("code") or ""
        mapped_cat, mapped_sub = obs_tax.stop_pair_for_hazard(l1_code, axis)
        cat_code = (getattr(body, "categoryCode", None) or "").strip() or picked_cat or mapped_cat
        sub_code = (getattr(body, "subCategoryCode", None) or "").strip() or picked_sub or mapped_sub
        # The severity here is the triager's call, carried over from the field
        # report's own risk band — NOT the observer-form dropdown. It can differ
        # from what the severity matrix suggests for the STOP pair above, and
        # create_observation requires a justification whenever it does. Supplied
        # unconditionally: it costs nothing when the two agree (no override row
        # is written at all) and it is the honest description of the decision
        # when they don't. `severityOverrideSource` keeps these out of the
        # default calibration report, which measures observer disagreement.
        from app.models.observation_severity import OVERRIDE_SOURCE_CAPTURE_CONVERSION

        try:
            payload = ObservationCreate(
                plantId=sub.plantId,
                areaId=sub.areaId,
                type=obs_type,
                categoryCode=cat_code,
                subCategoryCode=sub_code,
                severity=module_severity if module_severity != "CRITICAL" else "HIGH",
                severityOverrideReason=(
                    f"Converted from field report {sub.number} — the "
                    "severity assessed at triage is retained over the matrix suggestion."
                ),
                severityOverrideSource=OVERRIDE_SOURCE_CAPTURE_CONVERSION,
                description=description,
                date=when,
            )
        except ValidationError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Cannot map field report to an observation: {e.errors()[0].get('msg', 'invalid data')}") from e
        created = await create_observation(payload, user, db)
        entity_type, entity_id, entity_ref = "Observation", created.id, created.number
        # carry the field media across (same storage objects, new metadata rows)
        for a in await _attachments(db, sub.id):
            if a.kind in ("PHOTO", "VIDEO"):
                db.add(ObservationAttachment(
                    observationId=entity_id, category="INITIAL_PHOTO", fileName=a.fileName,
                    storagePath=a.storagePath, fileSize=a.fileSize, mimeType=a.mimeType,
                    caption=a.caption, uploadedById=a.uploadedById or user.id,
                ))

    elif body.target == "near_miss":
        from app.routers.near_miss import create_near_miss
        from app.schemas.near_miss import NearMissCreate

        try:
            payload = NearMissCreate(
                plantId=sub.plantId,
                date=when,
                description=description,
                potentialSeverity=module_severity,
                areaId=sub.areaId,
                # NOT sub.fireAssetId — `equipmentId` here means an `Equipment`
                # row, and a FireEquipment id in it would dangle. The fire asset
                # travels in `location` and the description; the structured link
                # stays on the CaptureSubmission, which conversion records both
                # ends of.
                equipmentId=sub.equipmentId,
                location=(sub.fireAssetSnapshot or {}).get("location") or None,
                isAnonymous=sub.isAnonymous,
                reporterType="ANONYMOUS" if sub.isAnonymous else "EMPLOYEE",
                riskLikelihood=sub.hiraLikelihood,
                riskConsequence=sub.hiraSeverity,
            )
        except ValidationError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Cannot map field report to a near miss: {e.errors()[0].get('msg', 'invalid data')}") from e
        created = await create_near_miss(payload, user, db)
        entity_type, entity_id, entity_ref = "NearMiss", created.id, created.number

    elif body.target == "incident":
        from app.routers.incidents import create_incident
        from app.schemas.incident import IncidentCreate

        if not body.incidentType:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "incidentType is required to convert to an incident")
        area_name = None
        if sub.areaId:
            area = await db.get(Area, sub.areaId)
            area_name = area.name if area else None
        try:
            payload = IncidentCreate(
                type=body.incidentType,
                plantId=sub.plantId,
                location=area_name or "Reported from field",
                areaId=sub.areaId,
                date=when,
                occurredAt=when,
                description=description,
            )
        except ValidationError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Cannot map field report to an incident: {e.errors()[0].get('msg', 'invalid data')}") from e
        created = await create_incident(payload, user, db)
        entity_type, entity_id, entity_ref = "Incident", created.id, created.number

    elif body.target == "ptw":
        # The authorisation chain (permit type, validity window, issuer,
        # receiver) is the officer's to supply at triage — a field technician
        # cannot (spec §8.2). scopeOfWork carries the captured narrative; the
        # existing create_permit handler then runs its normal approval workflow.
        from app.routers.ptw import create_permit
        from app.schemas.permit import PermitCreate

        if not body.permitType:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "permitType is required to convert to a permit")
        if not (body.validFrom and body.validTo and body.issuerId and body.receiverId):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "validFrom, validTo, issuerId and receiverId are required to convert to a permit",
            )
        area_name = None
        if sub.areaId:
            area = await db.get(Area, sub.areaId)
            area_name = area.name if area else None
        try:
            payload = PermitCreate(
                type=body.permitType,
                plantId=sub.plantId,
                location=area_name or "Reported from field",
                scopeOfWork=description,
                validFrom=body.validFrom,
                validTo=body.validTo,
                issuerId=body.issuerId,
                receiverId=body.receiverId,
                areaId=sub.areaId,
            )
        except ValidationError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Cannot map field report to a permit: {e.errors()[0].get('msg', 'invalid data')}") from e
        created = await create_permit(payload, user, db)
        entity_type, entity_id, entity_ref = "Permit", created.id, created.number

    elif body.target == "flra":
        # Crew + toolbox-talk are the officer's to supply; jobDescription
        # carries the captured narrative. The existing create_flra handler runs
        # its crew-signoff workflow from there.
        from app.routers.flra import create_flra
        from app.schemas.flra import FLRACreate

        if not body.teamMemberIds:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "teamMemberIds is required to convert to an FLRA")
        if not body.toolboxTalkById:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "toolboxTalkById is required to convert to an FLRA")
        area_name = None
        if sub.areaId:
            area = await db.get(Area, sub.areaId)
            area_name = area.name if area else None
        try:
            payload = FLRACreate(
                plantId=sub.plantId,
                date=when,
                location=area_name or "Reported from field",
                jobDescription=description,
                teamMemberIds=body.teamMemberIds,
                toolboxTalkById=body.toolboxTalkById,
            )
        except ValidationError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Cannot map field report to an FLRA: {e.errors()[0].get('msg', 'invalid data')}") from e
        created = await create_flra(payload, user, db)
        entity_type, entity_id, entity_ref = "FLRA", created.id, created.number

    else:  # pragma: no cover — pydantic Literal guards this
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown conversion target")

    # Attribute the converted record to the ORIGINAL field reporter (the
    # technician who raised the report) — NOT the officer who reviewed and
    # converted it. The create handlers stamp the API caller (the officer) as
    # observer/reporter; override it here so the record shows e.g. "Rajesh
    # Sharma", not the approving manager. Skipped for anonymous reports
    # (there is no identity to attribute) and for PTW/FLRA (the officer is the
    # legitimate originator/leader there — the worker cannot authorise a permit).
    if sub.reporterId and not sub.isAnonymous:
        if entity_type == "Observation":
            from app.models.observation import Observation
            _rec = await db.get(Observation, entity_id)
            if _rec is not None:
                _rec.observerId = sub.reporterId
        elif entity_type == "NearMiss":
            from app.models.near_miss import NearMiss
            _rec = await db.get(NearMiss, entity_id)
            if _rec is not None:
                _rec.reporterId = sub.reporterId
        elif entity_type == "Incident":
            from app.models.incident import Incident
            _rec = await db.get(Incident, entity_id)
            if _rec is not None:
                _rec.reporterId = sub.reporterId

    sub.convertedEntityType = entity_type
    sub.convertedEntityId = entity_id
    sub.convertedById = user.id
    sub.convertedAt = _now()
    sub.status = "converted"
    await db.commit()
    return {"ok": True, "entityType": entity_type, "entityId": entity_id, "entityRef": entity_ref}


@router.post("/submissions/{submission_id}/reject")
async def reject_submission(
    submission_id: str,
    body: RejectBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    sub = await _load(db, submission_id)
    await _require(db, user, _TRIAGE, plant_id=sub.plantId)
    if sub.status == "converted":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Already converted — cannot reject")
    sub.status = "rejected"
    sub.triageNote = body.reason
    sub.triagedById = user.id
    sub.triagedAt = _now()
    await db.commit()
    return {"ok": True}


# ── Anonymity unmask (audited) ────────────────────────────────────────────────
@router.post("/submissions/{submission_id}/unmask")
async def unmask_reporter(
    submission_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    sub = await _load(db, submission_id)
    await _require(db, user, _UNMASK, plant_id=sub.plantId)
    if not sub.isAnonymous:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Report is not anonymous")

    # anonHash → user (linear scan over plant users is fine at this scale;
    # the hash is deployment-keyed HMAC so there is no reverse index)
    reporter = None
    plant_users = (await db.execute(select(User).where(User.plantId == sub.plantId))).scalars().all()
    for u in plant_users:
        if svc.anon_hash(u.id) == sub.anonHash:
            reporter = u
            break

    from app.services.audit_log import record_event
    await record_event(
        db,
        entity_type="CaptureSubmission",
        entity_id=sub.id,
        entity_code=sub.number,
        plant_id=sub.plantId,
        action="READ_SENSITIVE",
        after={"unmaskedReporterId": reporter.id if reporter else None},
        reason="Anonymous reporter unmasked",
    )
    await db.commit()
    if reporter is None:
        return {"found": False, "reporter": None}
    return {"found": True, "reporter": {"id": reporter.id, "name": reporter.name, "designation": reporter.designation}}


# ── Attachments (two-phase signed-URL upload, house pattern) ─────────────────
@router.get("/submissions/{submission_id}/attachments")
async def list_attachments(
    submission_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    sub = await _load(db, submission_id)
    if not await _can_view(db, user, sub):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    return {"items": [svc.attachment_out(a) for a in await _attachments(db, sub.id)]}


@router.post("/submissions/{submission_id}/attachments")
async def upload_attachment(
    submission_id: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    sub = await _load(db, submission_id)
    # reporter (incl. anonymous owner) or triage-permitted officer may attach
    if not svc.is_owner(sub, user.id):
        await _require(db, user, _TRIAGE, plant_id=sub.plantId)
    if not is_storage_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Supabase Storage isn't configured. Set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.",
        )

    phase = payload.get("phase")
    if phase == "init":
        kind = str(payload.get("kind") or "PHOTO").upper()
        file_name = str(payload.get("fileName") or "").strip()
        file_size = int(payload.get("fileSize") or 0)
        mime_type = str(payload.get("mimeType") or "")
        client_media_id = payload.get("clientMediaId")
        if kind not in VALID_KINDS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid kind. Must be one of: {', '.join(sorted(VALID_KINDS))}")
        if not file_name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "File name is required")
        if file_size <= 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "File size must be a positive number")
        if file_size > MAX_CAPTURE_FILE_SIZE:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"File size exceeds the {MAX_CAPTURE_FILE_SIZE // 1024 // 1024} MB limit.")
        if mime_type not in ALLOWED_CAPTURE_MIME:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"MIME type {mime_type} is not allowed.")

        # idempotent re-init: same clientMediaId returns the existing row
        if client_media_id:
            dup = (
                await db.execute(
                    select(CaptureAttachment)
                    .where(CaptureAttachment.submissionId == sub.id)
                    .where(CaptureAttachment.clientMediaId == client_media_id)
                    .where(CaptureAttachment.deletedAt.is_(None))
                )
            ).scalar_one_or_none()
            if dup is not None:
                signed = create_signed_upload_url(dup.storagePath)
                return {"phase": "init", "attachmentId": dup.id, "storagePath": dup.storagePath,
                        "uploadUrl": signed["uploadUrl"], "token": signed["token"], "replayed": True}

        from app.services.storage import build_storage_path
        storage_path = build_storage_path(incident_id=sub.id, category=kind, file_name=file_name)
        if storage_path.startswith("incidents/"):
            storage_path = "capture/" + storage_path[len("incidents/"):]
        try:
            signed = create_signed_upload_url(storage_path)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Storage upload init failed: {e}") from e

        att = CaptureAttachment(
            submissionId=sub.id,
            kind=kind,
            fileName=file_name,
            storagePath=storage_path,
            fileSize=file_size,
            mimeType=mime_type,
            durationSec=payload.get("durationSec"),
            sha256=payload.get("sha256"),
            clientMediaId=client_media_id,
            uploadedById=user.id if not sub.isAnonymous else None,
        )
        db.add(att)
        await db.flush()
        await db.commit()
        return {"phase": "init", "attachmentId": att.id, "storagePath": storage_path,
                "uploadUrl": signed["uploadUrl"], "token": signed["token"]}

    if phase == "complete":
        attachment_id = payload.get("attachmentId")
        if not attachment_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "attachmentId required")
        att = await db.get(CaptureAttachment, attachment_id)
        if att is None or att.submissionId != sub.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found for this report")
        att.caption = payload.get("caption")
        if payload.get("durationSec") is not None:
            att.durationSec = payload.get("durationSec")
        # a completed VOICE note flips the submission's transcription pipeline on
        if att.kind == "VOICE" and sub.transcriptionStatus in ("none", "device"):
            if _features()["voiceTranscription"] and not sub.transcriptOriginal:
                sub.transcriptionStatus = "pending"
        await db.commit()
        return {"ok": True}

    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown phase")


@router.get("/submissions/{submission_id}/attachments/{attachment_id}/download")
async def download_attachment(
    submission_id: str,
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    sub = await _load(db, submission_id)
    if not await _can_view(db, user, sub):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    att = await db.get(CaptureAttachment, attachment_id)
    if att is None or att.submissionId != sub.id or att.deletedAt is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    url = create_signed_download_url(att.storagePath, expires_in_sec=300)
    return {"url": url, "fileName": att.fileName, "mimeType": att.mimeType}


# ── Resumable chunked upload (offline sync path — spec 1.4) ──────────────────
# Chunks arrive as <=~2.7MB base64 JSON (the Vercel proxy text-decodes request
# bodies, so raw binary can't transit it), staged in Postgres, assembled once
# complete, pushed to Supabase Storage, then attached to the submission.
# Every step is idempotent: re-init resumes (returns received indexes),
# re-sent chunks upsert, re-complete replays the attachment.

import base64
import hashlib

from app.models.capture import UploadChunk, UploadSession

MAX_CHUNK_BYTES = int(2.5 * 1024 * 1024)
MAX_TOTAL_CHUNKS = 64


@router.post("/uploads/init")
async def chunked_upload_init(
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, _CREATE)
    client_media_id = str(payload.get("clientMediaId") or "").strip()
    file_name = str(payload.get("fileName") or "").strip()
    mime_type = str(payload.get("mimeType") or "")
    kind = str(payload.get("kind") or "PHOTO").upper()
    total_size = int(payload.get("totalSize") or 0)
    chunk_size = int(payload.get("chunkSize") or 0)
    total_chunks = int(payload.get("totalChunks") or 0)
    sha256 = payload.get("sha256") or None

    if not client_media_id or not file_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "clientMediaId and fileName are required")
    if kind not in VALID_KINDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid kind")
    if mime_type not in ALLOWED_CAPTURE_MIME:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"MIME type {mime_type} is not allowed.")
    if total_size <= 0 or total_size > MAX_CAPTURE_FILE_SIZE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid total size")
    if chunk_size <= 0 or chunk_size > MAX_CHUNK_BYTES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid chunk size")
    if total_chunks <= 0 or total_chunks > MAX_TOTAL_CHUNKS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid chunk count")

    # content-hash dedup: the exact bytes are already in storage
    if sha256:
        stored = (
            await db.execute(
                select(UploadSession)
                .where(UploadSession.sha256 == sha256)
                .where(UploadSession.status == "COMPLETE")
                .where(UploadSession.storagePath.is_not(None))
                .limit(1)
            )
        ).scalar_one_or_none()
        if stored is not None:
            return {"sessionId": stored.id, "receivedIndexes": [], "alreadyStored": True}

    # resume an in-flight session for the same client media
    existing = (
        await db.execute(
            select(UploadSession)
            .where(UploadSession.ownerId == user.id)
            .where(UploadSession.clientMediaId == client_media_id)
            .where(UploadSession.status == "PENDING")
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        indexes = (
            await db.execute(select(UploadChunk.chunkIndex).where(UploadChunk.sessionId == existing.id))
        ).scalars().all()
        return {"sessionId": existing.id, "receivedIndexes": sorted(indexes), "alreadyStored": False}

    session = UploadSession(
        ownerId=user.id,
        clientMediaId=client_media_id,
        fileName=file_name,
        mimeType=mime_type,
        kind=kind,
        totalSize=total_size,
        chunkSize=chunk_size,
        totalChunks=total_chunks,
        sha256=sha256,
    )
    db.add(session)
    await db.commit()
    return {"sessionId": session.id, "receivedIndexes": [], "alreadyStored": False}


@router.post("/uploads/{session_id}/chunk")
async def chunked_upload_chunk(
    session_id: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    session = await db.get(UploadSession, session_id)
    if session is None or session.ownerId != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload session not found")
    if session.status != "PENDING":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Session is {session.status}")

    index = int(payload.get("index", -1))
    if index < 0 or index >= session.totalChunks:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Chunk index out of range")
    try:
        data = base64.b64decode(str(payload.get("dataB64") or ""), validate=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid base64 chunk") from e
    if len(data) == 0 or len(data) > MAX_CHUNK_BYTES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid chunk size")

    existing = (
        await db.execute(
            select(UploadChunk)
            .where(UploadChunk.sessionId == session.id)
            .where(UploadChunk.chunkIndex == index)
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(UploadChunk(sessionId=session.id, chunkIndex=index, data=data))
    else:
        existing.data = data  # retry overwrote — last write wins
    await db.commit()
    received = (
        await db.execute(select(func.count()).select_from(UploadChunk).where(UploadChunk.sessionId == session.id))
    ).scalar_one()
    return {"received": received, "totalChunks": session.totalChunks}


@router.post("/uploads/{session_id}/complete")
async def chunked_upload_complete(
    session_id: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    session = await db.get(UploadSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload session not found")
    # dedup hits reference sessions owned by whoever uploaded the bytes first;
    # everyone else may only ATTACH the stored object, never mutate the session.
    is_session_owner = session.ownerId == user.id

    submission_id = str(payload.get("submissionId") or "")
    if not submission_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "submissionId required")
    sub = await _load(db, submission_id)
    if not svc.is_owner(sub, user.id):
        await _require(db, user, _TRIAGE, plant_id=sub.plantId)

    # replay: this media is already attached to this submission
    dup = (
        await db.execute(
            select(CaptureAttachment)
            .where(CaptureAttachment.submissionId == sub.id)
            .where(CaptureAttachment.clientMediaId == session.clientMediaId)
            .where(CaptureAttachment.deletedAt.is_(None))
        )
    ).scalar_one_or_none()
    if dup is not None:
        return {"ok": True, "attachmentId": dup.id, "replayed": True}

    if session.status != "COMPLETE":
        if not is_session_owner:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your upload session")
        chunks = (
            await db.execute(
                select(UploadChunk).where(UploadChunk.sessionId == session.id).order_by(UploadChunk.chunkIndex.asc())
            )
        ).scalars().all()
        if len(chunks) != session.totalChunks:
            missing = sorted(set(range(session.totalChunks)) - {c.chunkIndex for c in chunks})
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Missing chunks: {missing[:10]}")
        data = b"".join(c.data for c in chunks)
        if len(data) != session.totalSize:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Assembled size mismatch — re-upload")
        if session.sha256 and hashlib.sha256(data).hexdigest() != session.sha256:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Content hash mismatch — re-upload")
        if not is_storage_configured():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Supabase Storage isn't configured.")

        from app.services.storage import build_storage_path, upload_object
        storage_path = build_storage_path(incident_id=sub.id, category=session.kind, file_name=session.fileName)
        if storage_path.startswith("incidents/"):
            storage_path = "capture/" + storage_path[len("incidents/"):]
        try:
            upload_object(storage_path, data, session.mimeType)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Storage upload failed: {e}") from e

        session.status = "COMPLETE"
        session.storagePath = storage_path
        for c in chunks:
            await db.delete(c)

    att = CaptureAttachment(
        submissionId=sub.id,
        kind=session.kind,
        fileName=session.fileName,
        storagePath=session.storagePath or "",
        fileSize=session.totalSize,
        mimeType=session.mimeType,
        durationSec=payload.get("durationSec"),
        sha256=session.sha256,
        clientMediaId=session.clientMediaId,
        uploadedById=user.id if not sub.isAnonymous else None,
    )
    db.add(att)
    if att.kind == "VOICE" and sub.transcriptionStatus in ("none", "device"):
        if _features()["voiceTranscription"] and not sub.transcriptOriginal:
            sub.transcriptionStatus = "pending"
    await db.commit()
    await db.refresh(att)
    return {"ok": True, "attachmentId": att.id, "replayed": False}
