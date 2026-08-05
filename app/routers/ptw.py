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
    PermitUpdate,
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
    "LIFTING": "TR-LIFT-01",
}

PERMIT_TYPE_CODE: dict[str, str] = {
    "HOT_WORK": "HW",
    "CONFINED_SPACE": "CS",
    "WORK_AT_HEIGHT": "WAH",
    "EXCAVATION": "EXC",
    "ELECTRICAL_LOTO": "ELE",
    "LIFTING": "LIFT",
    "GENERAL_COLD": "GC",
}


@router.get("")
async def list_permits(
    include_archived: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    read_check = await can(db, user.id, "PTW.READ", PermissionContext())
    if not read_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, read_check.reason or "Access denied")
    plants = await get_accessible_plants(db, user.id)
    stmt = select(Permit)
    # Archived permits (retention flag on CLOSED) are hidden from the
    # default register; ?include_archived=true surfaces them.
    if not include_archived:
        stmt = stmt.where(Permit.isArchived.is_(False))
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

    # HIRA provenance — validate the link before persisting it, so a bad id
    # fails loudly at create time instead of leaving a dangling reference that
    # ON DELETE SET NULL would later hide.
    if payload.hiraEntryHazardId and not payload.hiraEntryId:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "hiraEntryHazardId requires hiraEntryId.",
        )
    if payload.hiraEntryId:
        from app.models.hira import HiraEntry as _HiraEntry, HiraEntryHazard as _HiraEntryHazard

        hira_entry = await db.get(_HiraEntry, payload.hiraEntryId)
        if hira_entry is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid hiraEntryId")
        if payload.hiraEntryHazardId:
            hz_row = await db.get(_HiraEntryHazard, payload.hiraEntryHazardId)
            if hz_row is None or hz_row.entryId != payload.hiraEntryId:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "hiraEntryHazardId does not belong to hiraEntryId",
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

    # FLRA policy (closed-loop rebuild): explicit wizard override wins, else
    # instance config (PTW_FLRA_REQUIRED_DEFAULT / PTW_FLRA_REQUIRED_TYPES).
    # Snapshotted per permit so the workflow + activation gate are auditable.
    from app.core.config import get_settings

    flra_required = (
        payload.flraRequired
        if payload.flraRequired is not None
        else get_settings().ptw_flra_required_for(payload.type.value)
    )

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
        contractorCompanyId=payload.contractorCompanyId,

        # ─── Wizard Step 1/2 additions ───
        validityHours=validity_hours,
        departmentId=payload.departmentId,
        specificLocation=payload.specificLocation,
        gpsLatitude=payload.gpsLatitude,
        gpsLongitude=payload.gpsLongitude,
        workOrderNumber=payload.workOrderNumber,
        # attachedDrawingIds is DEPRECATED (dangling ids) — drawings are now
        # uploaded post-create via POST /api/ptw/{id}/attachments.

        # ─── HIRA provenance ───
        # Populated when the permit was raised from a HIRA hazard row's
        # Create-PTW prompt. Validated below before the row is added.
        hiraEntryId=payload.hiraEntryId,
        hiraEntryHazardId=payload.hiraEntryHazardId,

        # ─── Closed-loop rebuild ───
        flraRequired=flra_required,

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
                    # Conditional FLRA step keys off this (conditionExpr).
                    "flraRequired": bool(permit.flraRequired),
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
        "flraRequired": bool(permit.flraRequired),
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
    reason: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete a permit (governed entity — never hard-deleted). Per the RBAC matrix:
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

    from app.core.soft_delete import soft_delete

    soft_delete(permit, user.id, reason or "Permit removed by authorised user via delete endpoint")
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


@router.patch("/{permit_id}/details", response_model=PermitOut)
async def update_permit_details(
    permit_id: str,
    payload: PermitUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PermitOut:
    """Edit a permit's core details while it is still open (DRAFT / SUBMITTED —
    before any approval). Once approved / active / terminal, its scope, validity
    and location are locked. Child collections (crew, isolations, gas plan,
    tools) are managed by the create wizard / active-phase panels, not here.
    Enforces PTW.UPDATE + scope."""
    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
    if permit.status not in (PermitStatus.DRAFT, PermitStatus.SUBMITTED):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A permit can only be edited before it is approved (current status: "
            f"{permit.status.value.replace('_', ' ').title()}).",
        )
    record = {
        "originatorId": permit.originatorId,
        "issuerId": permit.issuerId,
        "receiverId": permit.receiverId,
    }
    result = await can(
        db, user.id, "PTW.UPDATE",
        PermissionContext(record_id=permit.id, plant_id=permit.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    data = payload.model_dump(exclude_unset=True)
    for field in (
        "type", "location", "scopeOfWork", "validFrom", "validTo", "departmentId",
        "areaId", "specificLocation", "workOrderNumber", "weatherConditionsAtIssue",
        "windSpeedKmh", "contractorName", "contractorCompanyId",
    ):
        if field in data:
            setattr(permit, field, data[field])

    if permit.validFrom and permit.validTo and permit.validTo <= permit.validFrom:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Valid-to must be after valid-from.")

    await db.flush()
    await db.refresh(permit)
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

    # Closed-loop rebuild: suspension is a lifecycle action → field evidence.
    from app.models.permit import PermitEvidenceAction
    from app.services.ptw_evidence import EvidenceError, record_action_evidence

    try:
        await record_action_evidence(
            db,
            permit=permit,
            action=PermitEvidenceAction.SUSPEND,
            actor_id=user.id,
            gps_latitude=payload.evidence.gpsLatitude if payload.evidence else None,
            gps_longitude=payload.evidence.gpsLongitude if payload.evidence else None,
            gps_accuracy_meters=payload.evidence.gpsAccuracyMeters if payload.evidence else None,
            signature_image=payload.evidence.signatureImageBase64 if payload.evidence else None,
            declaration_text=payload.evidence.declarationText if payload.evidence else None,
            comments=payload.reason,
            photo_attachment_ids=payload.evidence.photoAttachmentIds if payload.evidence else None,
        )
    except EvidenceError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e

    permit.status = PermitStatus.SUSPENDED
    permit.suspendedAt = datetime.now(timezone.utc)
    permit.suspendedReason = payload.reason
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
        payload={"from": "ACTIVE", "to": "SUSPENDED", "reason": payload.reason},
    )
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

    # Closed-loop rebuild: resumption is a lifecycle action → field evidence.
    from app.models.permit import PermitEvidenceAction
    from app.services.ptw_evidence import EvidenceError, record_action_evidence

    try:
        await record_action_evidence(
            db,
            permit=permit,
            action=PermitEvidenceAction.RESUME,
            actor_id=user.id,
            gps_latitude=payload.evidence.gpsLatitude if payload.evidence else None,
            gps_longitude=payload.evidence.gpsLongitude if payload.evidence else None,
            gps_accuracy_meters=payload.evidence.gpsAccuracyMeters if payload.evidence else None,
            signature_image=payload.evidence.signatureImageBase64 if payload.evidence else None,
            declaration_text=payload.evidence.declarationText if payload.evidence else None,
            comments=payload.comments,
            photo_attachment_ids=payload.evidence.photoAttachmentIds if payload.evidence else None,
        )
    except EvidenceError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e

    permit.status = PermitStatus.ACTIVE
    permit.suspendedAt = None
    permit.suspendedReason = None
    from app.services import events as domain_events
    domain_events.emit(
        db,
        event_type=domain_events.PTW_RESUMED,
        entity_type="Permit",
        entity_id=permit.id,
        entity_ref=permit.number,
        site_id=permit.plantId,
        actor_id=user.id,
        payload={"from": "SUSPENDED", "to": "ACTIVE"},
    )

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
        # Closed-loop states: FLRA is prepared between issue and acceptance.
        PermitStatus.APPROVED,
        PermitStatus.ISSUED,
        PermitStatus.ACTIVE,
        # Deprecated intermediate statuses — kept for pre-rebuild rows.
        PermitStatus.ISSUER_APPROVED,
        PermitStatus.SAFETY_APPROVED,
        PermitStatus.PLANT_HEAD_APPROVED,
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
    # Newest-created first — platform-wide register convention.
    rows = (
        await db.execute(stmt.order_by(Permit.createdAt.desc(), Permit.id.desc()).limit(50))
    ).scalars().all()
    return {"items": [PermitOut.model_validate(r) for r in rows]}
