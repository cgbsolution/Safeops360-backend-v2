"""MOC (Management of Change) router. Mounts at /api/moc.

Phase 1 vertical slice — the change-request register + lifecycle. Reads are
open (like the EAI/Skill-Matrix reads); writes (create / transition / approve)
require an authenticated user.

Endpoints:
  GET  /api/moc/metrics                         — landing aggregates for a plant
  GET  /api/moc/change-requests                 — list (filterable)
  GET  /api/moc/change-requests/{id}            — detail + children
  POST /api/moc/change-requests                 — create (draft or submitted)
  POST /api/moc/change-requests/{id}/transition — advance lifecycle state
  POST /api/moc/change-requests/{id}/approve    — record an approval decision
  GET  /api/moc/freezes                         — active change freezes

The full spec covers five workflows, PSSR, cascading re-reviews and ten
cross-module integrations — those are later phases. This slice makes the
register real, browsable, and minimally interactive.
"""

from __future__ import annotations

from datetime import datetime, timezone

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.moc import (
    ChangeRequest,
    MocApprovalStep,
    MocDependentRecord,
    MocFreeze,
    MocImpactAssessment,
    MocStateHistory,
)
from app.models.plant import Plant
from app.models.user import User

router = APIRouter(prefix="/api/moc", tags=["moc"])

# Closed set of lifecycle states (spec §3.1). Used to validate transitions.
VALID_STATES = frozenset(
    {
        "draft",
        "submitted",
        "under_classification_review",
        "under_impact_assessment",
        "under_technical_review",
        "under_approval",
        "approved_pending_implementation",
        "implementation_in_progress",
        "pre_startup_review",
        "implementation_complete_pending_verification",
        "under_post_implementation_review",
        "closed_successful",
        "closed_aborted",
        "closed_rejected",
        "withdrawn",
        "expired",
        "rolled_back",
    }
)

CLOSED_STATES = frozenset(
    {"closed_successful", "closed_aborted", "closed_rejected", "withdrawn", "expired", "rolled_back"}
)


# ─── Payloads ─────────────────────────────────────────────────────────


class ChangeRequestCreate(BaseModel):
    plantId: str
    title: str
    description: str
    category: str
    subcategory: str | None = None
    classification: str = "minor"
    isTemporary: bool = False
    temporaryExpiryDate: datetime | None = None
    origin: str = "operational_request"
    departmentId: str | None = None
    affectedDepartments: list[str] = []
    affectedLocations: list[str] = []
    affectedEquipmentIds: list[str] = []
    affectedProcesses: list[str] = []
    affectedRoles: list[str] = []
    businessJustification: str | None = None
    expectedBenefits: str | None = None
    costEstimate: float | None = None
    costCurrency: str = "INR"
    proposedImplementationDate: datetime | None = None
    targetCompletionDate: datetime | None = None
    submit: bool = False  # false → save draft; true → submit immediately


class TransitionPayload(BaseModel):
    toStatus: str
    rationale: str | None = None


class ApprovalPayload(BaseModel):
    decision: str  # approved | rejected | conditional | abstained
    rationale: str
    conditions: str | None = None


class DependentRecordUpdate(BaseModel):
    # in_progress | completed | not_applicable_confirmed
    updateStatus: str
    updateEvidence: str | None = None


# ─── Helpers ──────────────────────────────────────────────────────────


async def _generate_moc_number(db: AsyncSession, plant: Plant) -> str:
    year = datetime.now(timezone.utc).year
    prefix = f"MOC-{year}-{plant.code}-"
    existing = (
        await db.execute(select(ChangeRequest.number).where(ChangeRequest.number.like(f"{prefix}%")))
    ).scalars().all()
    max_n = 0
    for n in existing:
        try:
            v = int(n.rsplit("-", 1)[-1])
            max_n = max(max_n, v)
        except ValueError:
            continue
    return f"{prefix}{max_n + 1:04d}"


def _cr_list_item(cr: ChangeRequest) -> dict:
    return {
        "id": cr.id,
        "number": cr.number,
        "title": cr.title,
        "category": cr.category,
        "subcategory": cr.subcategory,
        "classification": cr.classification,
        "status": cr.status,
        "isTemporary": cr.isTemporary,
        "temporaryExpiryDate": cr.temporaryExpiryDate.isoformat() if cr.temporaryExpiryDate else None,
        "origin": cr.origin,
        "plantId": cr.plantId,
        "departmentId": cr.departmentId,
        "initiatedByUserId": cr.initiatedByUserId,
        "initiatedAt": cr.initiatedAt.isoformat() if cr.initiatedAt else None,
        "targetCompletionDate": cr.targetCompletionDate.isoformat() if cr.targetCompletionDate else None,
        "overallResidualRisk": cr.overallResidualRisk,
    }


# ─── Reads ────────────────────────────────────────────────────────────


@router.get("/metrics")
async def metrics(plantId: str = Query(...), db: AsyncSession = Depends(get_db)) -> dict:
    rows = (
        await db.execute(select(ChangeRequest).where(ChangeRequest.plantId == plantId))
    ).scalars().all()

    by_status: dict[str, int] = {}
    by_classification: dict[str, int] = {}
    active = 0
    overdue = 0
    temp_expiring = 0
    now = datetime.now(timezone.utc)
    horizon = now.timestamp() + 30 * 86400

    for cr in rows:
        by_status[cr.status] = by_status.get(cr.status, 0) + 1
        by_classification[cr.classification] = by_classification.get(cr.classification, 0) + 1
        if cr.status not in CLOSED_STATES:
            active += 1
            if cr.targetCompletionDate and cr.targetCompletionDate.timestamp() < now.timestamp():
                overdue += 1
        if (
            cr.isTemporary
            and cr.temporaryExpiryDate
            and cr.returnToNormalCompletedAt is None
            and now.timestamp() <= cr.temporaryExpiryDate.timestamp() <= horizon
        ):
            temp_expiring += 1

    closed_successful = by_status.get("closed_successful", 0)

    return {
        "plantId": plantId,
        "total": len(rows),
        "active": active,
        "overdue": overdue,
        "temporaryExpiringSoon": temp_expiring,
        "closedSuccessful": closed_successful,
        "byStatus": by_status,
        "byClassification": by_classification,
    }


@router.get("/change-requests")
async def list_change_requests(
    plantId: str = Query(...),
    status_: str | None = Query(None, alias="status"),
    category: str | None = Query(None),
    classification: str | None = Query(None),
    q: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(ChangeRequest).where(ChangeRequest.plantId == plantId)
    if status_:
        stmt = stmt.where(ChangeRequest.status == status_)
    if category:
        stmt = stmt.where(ChangeRequest.category == category)
    if classification:
        stmt = stmt.where(ChangeRequest.classification == classification)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(ChangeRequest.title).like(like),
                func.lower(ChangeRequest.number).like(like),
            )
        )
    stmt = stmt.order_by(ChangeRequest.initiatedAt.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return {"items": [_cr_list_item(cr) for cr in rows], "total": len(rows)}


@router.get("/change-requests/{cr_id}")
async def get_change_request(cr_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    cr = await db.get(ChangeRequest, cr_id)
    if cr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Change request not found")

    steps = (
        await db.execute(
            select(MocApprovalStep)
            .where(MocApprovalStep.changeRequestId == cr_id)
            .order_by(MocApprovalStep.sequence.asc())
        )
    ).scalars().all()
    deps = (
        await db.execute(
            select(MocDependentRecord).where(MocDependentRecord.changeRequestId == cr_id)
        )
    ).scalars().all()
    history = (
        await db.execute(
            select(MocStateHistory)
            .where(MocStateHistory.changeRequestId == cr_id)
            .order_by(MocStateHistory.transitionedAt.asc())
        )
    ).scalars().all()
    ia = (
        await db.execute(
            select(MocImpactAssessment).where(MocImpactAssessment.changeRequestId == cr_id)
        )
    ).scalar_one_or_none()

    item = _cr_list_item(cr)
    item.update(
        {
            "description": cr.description,
            "businessJustification": cr.businessJustification,
            "expectedBenefits": cr.expectedBenefits,
            "costEstimate": cr.costEstimate,
            "costCurrency": cr.costCurrency,
            "affectedDepartments": cr.affectedDepartments or [],
            "affectedLocations": cr.affectedLocations or [],
            "affectedEquipmentIds": cr.affectedEquipmentIds or [],
            "affectedProcesses": cr.affectedProcesses or [],
            "affectedRoles": cr.affectedRoles or [],
            "proposedImplementationDate": cr.proposedImplementationDate.isoformat()
            if cr.proposedImplementationDate
            else None,
            "actualImplementationDate": cr.actualImplementationDate.isoformat()
            if cr.actualImplementationDate
            else None,
            "actualCompletionDate": cr.actualCompletionDate.isoformat()
            if cr.actualCompletionDate
            else None,
            "riskLevels": {
                "safety": cr.safetyRiskLevel,
                "environmental": cr.environmentalRiskLevel,
                "quality": cr.qualityRiskLevel,
                "operational": cr.operationalRiskLevel,
                "overallResidual": cr.overallResidualRisk,
            },
            "pssrRequired": cr.pssrRequired,
            "pssrOutcome": cr.pssrOutcome,
            "spawnedFromCapaId": cr.spawnedFromCapaId,
            "approvalSteps": [
                {
                    "id": s.id,
                    "sequence": s.sequence,
                    "role": s.role,
                    "specificUserId": s.specificUserId,
                    "isRequired": s.isRequired,
                    "decision": s.decision,
                    "decidedAt": s.decidedAt.isoformat() if s.decidedAt else None,
                    "decidedByUserId": s.decidedByUserId,
                    "rationale": s.rationale,
                    "conditions": s.conditions,
                }
                for s in steps
            ],
            "dependentRecords": [
                {
                    "id": d.id,
                    "recordType": d.recordType,
                    "recordId": d.recordId,
                    "recordReference": d.recordReference,
                    "impactType": d.impactType,
                    "impactDescription": d.impactDescription,
                    "updateStatus": d.updateStatus,
                }
                for d in deps
            ],
            "stateHistory": [
                {
                    "fromState": h.fromState,
                    "toState": h.toState,
                    "transitionedAt": h.transitionedAt.isoformat() if h.transitionedAt else None,
                    "transitionedByUserId": h.transitionedByUserId,
                    "rationale": h.rationale,
                }
                for h in history
            ],
            "impactAssessment": (
                {
                    "assessorUserId": ia.assessorUserId,
                    "assessorRole": ia.assessorRole,
                    "methodology": ia.methodology,
                    "dimensions": ia.dimensions,
                    "recommendedClassification": ia.recommendedClassification,
                    "pssrRequired": ia.pssrRequired,
                    "rollbackPlanRequired": ia.rollbackPlanRequired,
                }
                if ia
                else None
            ),
        }
    )
    return item


@router.get("/freezes")
async def list_freezes(
    plantId: str | None = Query(None), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    rows = (
        await db.execute(select(MocFreeze).where(MocFreeze.isActive.is_(True)))
    ).scalars().all()
    out = []
    for f in rows:
        if plantId and f.plantIds and plantId not in f.plantIds:
            continue  # scoped freeze that doesn't cover this plant
        out.append(
            {
                "id": f.id,
                "plantIds": f.plantIds or [],
                "reason": f.reason,
                "reasonDetail": f.reasonDetail,
                "startsAt": f.startsAt.isoformat() if f.startsAt else None,
                "endsAt": f.endsAt.isoformat() if f.endsAt else None,
                "exceptionsAllowed": f.exceptionsAllowed,
            }
        )
    return out


# ─── Writes ───────────────────────────────────────────────────────────


@router.post("/change-requests", status_code=status.HTTP_201_CREATED)
async def create_change_request(
    payload: ChangeRequestCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plant not found")

    # Block submission (not draft) when an active freeze covers this plant.
    if payload.submit:
        freezes = (
            await db.execute(select(MocFreeze).where(MocFreeze.isActive.is_(True)))
        ).scalars().all()
        for f in freezes:
            covers_plant = (not f.plantIds) or (payload.plantId in f.plantIds)
            covers_cat = (not f.categoryFilters) or (payload.category in f.categoryFilters)
            if covers_plant and covers_cat and not f.exceptionsAllowed:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"A change freeze is active ({f.reason}); submission is blocked.",
                )

    number = await _generate_moc_number(db, plant)
    initial_status = "submitted" if payload.submit else "draft"
    cr = ChangeRequest(
        plantId=payload.plantId,
        number=number,
        title=payload.title,
        description=payload.description,
        category=payload.category,
        subcategory=payload.subcategory,
        classification=payload.classification,
        isTemporary=payload.isTemporary,
        temporaryExpiryDate=payload.temporaryExpiryDate,
        origin=payload.origin,
        departmentId=payload.departmentId,
        affectedDepartments=payload.affectedDepartments,
        affectedLocations=payload.affectedLocations,
        affectedEquipmentIds=payload.affectedEquipmentIds,
        affectedProcesses=payload.affectedProcesses,
        affectedRoles=payload.affectedRoles,
        initiatedByUserId=user.id,
        businessJustification=payload.businessJustification,
        expectedBenefits=payload.expectedBenefits,
        costEstimate=payload.costEstimate,
        costCurrency=payload.costCurrency,
        proposedImplementationDate=payload.proposedImplementationDate,
        targetCompletionDate=payload.targetCompletionDate,
        status=initial_status,
    )
    db.add(cr)
    await db.flush()
    db.add(
        MocStateHistory(
            changeRequestId=cr.id,
            fromState=None,
            toState=initial_status,
            transitionedByUserId=user.id,
            rationale="Change request created",
        )
    )
    await db.commit()
    await db.refresh(cr)
    return {"id": cr.id, "number": cr.number, "status": cr.status}


@router.post("/change-requests/{cr_id}/transition")
async def transition(
    cr_id: str,
    payload: TransitionPayload,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    if payload.toStatus not in VALID_STATES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown status '{payload.toStatus}'")
    cr = await db.get(ChangeRequest, cr_id)
    if cr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Change request not found")
    if cr.status in CLOSED_STATES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Change request is closed ({cr.status})")

    # Closure gate — the heart of MOC: a change cannot close successfully until
    # every dependent register it affects has been updated (or explicitly
    # confirmed N/A). This is what keeps HIRA/EAI/Training/Skill-Matrix honest.
    if payload.toStatus == "closed_successful":
        deps = (
            await db.execute(
                select(MocDependentRecord).where(MocDependentRecord.changeRequestId == cr_id)
            )
        ).scalars().all()
        blocking = [
            d
            for d in deps
            if d.impactType in ("must_update", "must_review", "must_create")
            and d.updateStatus not in ("completed", "not_applicable_confirmed")
        ]
        if blocking:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Cannot close: {len(blocking)} dependent record(s) still need updating — "
                "update or confirm N/A on each before closure.",
            )

    prev = cr.status
    cr.status = payload.toStatus
    if payload.toStatus == "implementation_in_progress" and cr.actualImplementationDate is None:
        cr.actualImplementationDate = datetime.now(timezone.utc)
    if payload.toStatus == "closed_successful" and cr.actualCompletionDate is None:
        cr.actualCompletionDate = datetime.now(timezone.utc)

    # Cascade trigger: when implementation completes, the dependent registers
    # become due for re-review. Move not-yet-started records to in_progress so
    # the outstanding re-reviews are surfaced and the closure gate enforces them.
    cascaded = 0
    if payload.toStatus == "implementation_complete_pending_verification":
        deps = (
            await db.execute(
                select(MocDependentRecord).where(MocDependentRecord.changeRequestId == cr_id)
            )
        ).scalars().all()
        for d in deps:
            if d.updateStatus == "not_started":
                d.updateStatus = "in_progress"
                cascaded += 1

    db.add(
        MocStateHistory(
            changeRequestId=cr.id,
            fromState=prev,
            toState=payload.toStatus,
            transitionedByUserId=user.id,
            rationale=payload.rationale,
        )
    )
    await db.commit()
    return {"id": cr.id, "status": cr.status, "previousStatus": prev, "cascadedReReviews": cascaded}


@router.post("/change-requests/{cr_id}/approve")
async def approve(
    cr_id: str,
    payload: ApprovalPayload,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    cr = await db.get(ChangeRequest, cr_id)
    if cr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Change request not found")

    steps = (
        await db.execute(
            select(MocApprovalStep)
            .where(MocApprovalStep.changeRequestId == cr_id)
            .order_by(MocApprovalStep.sequence.asc())
        )
    ).scalars().all()
    pending = next((s for s in steps if s.decision == "pending"), None)
    if pending is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "No pending approval step")

    pending.decision = payload.decision
    pending.decidedAt = datetime.now(timezone.utc)
    pending.decidedByUserId = user.id
    pending.rationale = payload.rationale
    pending.conditions = payload.conditions

    new_status = cr.status
    if payload.decision == "rejected":
        new_status = "closed_rejected"
    else:
        still_pending = any(s.decision == "pending" and s.isRequired for s in steps if s.id != pending.id)
        if not still_pending:
            new_status = "approved_pending_implementation"

    if new_status != cr.status:
        prev = cr.status
        cr.status = new_status
        db.add(
            MocStateHistory(
                changeRequestId=cr.id,
                fromState=prev,
                toState=new_status,
                transitionedByUserId=user.id,
                rationale=f"Approval step {pending.sequence} {payload.decision}",
            )
        )
    await db.commit()
    return {"id": cr.id, "status": cr.status, "stepDecision": payload.decision}


@router.patch("/change-requests/{cr_id}/dependent-records/{dep_id}")
async def update_dependent_record(
    cr_id: str,
    dep_id: str,
    payload: DependentRecordUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    if payload.updateStatus not in ("not_started", "in_progress", "completed", "not_applicable_confirmed"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid updateStatus")
    dep = await db.get(MocDependentRecord, dep_id)
    if dep is None or dep.changeRequestId != cr_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dependent record not found")
    dep.updateStatus = payload.updateStatus
    dep.updateEvidence = payload.updateEvidence
    dep.updatedByUserId = user.id
    dep.updatedAt = datetime.now(timezone.utc)
    await db.commit()
    return {"id": dep.id, "updateStatus": dep.updateStatus}


@router.get("/active-for-equipment")
async def active_for_equipment(
    equipmentId: str = Query(...), db: AsyncSession = Depends(get_db)
) -> dict:
    """Active (non-closed) change requests affecting a given equipment id.

    The PTW module calls this at permit creation — equipment under an active
    MOC should warn the issuer / require elevated authorisation (spec §6.6).
    """
    rows = (
        await db.execute(
            select(ChangeRequest).where(~ChangeRequest.status.in_(CLOSED_STATES))
        )
    ).scalars().all()
    matches = [cr for cr in rows if equipmentId in (cr.affectedEquipmentIds or [])]
    implementing = any(cr.status == "implementation_in_progress" for cr in matches)
    return {
        "equipmentId": equipmentId,
        "hasActiveMoc": len(matches) > 0,
        "implementationInProgress": implementing,
        "changeRequests": [
            {"id": cr.id, "number": cr.number, "title": cr.title, "status": cr.status}
            for cr in matches
        ],
    }


@router.get("/export.csv")
async def export_csv(plantId: str = Query(...), db: AsyncSession = Depends(get_db)) -> Response:
    rows = (
        await db.execute(
            select(ChangeRequest)
            .where(ChangeRequest.plantId == plantId)
            .order_by(ChangeRequest.initiatedAt.desc())
        )
    ).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "Number",
            "Title",
            "Category",
            "Classification",
            "Status",
            "Temporary",
            "Origin",
            "Overall residual risk",
            "Cost estimate",
            "Initiated",
            "Target completion",
        ]
    )
    for cr in rows:
        w.writerow(
            [
                cr.number,
                cr.title,
                cr.category,
                cr.classification,
                cr.status,
                "Yes" if cr.isTemporary else "No",
                cr.origin,
                cr.overallResidualRisk or "",
                cr.costEstimate if cr.costEstimate is not None else "",
                cr.initiatedAt.date().isoformat() if cr.initiatedAt else "",
                cr.targetCompletionDate.date().isoformat() if cr.targetCompletionDate else "",
            ]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="moc-register.csv"'},
    )
