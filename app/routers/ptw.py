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
from app.models.plant import Area, Plant
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
from app.services.register_view import status_counts, workflow_chips
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
    # Soft-deleted permits are never part of the register. The list was
    # missing this filter while the frontend's own count query applied it, so
    # the tab totals and the rows disagreed on deleted records.
    stmt = select(Permit).where(Permit.isDeleted.is_(False))
    # Archived permits (retention flag on CLOSED) are hidden from the
    # default register; ?include_archived=true surfaces them.
    if not include_archived:
        stmt = stmt.where(Permit.isArchived.is_(False))
    if plants is None:
        pass
    elif not plants:
        return {"items": [], "total": 0, "statusCounts": {}, "typeCounts": {}}
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
    # Scope-only SELECT — the basis for the tab counts, which describe the
    # caller's whole accessible register rather than this page of rows.
    scoped = stmt
    status_map = await status_counts(db, scoped, Permit.status)
    type_map = await status_counts(db, scoped, Permit.type)

    rows = (await db.execute(scoped.order_by(Permit.createdAt.desc()).limit(100))).scalars().all()

    plant_names = dict(
        (
            await db.execute(
                select(Plant.id, Plant.name).where(Plant.id.in_({r.plantId for r in rows}))
            )
        ).all()
    ) if rows else {}
    area_ids = {r.areaId for r in rows if r.areaId}
    area_names = dict(
        (await db.execute(select(Area.id, Area.name).where(Area.id.in_(area_ids)))).all()
    ) if area_ids else {}
    chips = await workflow_chips(db, "PTW", [r.id for r in rows])

    items = []
    for r in rows:
        item = PermitOut.model_validate(r).model_dump()
        item["plantName"] = plant_names.get(r.plantId)
        item["areaName"] = area_names.get(r.areaId) if r.areaId else None
        item["workflow"] = chips.get(r.id)
        items.append(item)
    return {
        "items": items,
        "total": len(items),
        "statusCounts": status_map,
        "typeCounts": type_map,
    }


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


@router.get("/{permit_id}")
async def get_permit(
    permit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
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

    out: dict[str, Any] = PermitOut.model_validate(permit).model_dump()

    def _cols(row) -> dict[str, Any]:
        return {c.name: getattr(row, c.name) for c in row.__table__.columns}

    # ── Header identities ─────────────────────────────────────────────
    plant = await db.get(Plant, permit.plantId)
    out["plant"] = {"id": plant.id, "name": plant.name} if plant else None
    area = await db.get(Area, permit.areaId) if permit.areaId else None
    out["area"] = {"id": area.id, "name": area.name} if area else None

    party_ids = {
        pid
        for pid in (permit.originatorId, permit.issuerId, permit.receiverId)
        if pid
    }
    parties = {
        uid: {"id": uid, "name": name, "designation": desig}
        for uid, name, desig in (
            await db.execute(
                select(User.id, User.name, User.designation).where(User.id.in_(party_ids))
            )
        ).all()
    } if party_ids else {}
    out["originator"] = parties.get(permit.originatorId)
    out["issuer"] = parties.get(permit.issuerId)
    out["receiver"] = parties.get(permit.receiverId)

    # ── Child collections, each ordered the way the page reads them ───
    # One helper query per child + a single name lookup, rather than a join
    # per row: these tables are small per permit but the name columns repeat.
    async def _named(model, order_by, *user_cols, limit: int | None = None):
        stmt = select(model).where(model.permitId == permit.id).order_by(order_by)
        if limit:
            stmt = stmt.limit(limit)
        rows = (await db.execute(stmt)).scalars().all()
        ids = {getattr(r, c) for r in rows for c in user_cols if getattr(r, c, None)}
        names = dict(
            (await db.execute(select(User.id, User.name).where(User.id.in_(ids)))).all()
        ) if ids else {}
        return rows, names

    from app.models.permit import (
        PermitActionEvidence,
        PermitApproval,
        PermitExtension,
        PermitGasTestReading,
        PermitSuspension,
    )
    from app.models.flra import FLRA, FLRACrewSignature

    # Crew — the roster the FLRA and activation gate both key off.
    crew_rows = (
        await db.execute(
            select(PermitCrewMember, User.id, User.name, User.designation)
            .outerjoin(User, User.id == PermitCrewMember.userId)
            .where(PermitCrewMember.permitId == permit.id)
        )
    ).all()
    out["workCrew"] = [
        {**_cols(c), "user": {"id": uid, "name": name, "designation": desig}}
        for c, uid, name, desig in crew_rows
    ]

    # FLRAs on this permit, each with its signature rows.
    flras = (
        await db.execute(select(FLRA).where(FLRA.permitId == permit.id))
    ).scalars().all()
    sigs_by_flra: dict[str, list[dict[str, Any]]] = {}
    if flras:
        sig_rows = (
            await db.execute(
                select(FLRACrewSignature).where(
                    FLRACrewSignature.flraId.in_([f.id for f in flras])
                )
            )
        ).scalars().all()
        for sig in sig_rows:
            sigs_by_flra.setdefault(sig.flraId, []).append(_cols(sig))
    out["flras"] = [
        {**_cols(f), "crewSignatures": sigs_by_flra.get(f.id, [])} for f in flras
    ]

    ext_rows, ext_names = await _named(
        PermitExtension, PermitExtension.requestedAt.desc(), "requestedById", "approvedById"
    )
    out["extensions"] = [
        {
            **_cols(e),
            "requestedBy": {"name": ext_names.get(e.requestedById)} if e.requestedById else None,
            "approvedBy": {"name": ext_names.get(e.approvedById)} if e.approvedById else None,
        }
        for e in ext_rows
    ]

    isolations = (
        await db.execute(
            select(PermitIsolation).where(PermitIsolation.permitId == permit.id)
        )
    ).scalars().all()
    out["isolations"] = [_cols(i) for i in isolations]

    appr_rows = (
        await db.execute(
            select(PermitApproval, User.name, User.designation)
            .outerjoin(User, User.id == PermitApproval.approverId)
            .where(PermitApproval.permitId == permit.id)
            .order_by(PermitApproval.decidedAt.asc())
        )
    ).all()
    out["approvalsLog"] = [
        {**_cols(a), "approver": {"name": name, "designation": desig}}
        for a, name, desig in appr_rows
    ]

    susp_rows, susp_names = await _named(
        PermitSuspension, PermitSuspension.suspendedAt.asc(), "suspendedById", "resumedById"
    )
    out["suspensions"] = [
        {
            **_cols(x),
            "suspendedBy": {"name": susp_names.get(x.suspendedById)} if x.suspendedById else None,
            "resumedBy": {"name": susp_names.get(x.resumedById)} if x.resumedById else None,
        }
        for x in susp_rows
    ]

    # Gas readings are capped at 50 like the page's own `take` — a confined
    # space permit can accumulate hundreds and the card only charts the trend.
    gas_rows, gas_names = await _named(
        PermitGasTestReading, PermitGasTestReading.recordedAt.asc(), "recordedById", limit=50
    )
    out["gasTestReadings"] = [
        {**_cols(g), "recordedBy": {"name": gas_names.get(g.recordedById)} if g.recordedById else None}
        for g in gas_rows
    ]

    ev_rows, ev_names = await _named(
        PermitActionEvidence, PermitActionEvidence.capturedAt.asc(), "actorId"
    )
    out["actionEvidence"] = [
        {
            **_cols(e),
            "actor": {"name": ev_names.get(e.actorId)} if e.actorId else None,
            # The gallery only needs the count, so photos are returned as bare
            # ids rather than hydrated rows.
            "photos": [],
        }
        for e in ev_rows
    ]
    return out


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
    - ADMIN can delete ALL_PLANTS
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
    permitId: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Permits the caller can attach a fresh FLRA to. Drives the FLRA form's
    linked-permit picker, and — with `permitId` — the pre-selected permit when
    the New FLRA page is opened from a permit (`/flra/new?permitId=…`).

    Items carry the nested `plant`, `receiver`, `workCrew` and `flras` the form
    needs to seed its crew roster. A bare PermitOut omits them, which silently
    left the roster empty because the form falls back to `?? []`.
    """
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
    stmt = select(Permit)
    if permitId:
        # Explicit lookup: the caller already chose this permit, so status
        # eligibility is not re-imposed — the page still needs to render a
        # permit that has since moved on. Access is enforced below.
        stmt = stmt.where(Permit.id == permitId)
    else:
        stmt = stmt.where(Permit.status.in_(eligible_statuses))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (Permit.number.ilike(like))
            | (Permit.location.ilike(like))
            | (Permit.scopeOfWork.ilike(like))
        )
    role_codes = await get_user_role_codes(db, user.id)
    is_priv = any(r in {"HSE_MANAGER", "ADMIN", "CORPORATE_HSE"} for r in role_codes)
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
    if not rows:
        return {"items": []}

    # ── Enrich with the nested objects the FLRA form reads ──────────────
    from app.models.permit import PermitCrewMember
    from app.models.flra import FLRA
    from app.models.plant import Plant

    permit_ids = [p.id for p in rows]

    plant_rows = (
        await db.execute(
            select(Plant.id, Plant.name).where(Plant.id.in_({p.plantId for p in rows}))
        )
    ).all()
    plant_by_id = {pid: {"id": pid, "name": name} for pid, name in plant_rows}

    receiver_ids = {p.receiverId for p in rows if p.receiverId}
    user_rows = (
        await db.execute(select(User.id, User.name).where(User.id.in_(receiver_ids)))
        if receiver_ids
        else None
    )
    user_by_id = {uid: name for uid, name in user_rows.all()} if user_rows else {}

    # Active crew only — a removed member must not reappear on a new FLRA.
    crew_rows = (
        await db.execute(
            select(PermitCrewMember.permitId, PermitCrewMember.userId, User.id, User.name)
            .join(User, User.id == PermitCrewMember.userId)
            .where(PermitCrewMember.permitId.in_(permit_ids))
            .where(PermitCrewMember.removedAt.is_(None))
        )
    ).all()
    crew_by_permit: dict[str, list[dict[str, Any]]] = {}
    for pid, uid, u_id, u_name in crew_rows:
        crew_by_permit.setdefault(pid, []).append(
            {"userId": uid, "user": {"id": u_id, "name": u_name}}
        )

    # Existing FLRAs — the picker greys out permits that already have one.
    flra_rows = (
        await db.execute(
            select(FLRA.permitId, FLRA.id, FLRA.status)
            .where(FLRA.permitId.in_(permit_ids))
            .where(FLRA.status.in_(["IN_PROGRESS", "COMPLETED"]))
        )
    ).all()
    flras_by_permit: dict[str, list[dict[str, Any]]] = {}
    for pid, fid, fstatus in flra_rows:
        flras_by_permit.setdefault(pid, []).append(
            {"id": fid, "status": fstatus.value if hasattr(fstatus, "value") else fstatus}
        )

    items = []
    for p in rows:
        item = PermitOut.model_validate(p).model_dump()
        item["plant"] = plant_by_id.get(p.plantId)
        item["receiver"] = (
            {"id": p.receiverId, "name": user_by_id.get(p.receiverId, "")}
            if p.receiverId
            else None
        )
        item["workCrew"] = crew_by_permit.get(p.id, [])
        item["flras"] = flras_by_permit.get(p.id, [])
        items.append(item)
    return {"items": items}
