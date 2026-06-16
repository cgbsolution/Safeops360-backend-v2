from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.permit import (
    Permit,
    PermitCrewMember,
    PermitGasTestPlan,
    PermitIsolation,
    PermitStatus,
    PermitSubjectEquipment,
    PermitToolEquipment,
    PermitType,
)
from app.models.plant import Plant
from app.models.training import TrainingProgram, TrainingRecord
from app.models.user import User
from app.models.workflow import Action, WorkflowHistory, WorkflowInstance
from app.schemas.permit import (
    AdminResetRequest,
    PermitCreate,
    PermitOut,
    ResumeRequest,
    SuspendRequest,
)
from app.services import workflow_engine
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
    get_user_role_codes,
)

router = APIRouter(prefix="/api/ptw", tags=["ptw"])

# Permit-type → required training program code. Mirror of Node side.
REQUIRED_TRAINING_CODES: dict[str, str] = {
    "HOT_WORK": "TR-HW-01",
    "CONFINED_SPACE": "TR-CSE-01",
    "WORK_AT_HEIGHT": "TR-WAH-01",
    "ELECTRICAL_LOTO": "TR-LOTO-01",
}

PERMIT_TYPE_CODE: dict[str, str] = {
    "HOT_WORK": "HW",
    "CONFINED_SPACE": "CS",
    "WORK_AT_HEIGHT": "WAH",
    "EXCAVATION": "EXC",
    "ELECTRICAL_LOTO": "ELE",
    "GENERAL_COLD": "GC",
}


@router.get("")
async def list_permits(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    read_check = await can(db, user.id, "PTW.READ", PermissionContext())
    if not read_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, read_check.reason or "Access denied")
    plants = await get_accessible_plants(db, user.id)
    stmt = select(Permit)
    if plants is None:
        pass
    elif not plants:
        return {"items": [], "total": 0}
    else:
        stmt = stmt.where(Permit.plantId.in_(plants))
    if read_check.matched_scope == "OWN_RECORDS":
        # Workers see permits they originated, issued, received, or are crew on.
        # Crew membership requires a join — handled via subquery below.
        from app.models.permit import PermitCrewMember
        crew_subq = select(PermitCrewMember.permitId).where(PermitCrewMember.userId == user.id)
        stmt = stmt.where(
            (Permit.originatorId == user.id)
            | (Permit.issuerId == user.id)
            | (Permit.receiverId == user.id)
            | (Permit.id.in_(crew_subq))
        )
    rows = (await db.execute(stmt.order_by(Permit.createdAt.desc()).limit(100))).scalars().all()
    return {"items": [PermitOut.model_validate(r) for r in rows], "total": len(rows)}


@router.post("", response_model=PermitOut, status_code=status.HTTP_201_CREATED)
async def create_permit(
    payload: PermitCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PermitOut:
    create_check = await can(db, user.id, "PTW.CREATE", PermissionContext(plant_id=payload.plantId))
    if not create_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, create_check.reason or "Access denied")

    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")

    if payload.issuerId == payload.receiverId:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Issuer and receiver cannot be the same person.")
    if payload.issuerId == user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Originator cannot be their own issuer.")

    issuer = await db.get(User, payload.issuerId)
    receiver = await db.get(User, payload.receiverId)
    if issuer is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid issuer")
    if receiver is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid receiver")

    # Training competency check on receiver — uses the canonical
    # competency service which reads TrainingProgram.isMandatoryForPermitTypes
    # (DB-driven) rather than the legacy hardcoded REQUIRED_TRAINING_CODES
    # dict. Supports MULTIPLE required programs per permit type
    # (e.g. Hot Work needs Hot Work Holder + Fire Watch + Basic Safety).
    from app.services.competency import check_competency_for_permit_type

    comp = await check_competency_for_permit_type(db, payload.receiverId, payload.type.value)
    if not comp.ok:
        msgs = [b.message for b in comp.blockers]
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Receiver {receiver.name} cannot hold this permit:\n• " + "\n• ".join(msgs),
        )

    # Validity window
    if payload.validTo <= payload.validFrom:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Valid To must be later than Valid From.")
    if payload.validTo.timestamp() < datetime.now(timezone.utc).timestamp() - 300:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Valid To cannot be in the past.")
    is_high_risk = payload.type.value in {"HOT_WORK", "CONFINED_SPACE"}
    max_hours = 24 if is_high_risk else 72
    duration_h = (payload.validTo - payload.validFrom).total_seconds() / 3600.0
    if duration_h > max_hours:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Validity window exceeds {max_hours}h cap for this permit type."
        )

    type_code = PERMIT_TYPE_CODE.get(payload.type.value, "PTW")
    # Generate the next permit number for this plant. We pull the MAX
    # numeric suffix of existing permit numbers (not COUNT(*)) so that
    # deletions don't shrink the counter and cause the next insert to
    # collide with a number that was already issued. The `Permit_number_key`
    # unique constraint will still trip on the unlikely concurrent-insert
    # race, but for a single-tenant per-plant counter that's acceptable.
    prefix = f"PTW-{plant.code}-"
    existing_numbers = (
        await db.execute(
            select(Permit.number)
            .where(Permit.plantId == payload.plantId)
            .where(Permit.number.like(f"{prefix}%"))
        )
    ).scalars().all()
    max_suffix = 0
    for n in existing_numbers:
        try:
            suffix_int = int(n.rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            continue
        if suffix_int > max_suffix:
            max_suffix = suffix_int
    number = f"{prefix}{max_suffix + 1:05d}"

    # Auto-detect requirements from permit type — wizard reads the same
    # rules client-side, but we re-compute server-side for defence in depth.
    needs_gas_test = payload.type.value in {"HOT_WORK", "CONFINED_SPACE"}
    needs_fire_watch = payload.type.value == "HOT_WORK"
    validity_hours = int((payload.validTo - payload.validFrom).total_seconds() / 3600.0)

    permit = Permit(
        number=number,
        type=payload.type,
        plantId=payload.plantId,
        areaId=payload.areaId,
        location=payload.location,
        scopeOfWork=payload.scopeOfWork,
        validFrom=payload.validFrom,
        validTo=payload.validTo,
        originatorId=user.id,
        issuerId=payload.issuerId,
        receiverId=payload.receiverId,
        contractorName=payload.contractorName,

        # ─── Wizard Step 1/2 additions ───
        validityHours=validity_hours,
        departmentId=payload.departmentId,
        specificLocation=payload.specificLocation,
        gpsLatitude=payload.gpsLatitude,
        gpsLongitude=payload.gpsLongitude,
        workOrderNumber=payload.workOrderNumber,
        attachedDrawingIds=payload.attachedDrawingIds or None,

        # ─── Wizard Step 3 additions ───
        fireWatchPersonId=payload.fireWatchPersonId,
        standbyPersonId=payload.standbyPersonId,

        # ─── Wizard Step 7 additions ───
        weatherConditionsAtIssue=payload.weatherConditionsAtIssue,
        windSpeedKmh=payload.windSpeedKmh,
        adjacentAreaNotifications=payload.adjacentAreaNotifications,

        # ─── Legacy + auto-derived ───
        isolationsRequired=payload.isolationsRequired,
        ppeChecklist=payload.ppeChecklist,
        gasTestRequired=payload.gasTestRequired or needs_gas_test,
        gasTestResult=payload.gasTestResult,
        o2Level=payload.o2Level,
        lelLevel=payload.lelLevel,
        h2sLevel=payload.h2sLevel,
        fireWatchRequired=payload.fireWatchRequired or needs_fire_watch,
        rescuePlan=payload.rescuePlan,
        status=PermitStatus.DRAFT,
    )
    db.add(permit)
    await db.flush()

    # ─── Wizard child rows ───
    if payload.workCrew:
        # Competency check on every crew member, not just the receiver.
        # Capture validity-at-issuance flags so the activation gate
        # (Commit 4 — PTW) has the snapshot it needs.
        from app.services.competency import check_competency_for_permit_type
        from app.services.ppe_gate import check_ppe_for_crew

        for c in payload.workCrew:
            crew_comp = await check_competency_for_permit_type(
                db, c.userId, payload.type.value
            )
            if not crew_comp.ok:
                target = await db.get(User, c.userId)
                msgs = [b.message for b in crew_comp.blockers]
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    (
                        f"Crew member {target.name if target else c.userId} cannot be added "
                        f"to this {payload.type.value} permit:\n• " + "\n• ".join(msgs)
                    ),
                )

        # PPE snapshot at crew add (PPE-01 Pass 2). Unlike competency this is
        # NOT blocking here — PPE can still be issued between permit creation
        # and activation; the activation gate enforces it live.
        ppe_results = await check_ppe_for_crew(
            db,
            plant_id=payload.plantId,
            user_ids=[c.userId for c in payload.workCrew],
            permit_type_code=payload.type.value,
        )
        for c in payload.workCrew:
            ppe_res = ppe_results.get(c.userId)
            db.add(PermitCrewMember(
                permitId=permit.id,
                userId=c.userId,
                role=c.role,
                trainingValidAtIssuance=True,  # passed competency check
                ppeValidAtIssuance=ppe_res.ok if ppe_res else None,
                ppeValidationNotes=(
                    ppe_res.summary() if ppe_res and not ppe_res.ok else None
                ),
            ))
    if payload.isolations:
        for iso in payload.isolations:
            db.add(PermitIsolation(
                permitId=permit.id,
                isolationType=iso.isolationType,
                description=iso.description,
                isolationPointTag=iso.isolationPointTag,
                lotoTagNumber=iso.lotoTagNumber,
            ))
    if payload.toolsEquipment:
        from app.models.equipment import Equipment

        for tool in payload.toolsEquipment:
            # Defensive FK check — drop tools whose equipmentId doesn't resolve
            if tool.equipmentId:
                eq = await db.get(Equipment, tool.equipmentId)
                if eq is None:
                    continue
            db.add(PermitToolEquipment(
                permitId=permit.id,
                equipmentId=tool.equipmentId,
                freeTextDescription=tool.freeTextDescription,
            ))
    if payload.subjectEquipment:
        from app.models.equipment import Equipment

        for s in payload.subjectEquipment:
            eq = await db.get(Equipment, s.equipmentId)
            if eq is None:
                continue
            db.add(PermitSubjectEquipment(
                permitId=permit.id,
                equipmentId=s.equipmentId,
                workNature=s.workNature,
            ))
    if payload.gasTestPlan:
        plan = payload.gasTestPlan
        db.add(PermitGasTestPlan(
            permitId=permit.id,
            refreshFrequencyMinutes=plan.refreshFrequencyMinutes,
            parametersToTest=[p.model_dump() for p in plan.parametersToTest],
            instrumentSerial=plan.instrumentSerial,
            instrumentLastCalibrated=plan.instrumentLastCalibrated,
        ))

    await db.flush()
    await db.refresh(permit)

    try:
        async with db.begin_nested():
            await workflow_engine.initiate(
                db,
                module="PTW",
                record_id=permit.id,
                record_number=permit.number,
                record_title=permit.scopeOfWork[:120],
                record_data={
                    "type": permit.type.value,
                    "plantId": permit.plantId,
                    "originatorId": permit.originatorId,
                    "issuerId": permit.issuerId,
                    "receiverId": permit.receiverId,
                },
                initiator_id=user.id,
                plant_id=permit.plantId,
            )
    except Exception as e:  # noqa: BLE001
        import sys
        import traceback
        print(f"PTW workflow init failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    # Refresh once more: workflow_engine.initiate flips Permit.status to
    # SUBMITTED via _sync_record_status, and the resulting UPDATE expires
    # server-default columns like updatedAt. Without this refresh, Pydantic
    # serialization triggers a lazy load on the expired attribute and dies
    # with MissingGreenlet (sync code attempting async IO).
    await db.refresh(permit)
    return PermitOut.model_validate(permit)


@router.get("/{permit_id}", response_model=PermitOut)
async def get_permit(
    permit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PermitOut:
    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
    record = {
        "originatorId": permit.originatorId,
        "issuerId": permit.issuerId,
        "receiverId": permit.receiverId,
    }
    result = await can(
        db, user.id, "PTW.READ",
        PermissionContext(record_id=permit.id, plant_id=permit.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    return PermitOut.model_validate(permit)


@router.get("/{permit_id}/activation-gate")
async def get_activation_gate(
    permit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Returns the full PTW activation gate status — every blocker reason
    aggregated so the receiver-step UI can render them all at once."""
    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
    result = await can(
        db,
        user.id,
        "PTW.READ",
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

    from app.services.ptw_activation_gate import can_ptw_transition_to_active

    gate = await can_ptw_transition_to_active(db, permit_id)
    return {
        "ok": gate.ok,
        "blockers": [
            {"code": b.code, "message": b.message, "severity": b.severity}
            for b in gate.blockers
        ],
        "flra": {
            "id": gate.flra_id,
            "number": gate.flra_number,
            "status": gate.flra_status,
            "signedCount": gate.signed_count,
            "totalCrew": gate.total_crew,
        }
        if gate.flra_id
        else None,
        "crewValidityIssues": gate.crew_validity_issues,
        "crewPpeIssues": gate.crew_ppe_issues,
        "crewPpeWarnings": gate.crew_ppe_warnings,
        "isolations": {
            "pending": gate.isolations_pending,
            "total": gate.isolations_total,
        },
    }


@router.delete("/{permit_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_permit(
    permit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hard-delete a permit. Per the RBAC matrix:
    - PERMIT_ISSUER can delete OWN_RECORDS (their own draft permits)
    - HSE_MANAGER can delete OWN_PLANT
    - SYSTEM_ADMIN can delete ALL_PLANTS
    The permission service enforces the scope. Cascades remove workflow
    instance, tasks, history, child rows (isolations, gas readings,
    suspensions, extensions, approvals, attachments) via FK ondelete=CASCADE.
    The linked FLRAs and WorkflowInstance need explicit cleanup since
    they don't FK-cascade from Permit."""
    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
    record = {
        "originatorId": permit.originatorId,
        "issuerId": permit.issuerId,
        "receiverId": permit.receiverId,
    }
    result = await can(
        db,
        user.id,
        "PTW.DELETE",
        PermissionContext(record_id=permit.id, plant_id=permit.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    inst_rows = (
        await db.execute(
            select(WorkflowInstance).where(
                WorkflowInstance.module == "PTW",
                WorkflowInstance.recordId == permit_id,
            )
        )
    ).scalars().all()
    for inst in inst_rows:
        await db.delete(inst)

    await db.delete(permit)
    await db.flush()


@router.patch("/{permit_id}", response_model=PermitOut)
async def admin_reset(
    permit_id: str,
    payload: AdminResetRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PermitOut:
    """Admin override — reset stuck records to DRAFT or SUBMITTED only."""
    result = await can(db, user.id, "CONFIGURATION.WORKFLOWS", PermissionContext())
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Admin only")
    if payload.status not in {"DRAFT", "SUBMITTED"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Admin override only supports DRAFT or SUBMITTED.")
    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    permit.status = PermitStatus(payload.status)
    await db.flush()
    return PermitOut.model_validate(permit)


@router.post("/{permit_id}/suspend")
async def suspend_permit(
    permit_id: str,
    payload: SuspendRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
    record = {"originatorId": permit.originatorId, "issuerId": permit.issuerId, "receiverId": permit.receiverId}
    result = await can(
        db, user.id, "PTW.UPDATE",
        PermissionContext(record_id=permit.id, plant_id=permit.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    if permit.status != PermitStatus.ACTIVE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Only ACTIVE permits can be suspended (current: {permit.status.value}).")

    permit.status = PermitStatus.SUSPENDED
    permit.suspendedAt = datetime.now(timezone.utc)
    permit.suspendedReason = payload.reason
    instance = (
        await db.execute(
            select(WorkflowInstance).where(WorkflowInstance.module == "PTW", WorkflowInstance.recordId == permit_id)
        )
    ).scalar_one_or_none()
    if instance:
        db.add(
            WorkflowHistory(
                instanceId=instance.id,
                stepId=instance.currentStepId,
                stepName=instance.currentStepName or "Suspended",
                action=Action.ESCALATED,
                performedById=user.id,
                comments=f"Permit suspended by HSE: {payload.reason}",
                fromStatus="ACTIVE",
                toStatus="SUSPENDED",
            )
        )
    await db.flush()
    return {"ok": True}


@router.post("/{permit_id}/resume")
async def resume_permit(
    permit_id: str,
    payload: ResumeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
    record = {"originatorId": permit.originatorId, "issuerId": permit.issuerId, "receiverId": permit.receiverId}
    result = await can(
        db, user.id, "PTW.UPDATE",
        PermissionContext(record_id=permit.id, plant_id=permit.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    if permit.status != PermitStatus.SUSPENDED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Only SUSPENDED permits can be resumed (current: {permit.status.value}).")
    if permit.validTo.timestamp() < datetime.now(timezone.utc).timestamp():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Validity window has expired. Request an extension before resuming.")
    permit.status = PermitStatus.ACTIVE
    permit.suspendedAt = None
    permit.suspendedReason = None

    instance = (
        await db.execute(
            select(WorkflowInstance).where(WorkflowInstance.module == "PTW", WorkflowInstance.recordId == permit_id)
        )
    ).scalar_one_or_none()
    if instance:
        comments = f"Permit resumed after suspension: {payload.comments}" if payload.comments else "Permit resumed after suspension."
        db.add(
            WorkflowHistory(
                instanceId=instance.id,
                stepId=instance.currentStepId,
                stepName=instance.currentStepName or "Resumed",
                action=Action.APPROVED,
                performedById=user.id,
                comments=comments,
                fromStatus="SUSPENDED",
                toStatus="ACTIVE",
            )
        )
    await db.flush()
    return {"ok": True}


@router.get("/eligible-for-flra/list")
async def eligible_for_flra(
    q: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Permits the caller can attach a fresh FLRA to. Drives the FLRA form's
    linked-permit picker."""
    eligible_statuses = [
        PermitStatus.ISSUER_APPROVED,
        PermitStatus.SAFETY_APPROVED,
        PermitStatus.PLANT_HEAD_APPROVED,
        PermitStatus.ACTIVE,
    ]
    stmt = select(Permit).where(Permit.status.in_(eligible_statuses))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (Permit.number.ilike(like))
            | (Permit.location.ilike(like))
            | (Permit.scopeOfWork.ilike(like))
        )
    role_codes = await get_user_role_codes(db, user.id)
    is_priv = any(r in {"HSE_MANAGER", "ADMIN", "SYSTEM_ADMIN", "CORPORATE_HSE"} for r in role_codes)
    if not is_priv:
        from app.models.permit import PermitCrewMember
        crew_subq = select(PermitCrewMember.permitId).where(PermitCrewMember.userId == user.id)
        stmt = stmt.where(
            (Permit.receiverId == user.id)
            | (Permit.originatorId == user.id)
            | (Permit.issuerId == user.id)
            | (Permit.id.in_(crew_subq))
        )
    rows = (await db.execute(stmt.order_by(Permit.validFrom.desc()).limit(50))).scalars().all()
    return {"items": [PermitOut.model_validate(r) for r in rows]}
