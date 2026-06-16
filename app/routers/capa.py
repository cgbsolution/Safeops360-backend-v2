"""CAPA router — Phase A skeleton.

Endpoints exposed in this skeleton:
  - GET    /api/capa/source-categories       — master
  - GET    /api/capa/source-types            — master (optionally by category)
  - GET    /api/capa/sub-categories          — master
  - GET    /api/capa/verification-methods    — master
  - GET    /api/capa/sla-profiles            — master
  - GET    /api/capa                         — list (plant-scoped, source-filtered)
  - GET    /api/capa/{id}                    — full detail
  - POST   /api/capa                         — create (universal intake)

Future endpoints (Phase 3 / 4):
  - PATCH /api/capa/{id}                     — update
  - POST  /api/capa/{id}/submit-rca          — submit RCA
  - POST  /api/capa/{id}/actions             — add action
  - PATCH /api/capa/{id}/actions/{aid}       — update action (status, evidence)
  - POST  /api/capa/{id}/verify              — submit verification result
  - POST  /api/capa/{id}/close               — close
  - POST  /api/capa/{id}/recurrence-check    — complete the 90-day check
  - GET   /api/capa/dashboard/*              — KPI aggregates
  - GET   /api/capa/patterns                 — detected pattern groups
  - GET   /api/capa/{id}/export.xlsx         — ISO 9001 register export
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.capa import (
    Capa,
    CapaAction,
    CapaPatternGroup,
    CapaRootCause,
    CapaSlaProfile,
    CapaSourceCategory,
    CapaSourceType,
    CapaSubCategory,
    CapaVerificationMethod,
)
from app.models.plant import Plant
from app.models.user import User
from app.schemas.capa import (
    CapaActionCreateRequest,
    CapaActionUpdateRequest,
    CapaCloseRequest,
    CapaCreate,
    CapaListItem,
    CapaListResponse,
    CapaOut,
    CapaPatternActionRequest,
    CapaRecurrenceCheckRequest,
    CapaSlaProfileOut,
    CapaSourceCategoryOut,
    CapaSourceTypeOut,
    CapaSubCategoryOut,
    CapaSubmitRcaRequest,
    CapaUpdate,
    CapaVerificationMethodOut,
    CapaVerifyRequest,
)
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
)

router = APIRouter(prefix="/api/capa", tags=["capa"])


# ─────────────────────────────────────────────────────────────────────
# Master data
# ─────────────────────────────────────────────────────────────────────


@router.get("/source-categories", response_model=list[CapaSourceCategoryOut])
async def list_source_categories(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CapaSourceCategoryOut]:
    check = await can(db, user.id, "CAPA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    rows = (
        await db.execute(
            select(CapaSourceCategory)
            .where(CapaSourceCategory.isActive.is_(True))
            .order_by(CapaSourceCategory.sortOrder)
        )
    ).scalars().all()
    return [CapaSourceCategoryOut.model_validate(r) for r in rows]


@router.get("/source-types", response_model=list[CapaSourceTypeOut])
async def list_source_types(
    category_code: str | None = Query(None, alias="categoryCode"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CapaSourceTypeOut]:
    check = await can(db, user.id, "CAPA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    stmt = select(CapaSourceType).where(CapaSourceType.isActive.is_(True))
    if category_code:
        cat = (
            await db.execute(select(CapaSourceCategory.id).where(CapaSourceCategory.code == category_code))
        ).scalar_one_or_none()
        if cat is None:
            return []
        stmt = stmt.where(CapaSourceType.categoryId == cat)
    stmt = stmt.order_by(CapaSourceType.sortOrder)
    rows = (await db.execute(stmt)).scalars().all()
    return [CapaSourceTypeOut.model_validate(r) for r in rows]


@router.get("/sub-categories", response_model=list[CapaSubCategoryOut])
async def list_sub_categories(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CapaSubCategoryOut]:
    check = await can(db, user.id, "CAPA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    rows = (
        await db.execute(
            select(CapaSubCategory)
            .where(CapaSubCategory.isActive.is_(True))
            .order_by(CapaSubCategory.sortOrder)
        )
    ).scalars().all()
    return [CapaSubCategoryOut.model_validate(r) for r in rows]


@router.get("/verification-methods", response_model=list[CapaVerificationMethodOut])
async def list_verification_methods(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CapaVerificationMethodOut]:
    check = await can(db, user.id, "CAPA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    rows = (
        await db.execute(
            select(CapaVerificationMethod)
            .where(CapaVerificationMethod.isActive.is_(True))
            .order_by(CapaVerificationMethod.sortOrder)
        )
    ).scalars().all()
    return [CapaVerificationMethodOut.model_validate(r) for r in rows]


@router.get("/sla-profiles", response_model=list[CapaSlaProfileOut])
async def list_sla_profiles(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CapaSlaProfileOut]:
    check = await can(db, user.id, "CAPA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    rows = (
        await db.execute(
            select(CapaSlaProfile)
            .where(CapaSlaProfile.isActive.is_(True))
        )
    ).scalars().all()
    return [CapaSlaProfileOut.model_validate(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────
# CAPA list + detail
# ─────────────────────────────────────────────────────────────────────


@router.get("", response_model=CapaListResponse)
async def list_capas(
    state: str | None = None,
    source_type: str | None = Query(None, alias="sourceType"),
    source_category: str | None = Query(None, alias="sourceCategory"),
    severity: str | None = None,
    plant_id: str | None = Query(None, alias="plantId"),
    primary_owner: str | None = Query(None, alias="primaryOwner"),
    q: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CapaListResponse:
    check = await can(db, user.id, "CAPA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    accessible_plants = await get_accessible_plants(db, user.id)

    stmt = (
        select(Capa)
        .options(
            selectinload(Capa.actions),
        )
    )
    if accessible_plants is None:
        pass
    elif len(accessible_plants) == 0:
        return CapaListResponse(items=[], total=0)
    else:
        stmt = stmt.where(Capa.plantId.in_(accessible_plants))

    if state:
        stmt = stmt.where(Capa.state == state)
    if source_type:
        stmt = stmt.where(Capa.sourceTypeCode == source_type)
    if source_category:
        cat = (
            await db.execute(select(CapaSourceCategory.id).where(CapaSourceCategory.code == source_category))
        ).scalar_one_or_none()
        if cat is None:
            return CapaListResponse(items=[], total=0)
        stmt = stmt.where(Capa.sourceCategoryId == cat)
    if severity:
        stmt = stmt.where(Capa.severity == severity)
    if plant_id:
        stmt = stmt.where(Capa.plantId == plant_id)
    if primary_owner:
        stmt = stmt.where(Capa.primaryOwnerUserId == primary_owner)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                Capa.title.ilike(like),
                Capa.capaNumber.ilike(like),
                Capa.problemDescription.ilike(like),
                Capa.aliasNumber.ilike(like),
            )
        )

    stmt = stmt.order_by(
        Capa.closureTargetDate.asc().nullslast(),
        Capa.severity.desc(),
        Capa.createdAt.desc(),
    ).limit(500)
    rows = (await db.execute(stmt)).scalars().all()

    # Name + category-code lookups
    owner_ids = list({r.primaryOwnerUserId for r in rows})
    owner_names: dict[str, str] = {}
    if owner_ids:
        ones = (await db.execute(select(User.id, User.name).where(User.id.in_(owner_ids)))).all()
        owner_names = {uid: nm for uid, nm in ones}

    category_codes: dict[str, str] = {}
    if rows:
        cat_rows = (
            await db.execute(
                select(CapaSourceCategory.id, CapaSourceCategory.code).where(
                    CapaSourceCategory.id.in_({r.sourceCategoryId for r in rows})
                )
            )
        ).all()
        category_codes = {cid: code for cid, code in cat_rows}

    now = datetime.now(timezone.utc)
    items = []
    for r in rows:
        d = CapaListItem.model_validate(r).model_dump()
        d["primaryOwnerName"] = owner_names.get(r.primaryOwnerUserId)
        d["sourceCategoryCode"] = category_codes.get(r.sourceCategoryId)
        d["actionCount"] = len(r.actions)
        if r.createdAt:
            created = r.createdAt.replace(tzinfo=timezone.utc) if r.createdAt.tzinfo is None else r.createdAt
            d["daysOpen"] = max(0, (now - created).days)
        if r.closureTargetDate and r.state not in ("CLOSED", "CLOSED_RECURRED", "CANCELLED"):
            ctd = r.closureTargetDate.replace(tzinfo=timezone.utc) if r.closureTargetDate.tzinfo is None else r.closureTargetDate
            d["daysOverdue"] = max(0, (now - ctd).days)
        items.append(CapaListItem(**d))

    # Aggregate counts across the user's scope (not just filtered slice)
    scope_q = select(Capa).where(Capa.plantId.in_(accessible_plants)) if accessible_plants else select(Capa)
    cat_counts_rows = (
        await db.execute(
            select(CapaSourceCategory.code, func.count(Capa.id))
            .select_from(Capa)
            .join(CapaSourceCategory, Capa.sourceCategoryId == CapaSourceCategory.id)
            .group_by(CapaSourceCategory.code)
        )
    ).all()
    state_counts_rows = (
        await db.execute(select(Capa.state, func.count(Capa.id)).group_by(Capa.state))
    ).all()
    sev_counts_rows = (
        await db.execute(select(Capa.severity, func.count(Capa.id)).group_by(Capa.severity))
    ).all()

    return CapaListResponse(
        items=items,
        total=len(items),
        sourceCategoryCounts={code: int(c) for code, c in cat_counts_rows},
        stateCounts={s: int(c) for s, c in state_counts_rows},
        severityCounts={s: int(c) for s, c in sev_counts_rows},
    )


@router.get("/{capa_id}", response_model=CapaOut)
async def get_capa(
    capa_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CapaOut:
    stmt = (
        select(Capa)
        .where(Capa.id == capa_id)
        .options(
            selectinload(Capa.actions),
            selectinload(Capa.rootCauses),
            selectinload(Capa.contributors),
            selectinload(Capa.attachments),
            selectinload(Capa.comments),
        )
    )
    capa = (await db.execute(stmt)).scalar_one_or_none()
    if capa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CAPA not found")

    check = await can(
        db,
        user.id,
        "CAPA.READ",
        PermissionContext(
            record_id=capa.id,
            plant_id=capa.plantId,
            record={
                "primaryOwnerUserId": capa.primaryOwnerUserId,
                "raisedByUserId": capa.raisedByUserId,
            },
        ),
    )
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    return CapaOut.model_validate(capa)


@router.post("", response_model=CapaOut, status_code=status.HTTP_201_CREATED)
async def create_capa(
    payload: CapaCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CapaOut:
    await require_permission_with_context(
        "CAPA.CREATE", user, db, plant_id=payload.plantId
    )

    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")

    source_type = (
        await db.execute(
            select(CapaSourceType)
            .where(CapaSourceType.code == payload.sourceTypeCode)
            .where(CapaSourceType.isActive.is_(True))
        )
    ).scalar_one_or_none()
    if source_type is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid sourceTypeCode: {payload.sourceTypeCode}")

    category = await db.get(CapaSourceCategory, source_type.categoryId)
    if category is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Source type has no category")

    sub_category = None
    if payload.subCategoryCode:
        sub_category = (
            await db.execute(select(CapaSubCategory).where(CapaSubCategory.code == payload.subCategoryCode))
        ).scalar_one_or_none()

    # Capa number — per D4: CAPA-{prefix}-YYYY-PLT-NNN
    year = datetime.now(timezone.utc).year
    count = (
        await db.execute(
            select(func.count(Capa.id))
            .where(Capa.plantId == payload.plantId)
            .where(Capa.sourceCategoryId == category.id)
        )
    ).scalar_one() or 0
    capa_number = f"CAPA-{category.prefix}-{year}-{plant.code}-{(count + 1):03d}"

    # Apply SLA profile (severity-specific if available, else source default, else global)
    sla = (
        await db.execute(
            select(CapaSlaProfile)
            .where(CapaSlaProfile.sourceTypeCode == payload.sourceTypeCode)
            .where(CapaSlaProfile.severity == payload.severity)
            .where(CapaSlaProfile.isActive.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none()
    if sla is None:
        sla = (
            await db.execute(
                select(CapaSlaProfile)
                .where(CapaSlaProfile.sourceTypeCode == payload.sourceTypeCode)
                .where(CapaSlaProfile.severity.is_(None))
                .where(CapaSlaProfile.isActive.is_(True))
                .limit(1)
            )
        ).scalar_one_or_none()
    if sla is None:
        sla = (
            await db.execute(
                select(CapaSlaProfile)
                .where(CapaSlaProfile.code == "GLOBAL_DEFAULT")
                .limit(1)
            )
        ).scalar_one_or_none()

    from datetime import timedelta

    now = datetime.now(timezone.utc)
    rca_due = now + timedelta(days=sla.rcaDueDays) if sla else None
    actions_planned_due = now + timedelta(days=sla.actionsPlannedDueDays) if sla else None
    closure_target = now + timedelta(days=sla.closureTargetDays) if sla else None

    capa = Capa(
        capaNumber=capa_number,
        title=payload.title,
        plantId=payload.plantId,
        sourceCategoryId=category.id,
        sourceTypeId=source_type.id,
        sourceTypeCode=source_type.code,
        sourceReferenceId=payload.sourceReferenceId,
        sourceReferenceUrl=payload.sourceReferenceUrl,
        sourceReferenceSummary=payload.sourceReferenceSummary,
        sourceMetadata=payload.sourceMetadata,
        problemDescription=payload.problemDescription,
        problemImpact=payload.problemImpact,
        detectionMethod=payload.detectionMethod,
        detectedAt=payload.detectedAt,
        detectedByUserId=user.id,
        affectedAreas=payload.affectedAreas,
        affectedDepartments=payload.affectedDepartments,
        primaryCategory=payload.primaryCategory,
        subCategoryId=sub_category.id if sub_category else None,
        actionType=payload.actionType,
        severity=payload.severity,
        priority=payload.priority,
        state="SUBMITTED",
        stateChangedAt=now,
        stateChangedByUserId=user.id,
        rcaDueDate=rca_due,
        correctiveActionDueDate=actions_planned_due,
        preventiveActionDueDate=actions_planned_due,
        closureTargetDate=closure_target,
        raisedByUserId=user.id,
        primaryOwnerUserId=payload.primaryOwnerUserId,
        createdByUserId=user.id,
    )
    db.add(capa)
    await db.flush()
    await db.refresh(capa)

    # Workflow auto-init — spawn WorkflowInstance using the severity-matched
    # definition (CAPA/LOW, /MODERATE, /HIGH, /CRITICAL). Best-effort:
    # SAVEPOINT keeps a workflow failure from rolling back the CAPA insert.
    import sys
    import traceback

    try:
        async with db.begin_nested():
            from app.services import workflow_engine

            await workflow_engine.initiate(
                db,
                module="CAPA",
                record_id=capa.id,
                record_number=capa.capaNumber,
                record_title=capa.title[:120],
                record_data={
                    "type": payload.severity,  # the engine reads recordType from here
                    "severity": payload.severity,
                    "priority": payload.priority,
                    "sourceTypeCode": source_type.code,
                    "plantId": payload.plantId,
                    "primaryOwnerUserId": payload.primaryOwnerUserId,
                },
                initiator_id=user.id,
                plant_id=payload.plantId,
            )
    except Exception as e:  # noqa: BLE001
        print(f"CAPA workflow init failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    refresh_stmt = (
        select(Capa)
        .where(Capa.id == capa.id)
        .options(
            selectinload(Capa.actions),
            selectinload(Capa.rootCauses),
            selectinload(Capa.contributors),
            selectinload(Capa.attachments),
            selectinload(Capa.comments),
        )
    )
    capa = (await db.execute(refresh_stmt)).scalar_one()
    return CapaOut.model_validate(capa)


# ─────────────────────────────────────────────────────────────────────
# Dashboard aggregates
# ─────────────────────────────────────────────────────────────────────


@router.get("/dashboard/volume-by-source")
async def dashboard_volume_by_source(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Returns CAPA count per source category (for the donut widget)."""
    check = await can(db, user.id, "CAPA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    accessible = await get_accessible_plants(db, user.id)

    stmt = (
        select(CapaSourceCategory.code, CapaSourceCategory.name, func.count(Capa.id))
        .select_from(Capa)
        .join(CapaSourceCategory, Capa.sourceCategoryId == CapaSourceCategory.id)
        .group_by(CapaSourceCategory.code, CapaSourceCategory.name)
    )
    if accessible is not None:
        if not accessible:
            return []
        stmt = stmt.where(Capa.plantId.in_(accessible))
    rows = (await db.execute(stmt)).all()
    return [{"code": c, "name": n, "count": int(cnt)} for c, n, cnt in rows]


@router.get("/dashboard/state-distribution")
async def dashboard_state_distribution(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """CAPA count by workflow state."""
    check = await can(db, user.id, "CAPA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    accessible = await get_accessible_plants(db, user.id)

    stmt = select(Capa.state, func.count(Capa.id)).group_by(Capa.state)
    if accessible is not None:
        if not accessible:
            return []
        stmt = stmt.where(Capa.plantId.in_(accessible))
    rows = (await db.execute(stmt)).all()
    return [{"state": s, "count": int(c)} for s, c in rows]


@router.get("/dashboard/overdue")
async def dashboard_overdue(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Overdue CAPA count + breakdown by severity."""
    check = await can(db, user.id, "CAPA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    accessible = await get_accessible_plants(db, user.id)

    now = datetime.now(timezone.utc)
    base = (
        select(func.count())
        .select_from(Capa)
        .where(Capa.closureTargetDate < now)
        .where(Capa.state.notin_(["CLOSED", "CLOSED_RECURRED", "CANCELLED"]))
    )
    if accessible is not None:
        if not accessible:
            return {"total": 0, "bySeverity": {}}
        base = base.where(Capa.plantId.in_(accessible))

    total = (await db.execute(base)).scalar_one() or 0

    by_sev_q = (
        select(Capa.severity, func.count(Capa.id))
        .where(Capa.closureTargetDate < now)
        .where(Capa.state.notin_(["CLOSED", "CLOSED_RECURRED", "CANCELLED"]))
        .group_by(Capa.severity)
    )
    if accessible is not None:
        by_sev_q = by_sev_q.where(Capa.plantId.in_(accessible))
    sev_rows = (await db.execute(by_sev_q)).all()
    return {"total": int(total), "bySeverity": {s: int(c) for s, c in sev_rows}}


@router.get("/dashboard/effectiveness")
async def dashboard_effectiveness(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """% of verified CAPAs that were EFFECTIVE over last 90 days."""
    from datetime import timedelta

    check = await can(db, user.id, "CAPA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    accessible = await get_accessible_plants(db, user.id)

    ago90 = datetime.now(timezone.utc) - timedelta(days=90)
    base = (
        select(Capa.verificationResult, func.count(Capa.id))
        .where(Capa.verificationCompletedAt >= ago90)
        .where(Capa.verificationResult.is_not(None))
        .group_by(Capa.verificationResult)
    )
    if accessible is not None:
        if not accessible:
            return {"effective": 0, "total": 0, "percentEffective": 0}
        base = base.where(Capa.plantId.in_(accessible))
    rows = (await db.execute(base)).all()
    by_result = {r: int(c) for r, c in rows}
    effective = by_result.get("EFFECTIVE", 0)
    total = sum(by_result.values())
    pct = round((effective / total) * 100) if total else 0
    return {"effective": effective, "total": total, "percentEffective": pct, "breakdown": by_result}


@router.get("/dashboard/top-root-causes")
async def dashboard_top_root_causes(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Top root cause categories across all closed CAPAs."""
    from app.models.capa import CapaRootCause

    check = await can(db, user.id, "CAPA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    accessible = await get_accessible_plants(db, user.id)

    stmt = (
        select(CapaRootCause.category, func.count(CapaRootCause.id))
        .select_from(CapaRootCause)
        .join(Capa, CapaRootCause.capaId == Capa.id)
        .group_by(CapaRootCause.category)
        .order_by(func.count(CapaRootCause.id).desc())
        .limit(8)
    )
    if accessible is not None:
        if not accessible:
            return []
        stmt = stmt.where(Capa.plantId.in_(accessible))
    rows = (await db.execute(stmt)).all()
    return [{"category": cat, "count": int(c)} for cat, c in rows]


# ─────────────────────────────────────────────────────────────────────
# Pattern detection — deterministic v1
# ─────────────────────────────────────────────────────────────────────


@router.get("/patterns")
async def list_patterns(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Deterministic pattern detection — surfaces CAPAs likely sharing root
    cause. v1 rule: 3+ CAPAs in the same plant with the same primaryCategory
    AND the same sourceTypeCode (within the last 180 days).
    Returns groups for reviewer confirmation.
    """
    from datetime import timedelta
    from app.models.capa import CapaPatternGroup

    check = await can(db, user.id, "CAPA.PATTERN_LINK", PermissionContext())
    if not check.allowed:
        # Lower bar: anyone with CAPA.READ can see, just not link
        check = await can(db, user.id, "CAPA.READ", PermissionContext())
        if not check.allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    accessible = await get_accessible_plants(db, user.id)
    ago180 = datetime.now(timezone.utc) - timedelta(days=180)

    # Already-confirmed groups
    confirmed_q = select(CapaPatternGroup).where(CapaPatternGroup.status == "CONFIRMED")
    if accessible is not None:
        if not accessible:
            return []
        confirmed_q = confirmed_q.where(CapaPatternGroup.plantId.in_(accessible))
    confirmed_rows = (await db.execute(confirmed_q)).scalars().all()

    # Deterministic candidate detection
    grp_q = (
        select(
            Capa.plantId,
            Capa.primaryCategory,
            Capa.sourceTypeCode,
            func.count(Capa.id).label("c"),
            func.array_agg(Capa.id).label("ids"),
        )
        .where(Capa.createdAt >= ago180)
        .group_by(Capa.plantId, Capa.primaryCategory, Capa.sourceTypeCode)
        .having(func.count(Capa.id) >= 3)
    )
    if accessible is not None:
        grp_q = grp_q.where(Capa.plantId.in_(accessible))
    grp_rows = (await db.execute(grp_q)).all()

    candidates = [
        {
            "type": "candidate",
            "plantId": pid,
            "primaryCategory": cat,
            "sourceTypeCode": stc,
            "capaCount": int(cnt),
            "capaIds": list(ids),
            "rationale": f"{int(cnt)} CAPAs at the same plant share primary category '{cat}' and source type '{stc}' in last 180 days.",
        }
        for pid, cat, stc, cnt, ids in grp_rows
    ]

    confirmed = [
        {
            "type": "confirmed",
            "id": g.id,
            "plantId": g.plantId,
            "status": g.status,
            "rationale": g.rationale,
            "capaIds": g.capaIds,
            "reviewedAt": g.reviewedAt.isoformat() if g.reviewedAt else None,
        }
        for g in confirmed_rows
    ]
    return confirmed + candidates


# ─────────────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────────────


@router.get("/export.csv")
async def export_csv(
    plant_id: str | None = Query(None, alias="plantId"),
    source_category: str | None = Query(None, alias="sourceCategory"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import Response

    check = await can(db, user.id, "CAPA.EXPORT", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    accessible = await get_accessible_plants(db, user.id)

    stmt = (
        select(Capa)
        .options(
            selectinload(Capa.actions),
            selectinload(Capa.rootCauses),
        )
        .order_by(Capa.createdAt.desc())
    )
    if accessible is not None:
        if not accessible:
            return Response(content="No data", media_type="text/csv")
        stmt = stmt.where(Capa.plantId.in_(accessible))
    if plant_id:
        stmt = stmt.where(Capa.plantId == plant_id)
    if source_category:
        cat = (
            await db.execute(select(CapaSourceCategory.id).where(CapaSourceCategory.code == source_category))
        ).scalar_one_or_none()
        if cat:
            stmt = stmt.where(Capa.sourceCategoryId == cat)

    rows = (await db.execute(stmt)).scalars().all()

    def esc(s):
        if s is None:
            return ""
        s = str(s)
        if any(c in s for c in [",", '"', "\n", "\r"]):
            return '"' + s.replace('"', '""') + '"'
        return s

    out = ["﻿CAPA Number,Alias,Title,Source Type,Severity,Priority,State,Primary Owner,Detected At,Closure Target,Action Count,Root Cause Count,Verification Result"]
    for r in rows:
        out.append(
            ",".join(
                esc(x)
                for x in [
                    r.capaNumber,
                    r.aliasNumber or "",
                    r.title,
                    r.sourceTypeCode,
                    r.severity,
                    r.priority,
                    r.state,
                    r.primaryOwnerUserId,
                    r.detectedAt.isoformat() if r.detectedAt else "",
                    r.closureTargetDate.isoformat() if r.closureTargetDate else "",
                    str(len(r.actions)),
                    str(len(r.rootCauses)),
                    r.verificationResult or "",
                ]
            )
        )
    csv = "\r\n".join(out)
    return Response(
        content=csv,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="capa-register.csv"'},
    )


# ═════════════════════════════════════════════════════════════════════
# Lifecycle write endpoints
# ═════════════════════════════════════════════════════════════════════


async def _load_capa_for_write(db: AsyncSession, capa_id: str) -> Capa:
    capa = await db.get(Capa, capa_id)
    if capa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CAPA not found")
    return capa


@router.patch("/{capa_id}", response_model=CapaOut)
async def update_capa(
    capa_id: str,
    payload: CapaUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CapaOut:
    capa = await _load_capa_for_write(db, capa_id)
    await require_permission_with_context(
        "CAPA.UPDATE", user, db, plant_id=capa.plantId, record_id=capa.id
    )

    data = payload.model_dump(exclude_unset=True)
    sub_code = data.pop("subCategoryCode", None)
    if sub_code:
        sub = (
            await db.execute(select(CapaSubCategory).where(CapaSubCategory.code == sub_code))
        ).scalar_one_or_none()
        if sub:
            capa.subCategoryId = sub.id

    for k, v in data.items():
        setattr(capa, k, v)
    capa.updatedByUserId = user.id
    if data.get("state") and data["state"] != capa.state:
        capa.stateChangedAt = datetime.now(timezone.utc)
        capa.stateChangedByUserId = user.id

    await db.flush()
    refresh_stmt = (
        select(Capa)
        .where(Capa.id == capa.id)
        .options(
            selectinload(Capa.actions),
            selectinload(Capa.rootCauses),
            selectinload(Capa.contributors),
            selectinload(Capa.attachments),
            selectinload(Capa.comments),
        )
    )
    capa = (await db.execute(refresh_stmt)).scalar_one()
    return CapaOut.model_validate(capa)


@router.post("/{capa_id}/submit-rca", response_model=CapaOut)
async def submit_rca(
    capa_id: str,
    payload: CapaSubmitRcaRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CapaOut:
    capa = await _load_capa_for_write(db, capa_id)
    await require_permission_with_context(
        "CAPA.UPDATE", user, db, plant_id=capa.plantId, record_id=capa.id
    )

    capa.rcaMethodology = payload.rcaMethodology
    capa.rcaMethodologyRationale = payload.rcaMethodologyRationale
    capa.rcaSummary = payload.rcaSummary
    capa.contributingFactors = payload.contributingFactors
    capa.rcaCompleted = payload.rcaMethodology != "NONE_REQUIRED"
    capa.rcaCompletedAt = datetime.now(timezone.utc) if capa.rcaCompleted else None
    capa.rcaCompletedByUserId = user.id if capa.rcaCompleted else None

    # Wipe and recreate root causes
    existing_rcs = (
        await db.execute(select(CapaRootCause).where(CapaRootCause.capaId == capa.id))
    ).scalars().all()
    for rc in existing_rcs:
        await db.delete(rc)
    for idx, rc in enumerate(payload.rootCauses):
        db.add(
            CapaRootCause(
                capaId=capa.id,
                description=rc.get("description", ""),
                category=rc.get("category", "PROCESS"),
                confidence=rc.get("confidence", "MEDIUM"),
                sortOrder=idx,
            )
        )

    # Advance state to ACTIONS_PLANNED
    if capa.state in ("SUBMITTED", "UNDER_RCA", "DRAFT"):
        capa.state = "ACTIONS_PLANNED"
        capa.stateChangedAt = datetime.now(timezone.utc)
        capa.stateChangedByUserId = user.id

    capa.updatedByUserId = user.id
    await db.flush()
    refresh = (
        select(Capa)
        .where(Capa.id == capa.id)
        .options(
            selectinload(Capa.actions),
            selectinload(Capa.rootCauses),
            selectinload(Capa.contributors),
            selectinload(Capa.attachments),
            selectinload(Capa.comments),
        )
    )
    capa = (await db.execute(refresh)).scalar_one()
    return CapaOut.model_validate(capa)


@router.post("/{capa_id}/actions", response_model=CapaOut, status_code=status.HTTP_201_CREATED)
async def add_action(
    capa_id: str,
    payload: CapaActionCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CapaOut:
    capa = await _load_capa_for_write(db, capa_id)
    await require_permission_with_context(
        "CAPA.UPDATE", user, db, plant_id=capa.plantId, record_id=capa.id
    )
    if payload.actionType not in ("IMMEDIATE_CONTAINMENT", "CORRECTIVE", "PREVENTIVE"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid actionType")

    # Sequence the new action after existing same-type actions
    last_sort = (
        await db.execute(
            select(func.max(CapaAction.sortOrder))
            .where(CapaAction.capaId == capa.id)
            .where(CapaAction.actionType == payload.actionType)
        )
    ).scalar_one() or 0

    db.add(
        CapaAction(
            capaId=capa.id,
            actionType=payload.actionType,
            description=payload.description,
            rationale=payload.rationale,
            ownerUserId=payload.ownerUserId,
            ownerRole=payload.ownerRole,
            dueDate=payload.dueDate,
            costEstimate=payload.costEstimate,
            sortOrder=last_sort + 1,
            status="PROPOSED",
        )
    )
    capa.updatedByUserId = user.id
    await db.flush()

    refresh = (
        select(Capa)
        .where(Capa.id == capa.id)
        .options(
            selectinload(Capa.actions),
            selectinload(Capa.rootCauses),
            selectinload(Capa.contributors),
            selectinload(Capa.attachments),
            selectinload(Capa.comments),
        )
    )
    capa = (await db.execute(refresh)).scalar_one()
    return CapaOut.model_validate(capa)


@router.patch("/{capa_id}/actions/{action_id}", response_model=CapaOut)
async def update_action(
    capa_id: str,
    action_id: str,
    payload: CapaActionUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CapaOut:
    capa = await _load_capa_for_write(db, capa_id)
    await require_permission_with_context(
        "CAPA.UPDATE", user, db, plant_id=capa.plantId, record_id=capa.id
    )
    action = await db.get(CapaAction, action_id)
    if action is None or action.capaId != capa_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")

    data = payload.model_dump(exclude_unset=True)
    # If status flipping to COMPLETED, set completedAt automatically
    if data.get("status") == "COMPLETED" and not data.get("completedAt"):
        data["completedAt"] = datetime.now(timezone.utc)
    if data.get("status") == "IN_PROGRESS" and not data.get("startedAt") and action.startedAt is None:
        data["startedAt"] = datetime.now(timezone.utc)
    for k, v in data.items():
        setattr(action, k, v)

    capa.updatedByUserId = user.id

    # If all actions completed AND CAPA is ACTIONS_IN_PROGRESS, advance to PENDING_VERIFICATION
    open_actions = (
        await db.execute(
            select(func.count())
            .select_from(CapaAction)
            .where(CapaAction.capaId == capa_id)
            .where(CapaAction.status != "COMPLETED")
            .where(CapaAction.status != "CANCELLED")
        )
    ).scalar_one() or 0
    if open_actions == 0 and capa.state == "ACTIONS_IN_PROGRESS":
        capa.state = "PENDING_VERIFICATION"
        capa.stateChangedAt = datetime.now(timezone.utc)
        capa.stateChangedByUserId = user.id

    await db.flush()
    refresh = (
        select(Capa)
        .where(Capa.id == capa.id)
        .options(
            selectinload(Capa.actions),
            selectinload(Capa.rootCauses),
            selectinload(Capa.contributors),
            selectinload(Capa.attachments),
            selectinload(Capa.comments),
        )
    )
    capa = (await db.execute(refresh)).scalar_one()
    return CapaOut.model_validate(capa)


@router.post("/{capa_id}/verify", response_model=CapaOut)
async def verify_capa(
    capa_id: str,
    payload: CapaVerifyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CapaOut:
    capa = await _load_capa_for_write(db, capa_id)
    await require_permission_with_context(
        "CAPA.VERIFY", user, db, plant_id=capa.plantId, record_id=capa.id
    )

    valid = {"EFFECTIVE", "PARTIALLY_EFFECTIVE", "INEFFECTIVE", "INCONCLUSIVE"}
    if payload.verificationResult not in valid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid verificationResult")

    if payload.verificationMethodCode:
        method = (
            await db.execute(
                select(CapaVerificationMethod).where(CapaVerificationMethod.code == payload.verificationMethodCode)
            )
        ).scalar_one_or_none()
        if method:
            capa.verificationMethodId = method.id

    capa.verificationSuccessCriteria = payload.verificationSuccessCriteria
    capa.verificationResult = payload.verificationResult
    capa.verificationEvidence = payload.verificationEvidence
    capa.verificationCompletedAt = datetime.now(timezone.utc)
    capa.verificationCompletedByUserId = user.id
    if payload.measurementPeriodDays is not None:
        capa.measurementPeriodDays = payload.measurementPeriodDays

    # State transition: if EFFECTIVE → VERIFIED; if INEFFECTIVE → ACTIONS_PLANNED (loop back); else → VERIFIED
    if payload.verificationResult == "INEFFECTIVE":
        capa.state = "ACTIONS_PLANNED"
    else:
        capa.state = "VERIFIED"
    capa.stateChangedAt = datetime.now(timezone.utc)
    capa.stateChangedByUserId = user.id
    capa.updatedByUserId = user.id

    await db.flush()
    refresh = (
        select(Capa)
        .where(Capa.id == capa.id)
        .options(
            selectinload(Capa.actions),
            selectinload(Capa.rootCauses),
            selectinload(Capa.contributors),
            selectinload(Capa.attachments),
            selectinload(Capa.comments),
        )
    )
    capa = (await db.execute(refresh)).scalar_one()
    return CapaOut.model_validate(capa)


@router.post("/{capa_id}/close", response_model=CapaOut)
async def close_capa(
    capa_id: str,
    payload: CapaCloseRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CapaOut:
    from datetime import timedelta

    capa = await _load_capa_for_write(db, capa_id)
    await require_permission_with_context(
        "CAPA.CLOSE", user, db, plant_id=capa.plantId, record_id=capa.id
    )

    if capa.state != "VERIFIED":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot close CAPA in state {capa.state}. Verification must complete first.",
        )

    now = datetime.now(timezone.utc)
    capa.state = "CLOSED"
    capa.stateChangedAt = now
    capa.stateChangedByUserId = user.id
    capa.closedAt = now
    capa.closedByUserId = user.id
    capa.updatedByUserId = user.id

    if payload.finalCost is not None:
        capa.actualCost = payload.finalCost
        if payload.finalCostCurrency:
            capa.actualCostCurrency = payload.finalCostCurrency

    # Schedule recurrence check using SLA profile (severity-aware)
    sla = (
        await db.execute(
            select(CapaSlaProfile)
            .where(CapaSlaProfile.sourceTypeCode == capa.sourceTypeCode)
            .where(or_(CapaSlaProfile.severity == capa.severity, CapaSlaProfile.severity.is_(None)))
            .where(CapaSlaProfile.isActive.is_(True))
            .order_by(CapaSlaProfile.severity.is_(None))  # severity-specific first
            .limit(1)
        )
    ).scalar_one_or_none()
    recurrence_days = sla.recurrenceCheckDays if sla else 90
    capa.recurrenceCheckDueDate = now + timedelta(days=recurrence_days)

    await db.flush()
    refresh = (
        select(Capa)
        .where(Capa.id == capa.id)
        .options(
            selectinload(Capa.actions),
            selectinload(Capa.rootCauses),
            selectinload(Capa.contributors),
            selectinload(Capa.attachments),
            selectinload(Capa.comments),
        )
    )
    capa = (await db.execute(refresh)).scalar_one()
    return CapaOut.model_validate(capa)


@router.post("/{capa_id}/recurrence-check", response_model=CapaOut)
async def recurrence_check(
    capa_id: str,
    payload: CapaRecurrenceCheckRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CapaOut:
    capa = await _load_capa_for_write(db, capa_id)
    await require_permission_with_context(
        "CAPA.RECURRENCE_CHECK", user, db, plant_id=capa.plantId, record_id=capa.id
    )

    if capa.state not in ("CLOSED", "CLOSED_RECURRED"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Recurrence check is only valid on closed CAPAs (current state: {capa.state}).",
        )

    capa.recurrenceCheckCompletedAt = datetime.now(timezone.utc)
    capa.recurrenceDetected = payload.recurrenceDetected
    capa.updatedByUserId = user.id

    if payload.recurrenceDetected and capa.state == "CLOSED":
        capa.state = "CLOSED_RECURRED"
        capa.stateChangedAt = datetime.now(timezone.utc)
        capa.stateChangedByUserId = user.id

    await db.flush()
    refresh = (
        select(Capa)
        .where(Capa.id == capa.id)
        .options(
            selectinload(Capa.actions),
            selectinload(Capa.rootCauses),
            selectinload(Capa.contributors),
            selectinload(Capa.attachments),
            selectinload(Capa.comments),
        )
    )
    capa = (await db.execute(refresh)).scalar_one()
    return CapaOut.model_validate(capa)


@router.post("/patterns/action")
async def pattern_action(
    payload: CapaPatternActionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    check = await can(db, user.id, "CAPA.PATTERN_LINK", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    if payload.action not in ("CONFIRM", "DISMISS"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "action must be CONFIRM or DISMISS")

    group = CapaPatternGroup(
        plantId=payload.plantId,
        status="CONFIRMED" if payload.action == "CONFIRM" else "DISMISSED",
        rationale=payload.rationale
        or f"{payload.primaryCategory} / {payload.sourceTypeCode} ({len(payload.capaIds)} CAPAs)",
        capaIds=payload.capaIds,
        reviewedByUserId=user.id,
        reviewedAt=datetime.now(timezone.utc),
    )
    db.add(group)
    await db.flush()
    return {"id": group.id, "status": group.status}
