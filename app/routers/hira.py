"""HIRA router — Phase 2 vertical slice.

Endpoints exposed:
  - GET    /api/hira/risk-matrices                — list active matrices
  - GET    /api/hira/risk-matrices/{id}           — matrix + scales + cells
  - GET    /api/hira/hazards                      — hazard library search
  - GET    /api/hira/controls                     — control library
  - GET    /api/hira/studies                      — plant-scoped study list
  - POST   /api/hira/studies                      — create study
  - GET    /api/hira/studies/{id}                 — study detail
  - GET    /api/hira/studies/{id}/entries         — entries in study
  - POST   /api/hira/studies/{id}/entries         — create entry
  - GET    /api/hira/entries/{id}                 — entry detail

Workflow integration: study creation does NOT yet kick off the workflow
engine. That happens in Phase 4 when the HIRA_STUDY_STANDARD definition
is seeded. Until then studies stay in DRAFT and can be edited freely.
"""

from __future__ import annotations

from datetime import datetime, timezone

try:
    from dateutil.relativedelta import relativedelta as _relativedelta
    _HAS_RELATIVEDELTA = True
except ImportError:
    _HAS_RELATIVEDELTA = False

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.hira import (
    HiraCapa,
    HiraControl,
    HiraEntry,
    HiraEntryControl,
    HiraEntryHazard,
    HiraEntryRecommendedControl,
    HiraEntryRegulationRef,
    HiraHazard,
    HiraReviewCycle,
    HiraStudy,
    HiraStudyTeamMember,
    HiraVersion,
    RiskMatrix,
    RiskMatrixCell,
    RiskMatrixLikelihood,
    RiskMatrixSeverity,
)
from app.models.plant import Plant
from app.models.user import User
from app.schemas.hira import (
    HiraCapaCreate,
    HiraCapaOut,
    HiraCapaUpdate,
    HiraControlOut,
    HiraDashboardCoverage,
    HiraDashboardHighRisk,
    HiraDashboardReviewCompliance,
    HiraDashboardRiskReduction,
    HiraDashboardTopHazard,
    HiraEntryControlReplaceRequest,
    HiraEntryCreate,
    HiraEntryHazardReplaceItem,
    HiraEntryListItem,
    HiraEntryListResponse,
    HiraEntryOut,
    HiraEntryRecommendedControlReplaceRequest,
    HiraEntryRegulationRefReplaceRequest,
    HiraEntryTransitionRequest,
    HiraEntryUpdate,
    HiraHazardOut,
    HiraIntegrationEntry,
    HiraIntegrationForFlraResponse,
    HiraIntegrationForPtwResponse,
    HiraInspectionPriorityResult,
    HiraReviewCycleBulkNoChangeRequest,
    HiraReviewCycleListItem,
    HiraReviewCycleOut,
    HiraReviewCycleSubmitRequest,
    HiraStudyCreate,
    HiraStudyListItem,
    HiraStudyListResponse,
    HiraStudyDetailResponse,
    HiraStudyOut,
    HiraStudyTransitionRequest,
    HiraStudyUpdate,
    HiraVersionOut,
    RiskMatrixOut,
)
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
)

router = APIRouter(prefix="/api/hira", tags=["hira"])


# ─────────────────────────────────────────────────────────────────────
# Risk matrix master
# ─────────────────────────────────────────────────────────────────────


@router.get("/risk-matrices", response_model=list[RiskMatrixOut])
async def list_risk_matrices(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RiskMatrixOut]:
    """Active matrices the caller can reference when creating studies.

    HIRA.READ is sufficient — matrices are masters, not records. Everyone
    who can read HIRA needs to read the matrices to render risk levels.
    """
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    stmt = (
        select(RiskMatrix)
        .where(RiskMatrix.isActive.is_(True))
        .options(
            selectinload(RiskMatrix.likelihoods),
            selectinload(RiskMatrix.severities),
            selectinload(RiskMatrix.cells),
        )
        .order_by(RiskMatrix.isDefault.desc(), RiskMatrix.name)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [RiskMatrixOut.model_validate(r) for r in rows]


@router.get("/risk-matrices/{matrix_id}", response_model=RiskMatrixOut)
async def get_risk_matrix(
    matrix_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RiskMatrixOut:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    stmt = (
        select(RiskMatrix)
        .where(RiskMatrix.id == matrix_id)
        .options(
            selectinload(RiskMatrix.likelihoods),
            selectinload(RiskMatrix.severities),
            selectinload(RiskMatrix.cells),
        )
    )
    matrix = (await db.execute(stmt)).scalar_one_or_none()
    if matrix is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Risk matrix not found")
    return RiskMatrixOut.model_validate(matrix)


# ─────────────────────────────────────────────────────────────────────
# Hazard + control libraries
# ─────────────────────────────────────────────────────────────────────


@router.get("/hazards", response_model=list[HiraHazardOut])
async def list_hazards(
    q: str | None = Query(None, description="Free-text search across name, description, code"),
    category: str | None = None,
    energy_form: str | None = None,
    limit: int = Query(50, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[HiraHazardOut]:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    stmt = select(HiraHazard).where(HiraHazard.isActive.is_(True))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                HiraHazard.name.ilike(like),
                HiraHazard.description.ilike(like),
                HiraHazard.code.ilike(like),
            )
        )
    if category:
        stmt = stmt.where(HiraHazard.category == category)
    if energy_form:
        stmt = stmt.where(HiraHazard.energyForm == energy_form)
    stmt = stmt.order_by(HiraHazard.category, HiraHazard.name).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [HiraHazardOut.model_validate(r) for r in rows]


@router.get("/controls", response_model=list[HiraControlOut])
async def list_controls(
    hierarchy: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[HiraControlOut]:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    stmt = select(HiraControl).where(HiraControl.isActive.is_(True))
    if hierarchy:
        stmt = stmt.where(HiraControl.hierarchy == hierarchy)
    stmt = stmt.order_by(HiraControl.hierarchy, HiraControl.description).limit(200)
    rows = (await db.execute(stmt)).scalars().all()
    return [HiraControlOut.model_validate(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────
# Studies
# ─────────────────────────────────────────────────────────────────────


@router.get("/studies", response_model=HiraStudyListResponse)
async def list_studies(
    status_filter: str | None = Query(None, alias="status"),
    plant_id: str | None = None,
    department_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraStudyListResponse:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    accessible_plants = await get_accessible_plants(db, user.id)

    base = (
        select(HiraStudy)
        .options(
            selectinload(HiraStudy.plant),
            selectinload(HiraStudy.department),
            selectinload(HiraStudy.area),
        )
    )
    if accessible_plants is None:
        pass  # ALL_PLANTS
    elif len(accessible_plants) == 0:
        return HiraStudyListResponse(items=[], total=0, statusCounts={})
    else:
        base = base.where(HiraStudy.plantId.in_(accessible_plants))

    stmt = base
    if status_filter:
        stmt = stmt.where(HiraStudy.status == status_filter)
    if plant_id:
        stmt = stmt.where(HiraStudy.plantId == plant_id)
    if department_id:
        stmt = stmt.where(HiraStudy.departmentId == department_id)

    stmt = stmt.order_by(HiraStudy.status.asc(), HiraStudy.initiatedAt.desc()).limit(200)
    rows = (await db.execute(stmt)).scalars().all()

    # Bulk-fetch team leader names + entry counts in one query each
    leader_ids = list({r.teamLeaderId for r in rows})
    leader_names: dict[str, str] = {}
    if leader_ids:
        leader_rows = (await db.execute(select(User.id, User.name).where(User.id.in_(leader_ids)))).all()
        leader_names = {uid: nm for uid, nm in leader_rows}

    entry_counts: dict[str, int] = {}
    if rows:
        ec = (
            await db.execute(
                select(HiraEntry.studyId, func.count(HiraEntry.id))
                .where(HiraEntry.studyId.in_([r.id for r in rows]))
                .where(HiraEntry.isCurrentVersion.is_(True))
                .group_by(HiraEntry.studyId)
            )
        ).all()
        entry_counts = {sid: int(cnt) for sid, cnt in ec}

    items = []
    for r in rows:
        d = HiraStudyListItem.model_validate(r).model_dump()
        d["plantName"] = r.plant.name if r.plant else None
        d["departmentName"] = r.department.name if r.department else None
        d["areaName"] = r.area.name if r.area else None
        d["teamLeaderName"] = leader_names.get(r.teamLeaderId)
        d["entryCount"] = entry_counts.get(r.id, 0)
        items.append(HiraStudyListItem(**d))

    # Status counts — across the user's accessible scope (not just the filtered slice)
    sc_q = select(HiraStudy.status, func.count(HiraStudy.id)).group_by(HiraStudy.status)
    if accessible_plants is not None:
        sc_q = sc_q.where(HiraStudy.plantId.in_(accessible_plants))
    sc_rows = (await db.execute(sc_q)).all()
    status_counts = {s: int(c) for s, c in sc_rows}

    return HiraStudyListResponse(items=items, total=len(items), statusCounts=status_counts)


@router.post("/studies", response_model=HiraStudyOut, status_code=status.HTTP_201_CREATED)
async def create_study(
    payload: HiraStudyCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraStudyOut:
    await require_permission_with_context(
        "HIRA.CREATE", user, db, plant_id=payload.plantId
    )

    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")

    matrix = await db.get(RiskMatrix, payload.riskMatrixId)
    if matrix is None or not matrix.isActive:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid risk matrix")

    leader = await db.get(User, payload.teamLeaderId)
    if leader is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="teamLeaderId does not exist")

    # Number generation — HIRA-YYYY-PLT-NNN (MAX-based, gap-safe)
    max_stmt = select(func.max(HiraStudy.number)).where(HiraStudy.plantId == payload.plantId)
    last_number = (await db.execute(max_stmt)).scalar_one_or_none()
    if last_number:
        try:
            last_seq = int(last_number.rsplit("-", 1)[-1])
        except (ValueError, AttributeError):
            last_seq = 0
    else:
        last_seq = 0
    number = f"HIRA-{datetime.now(timezone.utc).year}-{plant.code}-{last_seq + 1:03d}"

    study = HiraStudy(
        number=number,
        plantId=payload.plantId,
        departmentId=payload.departmentId,
        areaId=payload.areaId,
        scopeType=payload.scopeType,
        activityIds=payload.activityIds,
        equipmentIds=payload.equipmentIds,
        processCode=payload.processCode,
        title=payload.title,
        description=payload.description,
        riskMatrixId=payload.riskMatrixId,
        teamLeaderId=payload.teamLeaderId,
        status="DRAFT",
        targetCompletionDate=payload.targetCompletionDate,
        reviewFrequency=payload.reviewFrequency,
        customReviewMonths=payload.customReviewMonths,
        applicableRegulations=payload.applicableRegulations,
        regulatoryReviewRequired=payload.regulatoryReviewRequired,
        createdById=user.id,
    )
    db.add(study)
    await db.flush()

    # Team members
    for tm in payload.team:
        db.add(
            HiraStudyTeamMember(
                studyId=study.id,
                userId=tm.userId,
                teamRole=tm.teamRole,
                department=tm.department,
            )
        )
    await db.flush()

    # Workflow init is intentionally deferred to Phase 4. Studies stay in
    # DRAFT until the workflow definition is seeded and submission routes
    # through the engine.

    await db.refresh(study)
    # Re-load with team eagerly so the response has them populated
    stmt = (
        select(HiraStudy)
        .where(HiraStudy.id == study.id)
        .options(selectinload(HiraStudy.team))
    )
    study = (await db.execute(stmt)).scalar_one()
    return HiraStudyOut.model_validate(study)


@router.get("/studies/{study_id}", response_model=HiraStudyOut)
async def get_study(
    study_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraStudyOut:
    stmt = (
        select(HiraStudy)
        .where(HiraStudy.id == study_id)
        .options(selectinload(HiraStudy.team))
    )
    study = (await db.execute(stmt)).scalar_one_or_none()
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")

    check = await can(
        db,
        user.id,
        "HIRA.READ",
        PermissionContext(record_id=study.id, plant_id=study.plantId, record={"createdById": study.createdById, "teamLeaderId": study.teamLeaderId}),
    )
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    return HiraStudyOut.model_validate(study)


@router.get("/studies/{study_id}/detail", response_model=HiraStudyDetailResponse)
async def get_study_detail(
    study_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraStudyDetailResponse:
    """Composite endpoint serving the study detail page in one round-trip.

    Returns study + team + matrix + entries + denormalised display names so
    the Next.js page renders without touching Prisma directly.
    """
    stmt = (
        select(HiraStudy)
        .where(HiraStudy.id == study_id)
        .options(
            selectinload(HiraStudy.team),
            selectinload(HiraStudy.plant),
            selectinload(HiraStudy.department),
            selectinload(HiraStudy.area),
        )
    )
    study = (await db.execute(stmt)).scalar_one_or_none()
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")

    check = await can(
        db,
        user.id,
        "HIRA.READ",
        PermissionContext(
            record_id=study.id,
            plant_id=study.plantId,
            record={"createdById": study.createdById, "teamLeaderId": study.teamLeaderId},
        ),
    )
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    # Matrix
    matrix = await db.get(RiskMatrix, study.riskMatrixId)

    # Entries (current versions only) with hazard / control counts
    entry_rows = (
        await db.execute(
            select(HiraEntry)
            .where(HiraEntry.studyId == study_id)
            .where(HiraEntry.isCurrentVersion.is_(True))
            .order_by(HiraEntry.sequenceNumber.asc())
        )
    ).scalars().all()
    entry_ids = [e.id for e in entry_rows]
    hazard_counts: dict[str, int] = {}
    ec_counts: dict[str, int] = {}
    rc_counts: dict[str, int] = {}
    if entry_ids:
        hc = (
            await db.execute(
                select(HiraEntryHazard.entryId, func.count(HiraEntryHazard.id))
                .where(HiraEntryHazard.entryId.in_(entry_ids))
                .group_by(HiraEntryHazard.entryId)
            )
        ).all()
        hazard_counts = {eid: int(c) for eid, c in hc}
        ec = (
            await db.execute(
                select(HiraEntryControl.entryId, func.count(HiraEntryControl.id))
                .where(HiraEntryControl.entryId.in_(entry_ids))
                .group_by(HiraEntryControl.entryId)
            )
        ).all()
        ec_counts = {eid: int(c) for eid, c in ec}
        rc = (
            await db.execute(
                select(HiraEntryRecommendedControl.entryId, func.count(HiraEntryRecommendedControl.id))
                .where(HiraEntryRecommendedControl.entryId.in_(entry_ids))
                .group_by(HiraEntryRecommendedControl.entryId)
            )
        ).all()
        rc_counts = {eid: int(c) for eid, c in rc}

    entries_payload = []
    for e in entry_rows:
        d = HiraEntryListItem.model_validate(e).model_dump()
        d["hazardCount"] = hazard_counts.get(e.id, 0)
        d["existingControlCount"] = ec_counts.get(e.id, 0)
        d["recommendedControlCount"] = rc_counts.get(e.id, 0)
        entries_payload.append(HiraEntryListItem(**d))

    # User name lookups
    user_ids = (
        {study.teamLeaderId, study.createdById}
        | ({study.approvedById} if study.approvedById else set())
        | {m.userId for m in study.team}
    )
    name_rows = (await db.execute(select(User.id, User.name).where(User.id.in_(user_ids)))).all()
    names = {uid: nm for uid, nm in name_rows}

    return HiraStudyDetailResponse(
        study=HiraStudyOut.model_validate(study),
        entries=entries_payload,
        plantName=study.plant.name if study.plant else None,
        departmentName=study.department.name if study.department else None,
        areaName=study.area.name if study.area else None,
        teamLeaderName=names.get(study.teamLeaderId),
        approvedByName=names.get(study.approvedById) if study.approvedById else None,
        createdByName=names.get(study.createdById),
        teamMemberNames={m.userId: names.get(m.userId) or "" for m in study.team},
        riskMatrix=(
            {
                "id": matrix.id,
                "code": matrix.code,
                "name": matrix.name,
                "likelihoodLevels": matrix.likelihoodLevels,
                "severityLevels": matrix.severityLevels,
                "acceptableResidual": matrix.acceptableResidual,
                "controlHierarchyEnforced": matrix.controlHierarchyEnforced,
            }
            if matrix
            else None
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# Entries
# ─────────────────────────────────────────────────────────────────────


@router.get("/studies/{study_id}/entries", response_model=HiraEntryListResponse)
async def list_entries(
    study_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraEntryListResponse:
    # Re-use the study read check so list inherits its scope
    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    check = await can(
        db,
        user.id,
        "HIRA.READ",
        PermissionContext(record_id=study.id, plant_id=study.plantId),
    )
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    stmt = (
        select(HiraEntry)
        .where(HiraEntry.studyId == study_id)
        .where(HiraEntry.isCurrentVersion.is_(True))
        .order_by(HiraEntry.sequenceNumber.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return HiraEntryListResponse(
        items=[HiraEntryListItem.model_validate(r) for r in rows],
        total=len(rows),
    )


def _compute_risk(
    matrix_cells: list[RiskMatrixCell],
    likelihood_score: int,
    severity_score: int,
) -> tuple[int, str, str]:
    """Return (riskScore, riskLevel, colorHex) from the matrix cell.

    Falls back to closest-cell-by-score proximity if no exact cell matches
    (defensive — the seed creates all cells).
    """
    for c in matrix_cells:
        if c.likelihoodScore == likelihood_score and c.severityScore == severity_score:
            return c.riskScore, c.riskLevel, c.colorHex
    score = likelihood_score * severity_score
    # Fallback: use closest cell by score proximity
    cells = matrix_cells
    closest = min(cells, key=lambda c: abs(c.riskScore - score)) if cells else None
    if closest:
        return closest.riskScore, closest.riskLevel, closest.colorHex
    # Last resort fallback
    if score >= 15:
        return score, "CRITICAL", "#dc2626"
    elif score >= 8:
        return score, "HIGH", "#ea580c"
    elif score >= 4:
        return score, "MODERATE", "#ca8a04"
    return score, "LOW", "#16a34a"


@router.post(
    "/studies/{study_id}/entries",
    response_model=HiraEntryOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_entry(
    study_id: str,
    payload: HiraEntryCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraEntryOut:
    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    if study.status not in ("DRAFT", "IN_PROGRESS"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot add entries to a study in status {study.status}. Initiate a review to revise.",
        )

    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=study.plantId, record_id=study.id
    )

    likelihood = await db.get(RiskMatrixLikelihood, payload.initialLikelihoodId)
    severity = await db.get(RiskMatrixSeverity, payload.initialSeverityId)
    if likelihood is None or severity is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid likelihood or severity id")
    if likelihood.matrixId != study.riskMatrixId or severity.matrixId != study.riskMatrixId:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Likelihood/severity must belong to the study's risk matrix",
        )

    # Load cells once to compute the initial risk level
    cells_stmt = select(RiskMatrixCell).where(RiskMatrixCell.matrixId == study.riskMatrixId)
    cells = list((await db.execute(cells_stmt)).scalars().all())
    risk_score, risk_level, risk_color = _compute_risk(cells, likelihood.score, severity.score)

    # Auto-assign sequenceNumber atomically
    seq_result = await db.execute(
        select(func.coalesce(func.max(HiraEntry.sequenceNumber), 0) + 1).where(HiraEntry.studyId == study_id)
    )
    next_seq = seq_result.scalar_one()

    entry = HiraEntry(
        studyId=study_id,
        sequenceNumber=next_seq,
        groupLabel=payload.groupLabel,
        activityDescription=payload.activityDescription,
        areaId=payload.areaId,
        subLocation=payload.subLocation,
        routine=payload.routine,
        frequency=payload.frequency,
        typicalDurationMin=payload.typicalDurationMin,
        personsEmployees=payload.personsEmployees,
        personsContractors=payload.personsContractors,
        personsVisitors=payload.personsVisitors,
        personsPublic=payload.personsPublic,
        affectedPersonGroups=payload.affectedPersonGroups,
        equipmentUsed=payload.equipmentUsed,
        materialsUsed=payload.materialsUsed,
        energySourcesPresent=payload.energySourcesPresent,
        initialLikelihoodId=payload.initialLikelihoodId,
        initialLikelihoodScore=likelihood.score,
        initialLikelihoodRationale=payload.initialLikelihoodRationale,
        initialSeverityId=payload.initialSeverityId,
        initialSeverityScore=severity.score,
        initialSeverityRationale=payload.initialSeverityRationale,
        initialRiskScore=risk_score,
        initialRiskLevel=risk_level,
        initialRiskColor=risk_color,
        status="DRAFT",
        versionNumber=1,
        isCurrentVersion=True,
        createdById=user.id,
    )
    db.add(entry)
    await db.flush()
    await db.refresh(entry)

    # Save hazards with their consequence descriptions
    for idx, h in enumerate(payload.hazards):
        db.add(
            HiraEntryHazard(
                entryId=entry.id,
                hazardId=h.hazardId,
                contextualDescription=h.contextualDescription,
                consequence=h.consequence,
                sortOrder=idx,
            )
        )
    if payload.hazards:
        await db.flush()

    # Re-load with children eagerly
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry.id)
        .options(
            selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
            selectinload(HiraEntry.existingControls),
            selectinload(HiraEntry.recommendedControls),
            selectinload(HiraEntry.regulationRefs),
        )
    )
    entry = (await db.execute(stmt)).scalar_one()
    return HiraEntryOut.model_validate(entry)


@router.get("/entries/{entry_id}", response_model=HiraEntryOut)
async def get_entry(
    entry_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraEntryOut:
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry_id)
        .options(
            selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
            selectinload(HiraEntry.existingControls),
            selectinload(HiraEntry.recommendedControls),
            selectinload(HiraEntry.regulationRefs),
            selectinload(HiraEntry.capas),
            selectinload(HiraEntry.study),
        )
    )
    entry = (await db.execute(stmt)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")

    check = await can(
        db,
        user.id,
        "HIRA.READ",
        PermissionContext(record_id=entry.id, plant_id=entry.study.plantId),
    )
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    # Denormalise hazard names so the editor doesn't need a second lookup
    out = HiraEntryOut.model_validate(entry).model_dump()
    for i, hz in enumerate(out["hazards"]):
        src_hz = entry.hazards[i]
        if src_hz.hazard is not None:
            hz["hazardCode"] = src_hz.hazard.code
            hz["hazardCategory"] = src_hz.hazard.category
            hz["hazardName"] = src_hz.hazard.name
    return HiraEntryOut(**out)


# ═════════════════════════════════════════════════════════════════════
# Write endpoints — Phase: pure 3-tier migration
# ═════════════════════════════════════════════════════════════════════


@router.patch("/studies/{study_id}", response_model=HiraStudyOut)
async def update_study(
    study_id: str,
    payload: HiraStudyUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraStudyOut:
    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")

    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=study.plantId, record_id=study.id
    )

    # Once APPROVED/ACTIVE, only specific fields editable. Substantive edits
    # require a major-revision review cycle (mirrors the Next.js route logic).
    editable_in_active = {"nextScheduledReviewDate"}
    if study.status in ("APPROVED", "ACTIVE"):
        for field, value in payload.model_dump(exclude_unset=True).items():
            if field not in editable_in_active:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Study is approved/active. Substantive edits require a review cycle.",
                )

    PROTECTED_FIELDS = {"status", "approvedAt", "approvedById", "effectiveFrom"}
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if k in PROTECTED_FIELDS:
            continue
        setattr(study, k, v)
    study.updatedById = user.id

    await db.flush()
    await db.refresh(study)

    stmt = (
        select(HiraStudy).where(HiraStudy.id == study.id).options(selectinload(HiraStudy.team))
    )
    study = (await db.execute(stmt)).scalar_one()
    return HiraStudyOut.model_validate(study)


@router.delete("/studies/{study_id}", response_model=HiraStudyOut)
async def archive_study(
    study_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraStudyOut:
    """Soft archive — statutory record, never DELETE."""
    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")

    await require_permission_with_context(
        "HIRA.DELETE", user, db, plant_id=study.plantId, record_id=study.id
    )

    study.status = "ARCHIVED"
    study.updatedById = user.id
    await db.flush()
    await db.refresh(study)

    stmt = (
        select(HiraStudy).where(HiraStudy.id == study.id).options(selectinload(HiraStudy.team))
    )
    study = (await db.execute(stmt)).scalar_one()
    return HiraStudyOut.model_validate(study)




@router.post("/studies/{study_id}/submit", response_model=HiraStudyOut)
async def submit_study(
    study_id: str,
    payload: HiraStudyTransitionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HiraStudyOut:
    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=study.plantId, record_id=study.id
    )
    TRANSITIONS = {"DRAFT": "IN_PROGRESS", "IN_PROGRESS": "TEAM_REVIEW", "TEAM_REVIEW": "APPROVAL_PENDING"}
    if study.status not in TRANSITIONS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot submit study in status {study.status}")
    study.status = TRANSITIONS[study.status]
    study.updatedById = user.id
    await db.flush()
    await db.refresh(study)
    return HiraStudyOut.model_validate(study)


@router.post("/studies/{study_id}/approve", response_model=HiraStudyOut)
async def approve_study(
    study_id: str,
    payload: HiraStudyTransitionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HiraStudyOut:
    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await require_permission_with_context(
        "HIRA.APPROVE", user, db, plant_id=study.plantId, record_id=study.id
    )
    if study.status != "APPROVAL_PENDING":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Study must be APPROVAL_PENDING to approve, current: {study.status}")
    now = datetime.now(timezone.utc)
    study.status = "APPROVED"
    study.approvedById = user.id
    study.approvedAt = now
    study.effectiveFrom = now
    study.updatedById = user.id
    await db.flush()
    await db.refresh(study)
    return HiraStudyOut.model_validate(study)


@router.post("/studies/{study_id}/activate", response_model=HiraStudyOut)
async def activate_study(
    study_id: str,
    payload: HiraStudyTransitionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HiraStudyOut:
    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await require_permission_with_context(
        "HIRA.APPROVE", user, db, plant_id=study.plantId, record_id=study.id
    )
    if study.status != "APPROVED":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Study must be APPROVED to activate, current: {study.status}")
    study.status = "ACTIVE"
    study.updatedById = user.id
    await db.flush()
    await db.refresh(study)
    return HiraStudyOut.model_validate(study)

def _derive_level(score: int) -> str:
    if score >= 15:
        return "CRITICAL"
    if score >= 8:
        return "HIGH"
    if score >= 4:
        return "MODERATE"
    return "LOW"


def _acceptability_ok(level: str, threshold: str) -> bool:
    order = ["LOW", "MODERATE", "HIGH", "CRITICAL"]
    return order.index(level) <= order.index(threshold)


@router.patch("/entries/{entry_id}", response_model=HiraEntryOut)
async def update_entry(
    entry_id: str,
    payload: HiraEntryUpdate,
    change_reason: str | None = Query(None, alias="changeReason"),
    change_trigger: str = Query("CORRECTION", alias="changeTrigger"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraEntryOut:
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry_id)
        .options(selectinload(HiraEntry.study))
    )
    entry = (await db.execute(stmt)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")

    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=entry.study.plantId, record_id=entry.id
    )

    data = payload.model_dump(exclude_unset=True)

    # If initial L/S changed, recompute risk
    if "initialLikelihoodId" in data or "initialSeverityId" in data:
        l_id = data.get("initialLikelihoodId", entry.initialLikelihoodId)
        s_id = data.get("initialSeverityId", entry.initialSeverityId)
        l = await db.get(RiskMatrixLikelihood, l_id)
        s = await db.get(RiskMatrixSeverity, s_id)
        if not l or l.matrixId != entry.study.riskMatrixId:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid initialLikelihoodId for matrix")
        if not s or s.matrixId != entry.study.riskMatrixId:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid initialSeverityId for matrix")
        cell = (
            await db.execute(
                select(RiskMatrixCell)
                .where(RiskMatrixCell.matrixId == entry.study.riskMatrixId)
                .where(RiskMatrixCell.likelihoodScore == l.score)
                .where(RiskMatrixCell.severityScore == s.score)
            )
        ).scalar_one_or_none()
        entry.initialLikelihoodId = l.id
        entry.initialLikelihoodScore = l.score
        entry.initialSeverityId = s.id
        entry.initialSeverityScore = s.score
        entry.initialRiskScore = cell.riskScore if cell else l.score * s.score
        entry.initialRiskLevel = cell.riskLevel if cell else _derive_level(l.score * s.score)
        entry.initialRiskColor = cell.colorHex if cell else None
        data.pop("initialLikelihoodId", None)
        data.pop("initialSeverityId", None)

    # Residual recompute
    if "residualLikelihoodId" in data or "residualSeverityId" in data:
        # Only clear all residual fields if BOTH are explicitly set to None
        both_null = (
            "residualLikelihoodId" in data and data["residualLikelihoodId"] is None
            and "residualSeverityId" in data and data["residualSeverityId"] is None
        )
        l_id = data.get("residualLikelihoodId") if "residualLikelihoodId" in data else entry.residualLikelihoodId
        s_id = data.get("residualSeverityId") if "residualSeverityId" in data else entry.residualSeverityId

        if both_null:
            entry.residualLikelihoodId = None
            entry.residualLikelihoodScore = None
            entry.residualSeverityId = None
            entry.residualSeverityScore = None
            entry.residualRiskScore = None
            entry.residualRiskLevel = None
            entry.residualRiskColor = None
            entry.residualAcceptable = None
        elif l_id and s_id:
            l = await db.get(RiskMatrixLikelihood, l_id)
            s = await db.get(RiskMatrixSeverity, s_id)
            if not l or l.matrixId != entry.study.riskMatrixId:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid residualLikelihoodId")
            if not s or s.matrixId != entry.study.riskMatrixId:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid residualSeverityId")
            cell = (
                await db.execute(
                    select(RiskMatrixCell)
                    .where(RiskMatrixCell.matrixId == entry.study.riskMatrixId)
                    .where(RiskMatrixCell.likelihoodScore == l.score)
                    .where(RiskMatrixCell.severityScore == s.score)
                )
            ).scalar_one_or_none()
            matrix = await db.get(RiskMatrix, entry.study.riskMatrixId)
            threshold = (matrix.acceptableResidual or {}).get((entry.routine or "ROUTINE").lower()) if matrix else None
            residual_level = cell.riskLevel if cell else _derive_level(l.score * s.score)
            entry.residualLikelihoodId = l.id
            entry.residualLikelihoodScore = l.score
            entry.residualSeverityId = s.id
            entry.residualSeverityScore = s.score
            entry.residualRiskScore = cell.riskScore if cell else l.score * s.score
            entry.residualRiskLevel = residual_level
            entry.residualRiskColor = cell.colorHex if cell else None
            entry.residualAcceptable = (
                _acceptability_ok(residual_level, threshold) if threshold else None
            )
        data.pop("residualLikelihoodId", None)
        data.pop("residualSeverityId", None)

    # Versioning — if approved/active study OR not v1, snapshot before mutating
    needs_version = entry.study.status in ("APPROVED", "ACTIVE") or entry.versionNumber > 1
    if needs_version:
        if not change_reason:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "changeReason query param is required when editing entries on approved/active studies",
            )
        # Snapshot the current entry (pre-edit) state
        snapshot_stmt = (
            select(HiraEntry)
            .where(HiraEntry.id == entry.id)
            .options(
                selectinload(HiraEntry.hazards),
                selectinload(HiraEntry.existingControls),
                selectinload(HiraEntry.recommendedControls),
                selectinload(HiraEntry.regulationRefs),
            )
        )
        snap_entry = (await db.execute(snapshot_stmt)).scalar_one()
        snapshot_dict = {
            "entry": {
                k: (v.isoformat() if hasattr(v, "isoformat") else v)
                for k, v in snap_entry.__dict__.items()
                if not k.startswith("_") and not isinstance(v, list)
            }
        }
        changes = []
        for field, new_val in data.items():
            old_val = getattr(entry, field, None)
            if old_val != new_val:
                changes.append({
                    "field": field,
                    "from": str(old_val) if old_val is not None else None,
                    "to": str(new_val) if new_val is not None else None,
                })
        db.add(
            HiraVersion(
                entryId=entry.id,
                versionNumber=entry.versionNumber,
                snapshot=snapshot_dict,
                changes=changes,
                changeReason=change_reason,
                changeTrigger=change_trigger,
                createdById=user.id,
            )
        )
        entry.versionNumber += 1

    # Apply remaining scalar fields (protected fields cannot be patched via PATCH)
    ENTRY_PROTECTED_FIELDS = {"status", "versionNumber", "isCurrentVersion"}
    for k, v in data.items():
        if k in ENTRY_PROTECTED_FIELDS:
            continue
        setattr(entry, k, v)
    entry.updatedById = user.id

    await db.flush()
    await db.refresh(entry)

    refresh_stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry.id)
        .options(
            selectinload(HiraEntry.hazards),
            selectinload(HiraEntry.existingControls),
            selectinload(HiraEntry.recommendedControls),
            selectinload(HiraEntry.regulationRefs),
        )
    )
    entry = (await db.execute(refresh_stmt)).scalar_one()
    return HiraEntryOut.model_validate(entry)




async def _get_entry_detail(entry_id: str, db: AsyncSession) -> HiraEntryOut:
    """Shared helper to reload an entry with all child relations."""
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry_id)
        .options(
            selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
            selectinload(HiraEntry.existingControls),
            selectinload(HiraEntry.recommendedControls),
            selectinload(HiraEntry.regulationRefs),
            selectinload(HiraEntry.capas),
            selectinload(HiraEntry.study),
        )
    )
    entry = (await db.execute(stmt)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    return HiraEntryOut.model_validate(entry)


@router.post("/entries/{entry_id}/submit-for-review", response_model=HiraEntryOut)
async def submit_entry_for_review(
    entry_id: str,
    payload: HiraEntryTransitionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HiraEntryOut:
    entry = await db.get(HiraEntry, entry_id, options=[selectinload(HiraEntry.study)])
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=entry.study.plantId, record_id=entry.id
    )
    if entry.status not in ("DRAFT", "FLAGGED_FOR_REVIEW"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Entry cannot be submitted from status {entry.status}")
    entry.status = "IN_REVIEW"
    entry.updatedById = user.id
    await db.flush()
    return await _get_entry_detail(entry_id, db)


@router.post("/entries/{entry_id}/approve", response_model=HiraEntryOut)
async def approve_entry(
    entry_id: str,
    payload: HiraEntryTransitionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HiraEntryOut:
    entry = await db.get(HiraEntry, entry_id, options=[selectinload(HiraEntry.study)])
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context(
        "HIRA.APPROVE", user, db, plant_id=entry.study.plantId, record_id=entry.id
    )
    if entry.status != "IN_REVIEW":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Entry must be IN_REVIEW to approve, current: {entry.status}")
    entry.status = "APPROVED"
    entry.updatedById = user.id
    await db.flush()
    return await _get_entry_detail(entry_id, db)


@router.get("/entries/{entry_id}/capas", response_model=list[HiraCapaOut])
async def list_entry_capas(
    entry_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[HiraCapaOut]:
    entry = await db.get(HiraEntry, entry_id, options=[selectinload(HiraEntry.study)])
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context("HIRA.READ", user, db, plant_id=entry.study.plantId, record_id=entry.id)
    rows = (await db.execute(
        select(HiraCapa).where(HiraCapa.entryId == entry_id).order_by(HiraCapa.createdAt)
    )).scalars().all()
    return [HiraCapaOut.model_validate(r) for r in rows]


@router.post("/entries/{entry_id}/capas", response_model=HiraCapaOut, status_code=status.HTTP_201_CREATED)
async def create_entry_capa(
    entry_id: str,
    payload: HiraCapaCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HiraCapaOut:
    entry = await db.get(HiraEntry, entry_id, options=[selectinload(HiraEntry.study)])
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context("HIRA.UPDATE", user, db, plant_id=entry.study.plantId, record_id=entry.id)
    # Auto-generate CAPA number
    count_r = await db.execute(select(func.count()).select_from(HiraCapa).where(HiraCapa.entryId == entry_id))
    count = count_r.scalar_one()
    capa_number = f"CAPA-{entry_id[:8].upper()}-{count + 1:03d}"
    capa = HiraCapa(
        entryId=entry_id,
        number=capa_number,
        description=payload.description,
        controlHierarchy=payload.controlHierarchy,
        ownerId=payload.ownerId,
        targetDate=payload.targetDate,
        status="OPEN",
        createdById=user.id,
        updatedById=user.id,
    )
    db.add(capa)
    await db.flush()
    await db.refresh(capa)
    return HiraCapaOut.model_validate(capa)


@router.patch("/capas/{capa_id}", response_model=HiraCapaOut)
async def update_capa(
    capa_id: str,
    payload: HiraCapaUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HiraCapaOut:
    capa = await db.get(HiraCapa, capa_id, options=[selectinload(HiraCapa.entry).selectinload(HiraEntry.study)])
    if capa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CAPA not found")
    await require_permission_with_context("HIRA.UPDATE", user, db, plant_id=capa.entry.study.plantId)
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(capa, k, v)
    capa.updatedById = user.id
    await db.flush()
    await db.refresh(capa)
    return HiraCapaOut.model_validate(capa)


@router.put("/entries/{entry_id}/hazards", response_model=dict)
async def replace_entry_hazards(
    entry_id: str,
    payload: list[HiraEntryHazardReplaceItem],
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    entry = await db.get(
        HiraEntry, entry_id,
        options=[selectinload(HiraEntry.study), selectinload(HiraEntry.hazards)],
    )
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=entry.study.plantId, record_id=entry.id
    )
    # Delete existing hazards
    for hz in entry.hazards:
        await db.delete(hz)
    await db.flush()
    # Insert new hazards
    for idx, h in enumerate(payload):
        db.add(HiraEntryHazard(
            entryId=entry.id,
            hazardId=h.hazardId,
            contextualDescription=h.contextualDescription,
            consequence=h.consequence,
            sortOrder=h.sortOrder if h.sortOrder is not None else idx,
        ))
    await db.flush()
    return {"count": len(payload)}


@router.put("/entries/{entry_id}/existing-controls")
async def replace_existing_controls(
    entry_id: str,
    payload: HiraEntryControlReplaceRequest,
    change_reason: str | None = Query(None, alias="changeReason"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry_id)
        .options(selectinload(HiraEntry.study))
    )
    entry = (await db.execute(stmt)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=entry.study.plantId, record_id=entry.id
    )

    needs_version = entry.study.status in ("APPROVED", "ACTIVE")
    if needs_version:
        if not change_reason:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "changeReason is required when study is APPROVED or ACTIVE")
        snapshot = {k: v for k, v in entry.__dict__.items() if not k.startswith("_") and not isinstance(v, list)}
        version = HiraVersion(
            entryId=entry.id,
            versionNumber=entry.versionNumber + 1,
            isCurrentVersion=False,
            snapshot=snapshot,
            changes=[{"action": "controls_replaced", "changeReason": change_reason}],
            changeTrigger="CONTROLS_UPDATED",
            changeReason=change_reason,
            changedById=user.id,
        )
        db.add(version)
        entry.versionNumber += 1
        await db.flush()

    # Wholesale replace
    existing = (
        await db.execute(select(HiraEntryControl).where(HiraEntryControl.entryId == entry_id))
    ).scalars().all()
    for e in existing:
        await db.delete(e)

    for idx, c in enumerate(payload.controls):
        db.add(
            HiraEntryControl(
                entryId=entry_id,
                controlId=c.controlId,
                hierarchy=c.hierarchy,
                description=c.description,
                effectiveness=c.effectiveness,
                verificationMethod=c.verificationMethod,
                verificationFreq=c.verificationFreq,
                responsibleRole=c.responsibleRole,
                evidenceAttached=c.evidenceAttached,
                documentReference=c.documentReference,
                sortOrder=c.sortOrder if c.sortOrder is not None else idx,
            )
        )

    entry.updatedById = user.id
    await db.flush()

    rows = (
        await db.execute(
            select(HiraEntryControl)
            .where(HiraEntryControl.entryId == entry_id)
            .order_by(HiraEntryControl.sortOrder.asc())
        )
    ).scalars().all()
    return {"controls": [{"id": r.id, "hierarchy": r.hierarchy, "description": r.description} for r in rows]}


@router.put("/entries/{entry_id}/recommended-controls")
async def replace_recommended_controls(
    entry_id: str,
    payload: HiraEntryRecommendedControlReplaceRequest,
    change_reason: str | None = Query(None, alias="changeReason"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry_id)
        .options(selectinload(HiraEntry.study))
    )
    entry = (await db.execute(stmt)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=entry.study.plantId, record_id=entry.id
    )

    needs_version = entry.study.status in ("APPROVED", "ACTIVE")
    if needs_version:
        if not change_reason:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "changeReason is required when study is APPROVED or ACTIVE")
        snapshot = {k: v for k, v in entry.__dict__.items() if not k.startswith("_") and not isinstance(v, list)}
        version = HiraVersion(
            entryId=entry.id,
            versionNumber=entry.versionNumber + 1,
            isCurrentVersion=False,
            snapshot=snapshot,
            changes=[{"action": "controls_replaced", "changeReason": change_reason}],
            changeTrigger="CONTROLS_UPDATED",
            changeReason=change_reason,
            changedById=user.id,
        )
        db.add(version)
        entry.versionNumber += 1
        await db.flush()

    existing = (
        await db.execute(
            select(HiraEntryRecommendedControl).where(
                HiraEntryRecommendedControl.entryId == entry_id
            )
        )
    ).scalars().all()
    incoming_ids = {c.id for c in payload.controls if c.id and not c.id.startswith("new-")}

    # Delete rows not in incoming AND not linked to a CAPA (preserves linked rows)
    for e in existing:
        if e.id not in incoming_ids and not e.capaId:
            await db.delete(e)

    existing_by_id = {e.id: e for e in existing}
    for c in payload.controls:
        if c.id and c.id in existing_by_id:
            row = existing_by_id[c.id]
            row.hierarchy = c.hierarchy
            row.description = c.description
            row.rationale = c.rationale
            row.targetLikelihoodReduction = c.targetLikelihoodReduction
            row.targetSeverityReduction = c.targetSeverityReduction
            row.estimatedCostBand = c.estimatedCostBand
            row.proposedImplementationDate = c.proposedImplementationDate
            row.responsibleId = c.responsibleId
            row.status = c.status
        else:
            db.add(
                HiraEntryRecommendedControl(
                    entryId=entry_id,
                    hierarchy=c.hierarchy,
                    description=c.description,
                    rationale=c.rationale,
                    targetLikelihoodReduction=c.targetLikelihoodReduction,
                    targetSeverityReduction=c.targetSeverityReduction,
                    estimatedCostBand=c.estimatedCostBand,
                    proposedImplementationDate=c.proposedImplementationDate,
                    responsibleId=c.responsibleId,
                    status=c.status,
                )
            )

    entry.updatedById = user.id
    await db.flush()

    rows = (
        await db.execute(
            select(HiraEntryRecommendedControl)
            .where(HiraEntryRecommendedControl.entryId == entry_id)
            .order_by(HiraEntryRecommendedControl.createdAt.asc())
        )
    ).scalars().all()
    return {"controls": [{"id": r.id, "hierarchy": r.hierarchy, "status": r.status} for r in rows]}


@router.put("/entries/{entry_id}/regulation-refs")
async def replace_regulation_refs(
    entry_id: str,
    payload: HiraEntryRegulationRefReplaceRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry_id)
        .options(selectinload(HiraEntry.study))
    )
    entry = (await db.execute(stmt)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=entry.study.plantId, record_id=entry.id
    )

    existing = (
        await db.execute(
            select(HiraEntryRegulationRef).where(HiraEntryRegulationRef.entryId == entry_id)
        )
    ).scalars().all()
    for e in existing:
        await db.delete(e)
    for r in payload.refs:
        if not r.regulation.strip():
            continue
        db.add(
            HiraEntryRegulationRef(
                entryId=entry_id,
                regulation=r.regulation.strip(),
                section=r.section,
                requirementSummary=r.requirementSummary,
            )
        )
    entry.updatedById = user.id
    await db.flush()

    rows = (
        await db.execute(
            select(HiraEntryRegulationRef)
            .where(HiraEntryRegulationRef.entryId == entry_id)
            .order_by(HiraEntryRegulationRef.createdAt.asc())
        )
    ).scalars().all()
    return {"refs": [{"id": r.id, "regulation": r.regulation, "section": r.section} for r in rows]}


# ─────────────────────────────────────────────────────────────────────
# Review cycles
# ─────────────────────────────────────────────────────────────────────


@router.get("/review-cycles", response_model=list[HiraReviewCycleListItem])
async def list_review_cycles(
    status_filter: str | None = Query(None, alias="status"),
    trigger_filter: str | None = Query(None, alias="trigger"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[HiraReviewCycleListItem]:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    accessible = await get_accessible_plants(db, user.id)
    stmt = (
        select(HiraReviewCycle)
        .join(HiraEntry, HiraReviewCycle.entryId == HiraEntry.id)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
        .options(
            selectinload(HiraReviewCycle.entry).selectinload(HiraEntry.study)
        )
    )
    if accessible is None:
        pass
    elif len(accessible) == 0:
        return []
    else:
        stmt = stmt.where(HiraStudy.plantId.in_(accessible))
    if status_filter:
        stmt = stmt.where(HiraReviewCycle.status == status_filter)
    else:
        stmt = stmt.where(HiraReviewCycle.status.in_(["SCHEDULED", "IN_PROGRESS"]))
    if trigger_filter:
        stmt = stmt.where(HiraReviewCycle.triggeredBy == trigger_filter)
    stmt = stmt.order_by(HiraReviewCycle.scheduledFor.asc()).limit(200)
    rows = (await db.execute(stmt)).scalars().all()

    result = []
    for r in rows:
        item = HiraReviewCycleListItem(
            id=r.id,
            entryId=r.entryId,
            scheduledFor=r.scheduledFor,
            triggeredBy=r.triggeredBy,
            triggerReferenceId=r.triggerReferenceId,
            status=r.status,
            assignedToId=r.assignedToId,
            outcome=r.outcome,
            createdAt=r.createdAt,
            entryTitle=r.entry.activityDescription if r.entry else None,
            entrySequenceNumber=r.entry.sequenceNumber if r.entry else None,
            studyNumber=r.entry.study.number if r.entry and r.entry.study else None,
            studyTitle=r.entry.study.title if r.entry and r.entry.study else None,
        )
        result.append(item)
    return result


@router.post("/review-cycles/bulk-no-change")
async def bulk_no_change(
    payload: HiraReviewCycleBulkNoChangeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Mark multiple SCHEDULED/IN_PROGRESS cycles as NO_CHANGE_REQUIRED in one call."""
    check = await can(db, user.id, "HIRA.EXECUTE", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    accessible_plants = await get_accessible_plants(db, user.id)

    rows = (
        await db.execute(
            select(HiraReviewCycle)
            .where(HiraReviewCycle.id.in_(payload.cycleIds))
            .options(selectinload(HiraReviewCycle.entry).selectinload(HiraEntry.study))
        )
    ).scalars().all()

    # Filter to only cycles within accessible plants
    if accessible_plants is not None:
        rows = [r for r in rows if r.entry and r.entry.study and r.entry.study.plantId in accessible_plants]

    now = datetime.now(timezone.utc)
    notes = payload.notes or "No change required — bulk submission"
    updated: list[str] = []
    skipped: list[str] = []

    for cycle in rows:
        if cycle.status not in ("SCHEDULED", "IN_PROGRESS"):
            skipped.append(cycle.id)
            continue
        next_due = _compute_next_review_due(
            cycle.entry.study.reviewFrequency, cycle.entry.study.customReviewMonths
        )
        cycle.status = "COMPLETED"
        cycle.completedAt = now
        cycle.completedById = user.id
        cycle.outcome = "NO_CHANGE_REQUIRED"
        cycle.outcomeNotes = notes
        cycle.entry.lastReviewedAt = now
        cycle.entry.lastReviewedById = user.id
        cycle.entry.nextReviewDue = next_due
        cycle.entry.reviewCount = (cycle.entry.reviewCount or 0) + 1
        cycle.entry.lastReviewType = "SCHEDULED"
        cycle.entry.status = "ACTIVE"
        updated.append(cycle.id)

    await db.flush()
    return {"updated": updated, "skipped": skipped}


@router.get("/review-cycles/{cycle_id}", response_model=HiraReviewCycleOut)
async def get_review_cycle(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraReviewCycleOut:
    cycle = await db.get(HiraReviewCycle, cycle_id)
    if cycle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cycle not found")
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    return HiraReviewCycleOut.model_validate(cycle)


def _compute_next_review_due(frequency: str, custom_months: int | None) -> datetime:
    from datetime import timedelta

    months = {
        "QUARTERLY": 3,
        "BIENNIAL": 24,
        "ANNUAL": 12,
        "CUSTOM": custom_months or 12,
        "TRIGGERED_ONLY": 36,
    }.get(frequency, 12)
    now = datetime.now(timezone.utc)
    if _HAS_RELATIVEDELTA:
        return now + _relativedelta(months=months)
    else:
        return now + timedelta(days=months * 30)


@router.post("/review-cycles/{cycle_id}/submit", response_model=HiraReviewCycleOut)
async def submit_review_cycle(
    cycle_id: str,
    payload: HiraReviewCycleSubmitRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraReviewCycleOut:
    stmt = (
        select(HiraReviewCycle)
        .where(HiraReviewCycle.id == cycle_id)
        .options(selectinload(HiraReviewCycle.entry).selectinload(HiraEntry.study))
    )
    cycle = (await db.execute(stmt)).scalar_one_or_none()
    if cycle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cycle not found")
    if cycle.status not in ("SCHEDULED", "IN_PROGRESS"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot submit in status {cycle.status}")

    valid = {"NO_CHANGE_REQUIRED", "MINOR_REVISION", "MAJOR_REVISION", "NEW_ENTRY_CREATED", "ENTRY_ARCHIVED"}
    if payload.outcome not in valid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid outcome")

    await require_permission_with_context(
        "HIRA.EXECUTE",
        user,
        db,
        plant_id=cycle.entry.study.plantId,
        record_id=cycle.entryId,
    )

    now = datetime.now(timezone.utc)
    next_due = _compute_next_review_due(
        cycle.entry.study.reviewFrequency, cycle.entry.study.customReviewMonths
    )

    entry_status_patch = None
    if payload.outcome == "MAJOR_REVISION":
        entry_status_patch = "FLAGGED_FOR_REVIEW"
    elif payload.outcome == "ENTRY_ARCHIVED":
        entry_status_patch = "ARCHIVED"
    elif payload.outcome in ("NO_CHANGE_REQUIRED", "MINOR_REVISION"):
        entry_status_patch = "ACTIVE"

    # MAJOR_REVISION stays IN_PROGRESS until Team Leader re-approves the entry
    if payload.outcome == "MAJOR_REVISION":
        cycle.status = "IN_PROGRESS"
    else:
        cycle.status = "COMPLETED"
        cycle.completedAt = now
        cycle.completedById = user.id
    cycle.outcome = payload.outcome
    cycle.outcomeNotes = payload.outcomeNotes

    cycle.entry.lastReviewedAt = now
    cycle.entry.lastReviewedById = user.id
    if payload.outcome != "MAJOR_REVISION":
        cycle.entry.nextReviewDue = next_due
        cycle.entry.reviewCount = (cycle.entry.reviewCount or 0) + 1
    cycle.entry.lastReviewType = {
        "SCHEDULE": "SCHEDULED",
        "INCIDENT": "INCIDENT_TRIGGERED",
        "MOC": "MOC_TRIGGERED",
        "AUDIT_FINDING": "AUDIT_TRIGGERED",
        "MANUAL": "MANUAL_TRIGGERED",
        "NEAR_MISS": "NEAR_MISS_TRIGGERED",
        "OBSERVATION": "OBSERVATION_TRIGGERED",
        "REGULATORY_CHANGE": "REGULATORY_CHANGE_TRIGGERED",
    }.get(cycle.triggeredBy, "AD_HOC")
    cycle.entry.triggeredByRecordId = cycle.triggerReferenceId
    if entry_status_patch:
        cycle.entry.status = entry_status_patch

    await db.flush()
    await db.refresh(cycle)
    return HiraReviewCycleOut.model_validate(cycle)


# ─────────────────────────────────────────────────────────────────────
# Versions
# ─────────────────────────────────────────────────────────────────────


@router.get("/entries/{entry_id}/versions", response_model=list[HiraVersionOut])
async def list_versions(
    entry_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[HiraVersionOut]:
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry_id)
        .options(selectinload(HiraEntry.study))
    )
    entry = (await db.execute(stmt)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")

    check = await can(
        db,
        user.id,
        "HIRA.READ",
        PermissionContext(record_id=entry.id, plant_id=entry.study.plantId),
    )
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    rows = (
        await db.execute(
            select(HiraVersion)
            .where(HiraVersion.entryId == entry_id)
            .order_by(HiraVersion.versionNumber.desc())
        )
    ).scalars().all()
    return [HiraVersionOut.model_validate(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────
# Integrations — FLRA / PTW / Inspection priority
# ─────────────────────────────────────────────────────────────────────


def _serialize_integration_entry(e: HiraEntry, study: HiraStudy) -> HiraIntegrationEntry:
    return HiraIntegrationEntry(
        id=e.id,
        sequenceNumber=e.sequenceNumber,
        activityDescription=e.activityDescription,
        initialRiskLevel=e.initialRiskLevel,
        initialRiskScore=e.initialRiskScore,
        residualRiskLevel=e.residualRiskLevel,
        residualRiskScore=e.residualRiskScore,
        residualAcceptable=e.residualAcceptable,
        studyId=study.id,
        studyNumber=study.number,
        studyTitle=study.title,
        hazards=[],
        influencesPtwRiskLevel=e.influencesPtwRiskLevel,
        influencesPtwPermitTypes=e.influencesPtwPermitTypes,
    )


@router.get("/integrations/for-flra", response_model=HiraIntegrationForFlraResponse)
async def for_flra(
    plant_id: str = Query(..., alias="plantId"),
    area_id: str | None = Query(None, alias="areaId"),
    activity_keyword: str | None = Query(None, alias="activityKeyword"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraIntegrationForFlraResponse:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    stmt = (
        select(HiraEntry, HiraStudy)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
        .where(HiraStudy.plantId == plant_id)
        .where(HiraStudy.status == "ACTIVE")
        .where(HiraEntry.isCurrentVersion.is_(True))
        .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "FLAGGED_FOR_REVIEW"]))
    )
    if area_id:
        stmt = stmt.where(HiraEntry.areaId == area_id)
    if activity_keyword:
        stmt = stmt.where(HiraEntry.activityDescription.ilike(f"%{activity_keyword}%"))
    stmt = stmt.limit(200)
    rows = (await db.execute(stmt)).all()
    entries = [_serialize_integration_entry(e, s) for e, s in rows]
    return HiraIntegrationForFlraResponse(entries=entries, count=len(entries))


@router.get("/integrations/for-ptw", response_model=HiraIntegrationForPtwResponse)
async def for_ptw(
    plant_id: str = Query(..., alias="plantId"),
    area_id: str | None = Query(None, alias="areaId"),
    permit_type: str | None = Query(None, alias="permitType"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraIntegrationForPtwResponse:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    base = (
        select(HiraEntry, HiraStudy)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
        .where(HiraStudy.plantId == plant_id)
        .where(HiraStudy.status == "ACTIVE")
        .where(HiraEntry.isCurrentVersion.is_(True))
        .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "FLAGGED_FOR_REVIEW"]))
    )
    if area_id:
        base = base.where(HiraEntry.areaId == area_id)

    explicit_q = base.where(HiraEntry.influencesPtwRiskLevel.is_(True))
    if permit_type:
        # Filter in SQL using JSON contains — fall back to Python if DB doesn't support it
        explicit_q = explicit_q.where(
            or_(
                HiraEntry.influencesPtwPermitTypes.is_(None),
                HiraEntry.influencesPtwPermitTypes.contains([permit_type]),
            )
        )
    explicit = explicit_q.limit(200)
    high_risk = base.where(
        or_(HiraEntry.residualRiskLevel == "HIGH", HiraEntry.residualRiskLevel == "CRITICAL")
    ).limit(200)

    explicit_rows = (await db.execute(explicit)).all()
    high_rows = (await db.execute(high_risk)).all()

    by_id: dict[str, tuple[HiraEntry, HiraStudy]] = {}
    for e, s in explicit_rows:
        by_id[e.id] = (e, s)
    for e, s in high_rows:
        by_id.setdefault(e.id, (e, s))

    sorted_entries = sorted(
        by_id.values(),
        key=lambda x: (x[0].residualRiskScore or x[0].initialRiskScore),
        reverse=True,
    )
    entries = [_serialize_integration_entry(e, s) for e, s in sorted_entries]
    gating = sum(1 for e in entries if e.residualRiskLevel == "CRITICAL")
    high = sum(1 for e in entries if e.residualRiskLevel == "HIGH")
    advisory = None
    if gating > 0:
        advisory = f"STOP — {gating} CRITICAL residual risk entr{'y' if gating == 1 else 'ies'} in this area. Corporate HSE approval required."
    elif high > 0:
        advisory = f"{high} HIGH residual risk entr{'y' if high == 1 else 'ies'} in this area — additional controls recommended for this permit."
    return HiraIntegrationForPtwResponse(
        entries=entries, count=len(entries), gatingBlockers=gating, highCount=high, advisory=advisory
    )


@router.get("/integrations/for-inspection", response_model=HiraInspectionPriorityResult)
async def for_inspection(
    plant_id: str = Query(..., alias="plantId"),
    area_id: str | None = Query(None, alias="areaId"),
    equipment_id: str | None = Query(None, alias="equipmentId"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraInspectionPriorityResult:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    stmt = (
        select(HiraEntry)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
        .where(HiraStudy.plantId == plant_id)
        .where(HiraStudy.status == "ACTIVE")
        .where(HiraEntry.isCurrentVersion.is_(True))
        .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "FLAGGED_FOR_REVIEW"]))
    )
    if area_id:
        stmt = stmt.where(HiraEntry.areaId == area_id)
    candidates = (await db.execute(stmt.limit(200))).scalars().all()

    if equipment_id:
        candidates = [
            c for c in candidates if equipment_id in ((c.equipmentUsed or []) if isinstance(c.equipmentUsed, list) else [])
        ]

    if not candidates:
        return HiraInspectionPriorityResult(
            multiplier=1.0, rationale="No HIRA entries match — baseline frequency.", sourceEntries=[]
        )

    order = ["LOW", "MODERATE", "HIGH", "CRITICAL"]
    highest = max(candidates, key=lambda c: order.index(c.residualRiskLevel or "LOW"))
    level = highest.residualRiskLevel or "LOW"
    multiplier = {"CRITICAL": 4.0, "HIGH": 2.0, "MODERATE": 1.5}.get(level, 1.0)
    sources = [
        {"id": c.id, "sequenceNumber": c.sequenceNumber, "residualRiskLevel": c.residualRiskLevel}
        for c in candidates
        if c.residualRiskLevel == level
    ][:5]
    return HiraInspectionPriorityResult(
        multiplier=multiplier,
        rationale=f"{len(candidates)} HIRA entries match this scope; highest residual = {level}. Apply {multiplier}x baseline inspection frequency.",
        sourceEntries=sources,
    )


# ─────────────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────────────


@router.get("/studies/{study_id}/export.csv")
async def export_study_csv(
    study_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import Response

    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await require_permission_with_context(
        "HIRA.EXPORT", user, db, plant_id=study.plantId, record_id=study.id
    )

    stmt = (
        select(HiraStudy)
        .where(HiraStudy.id == study_id)
        .options(
            selectinload(HiraStudy.plant),
            selectinload(HiraStudy.department),
            selectinload(HiraStudy.area),
            selectinload(HiraStudy.entries).selectinload(HiraEntry.area),
            selectinload(HiraStudy.entries).selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
            selectinload(HiraStudy.entries).selectinload(HiraEntry.existingControls),
            selectinload(HiraStudy.entries).selectinload(HiraEntry.recommendedControls),
            selectinload(HiraStudy.entries).selectinload(HiraEntry.regulationRefs),
        )
    )
    study = (await db.execute(stmt)).scalar_one()
    current_entries = [e for e in study.entries if e.isCurrentVersion]
    current_entries.sort(key=lambda e: e.sequenceNumber)

    def esc(s: str | None) -> str:
        if s is None:
            return ""
        s = str(s)
        if any(c in s for c in [",", '"', "\n", "\r"]):
            return '"' + s.replace('"', '""') + '"'
        return s

    rows: list[list[str]] = []
    rows.append([f"HIRA Register — {study.number}"])
    rows.append([f"Title: {study.title}"])
    rows.append([f"Plant: {study.plant.name if study.plant else '—'}"])
    rows.append([f"Status: {study.status}"])
    rows.append([f"Generated: {datetime.now(timezone.utc).isoformat()}"])
    rows.append([f"Total Entries: {len(current_entries)}"])
    rows.append([""])
    rows.append(
        [
            "Sr.No.",
            "Activity",
            "Area",
            "Routine",
            "Frequency",
            "Hazards",
            "Init L",
            "Init S",
            "Init Risk",
            "Init Level",
            "Existing Controls",
            "Resid L",
            "Resid S",
            "Resid Risk",
            "Resid Level",
            "Acceptable",
            "Recommended",
            "Reg Refs",
            "Status",
        ]
    )

    for e in current_entries:
        rows.append(
            [
                str(e.sequenceNumber),
                e.activityDescription,
                e.area.name if e.area else "",
                e.routine,
                e.frequency,
                "; ".join(f"{h.hazard.name if h.hazard else '(deleted)'} [{h.hazard.category if h.hazard else '?'}]" for h in e.hazards),
                str(e.initialLikelihoodScore),
                str(e.initialSeverityScore),
                str(e.initialRiskScore),
                e.initialRiskLevel,
                "; ".join(f"{c.hierarchy}: {c.description}" for c in e.existingControls),
                str(e.residualLikelihoodScore) if e.residualLikelihoodScore is not None else "",
                str(e.residualSeverityScore) if e.residualSeverityScore is not None else "",
                str(e.residualRiskScore) if e.residualRiskScore is not None else "",
                e.residualRiskLevel or "",
                "" if e.residualAcceptable is None else ("Yes" if e.residualAcceptable else "No"),
                "; ".join(f"[{c.status}] {c.hierarchy}: {c.description}" for c in e.recommendedControls),
                "; ".join(f"{r.regulation} {r.section or ''}".strip() for r in e.regulationRefs),
                e.status,
            ]
        )

    csv = "﻿" + "\r\n".join(",".join(esc(c) for c in r) for r in rows)
    return Response(
        content=csv,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{study.number}.csv"'},
    )


# ─────────────────────────────────────────────────────────────────────
# Dashboard aggregates
# ─────────────────────────────────────────────────────────────────────


@router.get("/dashboard/coverage", response_model=HiraDashboardCoverage)
async def dashboard_coverage(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraDashboardCoverage:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    accessible = await get_accessible_plants(db, user.id)

    from app.models.masters import Department

    total_q = select(func.count()).select_from(Department).where(Department.active.is_(True))
    if accessible is not None:
        if not accessible:
            return HiraDashboardCoverage(totalDepartments=0, coveredDepartments=0, coveragePct=0)
        total_q = total_q.where(Department.plantId.in_(accessible))

    total = (await db.execute(total_q)).scalar_one() or 0

    covered_q = (
        select(func.count(func.distinct(HiraStudy.departmentId)))
        .where(HiraStudy.status == "ACTIVE")
        .where(HiraStudy.departmentId.is_not(None))
    )
    if accessible is not None:
        covered_q = covered_q.where(HiraStudy.plantId.in_(accessible))
    covered = (await db.execute(covered_q)).scalar_one() or 0

    pct = int(round((covered / total) * 100)) if total > 0 else 0
    return HiraDashboardCoverage(totalDepartments=total, coveredDepartments=covered, coveragePct=pct)


@router.get("/dashboard/high-risk", response_model=HiraDashboardHighRisk)
async def dashboard_high_risk(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraDashboardHighRisk:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    accessible = await get_accessible_plants(db, user.id)

    base = (
        select(func.count())
        .select_from(HiraEntry)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
        .where(HiraEntry.isCurrentVersion.is_(True))
        .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "FLAGGED_FOR_REVIEW"]))
    )
    if accessible is not None:
        if not accessible:
            return HiraDashboardHighRisk(high=0, critical=0, total=0)
        base = base.where(HiraStudy.plantId.in_(accessible))

    high = (await db.execute(base.where(HiraEntry.residualRiskLevel == "HIGH"))).scalar_one() or 0
    critical = (await db.execute(base.where(HiraEntry.residualRiskLevel == "CRITICAL"))).scalar_one() or 0
    return HiraDashboardHighRisk(high=high, critical=critical, total=high + critical)


@router.get("/dashboard/risk-reduction", response_model=HiraDashboardRiskReduction)
async def dashboard_risk_reduction(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraDashboardRiskReduction:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    accessible = await get_accessible_plants(db, user.id)

    stmt = (
        select(HiraEntry.initialRiskScore, HiraEntry.residualRiskScore)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
        .where(HiraEntry.isCurrentVersion.is_(True))
        .where(HiraEntry.status.in_(["APPROVED", "ACTIVE"]))
    )
    if accessible is not None:
        if not accessible:
            return HiraDashboardRiskReduction(initialTotal=0, residualTotal=0, reductionPct=0)
        stmt = stmt.where(HiraStudy.plantId.in_(accessible))

    rows = (await db.execute(stmt)).all()
    initial_total = sum(r[0] or 0 for r in rows)
    residual_total = sum((r[1] if r[1] is not None else (r[0] or 0)) for r in rows)
    pct = int(round(((initial_total - residual_total) / initial_total) * 100)) if initial_total > 0 else 0
    return HiraDashboardRiskReduction(
        initialTotal=initial_total, residualTotal=residual_total, reductionPct=pct
    )


@router.get("/dashboard/top-hazards", response_model=list[HiraDashboardTopHazard])
async def dashboard_top_hazards(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[HiraDashboardTopHazard]:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    accessible = await get_accessible_plants(db, user.id)

    stmt = (
        select(HiraHazard.category, func.count(HiraEntryHazard.id).label("c"))
        .join(HiraEntryHazard, HiraEntryHazard.hazardId == HiraHazard.id)
        .join(HiraEntry, HiraEntryHazard.entryId == HiraEntry.id)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
        .where(HiraEntry.isCurrentVersion.is_(True))
        .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "FLAGGED_FOR_REVIEW"]))
        .group_by(HiraHazard.category)
        .order_by(func.count(HiraEntryHazard.id).desc())
        .limit(5)
    )
    if accessible is not None:
        if not accessible:
            return []
        stmt = stmt.where(HiraStudy.plantId.in_(accessible))
    rows = (await db.execute(stmt)).all()
    return [HiraDashboardTopHazard(category=cat, count=int(cnt)) for cat, cnt in rows]


@router.post("/cron/review-scheduler")
async def cron_review_scheduler(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Daily scheduled review job. Called from the Next.js cron route that
    Vercel cron pings. Auth via JWT mint by the cron proxy.
    """
    from datetime import timedelta

    # Allow CORPORATE_HSE / ADMIN / SYSTEM_ADMIN to run this (cron-internal users)
    check = await can(db, user.id, "HIRA.EXECUTE", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cron job requires HIRA.EXECUTE")

    now = datetime.now(timezone.utc)
    in30 = now + timedelta(days=30)
    in7 = now + timedelta(days=7)
    stats = {
        "created_T_minus_30": 0,
        "flagged_T_minus_7": 0,
        "forced_overdue": 0,
        "errors": [],
    }

    # T-30 candidates: active entries with nextReviewDue within 30 days AND no open cycle
    candidates = (
        await db.execute(
            select(HiraEntry, HiraStudy)
            .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
            .where(HiraEntry.isCurrentVersion.is_(True))
            .where(HiraEntry.status.in_(["APPROVED", "ACTIVE"]))
            .where(HiraEntry.nextReviewDue.is_not(None))
            .where(HiraEntry.nextReviewDue <= in30)
            .where(HiraEntry.nextReviewDue >= now)
            .where(
                ~HiraEntry.id.in_(
                    select(HiraReviewCycle.entryId).where(
                        HiraReviewCycle.status.in_(["SCHEDULED", "IN_PROGRESS"])
                    )
                )
            )
            .limit(500)
        )
    ).all()
    for entry, study in candidates:
        try:
            db.add(
                HiraReviewCycle(
                    entryId=entry.id,
                    scheduledFor=entry.nextReviewDue,
                    triggeredBy="SCHEDULE",
                    status="SCHEDULED",
                    assignedToId=study.teamLeaderId,
                    assignedRole="TEAM_LEADER",
                )
            )
            stats["created_T_minus_30"] += 1
        except Exception as e:
            stats["errors"].append(f"T-30 entry {entry.id}: {e}")

    # T-7 flag: entries with SCHEDULED cycle in next 7 days
    to_flag = (
        await db.execute(
            select(HiraEntry.id)
            .where(HiraEntry.isCurrentVersion.is_(True))
            .where(HiraEntry.status.in_(["APPROVED", "ACTIVE"]))
            .where(
                HiraEntry.id.in_(
                    select(HiraReviewCycle.entryId)
                    .where(HiraReviewCycle.status == "SCHEDULED")
                    .where(HiraReviewCycle.scheduledFor <= in7)
                    .where(HiraReviewCycle.scheduledFor >= now)
                )
            )
        )
    ).scalars().all()
    if to_flag:
        await db.execute(
            HiraEntry.__table__.update()
            .where(HiraEntry.id.in_(to_flag))
            .values(status="FLAGGED_FOR_REVIEW")
        )
        stats["flagged_T_minus_7"] = len(to_flag)

    # T+0 overdue
    overdue = (
        await db.execute(
            select(HiraEntry, HiraStudy)
            .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
            .where(HiraEntry.isCurrentVersion.is_(True))
            .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "FLAGGED_FOR_REVIEW"]))
            .where(HiraEntry.nextReviewDue.is_not(None))
            .where(HiraEntry.nextReviewDue < now)
            .where(
                ~HiraEntry.id.in_(
                    select(HiraReviewCycle.entryId).where(
                        HiraReviewCycle.status.in_(["SCHEDULED", "IN_PROGRESS"])
                    )
                )
            )
            .limit(500)
        )
    ).all()
    for entry, study in overdue:
        try:
            db.add(
                HiraReviewCycle(
                    entryId=entry.id,
                    scheduledFor=entry.nextReviewDue,
                    triggeredBy="SCHEDULE",
                    status="SCHEDULED",
                    assignedToId=study.teamLeaderId,
                    assignedRole="TEAM_LEADER",
                )
            )
            entry.status = "FLAGGED_FOR_REVIEW"
            stats["forced_overdue"] += 1
        except Exception as e:
            stats["errors"].append(f"Overdue entry {entry.id}: {e}")

    await db.flush()
    return {"success": True, "ranAt": now.isoformat(), "stats": stats}


@router.post("/cron/training-expiry")
async def cron_training_expiry(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Daily training-expiry HIRA flag job."""
    from datetime import timedelta

    from app.models.training import TrainingCertificate

    check = await can(db, user.id, "HIRA.EXECUTE", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cron job requires HIRA.EXECUTE")

    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)
    stats = {"entriesFlagged": 0, "cyclesCreated": 0, "errors": []}

    expired = (
        await db.execute(
            select(
                TrainingCertificate.id,
                TrainingCertificate.programId,
            ).where(
                or_(
                    (TrainingCertificate.validTo >= day_ago)
                    & (TrainingCertificate.validTo < now),
                    TrainingCertificate.status == "EXPIRED",
                )
            ).limit(500)
        )
    ).all()
    if not expired:
        return {"success": True, "ranAt": now.isoformat(), "stats": stats, "note": "No newly-expired certs"}

    expired_program_ids = {c[1] for c in expired}
    cert_by_program: dict[str, str] = {}
    for cid, pid in expired:
        cert_by_program.setdefault(pid, cid)

    candidates = (
        await db.execute(
            select(HiraEntry, HiraStudy)
            .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
            .where(HiraEntry.isCurrentVersion.is_(True))
            .where(HiraEntry.status.in_(["APPROVED", "ACTIVE"]))
            .where(HiraEntry.triggersTrainingProgramIds.is_not(None))
            .limit(2000)
        )
    ).all()

    for entry, study in candidates:
        refs = entry.triggersTrainingProgramIds or []
        hit = next((r for r in refs if r in expired_program_ids), None)
        if not hit:
            continue
        existing = (
            await db.execute(
                select(HiraReviewCycle.id)
                .where(HiraReviewCycle.entryId == entry.id)
                .where(HiraReviewCycle.status.in_(["SCHEDULED", "IN_PROGRESS"]))
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing:
            continue
        try:
            db.add(
                HiraReviewCycle(
                    entryId=entry.id,
                    scheduledFor=now + timedelta(days=14),
                    triggeredBy="MANUAL",
                    triggerReferenceId=cert_by_program.get(hit, hit),
                    status="SCHEDULED",
                    assignedToId=study.teamLeaderId,
                    assignedRole="TEAM_LEADER",
                    outcomeNotes=f"Training certificate for program {hit} expired",
                )
            )
            entry.status = "FLAGGED_FOR_REVIEW"
            stats["entriesFlagged"] += 1
            stats["cyclesCreated"] += 1
        except Exception as e:
            stats["errors"].append(f"Entry {entry.id}: {e}")

    await db.flush()
    return {"success": True, "ranAt": now.isoformat(), "stats": stats}


@router.get("/wizard/study-options")
async def study_wizard_options(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Returns the master data the new-study wizard needs in one round-trip:
    plants the caller can see, their departments + areas, all active users
    (for team picker), all active risk matrices.
    """
    from app.models.masters import Department
    from app.models.plant import Area, Plant

    check = await can(db, user.id, "HIRA.CREATE", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    accessible = await get_accessible_plants(db, user.id)

    plants_q = select(Plant.id, Plant.code, Plant.name)
    if accessible is not None:
        if not accessible:
            return {"plants": [], "departments": [], "areas": [], "users": [], "riskMatrices": []}
        plants_q = plants_q.where(Plant.id.in_(accessible))
    plant_rows = (await db.execute(plants_q.order_by(Plant.name))).all()

    plant_ids = [r[0] for r in plant_rows]
    depts = (
        await db.execute(
            select(Department.id, Department.plantId, Department.name)
            .where(Department.plantId.in_(plant_ids))
            .where(Department.active.is_(True))
            .order_by(Department.name)
        )
    ).all() if plant_ids else []
    areas = (
        await db.execute(
            select(Area.id, Area.plantId, Area.name)
            .where(Area.plantId.in_(plant_ids))
            .order_by(Area.name)
        )
    ).all() if plant_ids else []
    users_q = (
        select(User.id, User.name, User.email, User.department, User.plantId)
        .order_by(User.name)
        .limit(500)
    )
    if plant_ids:
        users_q = users_q.where(User.plantId.in_(plant_ids))
    users = (await db.execute(users_q)).all()
    matrices = (
        await db.execute(
            select(
                RiskMatrix.id,
                RiskMatrix.code,
                RiskMatrix.name,
                RiskMatrix.likelihoodLevels,
                RiskMatrix.severityLevels,
                RiskMatrix.isDefault,
                RiskMatrix.controlHierarchyEnforced,
            )
            .where(RiskMatrix.isActive.is_(True))
            .order_by(RiskMatrix.isDefault.desc(), RiskMatrix.name)
        )
    ).all()
    return {
        "plants": [
            {
                "id": pid,
                "code": code,
                "name": nm,
                "departments": [
                    {"id": d[0], "name": d[2]} for d in depts if d[1] == pid
                ],
                "areas": [{"id": a[0], "name": a[2]} for a in areas if a[1] == pid],
            }
            for pid, code, nm in plant_rows
        ],
        "departments": [{"id": d[0], "plantId": d[1], "name": d[2]} for d in depts],
        "areas": [{"id": a[0], "plantId": a[1], "name": a[2]} for a in areas],
        "users": [
            {"id": u[0], "name": u[1], "email": u[2], "department": u[3], "plantId": u[4]} for u in users
        ],
        "riskMatrices": [
            {
                "id": m[0],
                "code": m[1],
                "name": m[2],
                "likelihoodLevels": m[3],
                "severityLevels": m[4],
                "isDefault": m[5],
                "controlHierarchyEnforced": m[6],
            }
            for m in matrices
        ],
    }


@router.get("/wizard/entry-options")
async def entry_wizard_options(
    study_id: str = Query(..., alias="studyId"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Returns the form-option data the new-entry wizard needs: the parent
    study's matrix (scales + cells), the active hazard library, and the
    plant's areas.
    """
    from app.models.plant import Area

    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    check = await can(
        db,
        user.id,
        "HIRA.UPDATE",
        PermissionContext(record_id=study.id, plant_id=study.plantId),
    )
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    matrix_stmt = (
        select(RiskMatrix)
        .where(RiskMatrix.id == study.riskMatrixId)
        .options(
            selectinload(RiskMatrix.likelihoods),
            selectinload(RiskMatrix.severities),
            selectinload(RiskMatrix.cells),
        )
    )
    matrix = (await db.execute(matrix_stmt)).scalar_one_or_none()
    if matrix is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Risk matrix not found or inactive")

    hazards = (
        await db.execute(
            select(HiraHazard)
            .where(HiraHazard.isActive.is_(True))
            .order_by(HiraHazard.category, HiraHazard.name)
            .limit(300)
        )
    ).scalars().all()

    areas = (
        await db.execute(
            select(Area.id, Area.name).where(Area.plantId == study.plantId).order_by(Area.name)
        )
    ).all()

    return {
        "studyStatus": study.status,
        "matrix": RiskMatrixOut.model_validate(matrix).model_dump(),
        "hazards": [HiraHazardOut.model_validate(h).model_dump() for h in hazards],
        "areas": [{"id": a[0], "name": a[1]} for a in areas],
    }


@router.get("/dashboard/review-compliance", response_model=HiraDashboardReviewCompliance)
async def dashboard_review_compliance(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraDashboardReviewCompliance:
    from datetime import timedelta

    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    accessible = await get_accessible_plants(db, user.id)

    now = datetime.now(timezone.utc)
    in30 = now + timedelta(days=30)
    ago90 = now - timedelta(days=90)

    base = (
        select(func.count())
        .select_from(HiraReviewCycle)
        .join(HiraEntry, HiraReviewCycle.entryId == HiraEntry.id)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
    )
    if accessible is not None:
        if not accessible:
            return HiraDashboardReviewCompliance(overdue=0, dueSoon30Days=0, completedLast90Days=0)
        base = base.where(HiraStudy.plantId.in_(accessible))

    overdue = (
        await db.execute(
            base.where(HiraReviewCycle.status == "SCHEDULED").where(HiraReviewCycle.scheduledFor < now)
        )
    ).scalar_one() or 0
    due_soon = (
        await db.execute(
            base.where(HiraReviewCycle.status == "SCHEDULED")
            .where(HiraReviewCycle.scheduledFor >= now)
            .where(HiraReviewCycle.scheduledFor <= in30)
        )
    ).scalar_one() or 0
    completed_90 = (
        await db.execute(
            base.where(HiraReviewCycle.status == "COMPLETED").where(HiraReviewCycle.completedAt >= ago90)
        )
    ).scalar_one() or 0
    return HiraDashboardReviewCompliance(
        overdue=overdue, dueSoon30Days=due_soon, completedLast90Days=completed_90
    )
