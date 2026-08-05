"""MOC (Management of Change) router. Mounts at /api/moc.

Phase 1 vertical slice — the change-request register + lifecycle. Every
endpoint requires an authenticated user; reads are scoped to the caller's
MOC.READ plants and writes/approvals are authorised per-record via can()
(MOC.CREATE / MOC.UPDATE / MOC.APPROVE) against the change request's plant.

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

from datetime import datetime, timedelta, timezone
from typing import Any

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.equipment import Equipment
from app.models.moc import (
    ChangeRequest,
    MocApprovalStep,
    MocAttachment,
    MocDependentRecord,
    MocFreeze,
    MocImpactAssessment,
    MocStateHistory,
)
from app.models.plant import Plant
from app.models.training import TrainingCertificate
from app.models.user import Role, User, UserRole
from app.services.access_scope import build_query_scope
from app.services.storage import (
    build_moc_storage_path,
    create_signed_download_url,
    create_signed_upload_url,
    is_storage_configured,
)

# Emergency changes must obtain retroactive approval within this window.
EMERGENCY_RETRO_APPROVAL_HOURS = 72
# Default reviewer SLA before an approval is flagged overdue / escalated.
DEFAULT_ESCALATION_DAYS = 5
# The six departments surfaced in the Step-3 impact checklist.
IMPACT_DEPARTMENTS = ["safety", "engineering", "operations", "quality", "environmental", "maintenance"]

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


class ReviewerInput(BaseModel):
    role: str
    specificUserId: str | None = None
    isRequired: bool = True


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

    # ── Gensuite-parity 5-step wizard fields (all optional for back-compat) ──
    urgency: str = "standard"  # standard | emergency
    linkedMocIds: list[str] = []
    psmApplicable: bool = False
    psmDetails: dict | None = None
    riskMatrixPre: dict | None = None
    riskMatrixResidual: dict | None = None
    hazardCategories: list[str] = []
    mitigations: str | None = None
    departmentImpact: dict | None = None
    trainingRequired: bool = False
    trainingCertificateId: str | None = None
    reviewers: list[ReviewerInput] = []  # Step 4 → builds the approval chain
    escalationDays: int = DEFAULT_ESCALATION_DAYS

    submit: bool = False  # false → save draft; true → submit immediately


class TransitionPayload(BaseModel):
    toStatus: str
    rationale: str | None = None


class PssrPayload(BaseModel):
    # items: [{label, verdict (pass|fail|partial|na), note?}]
    items: list[dict]
    outcome: str  # go | no_go | conditional_go | deferred


class EffectivenessReviewPayload(BaseModel):
    effective: bool
    newRisks: bool = False
    notes: str | None = None
    cadenceDays: int | None = None


class AttachmentInit(BaseModel):
    category: str
    fileName: str
    fileSize: int
    mimeType: str


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


def _aware(d: datetime | None) -> datetime | None:
    """Normalise a possibly-naive datetime to timezone-aware UTC."""
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def _band_from_score(score: float | int | None) -> str | None:
    """Combined Risk Register / ERM convention (see erm/lib.ts bandForScore):
    likelihood×severity → low(1-4) | moderate(5-9) | high(10-15) | critical(16-25).
    MOC uses lowercase band strings (RISK_CHIP keys) so MEDIUM maps to 'moderate'."""
    if score is None:
        return None
    if score <= 4:
        return "low"
    if score <= 9:
        return "moderate"
    if score <= 15:
        return "high"
    return "critical"


def _residual_risk_string(matrix: dict | None) -> str | None:
    """Derive the MOC overallResidualRisk chip value from a residual risk matrix."""
    if not matrix:
        return None
    band = matrix.get("band")
    if isinstance(band, str):
        return {"LOW": "low", "MEDIUM": "moderate", "HIGH": "high", "CRITICAL": "critical"}.get(
            band.upper(), band.lower()
        )
    return _band_from_score(matrix.get("score"))


def _emergency_pending_retro(cr: ChangeRequest) -> bool:
    """An emergency change that started implementation before its retroactive
    approval was recorded (cleared once the chain is fully approved)."""
    return (
        cr.urgency == "emergency"
        and cr.emergencyRetroApprovalDueAt is not None
        and cr.status not in CLOSED_STATES
    )


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
        "urgency": cr.urgency,
        "emergencyPendingRetro": _emergency_pending_retro(cr),
        "trainingRequired": cr.trainingRequired,
    }


async def _require_moc_read(db: AsyncSession, user: User, plant_id: str | None) -> None:
    """Authorise a MOC read against a specific plant (or plant-agnostic when
    plant_id is None). Uses the permission-specific plant scope so an
    OWN_DEPARTMENT MOC.READ grant can't read another plant's register — fail
    closed for OWN_DEPARTMENT/OWN_RECORDS, unlike can(plant_id=…)."""
    scope = await build_query_scope(db, user.id, "MOC.READ")
    if plant_id is None:
        if scope.all_plants or scope.plant_ids:
            return
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    if not scope.allows_plant(plant_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied for this plant")


async def _fire_moc_hira_review(db: AsyncSession, cr: ChangeRequest) -> None:
    """Cascade an approved change into HIRA re-review cycles for the registers
    it touches (spec §6 — MOC→HIRA). The receiver is debounced, so calling this
    on both the transition and approval paths is safe."""
    from app.services.hira_moc_receiver import trigger_hira_review_for_moc

    await trigger_hira_review_for_moc(
        db,
        moc_id=cr.id,
        change_scope={
            "plant_id": cr.plantId,
            "department_id": cr.departmentId,
            "area_ids": list(cr.affectedLocations or []),
            "equipment_ids": list(cr.affectedEquipmentIds or []),
            "process_codes": list(cr.affectedProcesses or []),
            "implementation_date": cr.proposedImplementationDate or cr.targetCompletionDate,
        },
    )


# ─── Lifecycle gates (training / PSSR / emergency retro-approval) ─────


async def _training_gate_ok(db: AsyncSession, cr: ChangeRequest) -> tuple[bool, str | None]:
    """Spec Step 3 — a change flagged 'training required before go-live' cannot
    reach 'Approved for Implementation' until a completed (ACTIVE) training
    certificate is linked."""
    if not cr.trainingRequired:
        return True, None
    if not cr.trainingCertificateId:
        return (
            False,
            "Training is required before go-live — link a completed training certificate first.",
        )
    cert = await db.get(TrainingCertificate, cr.trainingCertificateId)
    if cert is None or cert.status != "ACTIVE":
        return False, "The linked training certificate is missing or not ACTIVE."
    return True, None


def _pssr_gate_ok(cr: ChangeRequest) -> tuple[bool, str | None]:
    """Spec Step 5 — a change requiring PSSR cannot close until the checklist is
    completed with a passing outcome."""
    if not cr.pssrRequired:
        return True, None
    chk = cr.pssrChecklist or {}
    if not chk.get("completedAt"):
        return False, "A pre-startup safety review (PSSR) must be completed before closure."
    if chk.get("outcome") == "no_go":
        return False, "PSSR outcome is No-Go — resolve the findings before closing."
    return True, None


async def _emergency_retro_ok(db: AsyncSession, cr: ChangeRequest) -> tuple[bool, str | None]:
    """An emergency change that started implementation pre-approval must obtain
    its retroactive approval (all required steps approved) before it can close."""
    if cr.urgency != "emergency" or cr.emergencyRetroApprovalDueAt is None:
        return True, None
    steps = (
        await db.execute(
            select(MocApprovalStep).where(MocApprovalStep.changeRequestId == cr.id)
        )
    ).scalars().all()
    if any(s.isRequired and s.decision == "pending" for s in steps):
        return False, "Emergency change still needs retroactive approval before it can be closed."
    return True, None


# ─── Reads ────────────────────────────────────────────────────────────


@router.get("/metrics")
async def metrics(
    plantId: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    await _require_moc_read(db, user, plantId)
    rows = (
        await db.execute(select(ChangeRequest).where(ChangeRequest.plantId == plantId))
    ).scalars().all()

    by_status: dict[str, int] = {}
    by_classification: dict[str, int] = {}
    by_risk: dict[str, int] = {}
    active = 0
    overdue = 0
    temp_expiring = 0
    temp_expiring_7d = 0
    emergency_pending_retro = 0
    overdue_approvals = 0
    now = datetime.now(timezone.utc)
    horizon = now.timestamp() + 30 * 86400
    horizon_7d = now.timestamp() + 7 * 86400
    escalation_cutoff = now - timedelta(days=DEFAULT_ESCALATION_DAYS)

    for cr in rows:
        by_status[cr.status] = by_status.get(cr.status, 0) + 1
        by_classification[cr.classification] = by_classification.get(cr.classification, 0) + 1
        if cr.overallResidualRisk:
            by_risk[cr.overallResidualRisk] = by_risk.get(cr.overallResidualRisk, 0) + 1
        if cr.status not in CLOSED_STATES:
            active += 1
            if cr.targetCompletionDate and cr.targetCompletionDate.timestamp() < now.timestamp():
                overdue += 1
        if _emergency_pending_retro(cr):
            emergency_pending_retro += 1
        if cr.status == "under_approval" and cr.updatedAt and _aware(cr.updatedAt) < escalation_cutoff:
            overdue_approvals += 1
        if (
            cr.isTemporary
            and cr.temporaryExpiryDate
            and cr.returnToNormalCompletedAt is None
        ):
            if now.timestamp() <= cr.temporaryExpiryDate.timestamp() <= horizon:
                temp_expiring += 1
            if now.timestamp() <= cr.temporaryExpiryDate.timestamp() <= horizon_7d:
                temp_expiring_7d += 1

    closed_successful = by_status.get("closed_successful", 0)

    return {
        "plantId": plantId,
        "total": len(rows),
        "active": active,
        "overdue": overdue,
        "temporaryExpiringSoon": temp_expiring,
        "temporaryExpiring7d": temp_expiring_7d,
        "emergencyPendingRetro": emergency_pending_retro,
        "overdueApprovals": overdue_approvals,
        "closedSuccessful": closed_successful,
        "byStatus": by_status,
        "byClassification": by_classification,
        "byRisk": by_risk,
    }


@router.get("/change-requests")
async def list_change_requests(
    plantId: str = Query(...),
    status_: str | None = Query(None, alias="status"),
    category: str | None = Query(None),
    classification: str | None = Query(None),
    q: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    await _require_moc_read(db, user, plantId)
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
    # Newest-created first — platform-wide register convention.
    stmt = stmt.order_by(ChangeRequest.createdAt.desc(), ChangeRequest.id.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return {"items": [_cr_list_item(cr) for cr in rows], "total": len(rows)}


@router.get("/change-requests/{cr_id}")
async def get_change_request(
    cr_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    cr = await db.get(ChangeRequest, cr_id)
    if cr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Change request not found")
    await _require_moc_read(db, user, cr.plantId)

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
    atts = (
        await db.execute(
            select(MocAttachment)
            .where(MocAttachment.changeRequestId == cr_id)
            .where(MocAttachment.deletedAt.is_(None))
            .order_by(MocAttachment.uploadedAt.desc())
        )
    ).scalars().all()

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
            # ── Gensuite-parity wizard fields ──
            "urgency": cr.urgency,
            "emergencyRetroApprovalDueAt": cr.emergencyRetroApprovalDueAt.isoformat()
            if cr.emergencyRetroApprovalDueAt
            else None,
            "emergencyPendingRetro": _emergency_pending_retro(cr),
            "linkedMocIds": cr.linkedMocIds or [],
            "psmApplicable": cr.psmApplicable,
            "psmDetails": cr.psmDetails,
            "riskMatrixPre": cr.riskMatrixPre,
            "riskMatrixResidual": cr.riskMatrixResidual,
            "hazardCategories": cr.hazardCategories or [],
            "mitigations": cr.mitigations,
            "departmentImpact": cr.departmentImpact,
            "trainingRequired": cr.trainingRequired,
            "trainingCertificateId": cr.trainingCertificateId,
            "pssrChecklist": cr.pssrChecklist,
            "effectivenessReview": cr.effectivenessReview,
            "attachments": [
                {
                    "id": a.id,
                    "category": a.category,
                    "fileName": a.fileName,
                    "fileSize": a.fileSize,
                    "mimeType": a.mimeType,
                    "caption": a.caption,
                    "uploadedById": a.uploadedById,
                    "uploadedAt": a.uploadedAt.isoformat() if a.uploadedAt else None,
                }
                for a in atts
            ],
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
    plantId: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    await _require_moc_read(db, user, plantId)
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
    await require_permission_with_context("MOC.CREATE", user, db, plant_id=payload.plantId)

    # A temporary change MUST carry an expiry date (spec Step 1 / verification
    # checklist) — enforced at submission; drafts may still be saved without it.
    if payload.submit and payload.isTemporary and payload.temporaryExpiryDate is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A temporary change requires an expiration date before it can be submitted.",
        )

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
    # PSSR is mandated for the higher-consequence classifications; the closure
    # gate enforces a completed checklist for these.
    pssr_required = payload.classification in ("major", "critical")
    residual_risk = _residual_risk_string(payload.riskMatrixResidual)
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
        # ── Gensuite-parity wizard fields ──
        urgency=payload.urgency if payload.urgency in ("standard", "emergency") else "standard",
        linkedMocIds=payload.linkedMocIds,
        psmApplicable=payload.psmApplicable,
        psmDetails=payload.psmDetails,
        riskMatrixPre=payload.riskMatrixPre,
        riskMatrixResidual=payload.riskMatrixResidual,
        hazardCategories=payload.hazardCategories,
        mitigations=payload.mitigations,
        departmentImpact=payload.departmentImpact,
        trainingRequired=payload.trainingRequired,
        trainingCertificateId=payload.trainingCertificateId,
        overallResidualRisk=residual_risk,
        pssrRequired=pssr_required,
    )
    db.add(cr)
    await db.flush()

    # Step 4 → materialise the approval chain from the selected reviewers.
    for i, rv in enumerate(payload.reviewers, start=1):
        db.add(
            MocApprovalStep(
                changeRequestId=cr.id,
                sequence=i,
                role=rv.role,
                specificUserId=rv.specificUserId,
                isRequired=rv.isRequired,
                decision="pending",
            )
        )

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
    await require_permission_with_context("MOC.UPDATE", user, db, plant_id=cr.plantId)
    # Segregation of duties: reaching the approved state through a raw transition
    # still requires approval authority — otherwise a MOC.UPDATE-only holder could
    # bypass MOC.APPROVE and the approval workflow entirely.
    if payload.toStatus == "approved_pending_implementation":
        await require_permission_with_context("MOC.APPROVE", user, db, plant_id=cr.plantId)
        # Training gate — cannot reach "Approved for Implementation" until a
        # required training record is linked and ACTIVE (spec Step 3).
        train_ok, train_msg = await _training_gate_ok(db, cr)
        if not train_ok:
            raise HTTPException(status.HTTP_409_CONFLICT, train_msg)
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
        # PSSR gate — a completed pre-startup safety review is required to close
        # major/critical changes (spec Step 5).
        pssr_ok, pssr_msg = _pssr_gate_ok(cr)
        if not pssr_ok:
            raise HTTPException(status.HTTP_409_CONFLICT, pssr_msg)
        # Emergency changes must have their retroactive approval on record.
        retro_ok, retro_msg = await _emergency_retro_ok(db, cr)
        if not retro_ok:
            raise HTTPException(status.HTTP_409_CONFLICT, retro_msg)

    prev = cr.status
    cr.status = payload.toStatus

    # I-18: on approval, auto-spawn an implementation-tracking CAPA (MOC_ACTION)
    # so an approved change is always tracked to closure. Idempotent.
    moc_capa = None
    if payload.toStatus == "approved_pending_implementation":
        from app.services.capa_spawn import spawn_moc_capas
        moc_capa = await spawn_moc_capas(db, cr, user.id)
        # MOC→HIRA cascade: flag affected HIRA entries for re-review (spec §6).
        await _fire_moc_hira_review(db, cr)

    if payload.toStatus == "implementation_in_progress" and cr.actualImplementationDate is None:
        cr.actualImplementationDate = datetime.now(timezone.utc)
    # Emergency fast-track: implementation may begin before full approval; the
    # retroactive approval then becomes due within 72h and the closure gate
    # blocks closure until the chain is approved (spec Step 1 — Emergency).
    if (
        payload.toStatus == "implementation_in_progress"
        and cr.urgency == "emergency"
        and prev not in ("approved_pending_implementation", "pre_startup_review")
        and cr.emergencyRetroApprovalDueAt is None
    ):
        cr.emergencyRetroApprovalDueAt = datetime.now(timezone.utc) + timedelta(
            hours=EMERGENCY_RETRO_APPROVAL_HOURS
        )
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
    return {"id": cr.id, "status": cr.status, "previousStatus": prev, "cascadedReReviews": cascaded, "mocCapa": moc_capa}


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
    await require_permission_with_context("MOC.APPROVE", user, db, plant_id=cr.plantId)

    # NOTE: spec §5 wants a mandatory impact/risk assessment before approval, but
    # there is currently no runtime path that CREATES a MocImpactAssessment (it is
    # only produced by prisma/seed-moc.ts). Hard-gating approval on it here would
    # make every app-created change request un-approvable. The gate is therefore
    # deferred until an assessment-create endpoint + UI exist — tracked as a known
    # gap, not enforced, to avoid blocking the live approval flow.

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

    # Training gate — a normal-path change flagged trainingRequired cannot
    # complete approval until an ACTIVE certificate is linked. Skipped for an
    # emergency already mid-implementation so its retro-approval isn't deadlocked
    # (raising here rolls back this decision, so no partial state is committed).
    if new_status == "approved_pending_implementation" and cr.emergencyRetroApprovalDueAt is None:
        train_ok, train_msg = await _training_gate_ok(db, cr)
        if not train_ok:
            raise HTTPException(status.HTTP_409_CONFLICT, train_msg)

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
        # Final approval reached → spawn the implementation-tracking CAPA (I-18,
        # parity with the /transition path) and cascade HIRA re-reviews (spec §6).
        if new_status == "approved_pending_implementation":
            from app.services.capa_spawn import spawn_moc_capas
            await spawn_moc_capas(db, cr, user.id)
            await _fire_moc_hira_review(db, cr)
            # Retroactive approval for an emergency change is now on record —
            # clear the pending-retro flag so the closure gate passes.
            cr.emergencyRetroApprovalDueAt = None
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
    cr = await db.get(ChangeRequest, cr_id)
    if cr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Change request not found")
    await require_permission_with_context("MOC.UPDATE", user, db, plant_id=cr.plantId)
    dep.updateStatus = payload.updateStatus
    dep.updateEvidence = payload.updateEvidence
    dep.updatedByUserId = user.id
    dep.updatedAt = datetime.now(timezone.utc)
    await db.commit()
    return {"id": dep.id, "updateStatus": dep.updateStatus}


@router.get("/active-for-equipment")
async def active_for_equipment(
    equipmentId: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Active (non-closed) change requests affecting a given equipment id.

    The PTW module calls this at permit creation — equipment under an active
    MOC should warn the issuer / require elevated authorisation (spec §6.6).
    Results are scoped to the caller's MOC.READ plants.
    """
    scope = await build_query_scope(db, user.id, "MOC.READ")
    rows = (
        await db.execute(
            select(ChangeRequest).where(~ChangeRequest.status.in_(CLOSED_STATES))
        )
    ).scalars().all()
    matches = [
        cr
        for cr in rows
        if equipmentId in (cr.affectedEquipmentIds or []) and scope.allows_plant(cr.plantId)
    ]
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
async def export_csv(
    plantId: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    await _require_moc_read(db, user, plantId)
    rows = (
        await db.execute(
            select(ChangeRequest)
            .where(ChangeRequest.plantId == plantId)
            # Match the on-screen register order so the export mirrors the list.
            .order_by(ChangeRequest.createdAt.desc(), ChangeRequest.id.desc())
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


# ─── Wizard support: equipment picker + reviewer routing ──────────────


@router.get("/equipment")
async def list_moc_equipment(
    plantId: str = Query(...),
    q: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    """Plant-scoped, searchable equipment for the Step-1 'affected assets'
    multi-select. Scoped to the caller's MOC.READ plants."""
    await _require_moc_read(db, user, plantId)
    stmt = select(Equipment).where(Equipment.plantId == plantId, Equipment.active == True)  # noqa: E712
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Equipment.name).like(like),
                func.lower(Equipment.code).like(like),
                func.lower(Equipment.location).like(like),
            )
        )
    rows = (await db.execute(stmt.order_by(Equipment.name).limit(50))).scalars().all()
    return [
        {"id": r.id, "code": r.code, "name": r.name, "category": r.category, "location": r.location}
        for r in rows
    ]


@router.get("/suggested-reviewers")
async def suggested_reviewers(
    plantId: str = Query(...),
    departments: str | None = Query(None),  # comma-separated impact departments
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Auto-suggest the Step-4 approval chain: DEPARTMENT_HEAD role holders at the
    plant, grouped by the departments flagged 'Affected' in Step 3. Advisory only —
    the initiator can add/remove reviewers manually."""
    await _require_moc_read(db, user, plantId)
    dept_list = [d.strip() for d in (departments or "").split(",") if d.strip()]

    rows = (
        await db.execute(
            select(User)
            .join(UserRole, UserRole.userId == User.id)
            .join(Role, Role.id == UserRole.roleId)
            .where(Role.code == "DEPARTMENT_HEAD", Role.isActive == True)  # noqa: E712
        )
    ).scalars().all()
    # Prefer heads at this plant; fall back to all holders if none are plant-scoped.
    heads = [u for u in rows if u.plantId == plantId] or list(rows)

    def _out(u: User) -> dict:
        return {"userId": u.id, "name": u.name, "role": "DEPARTMENT_HEAD", "department": u.department}

    def _match(u: User, dept: str) -> bool:
        hay = " ".join(filter(None, [u.department or "", u.designation or ""])).lower()
        return dept.lower() in hay

    by_department = {dept: [_out(u) for u in heads if _match(u, dept)] for dept in dept_list}
    return {"byDepartment": by_department, "allHeads": [_out(u) for u in heads]}


# ─── Step 5 lifecycle: PSSR checklist + post-implementation review ────


@router.post("/change-requests/{cr_id}/pssr")
async def submit_pssr(
    cr_id: str,
    payload: PssrPayload,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Record the pre-startup safety review. A go / conditional_go outcome marks
    the checklist complete, which the closure gate requires for major/critical
    changes."""
    cr = await db.get(ChangeRequest, cr_id)
    if cr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Change request not found")
    await require_permission_with_context("MOC.UPDATE", user, db, plant_id=cr.plantId)
    if payload.outcome not in ("go", "no_go", "conditional_go", "deferred"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid PSSR outcome")
    if not payload.items:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "PSSR checklist has no items.")
    for it in payload.items:
        if not it.get("verdict"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Every PSSR item needs a verdict.")

    now = datetime.now(timezone.utc)
    completed = payload.outcome in ("go", "conditional_go")
    cr.pssrChecklist = {
        "items": payload.items,
        "outcome": payload.outcome,
        "completedAt": now.isoformat() if completed else None,
        "completedBy": user.id if completed else None,
    }
    cr.pssrOutcome = payload.outcome
    cr.pssrConductedAt = now
    await db.commit()
    return {"id": cr.id, "pssrOutcome": cr.pssrOutcome, "completed": completed}


@router.post("/change-requests/{cr_id}/effectiveness-review")
async def submit_effectiveness_review(
    cr_id: str,
    payload: EffectivenessReviewPayload,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Record the post-implementation effectiveness review (30/60/90-day)."""
    cr = await db.get(ChangeRequest, cr_id)
    if cr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Change request not found")
    await require_permission_with_context("MOC.UPDATE", user, db, plant_id=cr.plantId)
    now = datetime.now(timezone.utc)
    prior = cr.effectivenessReview or {}
    cr.effectivenessReview = {
        "effective": payload.effective,
        "newRisks": payload.newRisks,
        "notes": payload.notes,
        "cadenceDays": payload.cadenceDays or prior.get("cadenceDays"),
        "dueAt": prior.get("dueAt"),
        "reviewedAt": now.isoformat(),
        "reviewedBy": user.id,
    }
    await db.commit()
    return {"id": cr.id, "effectivenessReview": cr.effectivenessReview}


# ─── Attachments — drawings / P&IDs / vendor specs (Supabase 2-phase) ─


MOC_ATTACHMENT_MAX_SIZE = 25 * 1024 * 1024
MOC_ATTACHMENT_MIME = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
    "text/csv",
    "text/plain",
    "application/vnd.ms-outlook",  # msg
    "application/octet-stream",  # CAD / P&ID exports
}
MOC_ATTACHMENT_CATEGORIES = {"drawing", "pid", "vendor_spec", "risk_assessment", "other"}


def _att_out(a: MocAttachment) -> dict:
    return {
        "id": a.id,
        "category": a.category,
        "fileName": a.fileName,
        "fileSize": a.fileSize,
        "mimeType": a.mimeType,
        "caption": a.caption,
        "uploadedById": a.uploadedById,
        "uploadedAt": a.uploadedAt.isoformat() if a.uploadedAt else None,
    }


@router.post("/change-requests/{cr_id}/attachments")
async def upload_moc_attachment(
    cr_id: str,
    payload: dict[str, Any],
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    cr = await db.get(ChangeRequest, cr_id)
    if cr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Change request not found")
    await require_permission_with_context("MOC.UPDATE", user, db, plant_id=cr.plantId)
    if not is_storage_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Supabase Storage isn't configured. Set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.",
        )

    phase = payload.get("phase")
    if phase == "init":
        try:
            init = AttachmentInit(**payload)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid init body: {e}") from e
        if init.category not in MOC_ATTACHMENT_CATEGORIES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Invalid category. Must be one of: {', '.join(sorted(MOC_ATTACHMENT_CATEGORIES))}",
            )
        if init.fileSize > MOC_ATTACHMENT_MAX_SIZE:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"File size exceeds the {MOC_ATTACHMENT_MAX_SIZE // 1024 // 1024} MB limit.",
            )
        if init.mimeType not in MOC_ATTACHMENT_MIME:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"MIME type {init.mimeType} is not allowed.")
        storage_path = build_moc_storage_path(
            moc_id=cr_id, category=init.category, file_name=init.fileName
        )
        try:
            signed = create_signed_upload_url(storage_path)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"Storage upload init failed: {e}"
            ) from e
        att = MocAttachment(
            changeRequestId=cr_id,
            category=init.category,
            fileName=init.fileName,
            storagePath=storage_path,
            fileSize=init.fileSize,
            mimeType=init.mimeType,
            uploadedById=user.id,
        )
        db.add(att)
        await db.flush()
        result = {
            "phase": "init",
            "attachmentId": att.id,
            "storagePath": storage_path,
            "uploadUrl": signed["uploadUrl"],
            "token": signed["token"],
        }
        await db.commit()
        return result

    if phase == "complete":
        attachment_id = payload.get("attachmentId")
        if not attachment_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "attachmentId required")
        att = await db.get(MocAttachment, attachment_id)
        if att is None or att.changeRequestId != cr_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found for this change request")
        att.caption = payload.get("caption")
        await db.commit()
        return {"ok": True}

    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown phase")


@router.get("/change-requests/{cr_id}/attachments")
async def list_moc_attachments(
    cr_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    cr = await db.get(ChangeRequest, cr_id)
    if cr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Change request not found")
    await _require_moc_read(db, user, cr.plantId)
    rows = (
        await db.execute(
            select(MocAttachment)
            .where(MocAttachment.changeRequestId == cr_id)
            .where(MocAttachment.deletedAt.is_(None))
            .order_by(MocAttachment.uploadedAt.desc())
        )
    ).scalars().all()
    return {"items": [_att_out(r) for r in rows]}


@router.get("/change-requests/{cr_id}/attachments/{attachment_id}/download")
async def download_moc_attachment(
    cr_id: str,
    attachment_id: str,
    inline: int = 0,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, str]:
    att = await db.get(MocAttachment, attachment_id)
    if att is None or att.changeRequestId != cr_id or att.deletedAt is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    cr = await db.get(ChangeRequest, cr_id)
    await _require_moc_read(db, user, cr.plantId if cr else None)
    url = create_signed_download_url(
        att.storagePath, expires_in_sec=300, download=None if inline else att.fileName
    )
    return {"url": url}


@router.delete("/change-requests/{cr_id}/attachments/{attachment_id}")
async def delete_moc_attachment(
    cr_id: str,
    attachment_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    att = await db.get(MocAttachment, attachment_id)
    if att is None or att.changeRequestId != cr_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    cr = await db.get(ChangeRequest, cr_id)
    await require_permission_with_context(
        "MOC.UPDATE", user, db, plant_id=cr.plantId if cr else None
    )
    att.deletedAt = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True}
