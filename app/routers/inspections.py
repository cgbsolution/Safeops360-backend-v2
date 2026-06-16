from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.equipment import Equipment, Inspection, InspectionStatus
from app.models.user import User
from app.schemas.inspection import InspectionCreate, InspectionOut, InspectionUpdate
from app.services import workflow_engine
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
)

router = APIRouter(prefix="/api/inspections", tags=["inspections"])

VALID_RESULTS = {"Pass", "Partial", "Fail"}


@router.get("/equipment")
async def list_inspection_equipment(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Equipment available for the inspection-create form. Scoped to the
    plants the caller can act in (via INSPECTION.CREATE) so the mobile
    picker can't offer items that will only 403 on submit.

    Used by the mobile InspectionCreateScreen — the web flow loads the
    same data directly from Prisma in src/app/(dashboard)/inspections/new.
    """
    create_check = await can(db, user.id, "INSPECTION.CREATE", PermissionContext())
    if not create_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, create_check.reason or "Access denied")
    plants = await get_accessible_plants(db, user.id)
    stmt = select(Equipment).where(Equipment.active == True)
    if plants is not None:
        if not plants:
            return []
        stmt = stmt.where(Equipment.plantId.in_(plants))
    rows = (await db.execute(stmt.order_by(Equipment.name))).scalars().all()
    return [
        {
            "id": r.id,
            "code": r.code,
            "name": r.name,
            "plantId": r.plantId,
            "frequency": r.frequency,
            "category": r.category,
            "location": r.location,
            "checklistTemplate": r.checklistTemplate,
        }
        for r in rows
    ]


@router.get("")
async def list_inspections(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    read_check = await can(db, user.id, "INSPECTION.READ", PermissionContext())
    if not read_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, read_check.reason or "Access denied")
    plants = await get_accessible_plants(db, user.id)
    stmt = select(Inspection)
    if plants is None:
        pass
    elif not plants:
        return {"items": [], "total": 0}
    else:
        # Inspection has plant via Equipment — narrow by joining
        eq_ids_q = select(Equipment.id).where(Equipment.plantId.in_(plants))
        stmt = stmt.where(Inspection.equipmentId.in_(eq_ids_q))
    rows = (await db.execute(stmt.order_by(Inspection.scheduledDate.desc()).limit(200))).scalars().all()
    return {"items": [InspectionOut.model_validate(r) for r in rows], "total": len(rows)}


@router.post("", response_model=InspectionOut, status_code=status.HTTP_201_CREATED)
async def create_inspection(
    payload: InspectionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InspectionOut:
    eq = await db.get(Equipment, payload.equipmentId)
    if eq is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid equipment")
    await require_permission_with_context("INSPECTION.CREATE", user, db, plant_id=eq.plantId)

    # The frontend sends a date-only string ("YYYY-MM-DD") which Pydantic
    # parses to a naive datetime. Comparing naive vs aware raises TypeError —
    # assume UTC for naive input so the bounds check works uniformly.
    scheduled = payload.scheduledDate
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    one_year_ago = datetime.now(timezone.utc) - timedelta(days=365)
    five_years_ahead = datetime.now(timezone.utc) + timedelta(days=365 * 5)
    if scheduled < one_year_ago or scheduled > five_years_ahead:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Scheduled date must be within the last year and five years ahead.")

    if payload.inspectorId:
        inspector = await db.get(User, payload.inspectorId)
        if inspector is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid inspector")

    last = (await db.execute(select(func.count()).select_from(Inspection))).scalar_one()
    number = f"INSP-{eq.code}-{last + 1:04d}"

    insp = Inspection(
        number=number,
        equipmentId=payload.equipmentId,
        plantId=eq.plantId,
        inspectorId=payload.inspectorId,
        scheduledDate=scheduled,
        status=InspectionStatus.SCHEDULED,
    )
    db.add(insp)
    await db.flush()
    await db.refresh(insp)
    try:
        async with db.begin_nested():
            await workflow_engine.initiate(
                db,
                module="INSPECTION",
                record_id=insp.id,
                record_number=insp.number,
                record_title=f"{eq.name} — scheduled {payload.scheduledDate.date()}",
                record_data={"equipmentId": eq.id, "inspectorId": payload.inspectorId, "plantId": eq.plantId},
                initiator_id=user.id,
                plant_id=eq.plantId,
            )
    except Exception as e:  # noqa: BLE001
        import sys
        import traceback
        print(f"Inspection workflow init failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    return InspectionOut.model_validate(insp)


@router.delete("/{inspection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_inspection(
    inspection_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hard-delete an inspection. Per the RBAC matrix only HSE_MANAGER
    (own plant) and SYSTEM_ADMIN have INSPECTION.DELETE."""
    insp = await db.get(Inspection, inspection_id)
    if insp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Inspection not found")
    eq = await db.get(Equipment, insp.equipmentId)
    record = {"inspectorId": insp.inspectorId}
    result = await can(
        db,
        user.id,
        "INSPECTION.DELETE",
        PermissionContext(record_id=insp.id, plant_id=eq.plantId if eq else None, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    await db.delete(insp)
    await db.flush()


@router.patch("/{inspection_id}", response_model=InspectionOut)
async def update_inspection(
    inspection_id: str,
    payload: InspectionUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InspectionOut:
    insp = await db.get(Inspection, inspection_id)
    if insp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    # "Completed" at the inspection-record level means the inspector submitted
    # a result, but the workflow may still be in verification. We only want to
    # forbid edits AFTER the workflow has fully concluded (COMPLETED workflow
    # status). Without this check the user gets stuck if step 1 of the two-
    # step submit (PATCH + workflow advance) succeeded at the DB but the
    # response failed — the inspection is marked COMPLETED, the workflow is
    # still IN_PROGRESS, and re-submission is locked out.
    if insp.status == InspectionStatus.COMPLETED:
        from app.models.workflow import WorkflowInstance
        from sqlalchemy import and_
        wf = (
            await db.execute(
                select(WorkflowInstance).where(
                    and_(
                        WorkflowInstance.module == "INSPECTION",
                        WorkflowInstance.recordId == insp.id,
                    )
                )
            )
        ).scalar_one_or_none()
        if wf is not None and wf.status == "COMPLETED":
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Cannot edit a completed inspection.",
            )
    eq = await db.get(Equipment, insp.equipmentId)
    record = {"inspectorId": insp.inspectorId}
    ctx = PermissionContext(record_id=insp.id, plant_id=eq.plantId if eq else None, record=record)

    # Recording inspection results (checklistResult / result / observations /
    # followUpRequired) is the inspector's job and only requires
    # INSPECTION.EXECUTE — the same permission the workflow's "Inspector
    # Executes Checklist" step gates on. Mutating administrative fields
    # (inspectorId / scheduledDate) still requires the broader UPDATE grant
    # since that's a supervisor-level reassign / reschedule action.
    is_admin_edit = payload.inspectorId is not None or payload.scheduledDate is not None
    perm_code = "INSPECTION.UPDATE" if is_admin_edit else "INSPECTION.EXECUTE"
    result = await can(db, user.id, perm_code, ctx)
    if not result.allowed and not is_admin_edit:
        # Fall back to UPDATE — some roles (HSE Manager, etc.) hold UPDATE
        # but not EXECUTE; we shouldn't lock them out of recording results.
        result = await can(db, user.id, "INSPECTION.UPDATE", ctx)
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    if payload.inspectorId is not None:
        if payload.inspectorId:
            new_insp_user = await db.get(User, payload.inspectorId)
            if new_insp_user is None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid inspector")
            insp.inspectorId = payload.inspectorId
        else:
            insp.inspectorId = None
    if payload.scheduledDate is not None:
        insp.scheduledDate = payload.scheduledDate
    if payload.checklistResult is not None:
        insp.checklistResult = payload.checklistResult or None
    if payload.result is not None:
        if payload.result not in VALID_RESULTS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Result must be one of {sorted(VALID_RESULTS)}.")
        insp.result = payload.result
        insp.completedDate = datetime.now(timezone.utc)
        insp.status = InspectionStatus.COMPLETED
    if payload.observations is not None:
        insp.observations = payload.observations or None
    if payload.followUpRequired is not None:
        insp.followUpRequired = payload.followUpRequired
    await db.flush()
    # Without refresh, attributes that SQLAlchemy marks as expired after a
    # flush (notably onupdate columns like updatedAt) trigger an implicit
    # lazy load when Pydantic reads them — but the greenlet context is gone
    # by then, raising MissingGreenlet. Refresh forces the load inside the
    # async context so model_validate gets concrete values.
    await db.refresh(insp)
    return InspectionOut.model_validate(insp)
