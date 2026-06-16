"""PTW active-phase operations.

Endpoints used after a permit reaches ACTIVE state:

  POST  /api/ptw/{id}/gas-test/reading        — record reading, may auto-suspend
  GET   /api/ptw/{id}/gas-test/status         — refresh state for countdown UI
  GET   /api/ptw/{id}/gas-test/readings       — paginated reading log

  POST  /api/ptw/{id}/active/suspend          — full audit-row suspension
  POST  /api/ptw/{id}/active/resume           — resume + close suspension row

  POST  /api/ptw/{id}/active/extension        — request validity extension
  POST  /api/ptw/{id}/active/extension/{ext_id}/decide  — approve/reject extension

  POST  /api/ptw/{id}/active/crew             — add crew member (sets re-FLRA flag)
  DELETE /api/ptw/{id}/active/crew/{crew_id}  — remove crew member (sets re-FLRA flag)

The legacy /suspend and /resume endpoints (single-row variants) stay live
for older clients but write to the new audit table when this router is
used. New clients should call /active/* exclusively.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.flra import FLRA, FLRAStatus
from app.models.permit import (
    Permit,
    PermitCrewMember,
    PermitExtension,
    PermitGasTestReading,
    PermitStatus,
    PermitSuspension,
)
from app.models.user import User
from app.services.gas_test import get_refresh_status, record_gas_reading
from app.services.permissions import PermissionContext, can, get_user_role_codes

router = APIRouter(prefix="/api/ptw", tags=["ptw-active"])


# ─── Schemas ──────────────────────────────────────────────────────────


class GasReadingValue(BaseModel):
    model_config = ConfigDict(extra="ignore")
    parameter: str
    value: float


class GasReadingPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    readings: list[GasReadingValue] = Field(min_length=1)
    instrumentSerial: str | None = None
    isPreEntry: bool = False


SuspensionReason = Literal[
    "GAS_TEST_EXCEEDANCE",
    "WEATHER",
    "EQUIPMENT_FAILURE",
    "ADJACENT_OPERATION",
    "INCIDENT_NEARBY",
    "CREW_FATIGUE",
    "OTHER",
]


class SuspendPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: SuspensionReason
    reasonDetail: str | None = None
    reFlraRequired: bool = True


class ResumePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    resumptionConditions: str | None = None


class ExtensionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    newValidTo: datetime
    reason: str = Field(min_length=5)


class ExtensionDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")
    decision: Literal["APPROVED", "REJECTED"]
    approverComments: str | None = None


class CrewAddPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    userId: str
    role: Literal["WORKER", "OPERATOR", "HELPER", "SUPERVISOR", "TECHNICIAN", "CONTRACTOR"] = "WORKER"


class CrewRemovePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(min_length=1)


class ReturnPayload(BaseModel):
    """Receiver returns the permit at end of work."""

    model_config = ConfigDict(extra="ignore")
    isolationsRestored: bool
    workAreaClean: bool
    notes: str | None = None
    photos: list[str] | None = None  # PermitAttachment ids


class SiteVerifyPayload(BaseModel):
    """Issuer / Safety Officer post-walk verification."""

    model_config = ConfigDict(extra="ignore")
    checklist: dict[str, bool]
    photos: list[str] | None = None
    notes: str | None = None


# ─── Helpers ──────────────────────────────────────────────────────────


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


# ─── Gas-test endpoints ────────────────────────────────────────────────


@router.post("/{permit_id}/gas-test/reading")
async def post_gas_reading(
    permit_id: str,
    payload: GasReadingPayload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")
    if permit.status not in {PermitStatus.ACTIVE, PermitStatus.SUSPENDED}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot record gas readings on a {permit.status.value} permit.",
        )
    result = await record_gas_reading(
        db,
        permit_id=permit_id,
        user_id=user.id,
        readings=[r.model_dump() for r in payload.readings],
        instrument_serial=payload.instrumentSerial,
        is_pre_entry=payload.isPreEntry,
    )
    return {
        "id": result.reading_id,
        "isExceedance": result.is_exceedance,
        "failedParameters": result.failed_parameters,
        "refreshDueBy": result.refresh_due_by.isoformat(),
        "autoSuspended": result.auto_suspended,
    }


@router.get("/{permit_id}/gas-test/status")
async def gas_test_status(
    permit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _load_permit_or_403(db, permit_id, user, "PTW.READ")
    return await get_refresh_status(db, permit_id)


@router.get("/{permit_id}/gas-test/readings")
async def list_gas_readings(
    permit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _load_permit_or_403(db, permit_id, user, "PTW.READ")
    rows = (
        await db.execute(
            select(PermitGasTestReading)
            .where(PermitGasTestReading.permitId == permit_id)
            .order_by(PermitGasTestReading.recordedAt.desc())
            .limit(50)
        )
    ).scalars().all()
    return {
        "items": [
            {
                "id": r.id,
                "recordedAt": r.recordedAt.isoformat(),
                "recordedById": r.recordedById,
                "readings": r.readings or [],
                "isExceedance": r.isExceedance,
                "isPreEntry": r.isPreEntry,
                "instrumentSerial": r.instrumentSerial,
                "refreshDueBy": r.refreshDueBy.isoformat() if r.refreshDueBy else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


# ─── Suspend / Resume endpoints (audit-row variants) ───────────────────


@router.post("/{permit_id}/active/suspend")
async def suspend_active(
    permit_id: str,
    payload: SuspendPayload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")
    if permit.status != PermitStatus.ACTIVE:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Only ACTIVE permits can be suspended (current status: {permit.status.value}).",
        )

    now = datetime.now(timezone.utc)
    susp = PermitSuspension(
        permitId=permit_id,
        suspendedById=user.id,
        reason=payload.reason,
        reasonDetail=payload.reasonDetail,
        reFlraRequired=payload.reFlraRequired,
    )
    db.add(susp)
    permit.status = PermitStatus.SUSPENDED
    permit.suspendedAt = now
    permit.suspendedReason = payload.reasonDetail or payload.reason
    permit.isCurrentlySuspended = True
    await db.flush()
    return {"ok": True, "suspensionId": susp.id, "reFlraRequired": payload.reFlraRequired}


@router.post("/{permit_id}/active/resume")
async def resume_active(
    permit_id: str,
    payload: ResumePayload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")
    if permit.status != PermitStatus.SUSPENDED:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Only SUSPENDED permits can be resumed (current status: {permit.status.value}).",
        )

    open_susp = (
        await db.execute(
            select(PermitSuspension)
            .where(PermitSuspension.permitId == permit_id)
            .where(PermitSuspension.resumedAt.is_(None))
            .order_by(PermitSuspension.suspendedAt.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    re_flra_required = bool(open_susp.reFlraRequired) if open_susp else True

    # If re-FLRA is required, the active FLRA must be COMPLETED & non-superseded.
    # Caller's UI should normally trigger an FLRA re-do before calling resume.
    if re_flra_required:
        live_flra = (
            await db.execute(
                select(FLRA)
                .where(FLRA.permitId == permit_id)
                .where(FLRA.status.in_([FLRAStatus.COMPLETED, FLRAStatus.IN_PROGRESS]))
                .order_by(FLRA.createdAt.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if live_flra is None or live_flra.status != FLRAStatus.COMPLETED:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Re-FLRA required before resuming. Complete a fresh FLRA first.",
            )

    now = datetime.now(timezone.utc)
    if open_susp is not None:
        open_susp.resumedAt = now
        open_susp.resumedById = user.id
        open_susp.resumptionConditions = payload.resumptionConditions

    permit.status = PermitStatus.ACTIVE
    permit.suspendedAt = None
    permit.suspendedReason = None
    permit.isCurrentlySuspended = False
    await db.flush()
    return {"ok": True, "reFlraEnforced": re_flra_required}


# ─── Extension endpoints ───────────────────────────────────────────────


@router.post("/{permit_id}/active/extension")
async def request_extension(
    permit_id: str,
    payload: ExtensionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")
    if permit.status not in {PermitStatus.ACTIVE, PermitStatus.SUSPENDED}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot extend a {permit.status.value} permit.",
        )
    new_to = payload.newValidTo
    if new_to.tzinfo is None:
        new_to = new_to.replace(tzinfo=timezone.utc)
    valid_to = permit.validTo
    if valid_to.tzinfo is None:
        valid_to = valid_to.replace(tzinfo=timezone.utc)
    if new_to <= valid_to:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "newValidTo must be later than the current validTo.",
        )
    # Cap at 8h beyond original cap per type (caller-side cap)
    ext = PermitExtension(
        permitId=permit_id,
        requestedById=user.id,
        newValidTo=new_to,
        reason=payload.reason,
        status="PENDING",
    )
    db.add(ext)
    await db.flush()
    return {"ok": True, "extensionId": ext.id, "status": "PENDING"}


@router.post("/{permit_id}/active/extension/{ext_id}/decide")
async def decide_extension(
    permit_id: str,
    ext_id: str,
    payload: ExtensionDecision,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")
    role_codes = await get_user_role_codes(db, user.id)
    if not any(
        r in {"PERMIT_ISSUER", "SAFETY_OFFICER", "HSE_MANAGER", "ADMIN", "SYSTEM_ADMIN", "PLANT_HEAD"}
        for r in role_codes
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only Issuer / Safety Officer / Plant Head / HSE / Admin can decide extensions.",
        )
    ext = await db.get(PermitExtension, ext_id)
    if ext is None or ext.permitId != permit_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Extension not found")
    if ext.status != "PENDING":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Extension is already {ext.status}."
        )

    now = datetime.now(timezone.utc)
    ext.approvedAt = now
    ext.approvedById = user.id
    ext.approverComments = payload.approverComments
    ext.status = payload.decision

    if payload.decision == "APPROVED":
        permit.validTo = ext.newValidTo
    await db.flush()
    return {"ok": True, "status": ext.status}


# ─── Crew change endpoints ─────────────────────────────────────────────


@router.post("/{permit_id}/active/crew")
async def add_crew(
    permit_id: str,
    payload: CrewAddPayload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")
    if permit.status not in {PermitStatus.ACTIVE, PermitStatus.SUSPENDED}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot edit crew on a {permit.status.value} permit.",
        )

    # PPE snapshot for the joining member (PPE-01 Pass 2). Non-blocking —
    # the re-FLRA + activation gate enforce PPE before work resumes.
    from app.services.ppe_gate import check_ppe_for_user

    ppe_res = await check_ppe_for_user(
        db,
        user_id=payload.userId,
        plant_id=permit.plantId,
        permit_type_code=(
            permit.type.value if hasattr(permit.type, "value") else str(permit.type)
        ),
    )

    # Reactivate a previously-removed row if present, else insert.
    existing = (
        await db.execute(
            select(PermitCrewMember)
            .where(PermitCrewMember.permitId == permit_id)
            .where(PermitCrewMember.userId == payload.userId)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.removedAt is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "User is already on this crew."
            )
        existing.removedAt = None
        existing.removalReason = None
        existing.role = payload.role
        existing.ppeValidAtIssuance = ppe_res.ok
        existing.ppeValidationNotes = ppe_res.summary() if not ppe_res.ok else None
    else:
        db.add(
            PermitCrewMember(
                permitId=permit_id,
                userId=payload.userId,
                role=payload.role,
                ppeValidAtIssuance=ppe_res.ok,
                ppeValidationNotes=ppe_res.summary() if not ppe_res.ok else None,
            )
        )

    # Mid-permit crew change ⇒ permit must re-FLRA before continuing work.
    # Pattern matches gas-test exceedance: open a suspension row + flip status.
    if permit.status == PermitStatus.ACTIVE:
        permit.status = PermitStatus.SUSPENDED
        permit.suspendedAt = datetime.now(timezone.utc)
        permit.suspendedReason = "Crew change — re-FLRA required"
        permit.isCurrentlySuspended = True
        db.add(
            PermitSuspension(
                permitId=permit_id,
                suspendedById=user.id,
                reason="OTHER",
                reasonDetail="Crew added mid-permit — re-FLRA required.",
                reFlraRequired=True,
            )
        )

    await db.flush()
    return {"ok": True, "reFlraRequired": True}


# ─── Return + Site Verification ────────────────────────────────────────


@router.post("/{permit_id}/active/return")
async def return_permit(
    permit_id: str,
    payload: ReturnPayload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Receiver hands the permit back at end of work. Records that
    isolations were restored and the work area is clean. Permit stays
    ACTIVE until site-verify + closure complete; the workflow's CLOSURE
    step now becomes executable."""
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")
    if permit.receiverId is not None and permit.receiverId != user.id:
        # Only the named receiver can return. HSE/Admin can override.
        role_codes = await get_user_role_codes(db, user.id)
        if not any(r in {"HSE_MANAGER", "ADMIN", "SYSTEM_ADMIN"} for r in role_codes):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Only the named receiver can return the permit.",
            )
    if permit.status not in {PermitStatus.ACTIVE, PermitStatus.SUSPENDED}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot return a {permit.status.value} permit.",
        )
    if permit.returnedAt is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Permit has already been returned.",
        )
    if not payload.isolationsRestored:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Confirm isolations are restored before returning.",
        )
    if not payload.workAreaClean:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Confirm work area is clean before returning.",
        )

    # Mark every isolation as restored (audit row) — caller may have already
    # done this individually but this catches the closure-ready state.
    from app.models.permit import PermitIsolation

    open_isolations = (
        await db.execute(
            select(PermitIsolation)
            .where(PermitIsolation.permitId == permit_id)
            .where(PermitIsolation.restoredAt.is_(None))
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    for iso in open_isolations:
        iso.restoredAt = now
        iso.restoredById = user.id

    permit.returnedAt = now
    permit.returnedById = user.id
    permit.returnNotes = payload.notes
    permit.returnPhotos = payload.photos
    await db.flush()
    return {
        "ok": True,
        "returnedAt": now.isoformat(),
        "isolationsAutoRestored": len(open_isolations),
    }


@router.post("/{permit_id}/active/site-verify")
async def site_verify(
    permit_id: str,
    payload: SiteVerifyPayload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Issuer / Safety Officer / Plant Head walks the area after return
    and records a checklist + photos. Closure cannot proceed without
    this step."""
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")
    role_codes = await get_user_role_codes(db, user.id)
    if not any(
        r in {"PERMIT_ISSUER", "SAFETY_OFFICER", "PLANT_HEAD", "HSE_MANAGER", "ADMIN", "SYSTEM_ADMIN"}
        for r in role_codes
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only Issuer / Safety Officer / Plant Head / HSE / Admin can site-verify.",
        )
    if permit.returnedAt is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Receiver must return the permit before site verification.",
        )
    if permit.siteVerifiedAt is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Site has already been verified for this permit.",
        )

    # Reject if any required checklist item failed
    failed = [k for k, v in payload.checklist.items() if not v]
    if failed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            (
                f"Checklist items not satisfied: {', '.join(failed)}. "
                "Re-walk the site or escalate to HSE before closing."
            ),
        )

    permit.siteVerifiedAt = datetime.now(timezone.utc)
    permit.siteVerifiedById = user.id
    permit.siteVerificationChecklist = payload.checklist
    permit.siteVerificationPhotos = payload.photos
    await db.flush()
    return {"ok": True, "siteVerifiedAt": permit.siteVerifiedAt.isoformat()}


@router.delete("/{permit_id}/active/crew/{crew_id}")
async def remove_crew(
    permit_id: str,
    crew_id: str,
    payload: CrewRemovePayload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")
    if permit.status not in {PermitStatus.ACTIVE, PermitStatus.SUSPENDED}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot edit crew on a {permit.status.value} permit.",
        )
    crew = await db.get(PermitCrewMember, crew_id)
    if crew is None or crew.permitId != permit_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Crew member not found")
    if crew.removedAt is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Already removed")

    crew.removedAt = datetime.now(timezone.utc)
    crew.removalReason = payload.reason

    # Same suspension logic as add — any roster change forces re-FLRA.
    if permit.status == PermitStatus.ACTIVE:
        permit.status = PermitStatus.SUSPENDED
        permit.suspendedAt = datetime.now(timezone.utc)
        permit.suspendedReason = "Crew removed — re-FLRA required"
        permit.isCurrentlySuspended = True
        db.add(
            PermitSuspension(
                permitId=permit_id,
                suspendedById=user.id,
                reason="OTHER",
                reasonDetail=f"Crew removed mid-permit ({payload.reason}) — re-FLRA required.",
                reFlraRequired=True,
            )
        )

    await db.flush()
    return {"ok": True, "reFlraRequired": True}
