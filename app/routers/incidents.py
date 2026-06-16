from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.incident import (
    Incident,
    IncidentAttachment,
    IncidentCapa,
    IncidentComment,
    IncidentDocumentReview,
    IncidentEquipment,
    IncidentEvidence,
    IncidentInvestigationMember,
    IncidentPerson,
    IncidentReclassification,
    IncidentStatus,
    IncidentTimelineEvent,
    IncidentType,
    IncidentWitnessStatement,
)
from app.models.observation import Observation
from app.models.permit import Permit
from app.models.plant import Plant
from app.models.user import User
from app.schemas.incident import (
    AttachmentInit,
    AttachmentOut,
    CommentInput,
    CommentOut,
    DocumentReviewInput,
    DocumentReviewOut,
    EquipmentOut,
    EquipmentUpdate,
    EvidenceInput,
    EvidenceOut,
    IncidentCapaInput,
    IncidentCapaOut,
    IncidentClassifyRequest,
    IncidentCreate,
    IncidentOut,
    IncidentReclassifyRequest,
    IncidentUpdate,
    PersonOut,
    PersonUpdate,
    StatutorySubmissionUpdate,
    TimelineEventInput,
    TimelineEventOut,
    WitnessStatementOut,
    WitnessStatementUpdate,
)
from app.services import workflow_engine
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
)
from app.services.rca import generate_rca_summary, normalise_rca_method
from app.services.storage import (
    build_storage_path,
    create_signed_download_url,
    create_signed_upload_url,
    is_storage_configured,
)

router = APIRouter(prefix="/api/incidents", tags=["incidents"])

VALID_CATEGORIES = {
    "INITIAL_PHOTO", "WITNESS_STATEMENT", "CCTV", "EQUIPMENT_DATA",
    "DOCUMENT", "SKETCH", "EXTERNAL_REPORT", "CAPA_EVIDENCE", "CLOSURE_DOC",
}
ALLOWED_MIME = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic",
    "video/mp4", "video/quicktime",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv", "text/plain",
}
MAX_FILE_SIZE = 50 * 1024 * 1024


@router.get("")
async def list_incidents(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    read_check = await can(db, user.id, "INCIDENT.READ", PermissionContext())
    if not read_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, read_check.reason or "Access denied")
    plants = await get_accessible_plants(db, user.id)
    stmt = select(Incident)
    if plants is None:
        pass
    elif not plants:
        return {"items": [], "total": 0}
    else:
        stmt = stmt.where(Incident.plantId.in_(plants))
    if read_check.matched_scope == "OWN_RECORDS":
        stmt = stmt.where(Incident.reporterId == user.id)
    rows = (await db.execute(stmt.order_by(Incident.date.desc()).limit(100))).scalars().all()
    return {"items": [IncidentOut.model_validate(r) for r in rows], "total": len(rows)}


# ─── Phase 1 helpers ─────────────────────────────────────────────────


# Initial-report SLA in minutes from occurrence. Per the brief, all incidents
# should be reported within 1 hour; the matrix below adjusts for severity in
# case post-classification reclassification needs to recompute.
_INITIAL_REPORT_SLA_MINUTES: dict[IncidentType, int] = {
    IncidentType.FIRST_AID: 60,
    IncidentType.MTC: 60,
    IncidentType.RWC: 60,
    IncidentType.LTI: 30,
    IncidentType.FATALITY: 15,
    IncidentType.PROPERTY_DAMAGE: 60,
    IncidentType.ENVIRONMENTAL: 60,
    IncidentType.FIRE: 30,
    IncidentType.PROCESS_SAFETY: 30,
    IncidentType.HIPO_NEAR_MISS: 60,
}


# Initial severity inferred from incident type. Plant HSE Manager refines
# this during Phase 2 classification with `classificationRationale`.
_INITIAL_SEVERITY: dict[IncidentType, str] = {
    IncidentType.FIRST_AID: "LOW",
    IncidentType.MTC: "MEDIUM",
    IncidentType.RWC: "MEDIUM",
    IncidentType.LTI: "HIGH",
    IncidentType.FATALITY: "CRITICAL",
    IncidentType.PROPERTY_DAMAGE: "MEDIUM",
    IncidentType.ENVIRONMENTAL: "HIGH",
    IncidentType.FIRE: "HIGH",
    IncidentType.PROCESS_SAFETY: "CRITICAL",
    IncidentType.HIPO_NEAR_MISS: "HIGH",
}


# Reportable status under Indian regulations. Computed at submit; HSE Manager
# can override during classification. LTI/Fatality always reportable;
# environmental/fire often reportable to CPCB; process-safety to DGFASLI.
def _initial_reportability(incident_type: IncidentType) -> tuple[bool, list[str]]:
    if incident_type in (IncidentType.LTI, IncidentType.FATALITY):
        return True, ["FACTORIES_ACT", "DGFASLI"]
    if incident_type == IncidentType.FIRE:
        return True, ["FACTORIES_ACT"]
    if incident_type == IncidentType.ENVIRONMENTAL:
        return True, ["CPCB"]
    if incident_type == IncidentType.PROCESS_SAFETY:
        return True, ["DGFASLI"]
    return False, []


async def _detect_active_permit(
    db: AsyncSession, plant_id: str, area_id: str | None, occurred_at: datetime
) -> str | None:
    """Find an active PTW that was running at the time of occurrence in
    the same plant + area. Used to auto-link the incident to the permit
    on Phase 1 submission."""

    if not area_id:
        return None
    from app.models.permit import PermitStatus

    stmt = (
        select(Permit.id)
        .where(Permit.plantId == plant_id)
        .where(Permit.areaId == area_id)
        .where(Permit.status.in_([PermitStatus.ACTIVE, PermitStatus.SUBMITTED]))
        .where(Permit.validFrom <= occurred_at)
        .where(Permit.validTo >= occurred_at)
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _detect_linked_observations(
    db: AsyncSession, plant_id: str, area_id: str | None
) -> list[str]:
    """Pull observation IDs from the same plant + area that were closed
    in the last 90 days. These become "missed warnings" linked on the
    incident detail page so the org can see signals it missed."""

    if not area_id:
        return []
    cutoff = datetime.now(timezone.utc).replace(microsecond=0)
    from datetime import timedelta as _td

    cutoff = cutoff - _td(days=90)

    # Observation has a `status` enum and `closedAt` column. Closed
    # observations sit at status=CLOSED with closedAt populated.
    stmt = (
        select(Observation.id)
        .where(Observation.plantId == plant_id)
        .where(Observation.areaId == area_id)
        .where(Observation.closedAt.isnot(None))
        .where(Observation.closedAt >= cutoff)
        .order_by(Observation.closedAt.desc())
        .limit(20)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


def _fk_exists_factory(db: AsyncSession):
    """Returns an `_fk_exists(model, fk)` async helper that returns True
    iff a row with that PK exists. Defensive against frontends sending
    stale or free-text values for FK columns."""

    async def _check(model: Any, fk: str | None) -> bool:
        if not fk:
            return False
        return (await db.execute(select(model).where(model.id == fk).limit(1))).scalar_one_or_none() is not None

    return _check


@router.post("", response_model=IncidentOut, status_code=status.HTTP_201_CREATED)
async def create_incident(
    payload: IncidentCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IncidentOut:
    """Phase 1 Initial Report. The first responder fills out a fast
    multi-section form (When / Where / What / Who / Photos) within ~1
    hour of occurrence. This endpoint persists the incident, auto-detects
    cross-module linkages (active PTW, source Near Miss, "missed warning"
    observations), spins up the workflow, and notifies the HSE Manager."""

    await require_permission_with_context("INCIDENT.CREATE", user, db, plant_id=payload.plantId)
    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")

    occurred_at = payload.occurredAt or payload.date
    reported_at = datetime.now(timezone.utc)
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    if occurred_at.timestamp() > reported_at.timestamp() + 300:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Incident occurrence cannot be in the future.")

    reporting_delay_min = max(0, int((reported_at - occurred_at).total_seconds() / 60))
    sla_minutes = _INITIAL_REPORT_SLA_MINUTES.get(payload.type, 60)
    initial_report_sla_target = occurred_at + timedelta(minutes=sla_minutes)

    is_reportable, reportable_under = _initial_reportability(payload.type)
    initial_severity = _INITIAL_SEVERITY.get(payload.type)

    # ─── FK sanity-check: drop user-supplied values that don't resolve
    # to real rows. Defensive against outdated frontends. ──────────────
    fk_exists = _fk_exists_factory(db)
    from app.models.equipment import Equipment
    from app.models.masters import Department as _Dept
    from app.models.near_miss import NearMiss as _NM

    department_id = payload.departmentId if await fk_exists(_Dept, payload.departmentId) else None
    source_nm_id = payload.sourceNearMissId if await fk_exists(_NM, payload.sourceNearMissId) else None
    active_permit_id = payload.activePermitId if await fk_exists(Permit, payload.activePermitId) else None

    # If the form didn't pre-pick a permit, try server-side detection.
    if not active_permit_id:
        active_permit_id = await _detect_active_permit(db, payload.plantId, payload.areaId, occurred_at)

    rca_method = normalise_rca_method(payload.rootCauseMethod)
    rca_data = payload.rootCauseData
    rca_summary = generate_rca_summary(rca_method, rca_data) if rca_method else None

    # Linked observations — "missed warnings" in the same area, last 90 days
    linked_observation_ids = await _detect_linked_observations(db, payload.plantId, payload.areaId)

    last = (
        await db.execute(select(func.count()).select_from(Incident).where(Incident.plantId == payload.plantId))
    ).scalar_one()
    number = f"INC-{occurred_at.year}-{plant.code}-{last + 1:04d}"

    incident = Incident(
        number=number,
        date=payload.date,
        type=payload.type,
        plantId=payload.plantId,
        areaId=payload.areaId,
        location=payload.location,
        reporterId=user.id,

        # Phase 1 — precise occurrence + reporter context
        occurredAt=occurred_at,
        reportedAt=reported_at,
        reportingDelayMinutes=reporting_delay_min,
        reporterRole=user.role,
        departmentId=department_id,
        specificLocation=payload.specificLocation,
        gpsLatitude=payload.gpsLatitude,
        gpsLongitude=payload.gpsLongitude,
        shiftId=payload.shiftId,
        weatherConditions=payload.weatherConditions,
        initialDescription=payload.initialDescription or payload.description,
        immediateAction=payload.immediateAction,
        activityBeingPerformed=payload.activityBeingPerformed,
        activityIsRoutine=payload.activityIsRoutine,
        activePermitId=active_permit_id,
        sourceNearMissId=source_nm_id,

        # Auto-classification (Phase 2 can refine later)
        severity=initial_severity,
        isReportable=is_reportable,
        reportableUnder=reportable_under or None,

        # Linked observations + SLA
        linkedObservationIds=linked_observation_ids or None,
        initialReportSlaTargetAt=initial_report_sla_target,

        # Legacy single-injured-person fields (kept for back-compat)
        injuredPersonName=payload.injuredPersonName,
        injuredPersonAge=payload.injuredPersonAge,
        injuredPersonDesignation=payload.injuredPersonDesignation,
        bodyPart=payload.bodyPart,
        natureOfInjury=payload.natureOfInjury,
        description=payload.description,
        immediateCause=payload.immediateCause,

        # RCA placeholders (filled in Phase 3 — but accepted here for back-compat)
        rootCauseMethod=rca_method,
        rootCauseDetail=None if rca_data else payload.rootCauseDetail,
        rootCauseData=rca_data,
        rootCauseSummary=rca_summary,
        correctiveActions=payload.correctiveActions,
        preventiveActions=payload.preventiveActions,
        lostDays=max(0, payload.lostDays or 0),
        propertyDamageCost=payload.propertyDamageCost,
        status=IncidentStatus.REPORTED,
    )
    db.add(incident)
    await db.flush()

    # Persist child rows (Phase 1 multi sub-forms)
    if payload.personsInvolved:
        for p in payload.personsInvolved:
            db.add(
                IncidentPerson(
                    incidentId=incident.id,
                    userId=p.userId,
                    externalName=p.externalName,
                    externalContact=p.externalContact,
                    role=p.role,
                    isContractor=p.isContractor,
                    contractorCompanyId=p.contractorCompanyId,
                    isInjured=p.isInjured,
                    bodyPartAffected=p.bodyPartAffected,
                    natureOfInjury=p.natureOfInjury,
                    injurySeverity=p.injurySeverity,
                    treatment=p.treatment,
                    hospitalName=p.hospitalName,
                    daysOff=p.daysOff,
                    ppeWornAtTime=p.ppeWornAtTime,
                )
            )
    if payload.witnesses:
        for w in payload.witnesses:
            db.add(
                IncidentWitnessStatement(
                    incidentId=incident.id,
                    witnessUserId=w.witnessUserId,
                    witnessName=w.witnessName,
                    witnessRole=w.witnessRole,
                    takenById=user.id,
                    takenAt=reported_at,
                    language=w.language,
                )
            )
    if payload.equipmentInvolved:
        for eq in payload.equipmentInvolved:
            if not await fk_exists(Equipment, eq.equipmentId):
                continue
            db.add(
                IncidentEquipment(
                    incidentId=incident.id,
                    equipmentId=eq.equipmentId,
                    involvement=eq.involvement,
                    damageEstimate=eq.damageEstimate,
                )
            )
    if payload.investigationTeamIds:
        for i, uid in enumerate(payload.investigationTeamIds):
            db.add(
                IncidentInvestigationMember(
                    incidentId=incident.id,
                    userId=uid,
                    role="LEAD" if i == 0 else "MEMBER",
                )
            )

    await db.flush()
    await db.refresh(incident)

    # Initiate workflow (best-effort SAVEPOINT — failure is logged, not fatal)
    try:
        async with db.begin_nested():
            await workflow_engine.initiate(
                db,
                module="INCIDENT",
                record_id=incident.id,
                record_number=incident.number,
                record_title=incident.description[:120],
                record_data={
                    "type": incident.type.value,
                    "severity": incident.severity,
                    "plantId": incident.plantId,
                    "reporterId": incident.reporterId,
                    "lostDays": incident.lostDays,
                    "isReportable": incident.isReportable,
                    # Investigation lead is empty until Phase 2 classification
                    # picks one. The workflow engine's INVESTIGATION_LEAD
                    # resolver falls back to actionOwnerId / HSE_MANAGER.
                    "investigationTeamLead": incident.investigationTeamLead,
                    "actionOwnerId": incident.investigationTeamLead,
                },
                initiator_id=user.id,
                plant_id=incident.plantId,
            )
    except Exception as e:  # noqa: BLE001
        import sys
        import traceback

        print(f"Incident workflow init failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    # If we linked a source Near Miss, mirror the linkage on the NM side
    # so both records cross-reference each other.
    if source_nm_id:
        nm = await db.get(_NM, source_nm_id)
        if nm is not None and not nm.promotedIncidentId:
            nm.promotedToIncident = True
            nm.promotedIncidentId = incident.id
            nm.promotedAt = datetime.now(timezone.utc)
            await db.flush()

    return IncidentOut.model_validate(incident)


@router.get("/{incident_id}", response_model=IncidentOut)
async def get_incident(
    incident_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IncidentOut:
    incident = await db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    record = {"reporterId": incident.reporterId}
    result = await can(
        db, user.id, "INCIDENT.READ",
        PermissionContext(record_id=incident.id, plant_id=incident.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    return IncidentOut.model_validate(incident)


@router.patch("/{incident_id}", response_model=IncidentOut)
async def update_incident(
    incident_id: str,
    payload: IncidentUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IncidentOut:
    incident = await db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if incident.status == IncidentStatus.CLOSED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot edit a closed incident.")

    record = {"reporterId": incident.reporterId}
    result = await can(
        db, user.id, "INCIDENT.UPDATE",
        PermissionContext(record_id=incident.id, plant_id=incident.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    if payload.immediateCause is not None:
        incident.immediateCause = payload.immediateCause or None
    if payload.rootCauseMethod is not None:
        incident.rootCauseMethod = normalise_rca_method(payload.rootCauseMethod)
    if payload.rootCauseDetail is not None:
        incident.rootCauseDetail = payload.rootCauseDetail or None
    if payload.rootCauseData is not None:
        incident.rootCauseData = payload.rootCauseData or None
        method = normalise_rca_method(incident.rootCauseMethod)
        incident.rootCauseSummary = generate_rca_summary(method, payload.rootCauseData) if method else None
    if payload.correctiveActions is not None:
        incident.correctiveActions = payload.correctiveActions or None
    if payload.preventiveActions is not None:
        incident.preventiveActions = payload.preventiveActions or None
    if payload.lostDays is not None:
        if payload.lostDays < 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Lost days must be ≥ 0.")
        incident.lostDays = payload.lostDays
    if payload.propertyDamageCost is not None:
        if payload.propertyDamageCost < 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Property damage cost must be ≥ 0.")
        incident.propertyDamageCost = payload.propertyDamageCost

    # ─── Phase 3 refinements — cause hierarchy ───
    if payload.immediateCauses is not None:
        incident.immediateCauses = payload.immediateCauses or None
    if payload.underlyingCauses is not None:
        incident.underlyingCauses = payload.underlyingCauses or None
    if payload.rootCauses is not None:
        incident.rootCauses = payload.rootCauses or None
    if payload.contributingFactors is not None:
        incident.contributingFactors = payload.contributingFactors or None

    # ─── Phase 7 — cost breakdown. Server auto-sums costTotal so the
    # detail view + dashboards always show a consistent figure. ───
    cost_changed = False
    for fld in ("costMedical", "costPropertyDamage", "costLostProduction",
                "costInsurance", "costLegalRegulatory", "costOther"):
        v = getattr(payload, fld)
        if v is not None:
            if v < 0:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{fld} must be ≥ 0.")
            setattr(incident, fld, v)
            cost_changed = True
    if cost_changed:
        incident.costTotal = sum(
            float(getattr(incident, fld) or 0)
            for fld in ("costMedical", "costPropertyDamage", "costLostProduction",
                        "costInsurance", "costLegalRegulatory", "costOther")
        )

    if payload.investigationTeamIds is not None:
        # Replace team
        existing = (
            await db.execute(select(IncidentInvestigationMember).where(IncidentInvestigationMember.incidentId == incident.id))
        ).scalars().all()
        for m in existing:
            await db.delete(m)
        for i, uid in enumerate(payload.investigationTeamIds):
            db.add(IncidentInvestigationMember(incidentId=incident.id, userId=uid, role="LEAD" if i == 0 else "MEMBER"))

    await db.flush()
    return IncidentOut.model_validate(incident)


# ─── Phase 2 Classification ─────────────────────────────────────────────


@router.post("/{incident_id}/classify", response_model=IncidentOut)
async def classify_incident(
    incident_id: str,
    payload: IncidentClassifyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IncidentOut:
    """Phase 2 — HSE Manager classifies the incident, sets statutory
    obligations, picks the investigation team, then approves the
    "HSE Manager Classification" CHECKER step in one transaction."""

    incident = await db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")

    record = {
        "reporterId": incident.reporterId,
        "investigationTeamLead": incident.investigationTeamLead,
    }
    result = await can(
        db, user.id, "INCIDENT.APPROVE",
        PermissionContext(record_id=incident.id, plant_id=incident.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    # Compute statutory deadline if newly reportable
    deadline = None
    if payload.isReportable and incident.occurredAt is not None:
        hours = _STATUTORY_DEADLINE_HOURS.get(payload.type)
        if hours is not None:
            deadline = incident.occurredAt + timedelta(hours=hours)

    # Apply classification fields
    incident.type = payload.type
    incident.severity = payload.severity
    incident.classificationRationale = payload.classificationRationale
    incident.classifiedAt = datetime.now(timezone.utc)
    incident.classifiedById = user.id
    incident.isReportable = payload.isReportable
    incident.reportableUnder = payload.reportableUnder or None
    incident.statutoryDeadline = deadline
    incident.investigationTeamLead = payload.investigationTeamLead
    incident.investigationCharterDate = payload.investigationCharterDate or datetime.now(timezone.utc)

    # Initial cost estimates (refined during investigation)
    if payload.costPropertyDamage is not None:
        incident.costPropertyDamage = payload.costPropertyDamage
    if payload.costLostProduction is not None:
        incident.costLostProduction = payload.costLostProduction

    # Investigation team — replace existing membership wholesale
    existing = (
        await db.execute(
            select(IncidentInvestigationMember).where(IncidentInvestigationMember.incidentId == incident.id)
        )
    ).scalars().all()
    for m in existing:
        await db.delete(m)
    member_ids: list[str] = []
    if payload.investigationTeamLead:
        member_ids.append(payload.investigationTeamLead)
    for uid in payload.investigationTeamMemberIds:
        if uid not in member_ids:
            member_ids.append(uid)
    for i, uid in enumerate(member_ids):
        db.add(
            IncidentInvestigationMember(
                incidentId=incident.id, userId=uid, role="LEAD" if i == 0 else "MEMBER"
            )
        )

    await db.flush()
    await db.refresh(incident)

    # Approve the workflow CHECKER step — this also propagates the new
    # severity / isReportable / investigationTeamLead via record_data so
    # subsequent step lookups (Plant Head review condition, statutory
    # submission condition, slaBySeverity) all see the fresh values.
    try:
        async with db.begin_nested():
            await workflow_engine.approve(
                db,
                task_id=payload.classificationTaskId,
                user_id=user.id,
                comments=payload.comments,
                record_data={
                    "type": incident.type.value,
                    "severity": incident.severity,
                    "plantId": incident.plantId,
                    "reporterId": incident.reporterId,
                    "lostDays": incident.lostDays,
                    "isReportable": incident.isReportable,
                    "investigationTeamLead": incident.investigationTeamLead,
                    "actionOwnerId": incident.investigationTeamLead,
                },
                plant_id=incident.plantId,
            )
    except Exception as e:  # noqa: BLE001
        await db.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Could not approve classification step: {str(e)[:200]}",
        ) from e

    return IncidentOut.model_validate(incident)


# ─── CAPA CRUD ───────────────────────────────────────────────────────────


@router.get("/{incident_id}/capas", response_model=list[IncidentCapaOut])
async def list_capas(
    incident_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[IncidentCapaOut]:
    incident = await db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
    record = {"reporterId": incident.reporterId, "investigationTeamLead": incident.investigationTeamLead}
    result = await can(
        db, user.id, "INCIDENT.READ",
        PermissionContext(record_id=incident.id, plant_id=incident.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    rows = (
        await db.execute(
            select(IncidentCapa)
            .where(IncidentCapa.incidentId == incident_id)
            .order_by(IncidentCapa.createdAt.asc())
        )
    ).scalars().all()
    return [IncidentCapaOut.model_validate(r) for r in rows]


@router.post("/{incident_id}/capas", response_model=IncidentCapaOut, status_code=status.HTTP_201_CREATED)
async def create_capa(
    incident_id: str,
    payload: IncidentCapaInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IncidentCapaOut:
    incident = await db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
    record = {"reporterId": incident.reporterId, "investigationTeamLead": incident.investigationTeamLead}
    result = await can(
        db, user.id, "INCIDENT.UPDATE",
        PermissionContext(record_id=incident.id, plant_id=incident.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    # Validate owner exists
    owner = await db.get(User, payload.ownerId)
    if owner is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid CAPA owner")

    # Generate sequential CAPA number for this incident
    last = (
        await db.execute(
            select(func.count()).select_from(IncidentCapa).where(IncidentCapa.incidentId == incident_id)
        )
    ).scalar_one()
    capa_number = f"{incident.number}-CAPA-{last + 1:02d}"

    capa = IncidentCapa(
        incidentId=incident_id,
        capaNumber=capa_number,
        description=payload.description,
        type=payload.type,
        rootCauseAddressed=payload.rootCauseAddressed,
        ownerId=payload.ownerId,
        targetDate=payload.targetDate,
        status="PENDING",
    )
    db.add(capa)
    await db.flush()
    await db.refresh(capa)
    return IncidentCapaOut.model_validate(capa)


@router.delete("/{incident_id}/capas/{capa_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_capa(
    incident_id: str,
    capa_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    incident = await db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
    record = {"reporterId": incident.reporterId, "investigationTeamLead": incident.investigationTeamLead}
    result = await can(
        db, user.id, "INCIDENT.UPDATE",
        PermissionContext(record_id=incident.id, plant_id=incident.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    capa = await db.get(IncidentCapa, capa_id)
    if capa is None or capa.incidentId != incident_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CAPA not found")
    if capa.status not in ("PENDING", "IN_PROGRESS"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Cannot delete a CAPA that's been completed or verified.",
        )
    await db.delete(capa)
    await db.flush()
    return None


# ─── Phase 3 child-row helpers ───────────────────────────────────────


async def _require_incident_for_action(
    db: AsyncSession, incident_id: str, user_id: str, action: str
) -> Incident:
    """Common permission gate for Phase 3 child-row CRUD. Loads the incident,
    permission-checks the action, and returns the loaded row. Raises HTTP
    exceptions on missing record / lack of permission."""
    incident = await db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
    record = {"reporterId": incident.reporterId, "investigationTeamLead": incident.investigationTeamLead}
    res = await can(
        db, user_id, f"INCIDENT.{action}",
        PermissionContext(record_id=incident.id, plant_id=incident.plantId, record=record),
    )
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")
    return incident


# ─── Timeline events ────────────────────────────────────────────────────


@router.get("/{incident_id}/timeline-events", response_model=list[TimelineEventOut])
async def list_timeline_events(
    incident_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TimelineEventOut]:
    await _require_incident_for_action(db, incident_id, user.id, "READ")
    rows = (
        await db.execute(
            select(IncidentTimelineEvent)
            .where(IncidentTimelineEvent.incidentId == incident_id)
            .order_by(IncidentTimelineEvent.sequence.asc())
        )
    ).scalars().all()
    return [TimelineEventOut.model_validate(r) for r in rows]


@router.post(
    "/{incident_id}/timeline-events",
    response_model=TimelineEventOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_timeline_event(
    incident_id: str,
    payload: TimelineEventInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TimelineEventOut:
    await _require_incident_for_action(db, incident_id, user.id, "UPDATE")
    row = IncidentTimelineEvent(
        incidentId=incident_id,
        sequence=payload.sequence,
        timestamp=payload.timestamp,
        description=payload.description,
        source=payload.source,
        sourceReference=payload.sourceReference,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return TimelineEventOut.model_validate(row)


@router.delete(
    "/{incident_id}/timeline-events/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_timeline_event(
    incident_id: str,
    event_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _require_incident_for_action(db, incident_id, user.id, "UPDATE")
    row = await db.get(IncidentTimelineEvent, event_id)
    if row is None or row.incidentId != incident_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Timeline event not found")
    await db.delete(row)
    await db.flush()


# ─── Evidence ───────────────────────────────────────────────────────────


@router.get("/{incident_id}/evidence", response_model=list[EvidenceOut])
async def list_evidence(
    incident_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[EvidenceOut]:
    await _require_incident_for_action(db, incident_id, user.id, "READ")
    rows = (
        await db.execute(
            select(IncidentEvidence)
            .where(IncidentEvidence.incidentId == incident_id)
            .order_by(IncidentEvidence.collectedAt.desc().nullslast())
        )
    ).scalars().all()
    return [EvidenceOut.model_validate(r) for r in rows]


@router.post(
    "/{incident_id}/evidence",
    response_model=EvidenceOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_evidence(
    incident_id: str,
    payload: EvidenceInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EvidenceOut:
    await _require_incident_for_action(db, incident_id, user.id, "UPDATE")
    row = IncidentEvidence(
        incidentId=incident_id,
        category=payload.category,
        title=payload.title,
        description=payload.description,
        fileUrl=payload.fileUrl,
        fileName=payload.fileName,
        fileSize=payload.fileSize,
        mimeType=payload.mimeType,
        collectedById=user.id,
        collectedAt=payload.collectedAt or datetime.now(timezone.utc),
        preservedFor=payload.preservedFor,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return EvidenceOut.model_validate(row)


@router.delete(
    "/{incident_id}/evidence/{evidence_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_evidence(
    incident_id: str,
    evidence_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _require_incident_for_action(db, incident_id, user.id, "UPDATE")
    row = await db.get(IncidentEvidence, evidence_id)
    if row is None or row.incidentId != incident_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence not found")
    await db.delete(row)
    await db.flush()


# ─── Witnesses (read + update only — Phase 1 created the rows) ──────────


@router.get("/{incident_id}/witnesses", response_model=list[WitnessStatementOut])
async def list_witnesses(
    incident_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[WitnessStatementOut]:
    await _require_incident_for_action(db, incident_id, user.id, "READ")
    rows = (
        await db.execute(
            select(IncidentWitnessStatement)
            .where(IncidentWitnessStatement.incidentId == incident_id)
            .order_by(IncidentWitnessStatement.takenAt.asc())
        )
    ).scalars().all()
    return [WitnessStatementOut.model_validate(r) for r in rows]


@router.patch(
    "/{incident_id}/witnesses/{witness_id}",
    response_model=WitnessStatementOut,
)
async def update_witness(
    incident_id: str,
    witness_id: str,
    payload: WitnessStatementUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WitnessStatementOut:
    await _require_incident_for_action(db, incident_id, user.id, "UPDATE")
    row = await db.get(IncidentWitnessStatement, witness_id)
    if row is None or row.incidentId != incident_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Witness not found")
    for fld in ("witnessRole", "statementText", "statementFileUrl",
                "audioRecordingUrl", "language"):
        v = getattr(payload, fld)
        if v is not None:
            setattr(row, fld, v or None)
    await db.flush()
    await db.refresh(row)
    return WitnessStatementOut.model_validate(row)


# ─── Persons & injuries (read + update — Phase 1 created the rows) ──────


@router.get("/{incident_id}/persons", response_model=list[PersonOut])
async def list_persons(
    incident_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PersonOut]:
    await _require_incident_for_action(db, incident_id, user.id, "READ")
    rows = (
        await db.execute(
            select(IncidentPerson)
            .where(IncidentPerson.incidentId == incident_id)
            .order_by(IncidentPerson.createdAt.asc())
        )
    ).scalars().all()
    return [PersonOut.model_validate(r) for r in rows]


@router.patch(
    "/{incident_id}/persons/{person_id}",
    response_model=PersonOut,
)
async def update_person(
    incident_id: str,
    person_id: str,
    payload: PersonUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PersonOut:
    await _require_incident_for_action(db, incident_id, user.id, "UPDATE")
    row = await db.get(IncidentPerson, person_id)
    if row is None or row.incidentId != incident_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")
    for fld in ("role", "isInjured", "bodyPartAffected", "natureOfInjury",
                "injurySeverity", "treatment", "hospitalName", "daysOff",
                "daysRestricted", "returnToWorkDate", "isFitForDuty",
                "ppeWornAtTime"):
        v = getattr(payload, fld)
        if v is not None:
            setattr(row, fld, v)
    await db.flush()
    await db.refresh(row)
    return PersonOut.model_validate(row)


# ─── Equipment damage (read + update) ───────────────────────────────────


@router.get("/{incident_id}/equipment", response_model=list[EquipmentOut])
async def list_incident_equipment(
    incident_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[EquipmentOut]:
    await _require_incident_for_action(db, incident_id, user.id, "READ")
    rows = (
        await db.execute(
            select(IncidentEquipment).where(IncidentEquipment.incidentId == incident_id)
        )
    ).scalars().all()
    return [EquipmentOut.model_validate(r) for r in rows]


@router.patch(
    "/{incident_id}/equipment/{eq_row_id}",
    response_model=EquipmentOut,
)
async def update_incident_equipment(
    incident_id: str,
    eq_row_id: str,
    payload: EquipmentUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EquipmentOut:
    await _require_incident_for_action(db, incident_id, user.id, "UPDATE")
    row = await db.get(IncidentEquipment, eq_row_id)
    if row is None or row.incidentId != incident_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Equipment row not found")
    for fld in ("involvement", "damageEstimate", "repairStatus"):
        v = getattr(payload, fld)
        if v is not None:
            setattr(row, fld, v)
    await db.flush()
    await db.refresh(row)
    return EquipmentOut.model_validate(row)


# ─── Documents Reviewed ─────────────────────────────────────────────────


@router.get("/{incident_id}/documents-reviewed", response_model=list[DocumentReviewOut])
async def list_documents_reviewed(
    incident_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DocumentReviewOut]:
    await _require_incident_for_action(db, incident_id, user.id, "READ")
    rows = (
        await db.execute(
            select(IncidentDocumentReview).where(IncidentDocumentReview.incidentId == incident_id)
        )
    ).scalars().all()
    return [DocumentReviewOut.model_validate(r) for r in rows]


@router.post(
    "/{incident_id}/documents-reviewed",
    response_model=DocumentReviewOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_document_review(
    incident_id: str,
    payload: DocumentReviewInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DocumentReviewOut:
    await _require_incident_for_action(db, incident_id, user.id, "UPDATE")
    row = IncidentDocumentReview(
        incidentId=incident_id,
        documentType=payload.documentType,
        documentReference=payload.documentReference,
        documentLinkId=payload.documentLinkId,
        reviewNotes=payload.reviewNotes,
        complianceFinding=payload.complianceFinding,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return DocumentReviewOut.model_validate(row)


@router.delete(
    "/{incident_id}/documents-reviewed/{doc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_document_review(
    incident_id: str,
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _require_incident_for_action(db, incident_id, user.id, "UPDATE")
    row = await db.get(IncidentDocumentReview, doc_id)
    if row is None or row.incidentId != incident_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document review not found")
    await db.delete(row)
    await db.flush()


# ─── Comments (with privileged-legal filter) ───────────────────────────


# Roles that can see / post privileged-legal comments. Anyone outside
# this list never sees them in the GET response.
_PRIVILEGED_ROLES = {"HSE_MANAGER", "PLANT_HEAD", "CORPORATE_HSE", "ADMIN", "SYSTEM_ADMIN"}


def _can_see_privileged(user: User) -> bool:
    return user.role in _PRIVILEGED_ROLES


@router.get("/{incident_id}/comments", response_model=list[CommentOut])
async def list_comments(
    incident_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CommentOut]:
    await _require_incident_for_action(db, incident_id, user.id, "READ")
    stmt = (
        select(IncidentComment)
        .where(IncidentComment.incidentId == incident_id)
        .order_by(IncidentComment.createdAt.asc())
    )
    if not _can_see_privileged(user):
        stmt = stmt.where(IncidentComment.isPrivilegedLegal.is_(False))
    rows = (await db.execute(stmt)).scalars().all()
    return [CommentOut.model_validate(r) for r in rows]


@router.post(
    "/{incident_id}/comments",
    response_model=CommentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_comment(
    incident_id: str,
    payload: CommentInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CommentOut:
    await _require_incident_for_action(db, incident_id, user.id, "READ")
    # Anyone with READ can post a comment. But only privileged roles can
    # post a privileged-legal comment.
    if payload.isPrivilegedLegal and not _can_see_privileged(user):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only HSE Manager / Plant Head / Corporate HSE can post privileged-legal comments.",
        )
    row = IncidentComment(
        incidentId=incident_id,
        authorId=user.id,
        content=payload.content,
        isPrivilegedLegal=payload.isPrivilegedLegal,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return CommentOut.model_validate(row)


# ─── Statutory submissions tracker ──────────────────────────────────────


@router.patch("/{incident_id}/statutory-submissions", response_model=IncidentOut)
async def update_statutory_submissions(
    incident_id: str,
    payload: StatutorySubmissionUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IncidentOut:
    """HSE Manager records when each regulator's submission has been
    filed and what reference number came back. Permission-gated to
    INCIDENT.UPDATE (HSE Manager / Plant Head / Corporate HSE)."""

    incident = await _require_incident_for_action(db, incident_id, user.id, "UPDATE")

    # Form 18
    if payload.form18Submitted is not None:
        incident.form18Submitted = payload.form18Submitted
        if payload.form18Submitted:
            incident.form18SubmissionDate = payload.form18SubmissionDate or datetime.now(timezone.utc)
            incident.form18PreparedAt = incident.form18PreparedAt or datetime.now(timezone.utc)
            incident.form18PreparedById = user.id
        if payload.form18SubmissionRef is not None:
            incident.form18SubmissionRef = payload.form18SubmissionRef or None

    # DGFASLI
    if payload.dgfasliSubmitted is not None:
        incident.dgfasliSubmitted = payload.dgfasliSubmitted
        if payload.dgfasliSubmitted:
            incident.dgfasliSubmissionDate = payload.dgfasliSubmissionDate or datetime.now(timezone.utc)

    # CPCB
    if payload.cpcbSubmitted is not None:
        incident.cpcbSubmitted = payload.cpcbSubmitted
        if payload.cpcbSubmitted:
            incident.cpcbSubmissionDate = payload.cpcbSubmissionDate or datetime.now(timezone.utc)

    await db.flush()
    await db.refresh(incident)
    return IncidentOut.model_validate(incident)


# ─── Reclassification ───────────────────────────────────────────────────


# Statutory deadlines (Indian regulations) — recomputed on every
# reclassification because escalating MTC → LTI flips the obligation set.
_STATUTORY_DEADLINE_HOURS: dict[IncidentType, int | None] = {
    IncidentType.FIRST_AID: None,
    IncidentType.MTC: None,
    IncidentType.RWC: None,
    IncidentType.LTI: 24,           # Form 18 within 24h of LTI
    IncidentType.FATALITY: 24,      # Form 18 + DGFASLI immediate
    IncidentType.PROPERTY_DAMAGE: None,
    IncidentType.ENVIRONMENTAL: 72, # CPCB notification window
    IncidentType.FIRE: 24,
    IncidentType.PROCESS_SAFETY: 24,
    IncidentType.HIPO_NEAR_MISS: None,
}


@router.post("/{incident_id}/reclassify", response_model=IncidentOut)
async def reclassify_incident(
    incident_id: str,
    payload: IncidentReclassifyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IncidentOut:
    """Reclassify type and severity mid-flow. Common case: MTC → LTI when a
    worker doesn't return after expected days. Writes an immutable audit row
    and recomputes statutory obligations + deadlines.

    Permission: requires INCIDENT.UPDATE on this record. Per the RBAC matrix
    only HSE Manager (own-plant), Plant Head (own-plant) and Corporate HSE
    (all-plants) have UPDATE — workers / supervisors / safety officers cannot
    reclassify."""

    incident = await db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")

    record = {
        "reporterId": incident.reporterId,
        "investigationTeamLead": incident.investigationTeamLead,
    }
    result = await can(
        db, user.id, "INCIDENT.UPDATE",
        PermissionContext(record_id=incident.id, plant_id=incident.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    from_type = incident.type.value
    from_severity = incident.severity

    if from_type == payload.toType.value and from_severity == payload.toSeverity:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No change — incident already at that classification.")

    # Recompute statutory obligations + deadline based on new type
    is_reportable, reportable_under = _initial_reportability(payload.toType)
    deadline_hours = _STATUTORY_DEADLINE_HOURS.get(payload.toType)
    new_deadline = None
    if deadline_hours is not None and incident.occurredAt is not None:
        new_deadline = incident.occurredAt + timedelta(hours=deadline_hours)

    # Detect "urgent retroactive submission" — was-not-reportable, now-reportable,
    # and the deadline window has already elapsed since occurrence.
    triggers_statutory_update = False
    if (
        is_reportable
        and not incident.isReportable
        and new_deadline is not None
        and new_deadline < datetime.now(timezone.utc)
    ):
        triggers_statutory_update = True

    # Audit row first — never lose the history even if mutation fails below
    db.add(
        IncidentReclassification(
            incidentId=incident.id,
            fromType=from_type,
            toType=payload.toType.value,
            fromSeverity=from_severity,
            toSeverity=payload.toSeverity,
            reason=payload.reason,
            reclassifiedById=user.id,
            triggersStatutoryUpdate=triggers_statutory_update,
        )
    )

    # Mutate the incident
    incident.type = payload.toType
    incident.severity = payload.toSeverity
    incident.isReportable = is_reportable
    incident.reportableUnder = reportable_under or None
    incident.statutoryDeadline = new_deadline

    await db.flush()
    await db.refresh(incident)
    return IncidentOut.model_validate(incident)


# ─── Attachments ─────────────────────────────────────────────────────────


async def _has_uploaded_attachment(db: AsyncSession, user_id: str, incident_id: str) -> bool:
    """True if the caller uploaded at least one (non-deleted) attachment to
    this incident. Whoever contributes evidence must always be able to see it
    back in the gallery, even without an INCIDENT.READ grant."""
    stmt = (
        select(IncidentAttachment.id)
        .where(IncidentAttachment.incidentId == incident_id)
        .where(IncidentAttachment.uploadedById == user_id)
        .where(IncidentAttachment.deletedAt.is_(None))
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


@router.get("/{incident_id}/attachments")
async def list_attachments(
    incident_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    incident = await db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
    record = {"reporterId": incident.reporterId}
    result = await can(
        db, user.id, "INCIDENT.READ",
        PermissionContext(record_id=incident.id, plant_id=incident.plantId, record=record),
    )
    # The reporter and anyone who uploaded evidence here can always see the
    # gallery, even without an INCIDENT.READ grant — so an uploader never loses
    # sight of their own contribution.
    if (
        not result.allowed
        and incident.reporterId != user.id
        and not await _has_uploaded_attachment(db, user.id, incident_id)
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    rows = (
        await db.execute(
            select(IncidentAttachment)
            .options(selectinload(IncidentAttachment.uploadedBy))
            .where(IncidentAttachment.incidentId == incident_id)
            .where(IncidentAttachment.deletedAt.is_(None))
            .order_by(IncidentAttachment.uploadedAt.desc())
        )
    ).scalars().all()
    return {"items": [AttachmentOut.model_validate(r) for r in rows]}


@router.post("/{incident_id}/attachments")
async def upload_attachment(
    incident_id: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    incident = await db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")

    record = {"reporterId": incident.reporterId}
    result = await can(
        db, user.id, "INCIDENT.UPDATE",
        PermissionContext(record_id=incident.id, plant_id=incident.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    if not is_storage_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Supabase Storage isn't configured. Set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.",
        )

    phase = payload.get("phase")
    if phase == "init":
        try:
            init = AttachmentInit(**payload)
        except Exception as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid init body: {e}") from e
        if init.category not in VALID_CATEGORIES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid category. Must be one of: {', '.join(VALID_CATEGORIES)}")
        if init.fileSize > MAX_FILE_SIZE:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"File size exceeds the {MAX_FILE_SIZE // 1024 // 1024} MB limit.")
        if init.mimeType not in ALLOWED_MIME:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"MIME type {init.mimeType} is not allowed.")
        storage_path = build_storage_path(incident_id=incident_id, category=init.category, file_name=init.fileName)
        try:
            signed = create_signed_upload_url(storage_path)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"Storage upload init failed: {e}",
            ) from e
        att = IncidentAttachment(
            incidentId=incident_id,
            category=init.category,
            fileName=init.fileName,
            storagePath=storage_path,
            fileSize=init.fileSize,
            mimeType=init.mimeType,
            uploadedById=user.id,
            capaRef=init.capaRef,
            witnessRef=init.witnessRef,
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
        att = await db.get(IncidentAttachment, attachment_id)
        if att is None or att.incidentId != incident_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found for this incident")
        att.caption = payload.get("caption")
        att.exifData = payload.get("exifData")
        await db.flush()
        return {"ok": True}

    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown phase")


@router.delete("/{incident_id}/attachments/{attachment_id}")
async def delete_attachment(
    incident_id: str,
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    att = await db.get(IncidentAttachment, attachment_id)
    if att is None or att.incidentId != incident_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    incident = await db.get(Incident, incident_id)
    record = {"reporterId": incident.reporterId if incident else None, "uploadedById": att.uploadedById}
    result = await can(
        db, user.id, "INCIDENT.UPDATE",
        PermissionContext(record_id=att.id, plant_id=incident.plantId if incident else None, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    att.deletedAt = datetime.now(timezone.utc)
    await db.flush()
    return {"ok": True}


@router.get("/{incident_id}/attachments/{attachment_id}/download")
async def download_attachment(
    incident_id: str,
    attachment_id: str,
    inline: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    att = await db.get(IncidentAttachment, attachment_id)
    if att is None or att.incidentId != incident_id or att.deletedAt is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    incident = await db.get(Incident, incident_id)
    # The uploader can always view their own file — guarantees the person who
    # uploaded a photo can preview it even without an INCIDENT.READ grant.
    is_uploader = att.uploadedById == user.id
    record = {"reporterId": incident.reporterId if incident else None, "uploadedById": att.uploadedById}
    result = await can(
        db, user.id, "INCIDENT.READ",
        PermissionContext(record_id=incident.id if incident else None, plant_id=incident.plantId if incident else None, record=record),
    )
    if not result.allowed and not is_uploader:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    url = create_signed_download_url(
        att.storagePath,
        expires_in_sec=300,
        download=None if inline else att.fileName,
    )
    return {"url": url}
