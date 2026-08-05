"""Training & Competency Engine router. Mounts at /api/training-engine.

Surfaces the Trigger + Assignment engine, the admin-configurable HazardToSkill
mapping + rule thresholds (the "moat"), the vendor-decoupled TrainingContent
adapter, and the correlation report. Mounted ungated (like insights/capture):
the signed dev licence predates the code and per-endpoint RBAC is the real gate.

RBAC:
  reads (queue, mappings, content, config, correlation) → SKILL_MATRIX.READ (plant-scoped)
  mapping / config / content admin                      → SKILL_MATRIX.COMPETENCY_CONFIGURE
  manual assign / escalate                              → SKILL_MATRIX.ASSESS
  a worker acting on their OWN assignment               → self (personUserId == user.id)
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.competency_matrix import Competency
from app.models.training_engine import (
    HazardToSkillMapping,
    TrainingAssignment,
    TrainingContent,
    TrainingRuleConfig,
    WorkerTrainingFlag,
)
from app.models.user import User
from app.schemas.training_engine import (
    AssignmentCompleteBody,
    AssignmentStatusBody,
    ContentCreate,
    ContentUpdate,
    HazardMappingCreate,
    HazardMappingUpdate,
    ManualAssignCreate,
    RuleConfigUpdate,
)
from app.services.access_scope import build_query_scope
from app.services.permissions import PermissionContext, can
from app.services.training_engine import person_risk, service
from app.services.training_engine.config import resolve_config
from app.services.training_engine.correlation import compute_report

router = APIRouter(prefix="/api/training-engine", tags=["training-engine"])


async def _require(db: AsyncSession, user: User, code: str, plant_id: str | None = None) -> None:
    res = await can(db, user.id, code, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


async def _read_scope(db: AsyncSession, user: User):
    return await build_query_scope(db, user.id, "SKILL_MATRIX.READ")


# ── serialisation helpers ─────────────────────────────────────────────────────
async def _name_maps(db: AsyncSession, comp_ids: set[str], user_ids: set[str]) -> tuple[dict, dict]:
    comps = {}
    if comp_ids:
        rows = (await db.execute(select(Competency).where(Competency.id.in_(comp_ids)))).scalars().all()
        comps = {c.id: c.name for c in rows}
    users = {}
    if user_ids:
        rows = (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
        users = {u.id: {"name": u.name, "role": u.role, "department": u.department} for u in rows}
    return comps, users


def _assignment_dict(a: TrainingAssignment, comp_name: str | None, worker: dict | None) -> dict:
    return {
        "id": a.id,
        "plantId": a.plantId,
        "personUserId": a.personUserId,
        "worker": worker,
        "competencyId": a.competencyId,
        "competencyName": comp_name,
        "source": a.source,
        "ruleType": a.ruleType,
        "sourceModule": a.sourceModule,
        "sourceRecordId": a.sourceRecordId,
        "sourceRecordRef": a.sourceRecordRef,
        "triggerMappingId": a.triggerMappingId,
        "provenance": a.provenance,
        "contentId": a.contentId,
        "assignedAt": a.assignedAt.isoformat() if a.assignedAt else None,
        "dueDate": a.dueDate.isoformat() if a.dueDate else None,
        "status": a.status,
        "isMandatory": a.isMandatory,
        "dismissible": a.dismissible,
        "escalationFlag": a.escalationFlag,
        "completedAt": a.completedAt.isoformat() if a.completedAt else None,
        "completionEvidenceType": a.completionEvidenceType,
    }


# ── ASSIGNMENTS ────────────────────────────────────────────────────────────────
@router.get("/assignments")
async def list_assignments(
    status_filter: str | None = Query(None, alias="status"),
    plantId: str | None = Query(None),
    competencyId: str | None = Query(None),
    source: str | None = Query(None),
    personUserId: str | None = Query(None),
    limit: int = Query(300, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """The HSE Manager / lead assignment queue — plant-scoped (fail-closed)."""
    scope = await _read_scope(db, user)
    stmt = select(TrainingAssignment).where(TrainingAssignment.isDeleted.is_(False))
    stmt = scope.apply(stmt, TrainingAssignment)
    if status_filter:
        stmt = stmt.where(TrainingAssignment.status == status_filter)
    if plantId and scope.allows_plant(plantId):
        stmt = stmt.where(TrainingAssignment.plantId == plantId)
    if competencyId:
        stmt = stmt.where(TrainingAssignment.competencyId == competencyId)
    if source:
        stmt = stmt.where(TrainingAssignment.source == source)
    if personUserId:
        stmt = stmt.where(TrainingAssignment.personUserId == personUserId)
    stmt = stmt.order_by(TrainingAssignment.assignedAt.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()

    comp_map, user_map = await _name_maps(db, {r.competencyId for r in rows}, {r.personUserId for r in rows})
    items = [_assignment_dict(r, comp_map.get(r.competencyId), user_map.get(r.personUserId)) for r in rows]
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    return {"items": items, "summary": {"total": len(items), "byStatus": by_status}}


@router.get("/assignments/mine")
async def my_assignments(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """The worker's own assigned training — no plant gate (own records)."""
    rows = (
        await db.execute(
            select(TrainingAssignment)
            .where(TrainingAssignment.personUserId == user.id)
            .where(TrainingAssignment.isDeleted.is_(False))
            .order_by(TrainingAssignment.assignedAt.desc())
        )
    ).scalars().all()
    comp_map, _ = await _name_maps(db, {r.competencyId for r in rows}, set())
    return {"items": [_assignment_dict(r, comp_map.get(r.competencyId), None) for r in rows]}


@router.get("/assignments/{assignment_id}")
async def get_assignment(
    assignment_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    a = await db.get(TrainingAssignment, assignment_id)
    if a is None or a.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assignment not found")
    if a.personUserId != user.id:
        scope = await _read_scope(db, user)
        if not scope.allows_plant(a.plantId):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    comp_map, user_map = await _name_maps(db, {a.competencyId}, {a.personUserId})
    out = _assignment_dict(a, comp_map.get(a.competencyId), user_map.get(a.personUserId))
    if a.contentId:
        content = await db.get(TrainingContent, a.contentId)
        if content:
            out["content"] = {
                "id": content.id,
                "title": content.title,
                "contentType": content.contentType,
                "deliveryMode": content.deliveryMode,
                "contentRef": content.contentRef,
                "durationMinutes": content.durationMinutes,
                "vendorId": content.vendorId,
                "vendorName": content.vendorName,
            }
    return out


@router.post("/assignments")
async def create_manual_assignment(
    payload: ManualAssignCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    await _require(db, user, "SKILL_MATRIX.ASSESS", payload.plantId)
    a = await service.assign_manual(
        db,
        plant_id=payload.plantId,
        person_user_id=payload.personUserId,
        competency_id=payload.competencyId,
        assigned_by=user.id,
        due_days=payload.dueDays,
        content_id=payload.contentId,
    )
    await db.commit()
    if a is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Could not create assignment")
    await db.refresh(a)
    comp_map, user_map = await _name_maps(db, {a.competencyId}, {a.personUserId})
    return _assignment_dict(a, comp_map.get(a.competencyId), user_map.get(a.personUserId))


@router.post("/assignments/{assignment_id}/status")
async def set_assignment_status(
    assignment_id: str,
    payload: AssignmentStatusBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Move an assignment to in_progress / escalated / cancelled. A worker may
    start (in_progress) their OWN assignment. A mandatory (severity-rule)
    assignment can NEVER be dismissed/cancelled by the worker — only completed or
    escalated (spec business rule)."""
    a = await db.get(TrainingAssignment, assignment_id)
    if a is None or a.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assignment not found")

    is_owner = a.personUserId == user.id
    new_status = payload.status
    if new_status not in ("in_progress", "escalated", "cancelled"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid status transition")

    if new_status == "cancelled":
        if a.isMandatory or not a.dismissible:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "This is a mandatory (SIF/severity) assignment and cannot be dismissed — complete or escalate it.",
            )
        # cancelling requires the assessor permission unless you own it
        if not is_owner:
            await _require(db, user, "SKILL_MATRIX.ASSESS", a.plantId)
    elif new_status == "escalated":
        await _require(db, user, "SKILL_MATRIX.ASSESS", a.plantId)
    elif new_status == "in_progress" and not is_owner:
        await _require(db, user, "SKILL_MATRIX.ASSESS", a.plantId)

    a.status = new_status
    a.updatedBy = user.id
    await db.commit()
    return {"id": a.id, "status": a.status}


@router.post("/assignments/{assignment_id}/complete")
async def complete_assignment_ep(
    assignment_id: str,
    payload: AssignmentCompleteBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    a = await db.get(TrainingAssignment, assignment_id)
    if a is None or a.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assignment not found")
    # The worker completes their own; a supervisor/assessor can complete on their behalf.
    if a.personUserId != user.id:
        await _require(db, user, "SKILL_MATRIX.ASSESS", a.plantId)
    res = await service.complete_assignment(
        db,
        a,
        evidence_type=payload.evidenceType,
        evidence_id=payload.evidenceId,
        note=payload.note,
        actor_id=user.id,
    )
    await db.commit()
    return res


@router.post("/evaluate")
async def evaluate_now(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Drain the trigger outbox immediately (demo / on-demand). Normally the
    training_engine_resolver scheduler job does this every 60s."""
    await _require(db, user, "SKILL_MATRIX.COMPETENCY_CONFIGURE")
    return await service.drain_trigger_events(db)


# ── HAZARD → SKILL MAPPINGS (the moat config) ────────────────────────────────
@router.get("/mappings")
async def list_mappings(
    sourceModule: str | None = Query(None),
    competencyId: str | None = Query(None),
    plantId: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    await _require(db, user, "SKILL_MATRIX.READ")
    stmt = select(HazardToSkillMapping).where(HazardToSkillMapping.isDeleted.is_(False))
    if sourceModule:
        stmt = stmt.where(HazardToSkillMapping.sourceModule == sourceModule)
    if competencyId:
        stmt = stmt.where(HazardToSkillMapping.competencyId == competencyId)
    if plantId:
        stmt = stmt.where(HazardToSkillMapping.plantId == plantId)
    rows = (await db.execute(stmt.order_by(HazardToSkillMapping.priority.asc()))).scalars().all()
    comp_map, _ = await _name_maps(db, {r.competencyId for r in rows}, set())
    return [
        {
            "id": m.id,
            "plantId": m.plantId,
            "sourceModule": m.sourceModule,
            "classificationField": m.classificationField,
            "classificationValue": m.classificationValue,
            "matchMode": m.matchMode,
            "competencyId": m.competencyId,
            "competencyName": comp_map.get(m.competencyId),
            "priority": m.priority,
            "notes": m.notes,
            "isActive": m.isActive,
        }
        for m in rows
    ]


@router.post("/mappings")
async def create_mapping(
    payload: HazardMappingCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    await _require(db, user, "SKILL_MATRIX.COMPETENCY_CONFIGURE", payload.plantId)
    m = HazardToSkillMapping(
        plantId=payload.plantId,
        sourceModule=payload.sourceModule,
        classificationField=payload.classificationField,
        classificationValue=payload.classificationValue,
        matchMode=payload.matchMode,
        competencyId=payload.competencyId,
        priority=payload.priority,
        notes=payload.notes,
        isActive=payload.isActive,
        createdBy=user.id,
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return {"id": m.id}


@router.patch("/mappings/{mapping_id}")
async def update_mapping(
    mapping_id: str,
    payload: HazardMappingUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    m = await db.get(HazardToSkillMapping, mapping_id)
    if m is None or m.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mapping not found")
    await _require(db, user, "SKILL_MATRIX.COMPETENCY_CONFIGURE", m.plantId)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(m, field, value)
    m.updatedBy = user.id
    await db.commit()
    return {"id": m.id}


@router.delete("/mappings/{mapping_id}")
async def delete_mapping(
    mapping_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    m = await db.get(HazardToSkillMapping, mapping_id)
    if m is None or m.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mapping not found")
    await _require(db, user, "SKILL_MATRIX.COMPETENCY_CONFIGURE", m.plantId)
    m.isDeleted = True
    m.updatedBy = user.id
    await db.commit()
    return {"id": m.id, "deleted": True}


# ── RULE CONFIG (thresholds / windows) ───────────────────────────────────────
@router.get("/config")
async def get_config(
    plantId: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    await _require(db, user, "SKILL_MATRIX.READ")
    view = await resolve_config(db, plantId)
    rows = (await db.execute(select(TrainingRuleConfig))).scalars().all()
    return {
        "effective": {
            "thresholdCount": view.thresholdCount,
            "thresholdWindowDays": view.thresholdWindowDays,
            "severitySifImmediate": view.severitySifImmediate,
            "severityThreshold": view.severityThreshold,
            "recertWindowDays": view.recertWindowDays,
            "assignmentDueDays": view.assignmentDueDays,
            "correlationWindowDays": view.correlationWindowDays,
            "personFlagThreshold": view.personFlagThreshold,
            "personFlagWindowDays": view.personFlagWindowDays,
            "personRiskElevated": view.personRiskElevated,
            "personRiskHigh": view.personRiskHigh,
            "personRiskCritical": view.personRiskCritical,
        },
        "rows": [
            {"id": r.id, "plantId": r.plantId, "thresholdCount": r.thresholdCount,
             "thresholdWindowDays": r.thresholdWindowDays, "severitySifImmediate": r.severitySifImmediate,
             "severityThreshold": r.severityThreshold, "recertWindowDays": r.recertWindowDays,
             "assignmentDueDays": r.assignmentDueDays, "correlationWindowDays": r.correlationWindowDays,
             "personFlagThreshold": getattr(r, "personFlagThreshold", None),
             "personFlagWindowDays": getattr(r, "personFlagWindowDays", None),
             "personRiskElevated": getattr(r, "personRiskElevated", None),
             "personRiskHigh": getattr(r, "personRiskHigh", None),
             "personRiskCritical": getattr(r, "personRiskCritical", None),
             "isActive": r.isActive}
            for r in rows
        ],
    }


@router.put("/config")
async def upsert_config(
    payload: RuleConfigUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    await _require(db, user, "SKILL_MATRIX.COMPETENCY_CONFIGURE", payload.plantId)
    existing = (
        await db.execute(
            select(TrainingRuleConfig).where(
                TrainingRuleConfig.plantId == payload.plantId if payload.plantId else TrainingRuleConfig.plantId.is_(None)
            )
        )
    ).scalar_one_or_none()
    fields = payload.model_dump(exclude_unset=True, exclude={"plantId"})
    if existing is None:
        existing = TrainingRuleConfig(plantId=payload.plantId, createdBy=user.id, **fields)
        db.add(existing)
    else:
        for k, v in fields.items():
            setattr(existing, k, v)
        existing.updatedBy = user.id
    await db.commit()
    await db.refresh(existing)
    return {"id": existing.id, "plantId": existing.plantId}


# ── CONTENT ADAPTER (vendor-decoupled) ───────────────────────────────────────
def _content_dict(c: TrainingContent, comp_name: str | None = None) -> dict:
    return {
        "id": c.id,
        "competencyId": c.competencyId,
        "competencyName": comp_name,
        "title": c.title,
        "description": c.description,
        "contentType": c.contentType,
        "deliveryMode": c.deliveryMode,
        "contentRef": c.contentRef,
        "vendorId": c.vendorId,
        "vendorName": c.vendorName,
        "durationMinutes": c.durationMinutes,
        "passingScore": c.passingScore,
        "language": c.language,
        "isActive": c.isActive,
        "isPrimary": c.isPrimary,
        "plantId": c.plantId,
    }


@router.get("/content")
async def list_content(
    competencyId: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    await _require(db, user, "SKILL_MATRIX.READ")
    stmt = select(TrainingContent).where(TrainingContent.isDeleted.is_(False))
    if competencyId:
        stmt = stmt.where(TrainingContent.competencyId == competencyId)
    rows = (await db.execute(stmt.order_by(TrainingContent.competencyId.asc()))).scalars().all()
    comp_map, _ = await _name_maps(db, {r.competencyId for r in rows}, set())
    return [_content_dict(c, comp_map.get(c.competencyId)) for c in rows]


@router.post("/content")
async def create_content(
    payload: ContentCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    await _require(db, user, "SKILL_MATRIX.COMPETENCY_CONFIGURE", payload.plantId)
    c = TrainingContent(**payload.model_dump(), createdBy=user.id)
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return _content_dict(c)


@router.patch("/content/{content_id}")
async def update_content(
    content_id: str,
    payload: ContentUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    c = await db.get(TrainingContent, content_id)
    if c is None or c.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Content not found")
    await _require(db, user, "SKILL_MATRIX.COMPETENCY_CONFIGURE", c.plantId)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(c, field, value)
    c.updatedBy = user.id
    await db.commit()
    await db.refresh(c)
    return _content_dict(c)


@router.delete("/content/{content_id}")
async def delete_content(
    content_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    c = await db.get(TrainingContent, content_id)
    if c is None or c.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Content not found")
    await _require(db, user, "SKILL_MATRIX.COMPETENCY_CONFIGURE", c.plantId)
    c.isDeleted = True
    c.updatedBy = user.id
    await db.commit()
    return {"id": c.id, "deleted": True}


# ── CORRELATION REPORT (spec §D) ─────────────────────────────────────────────
@router.get("/correlation")
async def correlation_report(
    plantId: str | None = Query(None),
    competencyId: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    scope = await _read_scope(db, user)
    if plantId:
        if not scope.allows_plant(plantId):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied for this plant")
        plant_ids = [plantId]
    else:
        plant_ids = None if scope.all_plants else scope.plant_ids
    rows = await compute_report(db, plant_ids=plant_ids, competency_id=competencyId)
    return {"generatedAt": datetime.now(timezone.utc).isoformat(), "rows": rows}


# ── PERSON-RISK ANALYTICS (auto-flag repeat-involved workers) ────────────────
def _flag_dict(f: WorkerTrainingFlag, worker: dict | None) -> dict:
    return {
        "id": f.id,
        "plantId": f.plantId,
        "personUserId": f.personUserId,
        "worker": worker,
        "riskScore": f.riskScore,
        "riskBand": f.riskBand,
        "windowDays": f.windowDays,
        "incidentCount": f.incidentCount,
        "nearMissCount": f.nearMissCount,
        "observationCount": f.observationCount,
        "sifCount": f.sifCount,
        "totalEvents": f.totalEvents,
        "recommendedCompetencies": f.recommendedCompetencies or [],
        "mappedCompetencyIds": f.mappedCompetencyIds or [],
        "assignmentIds": f.assignmentIds or [],
        "status": f.status,
        "flaggedAt": f.flaggedAt.isoformat() if f.flaggedAt else None,
        "lastEvaluatedAt": f.lastEvaluatedAt.isoformat() if f.lastEvaluatedAt else None,
        "acknowledgedBy": f.acknowledgedBy,
        "clearedBy": f.clearedBy,
    }


@router.get("/person-risk")
async def list_person_risk(
    status_filter: str | None = Query(None, alias="status"),
    riskBand: str | None = Query(None),
    plantId: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Flagged-worker list — the person-risk leaderboard (plant-scoped)."""
    scope = await _read_scope(db, user)
    stmt = select(WorkerTrainingFlag)
    stmt = scope.apply(stmt, WorkerTrainingFlag)
    if status_filter:
        stmt = stmt.where(WorkerTrainingFlag.status == status_filter)
    if riskBand:
        stmt = stmt.where(WorkerTrainingFlag.riskBand == riskBand)
    if plantId and scope.allows_plant(plantId):
        stmt = stmt.where(WorkerTrainingFlag.plantId == plantId)
    stmt = stmt.order_by(WorkerTrainingFlag.riskScore.desc())
    rows = (await db.execute(stmt)).scalars().all()

    _c, user_map = await _name_maps(db, set(), {r.personUserId for r in rows})
    items = [_flag_dict(r, user_map.get(r.personUserId)) for r in rows]
    by_band: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for r in rows:
        by_band[r.riskBand] = by_band.get(r.riskBand, 0) + 1
        by_status[r.status] = by_status.get(r.status, 0) + 1
    return {
        "items": items,
        "summary": {
            "total": len(items),
            "byBand": by_band,
            "byStatus": by_status,
            "critical": by_band.get("critical", 0),
            "high": by_band.get("high", 0),
        },
    }


@router.get("/person-risk/{user_id}")
async def get_person_risk(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Live worker-risk detail — event timeline, score breakdown, recommended
    training, and any persisted flag."""
    person = await db.get(User, user_id)
    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user_id != user.id:
        scope = await _read_scope(db, user)
        if not scope.allows_plant(person.plantId):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    detail = await person_risk.compute_person_detail(db, user_id)
    detail["worker"] = {
        "id": person.id, "name": person.name, "role": person.role,
        "department": person.department, "plantId": person.plantId, "designation": person.designation,
    }
    return detail


@router.post("/person-risk/{user_id}/assign")
async def assign_person_risk_training(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Assign the flagged worker the training their events map to (on demand)."""
    person = await db.get(User, user_id)
    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    await _require(db, user, "SKILL_MATRIX.ASSESS", person.plantId)
    return await person_risk.assign_now(db, user_id, actor_id=user.id)


@router.post("/person-risk/{user_id}/acknowledge")
async def acknowledge_person_risk(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    f = (await db.execute(select(WorkerTrainingFlag).where(WorkerTrainingFlag.personUserId == user_id))).scalar_one_or_none()
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No risk flag for this worker")
    await _require(db, user, "SKILL_MATRIX.ASSESS", f.plantId)
    f.status = "acknowledged"
    f.acknowledgedBy = user.id
    f.acknowledgedAt = datetime.now(timezone.utc)
    await db.commit()
    return {"id": f.id, "status": f.status}


@router.post("/person-risk/{user_id}/clear")
async def clear_person_risk(
    user_id: str,
    reason: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    f = (await db.execute(select(WorkerTrainingFlag).where(WorkerTrainingFlag.personUserId == user_id))).scalar_one_or_none()
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No risk flag for this worker")
    await _require(db, user, "SKILL_MATRIX.ASSESS", f.plantId)
    f.status = "cleared"
    f.clearedBy = user.id
    f.clearedAt = datetime.now(timezone.utc)
    f.clearReason = reason
    await db.commit()
    return {"id": f.id, "status": f.status}


@router.post("/person-risk/scan")
async def run_person_risk_scan_now(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Run the person-risk scan on demand (admin) — normally the person_risk_scan
    scheduler job runs every 6h."""
    await _require(db, user, "SKILL_MATRIX.COMPETENCY_CONFIGURE")
    scope = await _read_scope(db, user)
    plant_ids = None if scope.all_plants else scope.plant_ids
    return await person_risk.run_person_risk_scan(db, plant_ids=plant_ids)
