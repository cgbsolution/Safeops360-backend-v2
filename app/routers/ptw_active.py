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
    PermitEvidenceAction,
    PermitExtension,
    PermitGasTestReading,
    PermitStatus,
    PermitSuspension,
)
from app.models.user import User
from app.schemas.permit import PtwEvidenceInput
from app.services.gas_test import get_refresh_status, record_gas_reading
from app.services.permissions import PermissionContext, can, get_user_role_codes
from app.services.ptw_evidence import EvidenceError, record_action_evidence

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
    # Closed-loop rebuild: suspension is a lifecycle action → field evidence
    # (GPS + signature per EVIDENCE_POLICY) is required.
    evidence: PtwEvidenceInput | None = None


class ResumePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    resumptionConditions: str | None = None
    evidence: PtwEvidenceInput | None = None


class ExtensionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    newValidTo: datetime
    reason: str = Field(min_length=5)
    evidence: PtwEvidenceInput | None = None


class ExtensionDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")
    decision: Literal["APPROVED", "REJECTED"]
    approverComments: str | None = None
    evidence: PtwEvidenceInput | None = None


class CrewAddPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    userId: str
    role: Literal["WORKER", "OPERATOR", "HELPER", "SUPERVISOR", "TECHNICIAN", "CONTRACTOR"] = "WORKER"


class CrewRemovePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(min_length=1)


# ─── Helpers ──────────────────────────────────────────────────────────


async def _evidence_or_422(
    db: AsyncSession,
    *,
    permit: Permit,
    action: PermitEvidenceAction,
    actor_id: str,
    ev: PtwEvidenceInput | None,
    comments: str | None = None,
) -> None:
    """Validate + persist the field-evidence row; HTTP 422 with the full
    missing-element list when the action's policy isn't met."""
    try:
        await record_action_evidence(
            db,
            permit=permit,
            action=action,
            actor_id=actor_id,
            gps_latitude=ev.gpsLatitude if ev else None,
            gps_longitude=ev.gpsLongitude if ev else None,
            gps_accuracy_meters=ev.gpsAccuracyMeters if ev else None,
            signature_image=ev.signatureImageBase64 if ev else None,
            declaration_text=ev.declarationText if ev else None,
            comments=comments,
            photo_attachment_ids=ev.photoAttachmentIds if ev else None,
        )
    except EvidenceError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e


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

    await _evidence_or_422(
        db,
        permit=permit,
        action=PermitEvidenceAction.SUSPEND,
        actor_id=user.id,
        ev=payload.evidence,
        comments=payload.reasonDetail or payload.reason,
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
    # Daily Brief outbox: ptw.suspended → overlapping-permit impact (CRITICAL)
    from app.services import events as domain_events
    domain_events.emit(
        db,
        event_type=domain_events.PTW_SUSPENDED,
        entity_type="Permit",
        entity_id=permit.id,
        entity_ref=permit.number,
        site_id=permit.plantId,
        actor_id=user.id,
        payload={"from": "ACTIVE", "to": "SUSPENDED", "reason": payload.reasonDetail or payload.reason},
    )
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

    await _evidence_or_422(
        db,
        permit=permit,
        action=PermitEvidenceAction.RESUME,
        actor_id=user.id,
        ev=payload.evidence,
        comments=payload.resumptionConditions,
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
    from app.services import events as domain_events
    domain_events.emit(
        db,
        event_type=domain_events.PTW_RESUMED,
        entity_type="Permit",
        entity_id=permit.id,
        entity_ref=permit.number,
        site_id=permit.plantId,
        actor_id=user.id,
        payload={"from": "SUSPENDED", "to": "ACTIVE", "reFlraEnforced": re_flra_required},
    )
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

    await _evidence_or_422(
        db,
        permit=permit,
        action=PermitEvidenceAction.EXTEND,
        actor_id=user.id,
        ev=payload.evidence,
        comments=f"Extension requested to {new_to.isoformat()}: {payload.reason}",
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

    await _evidence_or_422(
        db,
        permit=permit,
        action=PermitEvidenceAction.EXTEND,
        actor_id=user.id,
        ev=payload.evidence,
        comments=f"Extension {payload.decision.lower()}: {payload.approverComments or ''}".strip(),
    )

    now = datetime.now(timezone.utc)
    ext.approvedAt = now
    ext.approvedById = user.id
    ext.approverComments = payload.approverComments
    ext.status = payload.decision

    if payload.decision == "APPROVED":
        permit.validTo = ext.newValidTo
        # Daily Brief outbox: an approved extension is a permit modification
        from app.services import events as domain_events
        domain_events.emit(
            db,
            event_type=domain_events.PTW_MODIFIED,
            entity_type="Permit",
            entity_id=permit.id,
            entity_ref=permit.number,
            site_id=permit.plantId,
            actor_id=user.id,
            payload={"fields": ["validTo"], "reason": f"validity extended to {ext.newValidTo.isoformat()}"},
        )
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

    # Safety-roster gate — a worker under an open deroster review, or a
    # confirmed one, cannot join a permit crew. BLOCKING, unlike the PPE
    # snapshot below: PPE is re-checked at activation, but nothing downstream
    # re-checks a safety hold, so this is the enforcement point.
    from app.services import roster_gate

    _gate = await roster_gate.for_user(db, payload.userId)
    if not _gate.allowed:
        raise HTTPException(status.HTTP_409_CONFLICT, _gate.detail)

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


# ─── Return + Site Verification — SUPERSEDED (closed-loop rebuild) ──────
#
# The legacy `POST /{id}/active/return` and `POST /{id}/active/site-verify`
# endpoints are replaced by the evidence-carrying pair in ptw_lifecycle.py:
#
#   POST /api/ptw/{id}/complete   — Work Completed declaration: structured
#                                   outcome enum + restoration confirmations
#                                   + GPS/photo/signature evidence
#   POST /api/ptw/{id}/handback   — Handback inspection: checklist + evidence
#
# Both old routes returned dangling photo ids into a table that was never
# wired; the new flow uses real PermitAttachment uploads. The single
# first-party web client was migrated in the same change.


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
