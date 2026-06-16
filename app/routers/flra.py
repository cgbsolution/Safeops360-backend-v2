from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.flra import (
    FLRA,
    FLRAAttachment,
    FLRACrewSignature,
    FLRAFitnessDeclaration,
    FLRAJobStep,
    FLRAStatus,
    FLRAStepHazard,
    FLRATeamMember,
)
from app.models.permit import Permit, PermitStatus
from app.models.plant import Plant
from app.models.training import TrainingProgram, TrainingRecord
from app.models.user import User
from app.models.workflow import Action, WorkflowHistory, WorkflowInstance
from app.schemas.flra import (
    FLRACreate,
    FLRAOut,
    FLRARedoRequest,
    FLRASignRequest,
    JobStepInput,
)
from app.services.flra_gate import maybe_complete_flra, resolve_crew_for_flra
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
    get_user_role_codes,
)
from app.routers.ptw import REQUIRED_TRAINING_CODES

router = APIRouter(prefix="/api/flra", tags=["flra"])


# ─── 5×5 risk matrix helper ────────────────────────────────────────────


def risk_level_from_score(score: int) -> str:
    """Maps 5×5 risk-matrix score to band. Same thresholds as Node side."""
    if score >= 15:
        return "CRITICAL"
    if score >= 8:
        return "HIGH"
    if score >= 4:
        return "MEDIUM"
    return "LOW"


def _validate_step_hazards(steps: list[JobStepInput]) -> None:
    """Reject FLRAs whose residual risk would block work."""
    for step in steps:
        for hz in step.hazards:
            residual = hz.residualLikelihood * hz.residualSeverity
            level = risk_level_from_score(residual)
            if level in {"HIGH", "CRITICAL"}:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    (
                        f'Hazard "{hz.hazardDescription[:60]}" still has {level} '
                        f"residual risk after controls. Add stronger controls "
                        "or escalate to HSE before signing."
                    ),
                )


# ─── Endpoints ─────────────────────────────────────────────────────────


@router.get("")
async def list_flras(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    read_check = await can(db, user.id, "FLRA.READ", PermissionContext())
    if not read_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, read_check.reason or "Access denied")
    plants = await get_accessible_plants(db, user.id)
    stmt = select(FLRA)
    if plants is None:
        pass
    elif not plants:
        return {"items": [], "total": 0}
    else:
        stmt = stmt.where(FLRA.plantId.in_(plants))
    rows = (await db.execute(stmt.order_by(FLRA.date.desc()).limit(100))).scalars().all()
    return {"items": [FLRAOut.model_validate(r) for r in rows], "total": len(rows)}


@router.post("", response_model=FLRAOut, status_code=status.HTTP_201_CREATED)
async def create_flra(
    payload: FLRACreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FLRAOut:
    await require_permission_with_context("FLRA.CREATE", user, db, plant_id=payload.plantId)
    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")

    # Linked-permit validation
    if payload.permitId:
        permit = await db.get(Permit, payload.permitId)
        if permit is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Linked permit not found.")
        if permit.status in {
            PermitStatus.DRAFT,
            PermitStatus.REJECTED,
            PermitStatus.EXPIRED,
            PermitStatus.CLOSED,
        }:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Cannot start FLRA on a {permit.status.value} permit.",
            )
        active_q = select(func.count()).select_from(FLRA).where(
            FLRA.permitId == payload.permitId,
            FLRA.status.in_([FLRAStatus.IN_PROGRESS, FLRAStatus.COMPLETED]),
        )
        if (await db.execute(active_q)).scalar_one():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Permit {permit.number} already has an active FLRA. Use Re-do FLRA instead.",
            )

    # Hazards: prefer structured `jobSteps`. Legacy `hazards` JSON kept for
    # back-compat with the older single-page form clients.
    use_structured = bool(payload.jobSteps)
    legacy_filled: list[dict[str, Any]] = []
    if not use_structured:
        try:
            hz = json.loads(payload.hazards or "[]")
        except json.JSONDecodeError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Hazards data is malformed.") from e
        legacy_filled = [
            h
            for h in hz
            if isinstance(h, dict)
            and (str(h.get("step", "")).strip() or str(h.get("hazard", "")).strip())
        ]
        if not legacy_filled:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Capture at least one hazard before saving.",
            )
    else:
        _validate_step_hazards(payload.jobSteps)

    last = (
        await db.execute(
            select(func.count()).select_from(FLRA).where(FLRA.plantId == payload.plantId)
        )
    ).scalar_one()
    number = f"FLRA-{plant.code}-{last + 1:04d}"

    crew_ids = await resolve_crew_for_flra(
        db,
        permit_id=payload.permitId,
        fallback_team_member_ids=payload.teamMemberIds,
    )

    is_standalone = payload.isStandalone or (payload.permitId is None)

    flra = FLRA(
        number=number,
        permitId=payload.permitId,
        plantId=payload.plantId,
        date=payload.date,
        location=payload.location,
        jobDescription=payload.jobDescription,
        leaderId=user.id,
        # Always retain a JSON-string copy for back-compat readers
        hazards=json.dumps(
            [
                {
                    "step": s.stepDescription,
                    "hazard": h.hazardDescription,
                    "category": h.hazardCategory,
                    "initialLikelihood": h.initialLikelihood,
                    "initialSeverity": h.initialSeverity,
                    "control": h.controlMeasures,
                    "residualLikelihood": h.residualLikelihood,
                    "residualSeverity": h.residualSeverity,
                }
                for s in payload.jobSteps
                for h in s.hazards
            ]
            if use_structured
            else legacy_filled
        ),
        toolboxTalkById=payload.toolboxTalkById,
        toolboxTalkConfirmed=payload.toolboxTalkConfirmed,
        status=FLRAStatus.IN_PROGRESS,
        # Commit 3 wizard fields
        isStandalone=is_standalone,
        departmentId=payload.departmentId,
        areaCode=payload.areaCode,
        specificLocation=payload.specificLocation,
        gpsLatitude=payload.gpsLatitude,
        gpsLongitude=payload.gpsLongitude,
        startTime=payload.startTime,
        jobIsRoutine=payload.jobIsRoutine,
        toolboxTalkConducted=payload.toolboxTalkConducted,
        toolboxTalkConductedAt=payload.toolboxTalkConductedAt,
        toolboxTalkTopics=payload.toolboxTalkTopics,
        toolboxTalkLanguage=payload.toolboxTalkLanguage,
        ppeChecklistResponses=payload.ppeChecklistResponses,
        toolsCheckedResponses=payload.toolsCheckedResponses,
        exitRoutesIdentified=payload.exitRoutesIdentified,
        emergencyContactsConfirmed=payload.emergencyContactsConfirmed,
    )
    db.add(flra)
    await db.flush()

    # Team members + crew signature rows
    for uid in payload.teamMemberIds:
        db.add(FLRATeamMember(flraId=flra.id, userId=uid))
    for uid in crew_ids:
        db.add(FLRACrewSignature(flraId=flra.id, userId=uid))

    # Structured job steps + hazards (Commit 3)
    for step in payload.jobSteps:
        js = FLRAJobStep(
            flraId=flra.id,
            sequence=step.sequence,
            stepDescription=step.stepDescription,
        )
        db.add(js)
        await db.flush()
        for hz in step.hazards:
            initial_score = hz.initialLikelihood * hz.initialSeverity
            residual_score = hz.residualLikelihood * hz.residualSeverity
            db.add(
                FLRAStepHazard(
                    jobStepId=js.id,
                    hazardDescription=hz.hazardDescription,
                    hazardCategory=hz.hazardCategory,
                    energySource=hz.energySource,
                    initialLikelihood=hz.initialLikelihood,
                    initialSeverity=hz.initialSeverity,
                    initialRiskScore=initial_score,
                    initialRiskLevel=risk_level_from_score(initial_score),
                    controlMeasures=hz.controlMeasures,
                    residualLikelihood=hz.residualLikelihood,
                    residualSeverity=hz.residualSeverity,
                    residualRiskScore=residual_score,
                    residualRiskLevel=risk_level_from_score(residual_score),
                )
            )

    # Fitness declarations (Commit 3)
    for fd in payload.fitnessDeclarations:
        db.add(
            FLRAFitnessDeclaration(
                flraId=flra.id,
                userId=fd.userId,
                isFit=fd.isFit,
                hasMedicalCondition=fd.hasMedicalCondition,
                conditionsDeclared=fd.conditionsDeclared,
                hadAdequateRest=fd.hadAdequateRest,
                underInfluenceCheck=fd.underInfluenceCheck,
                notes=fd.notes,
            )
        )

    await db.flush()
    await db.refresh(flra)
    return FLRAOut.model_validate(flra)


@router.get("/{flra_id}", response_model=FLRAOut)
async def get_flra(
    flra_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FLRAOut:
    flra = await db.get(FLRA, flra_id)
    if flra is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    record = {"leaderId": flra.leaderId}
    result = await can(
        db,
        user.id,
        "FLRA.READ",
        PermissionContext(record_id=flra.id, plant_id=flra.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    return FLRAOut.model_validate(flra)


@router.delete("/{flra_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_flra(
    flra_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hard-delete an FLRA. Per the RBAC matrix only HSE_MANAGER (own plant)
    and SYSTEM_ADMIN have FLRA.DELETE. Cascades remove team members,
    crew signatures, job steps + hazards, fitness declarations, and
    attachments via FK ondelete=CASCADE."""
    flra = await db.get(FLRA, flra_id)
    if flra is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "FLRA not found")
    record = {"leaderId": flra.leaderId}
    result = await can(
        db,
        user.id,
        "FLRA.DELETE",
        PermissionContext(record_id=flra.id, plant_id=flra.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    await db.delete(flra)
    await db.flush()


@router.post("/{flra_id}/sign")
async def sign_flra(
    flra_id: str,
    request: Request,
    payload: FLRASignRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Per-crew sign-off with training re-validation. Also handles the
    refusal-to-sign flow when payload.refusedToSign=true."""
    flra = await db.get(FLRA, flra_id)
    if flra is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "FLRA not found")
    if flra.status == FLRAStatus.SUPERSEDED:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This FLRA has been superseded by a re-do — sign the new one instead.",
        )
    if flra.status == FLRAStatus.CANCELLED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This FLRA has been cancelled.")
    if flra.status == FLRAStatus.COMPLETED:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This FLRA is already complete — no further signatures needed.",
        )

    # Permit-state lock
    permit = await db.get(Permit, flra.permitId) if flra.permitId else None
    if permit and permit.status in {
        PermitStatus.SUSPENDED,
        PermitStatus.EXPIRED,
        PermitStatus.CLOSED,
        PermitStatus.REJECTED,
    }:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Linked permit is {permit.status.value}. FLRA actions are locked.",
        )

    sig_q = select(FLRACrewSignature).where(
        FLRACrewSignature.flraId == flra.id, FLRACrewSignature.userId == user.id
    )
    sig = (await db.execute(sig_q)).scalar_one_or_none()
    if sig is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You are not listed on this FLRA's crew. Ask the crew leader to add you before signing.",
        )
    if sig.signed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You have already signed this FLRA.")
    if sig.refusedToSign:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "You have already refused — supervisor must replace you or re-do the FLRA.",
        )

    # Refusal path — supervisor must escalate; permit stays gated
    if payload and payload.refusedToSign:
        sig.refusedToSign = True
        sig.refusalReason = (payload.refusalReason or "").strip()
        sig.refusalEscalatedToId = payload.escalatedToId or flra.leaderId
        sig.refusalEscalatedAt = datetime.now(timezone.utc)
        await db.flush()
        return {
            "ok": True,
            "refused": True,
            "escalatedToId": sig.refusalEscalatedToId,
        }

    # Training re-check via canonical competency service. Replaces the
    # legacy single-program REQUIRED_TRAINING_CODES lookup with the
    # full multi-program permit-type gate driven by
    # TrainingProgram.isMandatoryForPermitTypes.
    training_valid = True
    training_expires_at: datetime | None = None
    if permit:
        from app.services.competency import check_competency_for_permit_type

        comp = await check_competency_for_permit_type(db, user.id, permit.type.value)
        if not comp.ok:
            msgs = [b.message for b in comp.blockers]
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                (
                    "You cannot sign this FLRA — competency requirements not met:\n• "
                    + "\n• ".join(msgs)
                    + "\nContact your supervisor."
                ),
            )
        # Capture earliest expiry across all required certs as the
        # signature's training-expires-at marker.
        from app.models.training import TrainingCertificate
        from sqlalchemy import select as _sa_select

        live = (
            await db.execute(
                _sa_select(TrainingCertificate)
                .where(TrainingCertificate.userId == user.id)
                .where(
                    TrainingCertificate.status.in_(["ACTIVE", "EXPIRING_SOON"])
                )
                .order_by(TrainingCertificate.validTo.asc().nulls_last())
                .limit(1)
            )
        ).scalar_one_or_none()
        if live is not None:
            training_expires_at = live.validTo

    # Block sign if user declared not fit
    fitness_q = select(FLRAFitnessDeclaration).where(
        FLRAFitnessDeclaration.flraId == flra.id,
        FLRAFitnessDeclaration.userId == user.id,
    )
    fd = (await db.execute(fitness_q)).scalar_one_or_none()
    if fd is not None and not fd.isFit:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "You declared yourself not fit for duty — supervisor must replace you before sign-off.",
        )

    sig.signed = True
    sig.signedAt = datetime.now(timezone.utc)
    sig.ipAddress = (
        request.headers.get("x-forwarded-for") or ""
    ).split(",")[0].strip() or request.headers.get("x-real-ip")
    sig.deviceInfo = request.headers.get("user-agent")
    sig.trainingValidAtSignature = training_valid
    sig.trainingExpiresAt = training_expires_at
    await db.flush()

    completed = await maybe_complete_flra(db, flra.id)
    return {"ok": True, "flraCompleted": completed}


@router.post("/{flra_id}/redo")
async def redo_flra(
    flra_id: str,
    payload: FLRARedoRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    flra = await db.get(FLRA, flra_id)
    if flra is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "FLRA not found")
    if flra.status not in {FLRAStatus.IN_PROGRESS, FLRAStatus.COMPLETED}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Cannot re-do a {flra.status.value} FLRA."
        )

    # Authorisation: crew member, leader, or HSE_MANAGER / ADMIN
    sig_q = select(FLRACrewSignature).where(
        FLRACrewSignature.flraId == flra.id, FLRACrewSignature.userId == user.id
    )
    is_crew = (
        await db.execute(sig_q)
    ).scalar_one_or_none() is not None or flra.leaderId == user.id
    role_codes = await get_user_role_codes(db, user.id)
    is_priv = any(r in {"HSE_MANAGER", "ADMIN", "SYSTEM_ADMIN"} for r in role_codes)
    if not is_crew and not is_priv:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only crew members or HSE Manager can trigger an FLRA re-do.",
        )

    plant = await db.get(Plant, flra.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Plant lookup failed")
    last = (
        await db.execute(
            select(func.count()).select_from(FLRA).where(FLRA.plantId == flra.plantId)
        )
    ).scalar_one()
    new_number = f"FLRA-{plant.code}-{last + 1:04d}"

    sigs = (
        await db.execute(select(FLRACrewSignature).where(FLRACrewSignature.flraId == flra.id))
    ).scalars().all()
    crew_ids = list({s.userId for s in sigs})

    flra.status = FLRAStatus.SUPERSEDED
    flra.supersededReason = payload.reason

    # If linked permit is ACTIVE, suspend it
    if flra.permitId:
        permit = await db.get(Permit, flra.permitId)
        if permit and permit.status == PermitStatus.ACTIVE:
            permit.status = PermitStatus.SUSPENDED
            permit.suspendedAt = datetime.now(timezone.utc)
            permit.suspendedReason = f"FLRA re-do triggered: {payload.reason}"
            instance = (
                await db.execute(
                    select(WorkflowInstance).where(
                        WorkflowInstance.module == "PTW",
                        WorkflowInstance.recordId == permit.id,
                    )
                )
            ).scalar_one_or_none()
            if instance:
                db.add(
                    WorkflowHistory(
                        instanceId=instance.id,
                        stepId=instance.currentStepId,
                        stepName=instance.currentStepName or "FLRA Re-do",
                        action=Action.SUSPENDED,
                        performedById=user.id,
                        comments=(
                            f"FLRA re-do: {payload.reason}. Work suspended pending new FLRA sign-off."
                        ),
                        fromStatus="ACTIVE",
                        toStatus="SUSPENDED",
                    )
                )

    new_flra = FLRA(
        number=new_number,
        permitId=flra.permitId,
        plantId=flra.plantId,
        date=datetime.now(timezone.utc),
        location=flra.location,
        jobDescription=flra.jobDescription,
        leaderId=flra.leaderId,
        hazards=flra.hazards,
        toolboxTalkById=flra.toolboxTalkById,
        toolboxTalkConfirmed=False,
        status=FLRAStatus.IN_PROGRESS,
        isStandalone=flra.isStandalone,
        departmentId=flra.departmentId,
        areaCode=flra.areaCode,
        specificLocation=flra.specificLocation,
        gpsLatitude=flra.gpsLatitude,
        gpsLongitude=flra.gpsLongitude,
    )
    db.add(new_flra)
    await db.flush()
    for uid in crew_ids:
        db.add(FLRATeamMember(flraId=new_flra.id, userId=uid))
        db.add(FLRACrewSignature(flraId=new_flra.id, userId=uid))
    flra.supersededById = new_flra.id
    await db.flush()
    return {"ok": True, "newFlraId": new_flra.id, "newFlraNumber": new_flra.number}
