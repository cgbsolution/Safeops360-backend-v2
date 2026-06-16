"""Near Miss router. Production-depth refactor (Commit 2 of 5).

Adds:
  • System-side processing on submission
        - repeat detection (same plant + area, last 30 days, ≥2 prior)
        - active-permit detection (same plant + area + within permit window)
        - contractor auto-link (if any person involved is contractor)
        - SLA target computed from severity
        - autoPromoteToIncident flag set when potentialSeverity = CRITICAL
          (the actual Incident record gets created in Commit 3 by the
          auto-promotion service together with notifications)
  • Children persistence (personsInvolved / personsPotentiallyAffected /
    witnesses) inline in the create payload.
  • Two-phase attachment endpoints (init / complete / list / download / delete)
    mirroring the Observation pattern.
  • Masters fetch endpoints (departments, contractor companies, masterItem
    by type) so the form's selectors can populate.
"""

from __future__ import annotations

import re
import secrets
import sys
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.masters import Department, MasterItem
from app.models.epc import ContractorCompany
from app.models.near_miss import NearMiss, NearMissStatus
from app.models.near_miss_children import (
    NearMissAttachment,
    NearMissPersonAffected,
    NearMissPersonInvolved,
    NearMissWitness,
)
from app.models.observation import Severity
from app.models.permit import Permit
from app.models.plant import Plant
from app.models.user import User
from app.schemas.near_miss import (
    ContractorCompanyOut,
    DepartmentOut,
    MasterListItem,
    NearMissCreate,
    NearMissOut,
    NearMissUpdate,
)
from app.services import workflow_engine
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
    get_module_scopes,
)
from app.services.storage import (
    create_signed_download_url,
    create_signed_upload_url,
    is_storage_configured,
)

router = APIRouter(prefix="/api/near-miss", tags=["near-miss"])


# ─── Risk matrix → level mapping (5×5 standard) ────────────────────────

_RISK_LEVELS: dict[int, str] = {}
for likelihood in range(1, 6):
    for consequence in range(1, 6):
        score = likelihood * consequence
        if score >= 15:
            level = "CRITICAL"
        elif score >= 9:
            level = "HIGH"
        elif score >= 4:
            level = "MEDIUM"
        else:
            level = "LOW"
        _RISK_LEVELS[score] = level


def _compute_risk(likelihood: int | None, consequence: int | None) -> tuple[int | None, str | None]:
    if likelihood is None or consequence is None:
        return None, None
    score = likelihood * consequence
    return score, _RISK_LEVELS.get(score)


# ─── SLA target by severity ────────────────────────────────────────────

_SLA_HOURS_BY_SEVERITY: dict[Severity, int] = {
    Severity.CRITICAL: 24,
    Severity.HIGH: 48,
    Severity.MEDIUM: 168,  # 7 days
    Severity.LOW: 336,  # 14 days
}


# ─── Detection helpers ────────────────────────────────────────────────


async def _detect_repeats(db: AsyncSession, plant_id: str, area_id: str | None, exclude_id: str | None) -> list[str]:
    """Find near-miss IDs in same plant + area in the last 30 days."""
    if area_id is None:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    stmt = (
        select(NearMiss.id)
        .where(NearMiss.plantId == plant_id, NearMiss.areaId == area_id, NearMiss.date >= cutoff)
    )
    if exclude_id:
        stmt = stmt.where(NearMiss.id != exclude_id)
    result = await db.execute(stmt)
    return [row[0] for row in result.all()]


async def _detect_active_permit(db: AsyncSession, plant_id: str, area_id: str | None, when: datetime) -> str | None:
    """Find an active permit covering the same plant+area at the event time.
    PermitStatus enum values (per Prisma): DRAFT, SUBMITTED, ACTIVE, EXPIRED,
    CLOSED, REJECTED — only ACTIVE and SUBMITTED count as 'live' for the
    near-miss permit-conflict rule."""
    if area_id is None:
        return None
    stmt = (
        select(Permit.id)
        .where(
            Permit.plantId == plant_id,
            Permit.areaId == area_id,
            Permit.validFrom <= when,
            Permit.validTo >= when,
            Permit.status.in_(["ACTIVE", "SUBMITTED"]),
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    row = result.first()
    return row[0] if row else None


# ─── List ─────────────────────────────────────────────────────────────


@router.get("")
async def list_near_misses(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    read_check = await can(db, user.id, "NEAR_MISS.READ", PermissionContext())
    if not read_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, read_check.reason or "Access denied")
    plants = await get_accessible_plants(db, user.id)
    stmt = select(NearMiss)
    if plants is None:
        pass
    elif not plants:
        return {"items": [], "total": 0}
    else:
        stmt = stmt.where(NearMiss.plantId.in_(plants))
    if read_check.matched_scope == "OWN_RECORDS":
        stmt = stmt.where(or_(NearMiss.reporterId == user.id, NearMiss.actionOwnerId == user.id))
    rows = (await db.execute(stmt.order_by(NearMiss.date.desc()).limit(200))).scalars().all()
    return {"items": [NearMissOut.model_validate(r) for r in rows], "total": len(rows)}


# ─── Masters (used by the form's selectors) ───────────────────────────


@router.get("/masters/departments", response_model=list[DepartmentOut])
async def list_departments(
    plant_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DepartmentOut]:
    """RBAC-filtered department dropdown for the near-miss form.

    Plant-wide roles (HSE Manager, Plant Head, Corporate HSE, System Admin —
    anyone whose NEAR_MISS.READ scope is OWN_PLANT or ALL_PLANTS) see every
    department at the plant. Department- or record-restricted roles
    (Workers, Supervisors, Department Heads — OWN_DEPARTMENT / OWN_RECORDS)
    see only their own department, so a worker can't accidentally raise
    a near miss against another department's records.

    NB: we explicitly check the READ scope, not any scope. Every role has
    NEAR_MISS.CREATE at ALL_PLANTS (the "you can always raise a near miss"
    rule), so checking *any* scope would put every user in the plant-wide
    branch. READ is the canonical "what records can this user see"
    permission and the right driver for visibility filters."""

    scopes = await get_module_scopes(db, user.id, "NEAR_MISS.READ")
    is_plant_wide = "ALL_PLANTS" in scopes or "OWN_PLANT" in scopes

    stmt = select(Department).where(Department.active == True)
    if plant_id:
        stmt = stmt.where(Department.plantId == plant_id)

    if not is_plant_wide:
        # User restricted to their own department. The User.department
        # column is a free-text string ("IT", "Operations", etc.); resolve
        # it against Department.name on the same plant. If the user has no
        # department string (data quality issue) return an empty list — the
        # form will surface that as "no options" rather than leaking the
        # full master list to a low-privilege user.
        if user.department and user.plantId:
            stmt = stmt.where(
                Department.name == user.department,
                Department.plantId == user.plantId,
            )
        else:
            return []

    stmt = stmt.order_by(Department.name)
    rows = (await db.execute(stmt)).scalars().all()
    return [DepartmentOut.model_validate(r) for r in rows]


@router.get("/masters/contractors", response_model=list[ContractorCompanyOut])
async def list_contractors(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ContractorCompanyOut]:
    stmt = select(ContractorCompany).where(ContractorCompany.status == "ACTIVE").order_by(ContractorCompany.name)
    rows = (await db.execute(stmt)).scalars().all()
    return [ContractorCompanyOut.model_validate(r) for r in rows]


@router.get("/masters/equipment")
async def list_equipment(
    plant_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, str]]:
    """List active Equipment rows for the equipment selector on the
    near-miss form. Filtered to a plant when provided."""
    from app.models.equipment import Equipment

    stmt = select(Equipment).where(Equipment.active == True)
    if plant_id:
        stmt = stmt.where(Equipment.plantId == plant_id)
    stmt = stmt.order_by(Equipment.name)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {"id": r.id, "code": r.code, "name": r.name, "category": r.category, "location": r.location}
        for r in rows
    ]


@router.get("/masters/items", response_model=list[MasterListItem])
async def list_master_items(
    type: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MasterListItem]:
    stmt = (
        select(MasterItem)
        .where(MasterItem.type == type, MasterItem.active == True)
        .order_by(MasterItem.sortOrder, MasterItem.label)
    )
    rows = (await db.execute(stmt)).scalars().all()
    out: list[MasterListItem] = []
    for r in rows:
        out.append(
            MasterListItem(
                id=r.id,
                code=r.code,
                label=r.label,
                sortOrder=r.sortOrder,
                metadata=r.metadata_,
            )
        )
    return out


# ─── Create ───────────────────────────────────────────────────────────


@router.post("", response_model=NearMissOut, status_code=status.HTTP_201_CREATED)
async def create_near_miss(
    payload: NearMissCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NearMissOut:
    await require_permission_with_context("NEAR_MISS.CREATE", user, db, plant_id=payload.plantId)
    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")
    if payload.date.timestamp() > datetime.now(timezone.utc).timestamp() + 300:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Near-miss date cannot be in the future.")

    # Generate number per plant
    last = (
        await db.execute(
            select(func.count()).select_from(NearMiss).where(NearMiss.plantId == payload.plantId)
        )
    ).scalar_one()
    number = f"NM-{payload.date.year}-{plant.code}-{last + 1:04d}"

    # Compute risk + SLA
    risk_score, risk_level = _compute_risk(payload.riskLikelihood, payload.riskConsequence)
    sla_hours = _SLA_HOURS_BY_SEVERITY.get(payload.potentialSeverity, 168)
    sla_target = datetime.now(timezone.utc) + timedelta(hours=sla_hours)

    # Auto-detection
    similar_ids = await _detect_repeats(db, payload.plantId, payload.areaId, exclude_id=None)
    is_repeat = len(similar_ids) >= 2
    active_permit_id = await _detect_active_permit(db, payload.plantId, payload.areaId, payload.date)

    # ── FK sanity-check: drop any FK value the user supplied that doesn't ──
    # actually exist. Defensive against misbehaving / outdated frontends
    # sending free-text strings (e.g. "Electric wire cutter") for an FK
    # column. Postgres would otherwise reject the INSERT with
    # ForeignKeyViolationError → 500.
    from app.models.equipment import Equipment
    from app.models.masters import Department as _Dept, MasterItem as _MI

    async def _fk_exists(model: Any, fk: str | None) -> bool:
        if not fk:
            return False
        return (await db.get(model, fk)) is not None

    payload_equipment_id = payload.equipmentId if await _fk_exists(Equipment, payload.equipmentId) else None
    payload_department_id = payload.departmentId if await _fk_exists(_Dept, payload.departmentId) else None
    payload_contractor_id = payload.contractorCompanyId if await _fk_exists(ContractorCompany, payload.contractorCompanyId) else None
    payload_suggested_owner = payload.suggestedActionOwnerId if await _fk_exists(User, payload.suggestedActionOwnerId) else None
    # shiftId, hazardCategory, energySource, activityBeingPerformed all FK
    # by id to MasterItem (untyped — generic lookup). Validate.
    payload_shift_id = payload.shiftId if await _fk_exists(_MI, payload.shiftId) else None
    payload_hazard_cat = payload.hazardCategory if await _fk_exists(_MI, payload.hazardCategory) else None
    payload_energy_src = payload.energySource if await _fk_exists(_MI, payload.energySource) else None
    payload_activity = payload.activityBeingPerformed if await _fk_exists(_MI, payload.activityBeingPerformed) else None

    contractor_company_id = payload_contractor_id

    # CRITICAL severity → flag for auto-promotion. The actual Incident
    # record + SMS/email notifications land in Commit 3.
    auto_promote = payload.potentialSeverity == Severity.CRITICAL

    # Legacy CSV from new structured potentialConsequences for back-compat
    legacy_consequence = payload.potentialConsequence
    if legacy_consequence is None and payload.potentialConsequences:
        legacy_consequence = ", ".join(c.type for c in payload.potentialConsequences)

    # Reporter type — if anonymous flag is set, mark accordingly
    reporter_type = payload.reporterType or ("ANONYMOUS" if payload.isAnonymous else "EMPLOYEE")

    nm = NearMiss(
        number=number,
        date=payload.date,
        plantId=payload.plantId,
        areaId=payload.areaId,
        reporterId=user.id,
        description=payload.description,
        location=payload.location,
        specificLocation=payload.specificLocation,
        gpsLatitude=payload.gpsLatitude,
        gpsLongitude=payload.gpsLongitude,
        departmentId=payload_department_id,
        shiftId=payload_shift_id,
        reporterType=reporter_type,
        isAnonymous=payload.isAnonymous,
        activityBeingPerformed=payload_activity,
        activityIsRoutine=payload.activityIsRoutine,
        activity=payload.activity,
        immediateAction=payload.immediateAction,
        equipmentId=payload_equipment_id,
        contractorCompanyId=contractor_company_id,
        potentialSeverity=payload.potentialSeverity,
        potentialConsequence=legacy_consequence,
        potentialConsequences=[c.model_dump(exclude_none=True) for c in (payload.potentialConsequences or [])] or None,
        multipleWorkersAggravator=payload.multipleWorkersAggravator,
        hazardCategory=payload_hazard_cat,
        energySource=payload_energy_src,
        riskLikelihood=payload.riskLikelihood,
        riskConsequence=payload.riskConsequence,
        riskScore=risk_score,
        riskLevel=risk_level,
        initialRootCauseCategory=payload.initialRootCauseCategory,
        controlsThatFailed=payload.controlsThatFailed,
        controlsThatWorked=payload.controlsThatWorked,
        recommendedActions=payload.recommendedActions,
        suggestedActionOwnerId=payload_suggested_owner,
        isRepeat=is_repeat,
        activePermitId=active_permit_id,
        permitReviewFlagged=bool(active_permit_id),
        autoPromoteToIncident=auto_promote,
        slaTargetAt=sla_target,
        status=NearMissStatus.REPORTED,
    )
    db.add(nm)
    await db.flush()

    # Persist child records (persons involved / affected / witnesses)
    if payload.personsInvolved:
        for p in payload.personsInvolved:
            db.add(NearMissPersonInvolved(nearMissId=nm.id, userId=p.userId, role=p.role))
    if payload.personsPotentiallyAffected:
        for p in payload.personsPotentiallyAffected:
            db.add(NearMissPersonAffected(nearMissId=nm.id, userId=p.userId, proximityToHazard=p.proximityToHazard))
    if payload.witnesses:
        for p in payload.witnesses:
            db.add(NearMissWitness(nearMissId=nm.id, witnessId=p.userId, statementCaptured=p.statementCaptured))

    await db.flush()
    await db.refresh(nm)

    # Initiate workflow inside a SAVEPOINT so a partial init failure can't
    # poison the transaction (matches the pattern used in observations.py).
    try:
        async with db.begin_nested():
            await workflow_engine.initiate(
                db,
                module="NEAR_MISS",
                record_id=nm.id,
                record_number=nm.number,
                record_title=nm.description[:120],
                record_data={
                    "potentialSeverity": nm.potentialSeverity.value,
                    "plantId": nm.plantId,
                    "reporterId": nm.reporterId,
                    "departmentId": nm.departmentId,
                    "isCritical": auto_promote,
                },
                initiator_id=user.id,
                plant_id=nm.plantId,
            )
    except Exception as e:  # noqa: BLE001
        print(f"Near-miss workflow init failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    # NB: auto-promotion to Incident does NOT fire at submit anymore. The
    # current rule is "promote when Joint Review approves on a CRITICAL
    # near miss" — the post-step hook in workflow_engine._advance handles
    # that. The near-miss workflow continues running in parallel with the
    # spawned incident investigation. The autoPromoteToIncident column
    # stays True for CRITICAL severity here so the workflow tracker UI
    # can show "will be promoted on Joint Review approval".

    await db.refresh(nm)
    return NearMissOut.model_validate(nm)


# ─── Manual promote (backfill / admin override) ──────────────────────


@router.post("/{nm_id}/promote-to-incident")
async def manual_promote_to_incident(
    nm_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Manually trigger near-miss → incident auto-promotion. Used to
    backfill records that should have auto-promoted at submit time but
    didn't (e.g. earlier transient bugs), and as an admin override when
    a non-Critical near miss is later judged to warrant investigation.
    Idempotent: returns the existing incident id if already promoted.
    Permission gated to HSE_MANAGER / CORPORATE_HSE / SYSTEM_ADMIN."""
    nm = await db.get(NearMiss, nm_id)
    if nm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Near miss not found")
    record = {"reporterId": nm.reporterId, "actionOwnerId": nm.actionOwnerId}
    result = await can(
        db, user.id, "NEAR_MISS.APPROVE",
        PermissionContext(record_id=nm.id, plant_id=nm.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    if nm.promotedIncidentId:
        return {"ok": True, "alreadyPromoted": True, "incidentId": nm.promotedIncidentId}

    from app.services.auto_promote_near_miss import promote_near_miss_to_incident

    try:
        incident_id = await promote_near_miss_to_incident(
            db, near_miss_id=nm.id, actor_id=user.id
        )
        await db.flush()
    except Exception as e:  # noqa: BLE001
        await db.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Could not promote near miss to incident: {str(e)[:200]}",
        ) from e
    return {"ok": True, "alreadyPromoted": False, "incidentId": incident_id}


# ─── Delete ───────────────────────────────────────────────────────────


@router.delete("/{nm_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_near_miss(
    nm_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hard-delete a near miss. Per the RBAC matrix only HSE_MANAGER (own
    plant), CORPORATE_HSE (all plants) and SYSTEM_ADMIN (all plants)
    have NEAR_MISS.DELETE — the permission service enforces the scope.
    Cascades the workflow chain + child rows (persons / CAPAs /
    attachments / comments) via FK ondelete=CASCADE.

    Blocked when the near miss has been auto-promoted to an Incident
    (NearMiss.promotedIncidentId is set) — the spawned Incident has
    independent audit weight and shouldn't be orphaned.
    """
    nm = await db.get(NearMiss, nm_id)
    if nm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Near miss not found")

    record = {"reporterId": nm.reporterId, "actionOwnerId": nm.actionOwnerId}
    result = await can(
        db, user.id, "NEAR_MISS.DELETE",
        PermissionContext(record_id=nm.id, plant_id=nm.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    # Block when this near miss has been auto-promoted to an Incident.
    # The FK lives on NearMiss.promotedIncidentId (1-to-1 to Incident);
    # if it's set, deleting the NM would orphan/cascade the linked
    # incident, so refuse and tell the user to close the incident first.
    if nm.promotedIncidentId:
        from app.models.incident import Incident

        target_num = nm.promotedIncidentId
        linked_incident = await db.get(Incident, nm.promotedIncidentId)
        if linked_incident is not None:
            target_num = linked_incident.number
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"This near miss is linked to incident {target_num} and cannot be deleted. Cancel/close the incident first.",
        )

    # Wrap the whole cleanup phase in one try/except so ANY database
    # failure (FK violation, schema drift, stale row, etc.) is returned
    # to the caller as a readable {detail} message instead of a raw
    # 500 Postgres traceback. The full error is still logged server-side
    # for debugging.
    try:
        # Drop the workflow instance(s) first — Prisma's FK cascade on
        # WorkflowInstance handles WorkflowHistory + WorkflowTask, but
        # NearMiss doesn't FK into WorkflowInstance, so we delete it
        # explicitly. Child rows on NearMiss (CAPAs, persons,
        # attachments, comments) cascade via their FKs.
        from app.models.workflow import WorkflowInstance

        inst_rows = (
            await db.execute(
                select(WorkflowInstance).where(
                    WorkflowInstance.module == "NEAR_MISS",
                    WorkflowInstance.recordId == nm_id,
                )
            )
        ).scalars().all()
        for inst in inst_rows:
            await db.delete(inst)

        # Soft-delete attachments via a CORE-level UPDATE (not ORM
        # mutation). An ORM-tracked `att.deletedAt = now` would queue
        # an UPDATE that SQLAlchemy emits AFTER the parent NearMiss
        # DELETE; Postgres' ON DELETE CASCADE nukes the attachment rows
        # first, then the queued UPDATE matches 0 rows → StaleDataError.
        # The bulk UPDATE below executes immediately and bypasses
        # identity-map tracking.
        from sqlalchemy import update

        now = datetime.now(timezone.utc)
        await db.execute(
            update(NearMissAttachment)
            .where(
                NearMissAttachment.nearMissId == nm_id,
                NearMissAttachment.deletedAt.is_(None),
            )
            .values(deletedAt=now)
            .execution_options(synchronize_session=False)
        )
        await db.flush()

        await db.delete(nm)
        await db.flush()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        import sys, traceback
        print(f"[near-miss delete] {nm_id} failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        await db.rollback()
        # Translate the technical error into something the user can act
        # on. FK violations get the most common-sense message; anything
        # else gets a generic "couldn't delete" with a short hint.
        msg = str(e)
        low = msg.lower()
        if "foreignkeyviolation" in low or "violates foreign key" in low:
            user_msg = (
                "Could not delete this near miss — another record (incident, "
                "CAPA, or audit row) still references it. Close or unlink "
                "those first, then retry."
            )
        elif "staledata" in low:
            user_msg = (
                "Could not delete this near miss — its child rows changed "
                "during cleanup. Refresh the page and try again."
            )
        elif "undefinedcolumn" in low or "does not exist" in low:
            user_msg = (
                "Could not delete this near miss — the database schema is "
                "out of sync with the application. Contact the administrator."
            )
        else:
            user_msg = "Could not delete this near miss. Please try again or contact support."
        raise HTTPException(status.HTTP_400_BAD_REQUEST, user_msg) from e
    return None


# ─── Get / Update ─────────────────────────────────────────────────────


@router.get("/{nm_id}", response_model=NearMissOut)
async def get_near_miss(
    nm_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NearMissOut:
    nm = await db.get(NearMiss, nm_id)
    if nm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    record = {"reporterId": nm.reporterId, "actionOwnerId": nm.actionOwnerId}
    result = await can(
        db, user.id, "NEAR_MISS.READ",
        PermissionContext(record_id=nm.id, plant_id=nm.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    return NearMissOut.model_validate(nm)


@router.patch("/{nm_id}", response_model=NearMissOut)
async def update_near_miss(
    nm_id: str,
    payload: NearMissUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NearMissOut:
    nm = await db.get(NearMiss, nm_id)
    if nm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if nm.status == NearMissStatus.CLOSED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot edit a closed near miss.")
    record = {"reporterId": nm.reporterId, "actionOwnerId": nm.actionOwnerId}
    result = await can(
        db, user.id, "NEAR_MISS.UPDATE",
        PermissionContext(record_id=nm.id, plant_id=nm.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    if payload.actionOwnerId is not None:
        if payload.actionOwnerId:
            owner = await db.get(User, payload.actionOwnerId)
            if owner is None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid action owner")
            nm.actionOwnerId = payload.actionOwnerId
        else:
            nm.actionOwnerId = None
    if payload.correctiveActions is not None:
        nm.correctiveActions = payload.correctiveActions or None
    if payload.rootCauseCategory is not None:
        nm.rootCauseCategory = payload.rootCauseCategory or None
    if payload.rootCauseDetail is not None:
        nm.rootCauseDetail = payload.rootCauseDetail or None
    if payload.targetDate is not None:
        # The frontend posts a date-only string → naive datetime. Coerce to
        # UTC so the comparison with an aware datetime.now() doesn't raise.
        target = payload.targetDate
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        if target < datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Target closure date cannot be in the past.")
        nm.targetDate = target
    await db.flush()
    await db.refresh(nm)
    return NearMissOut.model_validate(nm)


# ─── Attachments ─────────────────────────────────────────────────────
# Mirror of the Observation 2-phase pattern. NEAR_MISS bucket reuses the
# incident bucket (same Supabase project) — categorised by path prefix.

VALID_NM_CATEGORIES = {
    "INITIAL_PHOTO",
    "WITNESS_STATEMENT",
    "EVIDENCE",
    "CAPA_EVIDENCE",
    "VERIFICATION_PHOTO",
}
ALLOWED_NM_MIME = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic",
    "video/mp4", "video/quicktime",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/csv", "text/plain",
}
MAX_NM_FILE_SIZE = 50 * 1024 * 1024


def _build_nm_storage_path(*, near_miss_id: str, category: str, file_name: str) -> str:
    safe = re.sub(r"[\\/]", "_", file_name)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", safe)[:80]
    return f"near-miss/{near_miss_id}/{category.lower()}/{secrets.token_hex(4)}-{safe}"


def _attachment_to_dict(a: NearMissAttachment) -> dict[str, Any]:
    uploaded_by = None
    if a.uploadedBy is not None:
        uploaded_by = {
            "id": a.uploadedBy.id,
            "name": a.uploadedBy.name,
            "designation": a.uploadedBy.designation,
        }
    return {
        "id": a.id,
        "nearMissId": a.nearMissId,
        "category": a.category,
        "fileName": a.fileName,
        "fileSize": a.fileSize,
        "mimeType": a.mimeType,
        "caption": a.caption,
        "exifData": a.exifData,
        "uploadedAt": a.uploadedAt,
        "uploadedById": a.uploadedById,
        "uploadedBy": uploaded_by,
    }


async def _is_workflow_actor(db: AsyncSession, user_id: str, near_miss_id: str) -> bool:
    """True if the caller has any WorkflowTask (pending or completed) for
    this near miss. Lets reviewers / executors read attachments."""
    from app.models.workflow import WorkflowTask

    stmt = (
        select(WorkflowTask.id)
        .where(WorkflowTask.module == "NEAR_MISS")
        .where(WorkflowTask.recordId == near_miss_id)
        .where(WorkflowTask.assignedToId == user_id)
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def _has_uploaded_attachment(db: AsyncSession, user_id: str, near_miss_id: str) -> bool:
    """True if the caller uploaded at least one (non-deleted) attachment to
    this near miss. Whoever contributes evidence must always be able to see
    it back in the gallery — even a reporter who only holds NEAR_MISS.CREATE
    (no READ grant) and is no longer the pending workflow actor."""
    stmt = (
        select(NearMissAttachment.id)
        .where(NearMissAttachment.nearMissId == near_miss_id)
        .where(NearMissAttachment.uploadedById == user_id)
        .where(NearMissAttachment.deletedAt.is_(None))
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


@router.get("/{nm_id}/attachments")
async def list_attachments(
    nm_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    nm = await db.get(NearMiss, nm_id)
    if nm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Near miss not found")
    record = {"reporterId": nm.reporterId, "actionOwnerId": nm.actionOwnerId}
    result = await can(
        db, user.id, "NEAR_MISS.READ",
        PermissionContext(record_id=nm.id, plant_id=nm.plantId, record=record),
    )
    # The reporter and anyone who uploaded evidence here can always see the
    # gallery, even without a NEAR_MISS.READ grant — mirrors the upload-side
    # bypass so an uploader never loses sight of their own contribution.
    if (
        not result.allowed
        and nm.reporterId != user.id
        and not await _is_workflow_actor(db, user.id, nm_id)
        and not await _has_uploaded_attachment(db, user.id, nm_id)
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    rows = (
        await db.execute(
            select(NearMissAttachment)
            .options(selectinload(NearMissAttachment.uploadedBy))
            .where(NearMissAttachment.nearMissId == nm_id)
            .where(NearMissAttachment.deletedAt.is_(None))
            .order_by(NearMissAttachment.uploadedAt.desc())
        )
    ).scalars().all()
    return {"items": [_attachment_to_dict(r) for r in rows]}


@router.post("/{nm_id}/attachments")
async def upload_attachment(
    nm_id: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    nm = await db.get(NearMiss, nm_id)
    if nm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Near miss not found")
    record = {"reporterId": nm.reporterId, "actionOwnerId": nm.actionOwnerId}
    result = await can(
        db, user.id, "NEAR_MISS.UPDATE",
        PermissionContext(record_id=nm.id, plant_id=nm.plantId, record=record),
    )
    if not result.allowed:
        # Reporters can attach to their own record even before workflow assigns
        if nm.reporterId != user.id and not await _is_workflow_actor(db, user.id, nm_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    if not is_storage_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Supabase Storage isn't configured. Set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.",
        )

    phase = payload.get("phase")
    if phase == "init":
        category = str(payload.get("category") or "")
        file_name = str(payload.get("fileName") or "").strip()
        file_size = int(payload.get("fileSize") or 0)
        mime_type = str(payload.get("mimeType") or "")
        if not file_name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "File name is required")
        if category not in VALID_NM_CATEGORIES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Invalid category. Must be one of: {', '.join(sorted(VALID_NM_CATEGORIES))}",
            )
        if file_size <= 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "File size must be a positive number")
        if file_size > MAX_NM_FILE_SIZE:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"File size exceeds the {MAX_NM_FILE_SIZE // 1024 // 1024} MB limit.",
            )
        if mime_type not in ALLOWED_NM_MIME:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"MIME type {mime_type} is not allowed.")

        storage_path = _build_nm_storage_path(near_miss_id=nm_id, category=category, file_name=file_name)
        try:
            signed = create_signed_upload_url(storage_path)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"Storage upload init failed: {e}",
            ) from e

        att = NearMissAttachment(
            nearMissId=nm_id,
            category=category,
            fileName=file_name,
            storagePath=storage_path,
            fileSize=file_size,
            mimeType=mime_type,
            uploadedById=user.id,
        )
        db.add(att)
        await db.flush()
        return {
            "phase": "init",
            "attachmentId": att.id,
            "storagePath": storage_path,
            "uploadUrl": signed["uploadUrl"],
            "token": signed["token"],
        }

    if phase == "complete":
        attachment_id = payload.get("attachmentId")
        if not attachment_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "attachmentId required")
        att = await db.get(NearMissAttachment, attachment_id)
        if att is None or att.nearMissId != nm_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found for this near miss")
        att.caption = payload.get("caption")
        att.exifData = payload.get("exifData")
        await db.flush()
        return {"ok": True}

    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown phase")


@router.delete("/{nm_id}/attachments/{attachment_id}")
async def delete_attachment(
    nm_id: str,
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    att = await db.get(NearMissAttachment, attachment_id)
    if att is None or att.nearMissId != nm_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    nm = await db.get(NearMiss, nm_id)
    is_uploader = att.uploadedById == user.id
    if not is_uploader:
        record = {
            "reporterId": nm.reporterId if nm else None,
            "actionOwnerId": nm.actionOwnerId if nm else None,
            "uploadedById": att.uploadedById,
        }
        result = await can(
            db, user.id, "NEAR_MISS.UPDATE",
            PermissionContext(record_id=att.id, plant_id=nm.plantId if nm else None, record=record),
        )
        if not result.allowed and not await _is_workflow_actor(db, user.id, nm_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    att.deletedAt = datetime.now(timezone.utc)
    await db.flush()
    return {"ok": True}


@router.get("/{nm_id}/attachments/{attachment_id}/download")
async def download_attachment(
    nm_id: str,
    attachment_id: str,
    inline: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    att = await db.get(NearMissAttachment, attachment_id)
    if att is None or att.nearMissId != nm_id or att.deletedAt is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    nm = await db.get(NearMiss, nm_id)
    # The uploader can always view their own file — they put it there. This
    # mirrors delete_attachment's is_uploader bypass and guarantees the person
    # who uploaded a photo can preview it even without a NEAR_MISS.READ grant.
    is_uploader = att.uploadedById == user.id
    record = {
        "reporterId": nm.reporterId if nm else None,
        "actionOwnerId": nm.actionOwnerId if nm else None,
        "uploadedById": att.uploadedById,
    }
    result = await can(
        db, user.id, "NEAR_MISS.READ",
        PermissionContext(record_id=nm.id if nm else None, plant_id=nm.plantId if nm else None, record=record),
    )
    if not result.allowed and not is_uploader and not await _is_workflow_actor(db, user.id, nm_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    url = create_signed_download_url(
        att.storagePath,
        expires_in_sec=300,
        download=None if inline else att.fileName,
    )
    return {"url": url}


# ─── CAPAs ─────────────────────────────────────────────────────────────
# Read-list is open to anyone who can read the parent near miss.
# Create: ONLY the actor assigned the step-3 "Review Meeting & CAPA Definition"
#         task — not every HSE Manager. The workflow assignment is the rule.
# PATCH: owner submits completion / verifier approves-or-rejects.

from app.models.near_miss_children import NearMissCapa, NearMissComment

# Step name (from the seeded workflow definition) at which CAPAs are defined.
# Kept in sync with the frontend gate in near-miss/[id]/page.tsx.
CAPA_DEFINITION_STEP = "Review Meeting & CAPA Definition"
_OPEN_TASK_STATUSES = ("PENDING", "OVERDUE", "ESCALATED")


async def _is_capa_definition_actor(db: AsyncSession, user_id: str, near_miss_id: str) -> bool:
    """True only if the caller currently holds the OPEN
    "Review Meeting & CAPA Definition" task for this near miss. That assignee
    is the single person allowed to define CAPAs — being an HSE Manager or
    having taken some earlier step in the workflow is not enough."""
    from app.models.workflow import WorkflowTask

    stmt = (
        select(WorkflowTask.id)
        .where(WorkflowTask.module == "NEAR_MISS")
        .where(WorkflowTask.recordId == near_miss_id)
        .where(WorkflowTask.assignedToId == user_id)
        .where(WorkflowTask.stepName == CAPA_DEFINITION_STEP)
        .where(WorkflowTask.status.in_(_OPEN_TASK_STATUSES))
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


def _capa_to_dict(c: NearMissCapa) -> dict[str, Any]:
    return {
        "id": c.id,
        "nearMissId": c.nearMissId,
        "description": c.description,
        "type": c.type,
        "ownerId": c.ownerId,
        "targetDate": c.targetDate,
        "status": c.status,
        "evidenceUrl": c.evidenceUrl,
        "evidenceDescription": c.evidenceDescription,
        "completionNotes": c.completionNotes,
        "completedAt": c.completedAt,
        "verifiedById": c.verifiedById,
        "verifiedAt": c.verifiedAt,
        "rejectionReason": c.rejectionReason,
        "reworkRound": c.reworkRound,
        "createdAt": c.createdAt,
        "updatedAt": c.updatedAt,
    }


@router.get("/{nm_id}/capas")
async def list_capas(
    nm_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    nm = await db.get(NearMiss, nm_id)
    if nm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Near miss not found")
    record = {"reporterId": nm.reporterId, "actionOwnerId": nm.actionOwnerId}
    result = await can(
        db, user.id, "NEAR_MISS.READ",
        PermissionContext(record_id=nm.id, plant_id=nm.plantId, record=record),
    )
    if not result.allowed and not await _is_workflow_actor(db, user.id, nm_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    rows = (
        await db.execute(
            select(NearMissCapa)
            .where(NearMissCapa.nearMissId == nm_id)
            .order_by(NearMissCapa.createdAt)
        )
    ).scalars().all()
    return {"items": [_capa_to_dict(r) for r in rows]}


@router.post("/{nm_id}/capas")
async def create_capa(
    nm_id: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The actor assigned the step-3 "Review Meeting & CAPA Definition" task
    defines the CAPAs. Restricted to that assignee — not every HSE Manager and
    not the reporter — so it follows the workflow assignment. Multiple CAPAs
    fan out into parallel execution tasks at the next step."""
    nm = await db.get(NearMiss, nm_id)
    if nm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Near miss not found")
    if not await _is_capa_definition_actor(db, user.id, nm_id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the reviewer assigned the Review Meeting & CAPA Definition step can define CAPAs.",
        )

    description = str(payload.get("description") or "").strip()
    capa_type = str(payload.get("type") or "CORRECTIVE").upper()
    owner_id = str(payload.get("ownerId") or "").strip()
    target_str = payload.get("targetDate")
    if not description:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "description required")
    if capa_type not in {"CORRECTIVE", "PREVENTIVE"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "type must be CORRECTIVE or PREVENTIVE")
    if not owner_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ownerId required")
    if not target_str:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "targetDate required")
    try:
        target = datetime.fromisoformat(str(target_str).replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid targetDate") from e

    capa = NearMissCapa(
        nearMissId=nm_id,
        description=description,
        type=capa_type,
        ownerId=owner_id,
        targetDate=target,
        status="PENDING",
    )
    db.add(capa)
    await db.flush()
    await db.refresh(capa)
    return _capa_to_dict(capa)


@router.patch("/{nm_id}/capas/{capa_id}")
async def update_capa(
    nm_id: str,
    capa_id: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Three callers:
      • CAPA owner submits — sets status=COMPLETED, evidenceUrl/Description,
        completionNotes, completedAt
      • Verifier approves — status=VERIFIED, verifiedById, verifiedAt
      • Verifier rejects — status=REJECTED, rejectionReason, reworkRound++
    """
    capa = await db.get(NearMissCapa, capa_id)
    if capa is None or capa.nearMissId != nm_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CAPA not found")
    nm = await db.get(NearMiss, nm_id)
    if nm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Near miss not found")

    action = str(payload.get("action") or "").upper()
    if action == "SUBMIT":
        if capa.ownerId != user.id and not await _is_workflow_actor(db, user.id, nm_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the CAPA owner can submit")
        capa.completionNotes = payload.get("completionNotes")
        capa.evidenceUrl = payload.get("evidenceUrl")
        capa.evidenceDescription = payload.get("evidenceDescription")
        capa.status = "COMPLETED"
        capa.completedAt = datetime.now(timezone.utc)
    elif action == "VERIFY":
        result = await can(
            db, user.id, "NEAR_MISS.VERIFY",
            PermissionContext(record_id=nm.id, plant_id=nm.plantId,
                              record={"reporterId": nm.reporterId, "actionOwnerId": nm.actionOwnerId}),
        )
        if not result.allowed and not await _is_workflow_actor(db, user.id, nm_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
        capa.status = "VERIFIED"
        capa.verifiedById = user.id
        capa.verifiedAt = datetime.now(timezone.utc)
    elif action == "REJECT":
        result = await can(
            db, user.id, "NEAR_MISS.VERIFY",
            PermissionContext(record_id=nm.id, plant_id=nm.plantId,
                              record={"reporterId": nm.reporterId, "actionOwnerId": nm.actionOwnerId}),
        )
        if not result.allowed and not await _is_workflow_actor(db, user.id, nm_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
        reason = str(payload.get("rejectionReason") or "").strip()
        if not reason:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "rejectionReason required")
        capa.status = "REJECTED"
        capa.rejectionReason = reason
        capa.reworkRound = (capa.reworkRound or 0) + 1
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "action must be SUBMIT | VERIFY | REJECT")

    await db.flush()
    await db.refresh(capa)
    return _capa_to_dict(capa)


# ─── AI assist: draft a CAPA-execution narrative ──────────────────────

_CAPA_EXECUTION_DRAFT_PROMPT = """You are SafeOps360's CAPA Execution assistant. \
A near-miss has been reviewed and a corrective/preventive action (CAPA) has been \
assigned to a frontline owner to carry out. Your job is to draft, in the FIRST \
PERSON, the "action narrative" that the action owner records when they submit \
their completed task — i.e. a concise, concrete account of what corrective action \
was taken to close out this near-miss, the result observed, and any follow-up.

You are given the near-miss facts as JSON (description, the activity, immediate \
action already taken, the reporter's recommended actions, which controls failed / \
worked, severity/risk, hazard, and any specific CAPAs assigned). Ground the draft \
in those facts. If the owner has typed something in "userTypedSoFar", build on it \
rather than discarding it.

Output JSON ONLY with EXACTLY these keys:
{
  "narrative": "<3-6 sentences, first person, past tense, specific and practical>",
  "evidenceDescription": "<one short line naming the evidence the owner should attach, e.g. 'Photo of installed guard + signed toolbox-talk register'>"
}

Rules:
  • Be specific and operational — name the actual control installed/changed, not generic safety platitudes.
  • Do NOT invent fabricated measurements, dates, names, or sign-offs. Describe the action, leave specifics for the owner to confirm.
  • This is a DRAFT for a human to review and edit before submitting. Keep it realistic and conservative.
  • Output JSON only. No prose around it."""


@router.post("/{nm_id}/capa-execution-draft")
async def ai_capa_execution_draft(
    nm_id: str,
    payload: dict[str, Any] | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """AI assist for the CAPA Execution step. Drafts a first-person action
    narrative (plus a one-line evidence suggestion) from the near-miss facts so
    the assignee starts from a reviewable draft instead of a blank box. Advisory
    only — the user edits before submitting. Fails SOFT: returns
    {"ok": False, "reason": ...} when AI is unconfigured or the call fails, so
    the UI just falls back to the manual flow (never a 500)."""
    import json as _json

    from app.core.config import get_settings
    from app.models.near_miss_children import NearMissCapa
    from app.services.ai.anthropic_client import complete_json, is_configured

    nm = await db.get(NearMiss, nm_id)
    if nm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Near miss not found")
    record = {"reporterId": nm.reporterId, "actionOwnerId": nm.actionOwnerId}
    result = await can(
        db, user.id, "NEAR_MISS.READ",
        PermissionContext(record_id=nm.id, plant_id=nm.plantId, record=record),
    )
    if not result.allowed and not await _is_workflow_actor(db, user.id, nm_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    if not is_configured():
        return {"ok": False, "reason": "AI assist is not configured on this server."}

    hint = ""
    if payload:
        hint = str(payload.get("hint") or payload.get("draft") or "").strip()

    capas = (
        await db.execute(
            select(NearMissCapa)
            .where(NearMissCapa.nearMissId == nm_id)
            .order_by(NearMissCapa.createdAt)
        )
    ).scalars().all()

    context = {
        "location": nm.specificLocation or nm.location,
        "description": nm.description,
        "activity": nm.activity or nm.activityBeingPerformed,
        "immediateActionTaken": nm.immediateAction,
        "reporterRecommendedActions": nm.recommendedActions,
        "controlsThatFailed": nm.controlsThatFailed,
        "controlsThatWorked": nm.controlsThatWorked,
        "potentialSeverity": getattr(nm.potentialSeverity, "value", nm.potentialSeverity),
        "riskLevel": nm.riskLevel,
        "hazardCategory": nm.hazardCategory,
        "energySource": nm.energySource,
        "assignedCapas": [{"description": c.description, "type": c.type} for c in capas],
        "userTypedSoFar": hint or None,
    }

    drafted = await complete_json(
        system=_CAPA_EXECUTION_DRAFT_PROMPT,
        user=_json.dumps(context, default=str, indent=2),
        max_tokens=600,
        temperature=0.3,
    )
    if drafted is None:
        return {"ok": False, "reason": "The AI draft could not be generated — please write the narrative manually."}

    narrative = str(drafted.get("narrative") or "").strip()
    evidence = str(drafted.get("evidenceDescription") or "").strip()
    if not narrative:
        return {"ok": False, "reason": "The AI returned an empty draft — please write the narrative manually."}
    return {
        "ok": True,
        "narrative": narrative,
        "evidenceDescription": evidence or None,
        "model": get_settings().anthropic_model,
    }


# ─── Comments ─────────────────────────────────────────────────────────


@router.get("/{nm_id}/comments")
async def list_comments(
    nm_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    nm = await db.get(NearMiss, nm_id)
    if nm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Near miss not found")
    record = {"reporterId": nm.reporterId, "actionOwnerId": nm.actionOwnerId}
    result = await can(
        db, user.id, "NEAR_MISS.READ",
        PermissionContext(record_id=nm.id, plant_id=nm.plantId, record=record),
    )
    if not result.allowed and not await _is_workflow_actor(db, user.id, nm_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    rows = (
        await db.execute(
            select(NearMissComment)
            .options(selectinload(NearMissComment.author))
            .where(NearMissComment.nearMissId == nm_id)
            .order_by(NearMissComment.createdAt)
        )
    ).scalars().all()
    return {
        "items": [
            {
                "id": c.id,
                "content": c.content,
                "createdAt": c.createdAt,
                "author": {
                    "id": c.author.id,
                    "name": c.author.name,
                    "designation": c.author.designation,
                } if c.author else None,
            }
            for c in rows
        ]
    }


@router.post("/{nm_id}/comments")
async def add_comment(
    nm_id: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    nm = await db.get(NearMiss, nm_id)
    if nm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Near miss not found")
    record = {"reporterId": nm.reporterId, "actionOwnerId": nm.actionOwnerId}
    result = await can(
        db, user.id, "NEAR_MISS.READ",
        PermissionContext(record_id=nm.id, plant_id=nm.plantId, record=record),
    )
    if not result.allowed and not await _is_workflow_actor(db, user.id, nm_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    content = str(payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "content required")
    if len(content) > 2000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "comment too long (max 2000)")
    c = NearMissComment(nearMissId=nm_id, authorId=user.id, content=content)
    db.add(c)
    await db.flush()
    await db.refresh(c)
    return {
        "id": c.id,
        "content": c.content,
        "createdAt": c.createdAt,
        "author": {"id": user.id, "name": user.name, "designation": user.designation},
    }
