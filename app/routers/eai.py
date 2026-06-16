"""EAI router — Phase 2 (HIRA Phase 2).

Mirrors the HIRA router pattern for the Environmental Aspect & Impact
register. Per-plant feature-flag gating means EAI endpoints 404 unless
the plant has the EAI flag enabled.

Endpoints exposed:
  Masters:
    GET    /api/eai/aspect-categories
    GET    /api/eai/aspects                          — library search
    GET    /api/eai/receptors
    GET    /api/eai/regulations
    GET    /api/eai/impact-matrices
    GET    /api/eai/impact-matrices/{id}

  Studies:
    GET    /api/eai/studies                          — plant-scoped list
    POST   /api/eai/studies                          — create study
    GET    /api/eai/studies/{id}                     — detail
    PATCH  /api/eai/studies/{id}                     — update
    DELETE /api/eai/studies/{id}                     — archive
    GET    /api/eai/studies/{id}/entries             — entries in study
    POST   /api/eai/studies/{id}/entries             — create entry

  Entries:
    GET    /api/eai/entries/{id}                     — entry detail
    PATCH  /api/eai/entries/{id}                     — update
    PUT    /api/eai/entries/{id}/aspects             — replace aspects
    PUT    /api/eai/entries/{id}/impacts             — replace impacts
    PUT    /api/eai/entries/{id}/controls            — replace existing controls
    PUT    /api/eai/entries/{id}/recommended-controls
    PUT    /api/eai/entries/{id}/compliance-obligations
    PUT    /api/eai/entries/{id}/regulation-refs
    GET    /api/eai/entries/{id}/versions

  Review cycles:
    GET    /api/eai/review-cycles
    POST   /api/eai/review-cycles/{id}/submit

  Dashboard (used by Phase 3 Risk Aggregation Dashboard):
    GET    /api/eai/dashboard/coverage
    GET    /api/eai/dashboard/significant

  Feature flags:
    GET    /api/eai/feature-flag/{plant_id}
    PATCH  /api/eai/feature-flag/{plant_id}
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.eai import (
    EaiAspect,
    EaiAspectCategory,
    EaiComplianceObligation,
    EaiEntry,
    EaiEntryAspect,
    EaiEntryControl,
    EaiEntryImpact,
    EaiEntryRecommendedControl,
    EaiEntryRegulationRef,
    EaiFeatureFlag,
    EaiReceptor,
    EaiRegulation,
    EaiReviewCycle,
    EaiStudy,
    EaiStudyTeamMember,
    EaiVersion,
    EnvironmentalImpactMatrix,
    EnvironmentalImpactMatrixCell,
    EnvironmentalImpactMatrixLikelihood,
    EnvironmentalImpactMatrixMagnitude,
)
from app.models.plant import Plant
from app.models.user import User
from app.schemas.eai import (
    BulkNoChangeResponse,
    EaiAspectCategoryOut,
    EaiAspectOut,
    EaiBulkNoCycleRequest,
    EaiComplianceObligationIn,
    EaiComplianceObligationOut,
    EaiDashboardCoverage,
    EaiDashboardSignificant,
    EaiEntryAspectIn,
    EaiEntryAspectOut,
    EaiEntryControlIn,
    EaiEntryControlOut,
    EaiEntryCreate,
    EaiEntryImpactIn,
    EaiEntryImpactOut,
    EaiEntryListItem,
    EaiEntryListResponse,
    EaiEntryOut,
    EaiEntryRecommendedControlIn,
    EaiEntryRecommendedControlOut,
    EaiEntryRegulationRefIn,
    EaiEntryRegulationRefOut,
    EaiEntryUpdate,
    EaiFeatureFlagOut,
    EaiFeatureFlagUpdate,
    EaiReceptorOut,
    EaiRegulationOut,
    EaiReviewCycleOut,
    EaiReviewCycleSubmitRequest,
    EaiStudyCreate,
    EaiStudyListItem,
    EaiStudyListResponse,
    EaiStudyOut,
    EaiStudyTeamMemberOut,
    EaiStudyTransitionRequest,
    EaiStudyUpdate,
    EaiVersionOut,
    EnvironmentalImpactMatrixCellOut,
    EnvironmentalImpactMatrixLikelihoodOut,
    EnvironmentalImpactMatrixMagnitudeOut,
    EnvironmentalImpactMatrixOut,
)

router = APIRouter(prefix="/api/eai", tags=["eai"])


# ─────────────────────────────────────────────────────────────────────
# Feature flag helpers
# ─────────────────────────────────────────────────────────────────────


async def _is_eai_enabled(db: AsyncSession, plant_id: str) -> bool:
    flag = (
        await db.execute(select(EaiFeatureFlag).where(EaiFeatureFlag.plantId == plant_id))
    ).scalar_one_or_none()
    if flag is None:
        return False
    return bool(flag.eaiRegisterEnabled)


async def _require_eai_enabled(db: AsyncSession, plant_id: str) -> None:
    if not await _is_eai_enabled(db, plant_id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"EAI register is not enabled for plant {plant_id}",
        )


# ─────────────────────────────────────────────────────────────────────
# Masters
# ─────────────────────────────────────────────────────────────────────


@router.get("/aspect-categories", response_model=list[EaiAspectCategoryOut])
async def list_aspect_categories(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (
        await db.execute(
            select(EaiAspectCategory)
            .where(EaiAspectCategory.isActive.is_(True))
            .order_by(EaiAspectCategory.sortOrder.asc())
        )
    ).scalars().all()
    return rows


@router.get("/aspects", response_model=list[EaiAspectOut])
async def list_aspects(
    q: str | None = Query(None, description="Free-text search on name/description/code"),
    categoryId: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = select(EaiAspect).where(EaiAspect.isActive.is_(True))
    if categoryId:
        stmt = stmt.where(EaiAspect.categoryId == categoryId)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(EaiAspect.name).like(like),
                func.lower(EaiAspect.description).like(like),
                func.lower(EaiAspect.code).like(like),
            )
        )
    stmt = stmt.order_by(EaiAspect.name.asc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return rows


@router.get("/receptors", response_model=list[EaiReceptorOut])
async def list_receptors(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (
        await db.execute(
            select(EaiReceptor)
            .where(EaiReceptor.isActive.is_(True))
            .order_by(EaiReceptor.sortOrder.asc())
        )
    ).scalars().all()
    return rows


@router.get("/regulations", response_model=list[EaiRegulationOut])
async def list_regulations(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (
        await db.execute(
            select(EaiRegulation)
            .where(EaiRegulation.isActive.is_(True))
            .order_by(EaiRegulation.sortOrder.asc())
        )
    ).scalars().all()
    return rows


@router.get("/impact-matrices", response_model=list[EnvironmentalImpactMatrixOut])
async def list_impact_matrices(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (
        await db.execute(
            select(EnvironmentalImpactMatrix)
            .where(EnvironmentalImpactMatrix.isActive.is_(True))
            .options(
                selectinload(EnvironmentalImpactMatrix.likelihoods),
                selectinload(EnvironmentalImpactMatrix.magnitudes),
                selectinload(EnvironmentalImpactMatrix.cells),
            )
            .order_by(EnvironmentalImpactMatrix.isDefault.desc(), EnvironmentalImpactMatrix.name.asc())
        )
    ).scalars().all()
    return rows


@router.get("/impact-matrices/{matrix_id}", response_model=EnvironmentalImpactMatrixOut)
async def get_impact_matrix(
    matrix_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = (
        await db.execute(
            select(EnvironmentalImpactMatrix)
            .where(EnvironmentalImpactMatrix.id == matrix_id)
            .options(
                selectinload(EnvironmentalImpactMatrix.likelihoods),
                selectinload(EnvironmentalImpactMatrix.magnitudes),
                selectinload(EnvironmentalImpactMatrix.cells),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Impact matrix not found")
    return row


# ─────────────────────────────────────────────────────────────────────
# Studies
# ─────────────────────────────────────────────────────────────────────


async def _generate_study_number(db: AsyncSession, plant: Plant) -> str:
    year = datetime.now(timezone.utc).year
    prefix = f"EAI-{year}-{plant.code}-"
    existing = (
        await db.execute(
            select(EaiStudy.number).where(EaiStudy.number.like(f"{prefix}%"))
        )
    ).scalars().all()
    max_n = 0
    for n in existing:
        try:
            v = int(n.rsplit("-", 1)[-1])
            if v > max_n:
                max_n = v
        except ValueError:
            continue
    return f"{prefix}{max_n + 1:03d}"


@router.get("/studies", response_model=EaiStudyListResponse)
async def list_studies(
    plantId: str = Query(...),
    status_: str | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _require_eai_enabled(db, plantId)
    await require_permission_with_context("EAI.READ", user, db, plant_id=plantId)

    entry_count_sq = (
        select(EaiEntry.studyId, func.count(EaiEntry.id).label("entry_count"))
        .group_by(EaiEntry.studyId)
        .subquery()
    )
    sig_count_sq = (
        select(EaiEntry.studyId, func.count(EaiEntry.id).label("sig_count"))
        .where(EaiEntry.residualSignificant.is_(True))
        .group_by(EaiEntry.studyId)
        .subquery()
    )
    stmt = (
        select(
            EaiStudy,
            func.coalesce(entry_count_sq.c.entry_count, 0).label("entry_count"),
            func.coalesce(sig_count_sq.c.sig_count, 0).label("sig_count"),
        )
        .outerjoin(entry_count_sq, entry_count_sq.c.studyId == EaiStudy.id)
        .outerjoin(sig_count_sq, sig_count_sq.c.studyId == EaiStudy.id)
        .where(EaiStudy.plantId == plantId)
    )
    if status_:
        stmt = stmt.where(EaiStudy.status == status_)
    stmt = stmt.order_by(EaiStudy.initiatedAt.desc())
    rows = (await db.execute(stmt)).all()
    items: list[EaiStudyListItem] = []
    for s, entry_count, sig_count in rows:
        items.append(
            EaiStudyListItem(
                id=s.id,
                number=s.number,
                title=s.title,
                plantId=s.plantId,
                departmentId=s.departmentId,
                areaId=s.areaId,
                scopeType=s.scopeType,
                status=s.status,
                initiatedAt=s.initiatedAt,
                nextScheduledReviewDate=s.nextScheduledReviewDate,
                entryCount=entry_count or 0,
                significantCount=sig_count or 0,
            )
        )
    return EaiStudyListResponse(items=items, total=len(items))


@router.post("/studies", response_model=EaiStudyOut, status_code=status.HTTP_201_CREATED)
async def create_study(
    payload: EaiStudyCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _require_eai_enabled(db, payload.plantId)
    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plant not found")
    matrix = await db.get(EnvironmentalImpactMatrix, payload.impactMatrixId)
    if matrix is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "impactMatrixId is invalid")
    leader = await db.get(User, payload.teamLeaderId)
    if leader is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "teamLeaderId is invalid")

    number = await _generate_study_number(db, plant)
    study = EaiStudy(
        number=number,
        plantId=payload.plantId,
        departmentId=payload.departmentId,
        areaId=payload.areaId,
        scopeType=payload.scopeType,
        activityIds=payload.activityIds,
        processCode=payload.processCode,
        title=payload.title,
        description=payload.description,
        impactMatrixId=payload.impactMatrixId,
        teamLeaderId=payload.teamLeaderId,
        targetCompletionDate=payload.targetCompletionDate,
        reviewFrequency=payload.reviewFrequency,
        customReviewMonths=payload.customReviewMonths,
        applicableRegulations=payload.applicableRegulations,
        regulatoryReviewRequired=payload.regulatoryReviewRequired,
        createdById=user.id,
    )
    db.add(study)
    await db.flush()

    for member in payload.team:
        db.add(
            EaiStudyTeamMember(
                studyId=study.id,
                userId=member.userId,
                teamRole=member.teamRole,
                department=member.department,
            )
        )

    await db.commit()
    await db.refresh(study)
    # Eagerly load team to satisfy response model
    study_full = (
        await db.execute(
            select(EaiStudy)
            .where(EaiStudy.id == study.id)
            .options(selectinload(EaiStudy.team))
        )
    ).scalar_one()
    return study_full


@router.get("/studies/{study_id}", response_model=EaiStudyOut)
async def get_study(
    study_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    study = (
        await db.execute(
            select(EaiStudy)
            .where(EaiStudy.id == study_id)
            .options(selectinload(EaiStudy.team))
        )
    ).scalar_one_or_none()
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await _require_eai_enabled(db, study.plantId)
    await require_permission_with_context("EAI.READ", user, db, plant_id=study.plantId)
    return study


@router.patch("/studies/{study_id}", response_model=EaiStudyOut)
async def update_study(
    study_id: str,
    payload: EaiStudyUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    study = await db.get(EaiStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await _require_eai_enabled(db, study.plantId)
    await require_permission_with_context("EAI.UPDATE", user, db, plant_id=study.plantId)

    PROTECTED_FIELDS = {"status", "approvedAt", "approvedById", "effectiveFrom"}
    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field in PROTECTED_FIELDS:
            continue
        setattr(study, field, value)
    study.updatedById = user.id
    await db.commit()
    study_full = (
        await db.execute(
            select(EaiStudy)
            .where(EaiStudy.id == study.id)
            .options(selectinload(EaiStudy.team))
        )
    ).scalar_one()
    return study_full


@router.delete("/studies/{study_id}", response_model=EaiStudyOut, status_code=status.HTTP_200_OK)
async def archive_study(
    study_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    study = await db.get(EaiStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await _require_eai_enabled(db, study.plantId)
    await require_permission_with_context("EAI.EXECUTE", user, db, plant_id=study.plantId)
    study.status = "ARCHIVED"
    study.updatedById = user.id
    await db.commit()
    study_full = (
        await db.execute(
            select(EaiStudy)
            .where(EaiStudy.id == study.id)
            .options(selectinload(EaiStudy.team))
        )
    ).scalar_one()
    return study_full


@router.post("/studies/{study_id}/submit", response_model=EaiStudyOut)
async def submit_study(
    study_id: str,
    payload: EaiStudyTransitionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    study = await db.get(EaiStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await _require_eai_enabled(db, study.plantId)
    await require_permission_with_context("EAI.UPDATE", user, db, plant_id=study.plantId)
    transitions = {
        "DRAFT": "IN_PROGRESS",
        "IN_PROGRESS": "TEAM_REVIEW",
        "TEAM_REVIEW": "APPROVAL_PENDING",
    }
    if study.status not in transitions:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Cannot submit from status {study.status!r}")
    study.status = transitions[study.status]
    study.updatedById = user.id
    await db.commit()
    study_full = (
        await db.execute(
            select(EaiStudy).where(EaiStudy.id == study_id).options(selectinload(EaiStudy.team))
        )
    ).scalar_one()
    return study_full


@router.post("/studies/{study_id}/approve", response_model=EaiStudyOut)
async def approve_study(
    study_id: str,
    payload: EaiStudyTransitionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    study = await db.get(EaiStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await _require_eai_enabled(db, study.plantId)
    await require_permission_with_context("EAI.EXECUTE", user, db, plant_id=study.plantId)
    if study.status != "APPROVAL_PENDING":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Can only approve from APPROVAL_PENDING; current: {study.status!r}")
    study.status = "APPROVED"
    study.approvedAt = datetime.now(timezone.utc)
    study.approvedById = user.id
    study.updatedById = user.id
    await db.commit()
    study_full = (
        await db.execute(
            select(EaiStudy).where(EaiStudy.id == study_id).options(selectinload(EaiStudy.team))
        )
    ).scalar_one()
    return study_full


@router.post("/studies/{study_id}/activate", response_model=EaiStudyOut)
async def activate_study(
    study_id: str,
    payload: EaiStudyTransitionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    study = await db.get(EaiStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await _require_eai_enabled(db, study.plantId)
    await require_permission_with_context("EAI.EXECUTE", user, db, plant_id=study.plantId)
    if study.status != "APPROVED":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Can only activate from APPROVED; current: {study.status!r}")
    study.status = "ACTIVE"
    study.effectiveFrom = datetime.now(timezone.utc)
    study.updatedById = user.id
    await db.commit()
    study_full = (
        await db.execute(
            select(EaiStudy).where(EaiStudy.id == study_id).options(selectinload(EaiStudy.team))
        )
    ).scalar_one()
    return study_full


# ─────────────────────────────────────────────────────────────────────
# Entries
# ─────────────────────────────────────────────────────────────────────


def _resolve_impact_level(score: int) -> tuple[str, str]:
    if score <= 4:
        return "LOW", "#22c55e"
    if score <= 9:
        return "MODERATE", "#eab308"
    if score <= 16:
        return "SIGNIFICANT", "#f97316"
    return "MAJOR", "#ef4444"


def _is_significant(matrix: EnvironmentalImpactMatrix, level: str) -> bool:
    thresholds = matrix.significanceThresholds or {}
    return bool(thresholds.get(level.lower(), False)) or level in ("SIGNIFICANT", "MAJOR")


def _is_acceptable(matrix: EnvironmentalImpactMatrix, level: str, occurrence: str) -> bool:
    acceptable = matrix.acceptableResidual or {}
    threshold = acceptable.get(occurrence.lower())
    if threshold is None:
        return level == "LOW"
    order = {"LOW": 0, "MODERATE": 1, "SIGNIFICANT": 2, "MAJOR": 3}
    return order.get(level, 0) <= order.get(threshold.upper(), 0)


@router.get("/studies/{study_id}/entries", response_model=EaiEntryListResponse)
async def list_entries(
    study_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    study = await db.get(EaiStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await _require_eai_enabled(db, study.plantId)
    await require_permission_with_context("EAI.READ", user, db, plant_id=study.plantId)
    rows = (
        await db.execute(
            select(EaiEntry)
            .where(EaiEntry.studyId == study_id)
            .order_by(EaiEntry.sequenceNumber.asc())
        )
    ).scalars().all()
    return EaiEntryListResponse(
        items=[EaiEntryListItem.model_validate(r) for r in rows],
        total=len(rows),
    )


async def _next_sequence_for_study(db: AsyncSession, study_id: str) -> int:
    max_seq = (
        await db.execute(
            select(func.max(EaiEntry.sequenceNumber)).where(EaiEntry.studyId == study_id)
        )
    ).scalar_one()
    return (max_seq or 0) + 1


@router.post("/studies/{study_id}/entries", response_model=EaiEntryOut, status_code=status.HTTP_201_CREATED)
async def create_entry(
    study_id: str,
    payload: EaiEntryCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    study = await db.get(EaiStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await _require_eai_enabled(db, study.plantId)
    await require_permission_with_context("EAI.UPDATE", user, db, plant_id=study.plantId)
    if study.status in ("ARCHIVED",):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot add entries to an archived study")

    matrix = await db.get(EnvironmentalImpactMatrix, study.impactMatrixId)
    if matrix is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Study impact matrix missing")
    lk = await db.get(EnvironmentalImpactMatrixLikelihood, payload.initialLikelihoodId)
    mg = await db.get(EnvironmentalImpactMatrixMagnitude, payload.initialMagnitudeId)
    if lk is None or mg is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "initial likelihood/magnitude invalid")

    initial_score = lk.score * mg.score
    initial_level, initial_color = _resolve_impact_level(initial_score)
    initial_sig = _is_significant(matrix, initial_level)

    seq = await _next_sequence_for_study(db, study_id)

    entry = EaiEntry(
        studyId=study_id,
        sequenceNumber=seq,
        activityDescription=payload.activityDescription,
        areaId=payload.areaId,
        subLocation=payload.subLocation,
        occurrence=payload.occurrence,
        frequency=payload.frequency,
        typicalDurationMin=payload.typicalDurationMin,
        equipmentUsed=payload.equipmentUsed,
        materialsUsed=payload.materialsUsed,
        processInputs=payload.processInputs,
        initialLikelihoodId=payload.initialLikelihoodId,
        initialLikelihoodScore=lk.score,
        initialLikelihoodRationale=payload.initialLikelihoodRationale,
        initialMagnitudeId=payload.initialMagnitudeId,
        initialMagnitudeScore=mg.score,
        initialMagnitudeRationale=payload.initialMagnitudeRationale,
        initialImpactScore=initial_score,
        initialImpactLevel=initial_level,
        initialImpactColor=initial_color,
        initialSignificant=initial_sig,
        createdById=user.id,
    )
    db.add(entry)
    await db.flush()

    for a in payload.aspects:
        db.add(
            EaiEntryAspect(
                entryId=entry.id,
                aspectId=a.aspectId,
                contextualDescription=a.contextualDescription,
                quantification=a.quantification,
                occurrence=a.occurrence,
                sortOrder=a.sortOrder,
            )
        )
    for i in payload.impacts:
        db.add(EaiEntryImpact(entryId=entry.id, **i.model_dump()))
    for c in payload.existingControls:
        db.add(EaiEntryControl(entryId=entry.id, **c.model_dump()))
    for rc in payload.recommendedControls:
        db.add(EaiEntryRecommendedControl(entryId=entry.id, **rc.model_dump()))
    for co in payload.complianceObligations:
        db.add(EaiComplianceObligation(entryId=entry.id, **co.model_dump()))
    for rr in payload.regulationRefs:
        db.add(EaiEntryRegulationRef(entryId=entry.id, **rr.model_dump()))

    await db.commit()
    return await _load_entry_with_children(db, entry.id)


async def _load_entry_with_children(db: AsyncSession, entry_id: str) -> EaiEntry | None:
    entry = (
        await db.execute(
            select(EaiEntry)
            .where(EaiEntry.id == entry_id)
            .options(
                selectinload(EaiEntry.aspects),
                selectinload(EaiEntry.impacts),
                selectinload(EaiEntry.existingControls),
                selectinload(EaiEntry.recommendedControls),
                selectinload(EaiEntry.complianceObligations),
                selectinload(EaiEntry.regulationRefs),
            )
        )
    ).scalar_one_or_none()
    return entry


@router.get("/entries/{entry_id}", response_model=EaiEntryOut)
async def get_entry(
    entry_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = await _load_entry_with_children(db, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    study = await db.get(EaiStudy, entry.studyId)
    if study:
        await _require_eai_enabled(db, study.plantId)
        await require_permission_with_context("EAI.READ", user, db, plant_id=study.plantId)
    return entry


@router.patch("/entries/{entry_id}", response_model=EaiEntryOut)
async def update_entry(
    entry_id: str,
    payload: EaiEntryUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = await db.get(EaiEntry, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    study = await db.get(EaiStudy, entry.studyId)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await _require_eai_enabled(db, study.plantId)
    await require_permission_with_context("EAI.UPDATE", user, db, plant_id=study.plantId)
    matrix = await db.get(EnvironmentalImpactMatrix, study.impactMatrixId)
    if matrix is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Study impact matrix missing")

    update_data = payload.model_dump(exclude_unset=True)

    # Recompute initial impact if likelihood/magnitude changed
    init_changed = (
        update_data.get("initialLikelihoodId") is not None
        or update_data.get("initialMagnitudeId") is not None
    )
    if init_changed:
        lk_id = update_data.get("initialLikelihoodId", entry.initialLikelihoodId)
        mg_id = update_data.get("initialMagnitudeId", entry.initialMagnitudeId)
        lk = await db.get(EnvironmentalImpactMatrixLikelihood, lk_id)
        mg = await db.get(EnvironmentalImpactMatrixMagnitude, mg_id)
        if lk and mg:
            entry.initialLikelihoodScore = lk.score
            entry.initialMagnitudeScore = mg.score
            entry.initialImpactScore = lk.score * mg.score
            level, color = _resolve_impact_level(entry.initialImpactScore)
            entry.initialImpactLevel = level
            entry.initialImpactColor = color
            entry.initialSignificant = _is_significant(matrix, level) if matrix else False

    # Recompute residual impact if residual likelihood/magnitude changed
    resid_changed = (
        update_data.get("residualLikelihoodId") is not None
        or update_data.get("residualMagnitudeId") is not None
    )
    if resid_changed:
        lk_id = update_data.get("residualLikelihoodId") or entry.residualLikelihoodId
        mg_id = update_data.get("residualMagnitudeId") or entry.residualMagnitudeId
        if lk_id and mg_id:
            lk = await db.get(EnvironmentalImpactMatrixLikelihood, lk_id)
            mg = await db.get(EnvironmentalImpactMatrixMagnitude, mg_id)
            if lk and mg:
                entry.residualLikelihoodScore = lk.score
                entry.residualMagnitudeScore = mg.score
                entry.residualImpactScore = lk.score * mg.score
                level, color = _resolve_impact_level(entry.residualImpactScore)
                entry.residualImpactLevel = level
                entry.residualImpactColor = color
                entry.residualSignificant = _is_significant(matrix, level) if matrix else False
                entry.residualAcceptable = (
                    _is_acceptable(matrix, level, entry.occurrence) if matrix else None
                )

    # Write version snapshot when entry changes under a locked study
    change_reason = update_data.pop("changeReason", None)
    change_trigger = update_data.pop("changeTrigger", None)
    locked_study = study.status in ("APPROVED", "ACTIVE")
    if locked_study:
        if not change_reason:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "changeReason is required when modifying an entry under an APPROVED or ACTIVE study",
            )
        # compute diff before applying changes
        changes = []
        for field, new_val in update_data.items():
            old_val = getattr(entry, field, None)
            if str(old_val) != str(new_val):
                changes.append({"field": field, "from": str(old_val), "to": str(new_val)})
        # bump version
        entry.versionNumber = (entry.versionNumber or 1) + 1

    ENTRY_PROTECTED_FIELDS = {"changeReason", "changeTrigger", "versionNumber", "isCurrentVersion", "status"}
    for field, value in update_data.items():
        if field in ENTRY_PROTECTED_FIELDS:
            continue
        setattr(entry, field, value)

    # Snapshot captured AFTER field values are applied so it reflects the new state
    if locked_study:
        snap = {c: str(getattr(entry, c, None)) for c in [
            "activityDescription", "occurrence", "frequency",
            "initialLikelihoodScore", "initialMagnitudeScore", "initialImpactScore", "initialImpactLevel",
            "residualLikelihoodScore", "residualMagnitudeScore", "residualImpactScore", "residualImpactLevel",
            "legalComplianceStatus",
        ]}
        db.add(EaiVersion(
            entryId=entry.id,
            versionNumber=entry.versionNumber,
            snapshot=snap,
            changes=changes,
            changeReason=change_reason,
            changeTrigger=change_trigger or "MANUAL_EDIT",
            createdById=user.id,
        ))

    entry.updatedById = user.id
    await db.commit()
    return await _load_entry_with_children(db, entry_id)


# ── Child-collection replace endpoints ───────────────────────────────


@router.put("/entries/{entry_id}/aspects", response_model=list[EaiEntryAspectOut])
async def replace_entry_aspects(
    entry_id: str,
    payload: list[EaiEntryAspectIn],
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = await db.get(EaiEntry, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    study = await db.get(EaiStudy, entry.studyId)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await _require_eai_enabled(db, study.plantId)
    await require_permission_with_context("EAI.UPDATE", user, db, plant_id=study.plantId)
    existing = (
        await db.execute(select(EaiEntryAspect).where(EaiEntryAspect.entryId == entry_id))
    ).scalars().all()
    for row in existing:
        await db.delete(row)
    for item in payload:
        db.add(EaiEntryAspect(entryId=entry_id, **item.model_dump()))
    await db.commit()
    return (
        await db.execute(
            select(EaiEntryAspect)
            .where(EaiEntryAspect.entryId == entry_id)
            .order_by(EaiEntryAspect.sortOrder.asc())
        )
    ).scalars().all()


@router.put("/entries/{entry_id}/impacts", response_model=list[EaiEntryImpactOut])
async def replace_entry_impacts(
    entry_id: str,
    payload: list[EaiEntryImpactIn],
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = await db.get(EaiEntry, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    study = await db.get(EaiStudy, entry.studyId)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await _require_eai_enabled(db, study.plantId)
    await require_permission_with_context("EAI.UPDATE", user, db, plant_id=study.plantId)
    existing = (
        await db.execute(select(EaiEntryImpact).where(EaiEntryImpact.entryId == entry_id))
    ).scalars().all()
    for row in existing:
        await db.delete(row)
    for item in payload:
        db.add(EaiEntryImpact(entryId=entry_id, **item.model_dump()))
    await db.commit()
    return (
        await db.execute(
            select(EaiEntryImpact)
            .where(EaiEntryImpact.entryId == entry_id)
            .order_by(EaiEntryImpact.sortOrder.asc())
        )
    ).scalars().all()


@router.put("/entries/{entry_id}/controls", response_model=list[EaiEntryControlOut])
async def replace_entry_controls(
    entry_id: str,
    payload: list[EaiEntryControlIn],
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = await db.get(EaiEntry, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    study = await db.get(EaiStudy, entry.studyId)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await _require_eai_enabled(db, study.plantId)
    await require_permission_with_context("EAI.UPDATE", user, db, plant_id=study.plantId)
    existing = (
        await db.execute(select(EaiEntryControl).where(EaiEntryControl.entryId == entry_id))
    ).scalars().all()
    for row in existing:
        await db.delete(row)
    for item in payload:
        db.add(EaiEntryControl(entryId=entry_id, **item.model_dump()))
    await db.commit()
    return (
        await db.execute(
            select(EaiEntryControl)
            .where(EaiEntryControl.entryId == entry_id)
            .order_by(EaiEntryControl.sortOrder.asc())
        )
    ).scalars().all()


@router.put("/entries/{entry_id}/recommended-controls", response_model=list[EaiEntryRecommendedControlOut])
async def replace_entry_recommended_controls(
    entry_id: str,
    payload: list[EaiEntryRecommendedControlIn],
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = await db.get(EaiEntry, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    study = await db.get(EaiStudy, entry.studyId)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await _require_eai_enabled(db, study.plantId)
    await require_permission_with_context("EAI.UPDATE", user, db, plant_id=study.plantId)
    existing = (
        await db.execute(select(EaiEntryRecommendedControl).where(EaiEntryRecommendedControl.entryId == entry_id))
    ).scalars().all()
    for row in existing:
        await db.delete(row)
    for item in payload:
        db.add(EaiEntryRecommendedControl(entryId=entry_id, **item.model_dump()))
    await db.commit()
    return (
        await db.execute(
            select(EaiEntryRecommendedControl)
            .where(EaiEntryRecommendedControl.entryId == entry_id)
            .order_by(EaiEntryRecommendedControl.sortOrder.asc())
        )
    ).scalars().all()


@router.put("/entries/{entry_id}/compliance-obligations", response_model=list[EaiComplianceObligationOut])
async def replace_compliance_obligations(
    entry_id: str,
    payload: list[EaiComplianceObligationIn],
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = await db.get(EaiEntry, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    study = await db.get(EaiStudy, entry.studyId)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await _require_eai_enabled(db, study.plantId)
    await require_permission_with_context("EAI.UPDATE", user, db, plant_id=study.plantId)
    existing = (
        await db.execute(select(EaiComplianceObligation).where(EaiComplianceObligation.entryId == entry_id))
    ).scalars().all()
    for row in existing:
        await db.delete(row)
    for item in payload:
        db.add(EaiComplianceObligation(entryId=entry_id, **item.model_dump()))
    await db.commit()
    return (
        await db.execute(
            select(EaiComplianceObligation)
            .where(EaiComplianceObligation.entryId == entry_id)
            .order_by(EaiComplianceObligation.id.asc())
        )
    ).scalars().all()


@router.put("/entries/{entry_id}/regulation-refs", response_model=list[EaiEntryRegulationRefOut])
async def replace_regulation_refs(
    entry_id: str,
    payload: list[EaiEntryRegulationRefIn],
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = await db.get(EaiEntry, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    study = await db.get(EaiStudy, entry.studyId)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await _require_eai_enabled(db, study.plantId)
    await require_permission_with_context("EAI.UPDATE", user, db, plant_id=study.plantId)
    existing = (
        await db.execute(select(EaiEntryRegulationRef).where(EaiEntryRegulationRef.entryId == entry_id))
    ).scalars().all()
    for row in existing:
        await db.delete(row)
    for item in payload:
        db.add(EaiEntryRegulationRef(entryId=entry_id, **item.model_dump()))
    await db.commit()
    return (
        await db.execute(
            select(EaiEntryRegulationRef)
            .where(EaiEntryRegulationRef.entryId == entry_id)
            .order_by(EaiEntryRegulationRef.id.asc())
        )
    ).scalars().all()


@router.get("/entries/{entry_id}/versions", response_model=list[EaiVersionOut])
async def get_entry_versions(
    entry_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = await db.get(EaiEntry, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    study = await db.get(EaiStudy, entry.studyId)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await _require_eai_enabled(db, study.plantId)
    await require_permission_with_context("EAI.READ", user, db, plant_id=study.plantId)
    rows = (
        await db.execute(
            select(EaiVersion)
            .where(EaiVersion.entryId == entry_id)
            .order_by(EaiVersion.versionNumber.desc())
        )
    ).scalars().all()
    return rows


# ─────────────────────────────────────────────────────────────────────
# Review cycles
# ─────────────────────────────────────────────────────────────────────


@router.get("/review-cycles", response_model=list[EaiReviewCycleOut])
async def list_review_cycles(
    plantId: str | None = Query(None),
    assignedToId: str | None = Query(None),
    status_: str | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if plantId:
        await _require_eai_enabled(db, plantId)
        await require_permission_with_context("EAI.READ", user, db, plant_id=plantId)
    stmt = (
        select(EaiReviewCycle, EaiEntry.activityDescription, EaiEntry.sequenceNumber,
               EaiStudy.number, EaiStudy.title)
        .join(EaiEntry, EaiEntry.id == EaiReviewCycle.entryId)
        .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
    )
    if assignedToId:
        stmt = stmt.where(EaiReviewCycle.assignedToId == assignedToId)
    if status_:
        stmt = stmt.where(EaiReviewCycle.status == status_)
    if plantId:
        stmt = stmt.where(EaiStudy.plantId == plantId)
    stmt = stmt.order_by(EaiReviewCycle.scheduledFor.asc())
    rows = (await db.execute(stmt)).all()
    result = []
    for cycle, entry_title, entry_seq, study_number, study_title in rows:
        # NB: model_validate has no `update=` kwarg in Pydantic v2 — assign
        # the joined display fields after validation (same as get_review_cycle).
        out = EaiReviewCycleOut.model_validate(cycle)
        out.entryTitle = entry_title
        out.entrySequenceNumber = entry_seq
        out.studyNumber = study_number
        out.studyTitle = study_title
        result.append(out)
    return result


@router.post("/review-cycles/bulk-no-change", response_model=BulkNoChangeResponse)
async def bulk_no_change(
    payload: EaiBulkNoCycleRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    updated = 0
    for cycle_id in payload.cycleIds:
        cycle = await db.get(EaiReviewCycle, cycle_id)
        if cycle is None or cycle.status not in ("SCHEDULED", "IN_PROGRESS"):
            continue
        entry = await db.get(EaiEntry, cycle.entryId)
        if entry is not None:
            study = await db.get(EaiStudy, entry.studyId)
            if study is not None:
                await _require_eai_enabled(db, study.plantId)
                await require_permission_with_context("EAI.UPDATE", user, db, plant_id=study.plantId)
        cycle.status = "COMPLETED"
        cycle.completedAt = datetime.now(timezone.utc)
        cycle.completedById = user.id
        cycle.outcome = "NO_CHANGE_REQUIRED"
        if entry is not None:
            entry.lastReviewedAt = datetime.now(timezone.utc)
            entry.lastReviewedById = user.id
            entry.reviewCount = (entry.reviewCount or 0) + 1
            entry.lastReviewType = cycle.triggeredBy
        updated += 1
    await db.commit()
    return BulkNoChangeResponse(updated=updated)


@router.get("/review-cycles/{cycle_id}", response_model=EaiReviewCycleOut)
async def get_review_cycle(
    cycle_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = (
        await db.execute(
            select(EaiReviewCycle, EaiEntry.activityDescription, EaiEntry.sequenceNumber,
                   EaiStudy.number, EaiStudy.title, EaiStudy.plantId)
            .join(EaiEntry, EaiEntry.id == EaiReviewCycle.entryId)
            .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
            .where(EaiReviewCycle.id == cycle_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review cycle not found")
    cycle, entry_title, entry_seq, study_number, study_title, plant_id = row
    await _require_eai_enabled(db, plant_id)
    await require_permission_with_context("EAI.READ", user, db, plant_id=plant_id)
    out = EaiReviewCycleOut.model_validate(cycle)
    out.entryTitle = entry_title
    out.entrySequenceNumber = entry_seq
    out.studyNumber = study_number
    out.studyTitle = study_title
    return out


@router.post("/review-cycles/{cycle_id}/submit", response_model=EaiReviewCycleOut)
async def submit_review_cycle(
    cycle_id: str,
    payload: EaiReviewCycleSubmitRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cycle = await db.get(EaiReviewCycle, cycle_id)
    if cycle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review cycle not found")
    if cycle.status not in ("SCHEDULED", "IN_PROGRESS"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Review cycle cannot be submitted from status {cycle.status!r}",
        )
    # Resolve parent study for feature-flag and RBAC checks
    _submit_entry = await db.get(EaiEntry, cycle.entryId)
    if _submit_entry is not None:
        _submit_study = await db.get(EaiStudy, _submit_entry.studyId)
        if _submit_study is not None:
            await _require_eai_enabled(db, _submit_study.plantId)
            await require_permission_with_context("EAI.UPDATE", user, db, plant_id=_submit_study.plantId)
    cycle.status = "COMPLETED"
    cycle.completedAt = datetime.now(timezone.utc)
    cycle.completedById = user.id
    cycle.outcome = payload.outcome
    cycle.outcomeNotes = payload.outcomeNotes
    cycle.changesMade = payload.changesMade

    # Update the entry's review metadata
    entry = await db.get(EaiEntry, cycle.entryId)
    if entry is not None:
        entry.lastReviewedAt = datetime.now(timezone.utc)
        entry.lastReviewedById = user.id
        entry.lastReviewType = cycle.triggeredBy
        # Only increment reviewCount and update nextReviewDue on non-MAJOR_REVISION outcomes
        if payload.outcome != "MAJOR_REVISION":
            entry.reviewCount = (entry.reviewCount or 0) + 1
        # MAJOR_REVISION: create version snapshot documenting the review finding
        if payload.outcome == "MAJOR_REVISION" and payload.outcomeNotes:
            next_ver = (entry.versionNumber or 1) + 1
            entry.versionNumber = next_ver
            db.add(EaiVersion(
                entryId=entry.id,
                versionNumber=next_ver,
                snapshot={"outcome": payload.outcome, "notes": payload.outcomeNotes or ""},
                changes=[{"field": "review_outcome", "from": "PENDING", "to": "MAJOR_REVISION"}],
                changeReason=payload.outcomeNotes or "Major revision from review cycle",
                changeTrigger=cycle.triggeredBy or "REVIEW",
                createdById=user.id,
            ))

    await db.commit()
    # Re-query with joined context fields to populate EaiReviewCycleOut correctly
    row = (
        await db.execute(
            select(EaiReviewCycle, EaiEntry.activityDescription, EaiEntry.sequenceNumber,
                   EaiStudy.number, EaiStudy.title)
            .join(EaiEntry, EaiEntry.id == EaiReviewCycle.entryId)
            .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
            .where(EaiReviewCycle.id == cycle_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review cycle not found after commit")
    cycle_out, entry_title, entry_seq, study_number, study_title = row
    out = EaiReviewCycleOut.model_validate(cycle_out)
    out.entryTitle = entry_title
    out.entrySequenceNumber = entry_seq
    out.studyNumber = study_number
    out.studyTitle = study_title
    return out


# ─────────────────────────────────────────────────────────────────────
# Dashboard widgets
# ─────────────────────────────────────────────────────────────────────


@router.get("/dashboard/coverage", response_model=EaiDashboardCoverage)
async def dashboard_coverage(
    plantId: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _require_eai_enabled(db, plantId)
    await require_permission_with_context("EAI.READ", user, db, plant_id=plantId)
    from app.models.masters import Department

    departments_total = (
        await db.execute(
            select(func.count(Department.id)).where(Department.plantId == plantId)
        )
    ).scalar_one()
    departments_with_study = (
        await db.execute(
            select(func.count(func.distinct(EaiStudy.departmentId)))
            .where(EaiStudy.plantId == plantId)
            .where(EaiStudy.status.in_(["ACTIVE", "APPROVED"]))
            .where(EaiStudy.departmentId.isnot(None))
        )
    ).scalar_one()
    coverage = (
        (departments_with_study / departments_total * 100.0)
        if departments_total
        else 0.0
    )
    return EaiDashboardCoverage(
        departmentsTotal=departments_total or 0,
        departmentsWithActiveStudy=departments_with_study or 0,
        coveragePercent=round(coverage, 1),
    )


@router.get("/dashboard/significant", response_model=EaiDashboardSignificant)
async def dashboard_significant(
    plantId: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _require_eai_enabled(db, plantId)
    await require_permission_with_context("EAI.READ", user, db, plant_id=plantId)
    rows = (
        await db.execute(
            select(EaiEntry.residualImpactLevel, func.count(EaiEntry.id))
            .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
            .where(EaiStudy.plantId == plantId)
            .where(EaiEntry.residualSignificant.is_(True))
            .where(EaiEntry.isCurrentVersion.is_(True))
            .group_by(EaiEntry.residualImpactLevel)
        )
    ).all()
    by_level: dict[str, int] = {level or "UNKNOWN": int(count) for level, count in rows}

    # By category (via aspect → category)
    cat_rows = (
        await db.execute(
            select(EaiAspectCategory.code, func.count(EaiEntry.id.distinct()))
            .join(EaiAspect, EaiAspect.categoryId == EaiAspectCategory.id)
            .join(EaiEntryAspect, EaiEntryAspect.aspectId == EaiAspect.id)
            .join(EaiEntry, EaiEntry.id == EaiEntryAspect.entryId)
            .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
            .where(EaiStudy.plantId == plantId)
            .where(EaiEntry.residualSignificant.is_(True))
            .group_by(EaiAspectCategory.code)
        )
    ).all()
    by_category: dict[str, int] = {code: int(count) for code, count in cat_rows}

    return EaiDashboardSignificant(
        total=sum(by_level.values()),
        byLevel=by_level,
        byCategory=by_category,
    )


# ─────────────────────────────────────────────────────────────────────
# Feature flag
# ─────────────────────────────────────────────────────────────────────


@router.get("/feature-flag/{plant_id}", response_model=EaiFeatureFlagOut)
async def get_feature_flag(
    plant_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    flag = (
        await db.execute(select(EaiFeatureFlag).where(EaiFeatureFlag.plantId == plant_id))
    ).scalar_one_or_none()
    if flag is None:
        # Return a "disabled" default rather than 404 so the UI knows the state
        return EaiFeatureFlagOut(
            plantId=plant_id,
            eaiRegisterEnabled=False,
            combinedRegisterEnabled=False,
            riskDashboardEnabled=False,
            hiraAssistantV2Enabled=False,
            enabledAt=None,
        )
    return flag


@router.patch("/feature-flag/{plant_id}", response_model=EaiFeatureFlagOut)
async def update_feature_flag(
    plant_id: str,
    payload: EaiFeatureFlagUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    plant = await db.get(Plant, plant_id)
    if plant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plant not found")
    await require_permission_with_context("EAI.EXECUTE", user, db, plant_id=plant_id)

    flag = (
        await db.execute(select(EaiFeatureFlag).where(EaiFeatureFlag.plantId == plant_id))
    ).scalar_one_or_none()

    if flag is None:
        flag = EaiFeatureFlag(plantId=plant_id)
        db.add(flag)
        await db.flush()

    update = payload.model_dump(exclude_unset=True)
    for field, value in update.items():
        setattr(flag, field, value)
    any_enabled = (
        flag.eaiRegisterEnabled
        or flag.combinedRegisterEnabled
        or flag.riskDashboardEnabled
        or flag.hiraAssistantV2Enabled
    )
    if any_enabled and flag.enabledAt is None:
        flag.enabledAt = datetime.now(timezone.utc)
        flag.enabledById = user.id

    await db.commit()
    await db.refresh(flag)
    return flag
