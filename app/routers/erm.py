"""Enterprise Risk Management (ERM) router.

All endpoints are tenant(=plant-set)-scoped and RBAC-enforced via the shared
`can()` permission service. Business logic (scoring, rollup, escalation,
snapshots) lives in app/services/erm.py.

Permission codes (seeded in seed-rbac.ts):
  ERM.READ ERM.CREATE ERM.UPDATE ERM.DELETE ERM.APPROVE(validate) ERM.CLOSE
  ERM.EXPORT ERM.ASSESS ERM.TREAT ERM.ACCEPT ERM.REVIEW ERM.LINK
  ERM.BOARD_PACK ERM.TAXONOMY_ADMIN ERM.MATRIX_ADMIN ERM.ROLLUP_ADMIN
"""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.capa import Capa, CapaSourceCategory, CapaSourceType
from app.models.erm import (
    EnterpriseRisk,
    ErmBoardPack,
    ErmRiskSnapshot,
    ReviewCycleConfig,
    RiskAssessment,
    RiskCategory,
    RiskLinkage,
    RiskReview,
    RiskSubCategory,
    RollupLinkage,
    RollupRule,
    ScoringMatrixConfig,
)
from app.models.plant import Plant
from app.models.user import User
from app.schemas import erm as S
from app.services import erm as svc
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
    get_user_role_codes,
)

router = APIRouter(prefix="/api/erm", tags=["erm"])


# ─────────────────────────────────────────────────────────────────────
# Guards & helpers
# ─────────────────────────────────────────────────────────────────────
async def _require(
    db: AsyncSession,
    user: User,
    code: str,
    *,
    plant_id: str | None = None,
    record: dict | None = None,
    record_id: str | None = None,
) -> None:
    res = await can(
        db, user.id, code, PermissionContext(plant_id=plant_id, record=record, record_id=record_id)
    )
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


def _owner_record(risk: EnterpriseRisk) -> dict:
    # Maps ERM owner fields onto the names the OWN_RECORDS scope check recognises.
    return {
        "ownerId": risk.riskOwnerId,
        "createdById": risk.createdBy,
        "responsiblePersonId": risk.riskChampionId,
    }


async def _category_index(db: AsyncSession) -> dict[str, RiskCategory]:
    rows = (await db.execute(select(RiskCategory))).scalars().all()
    return {c.id: c for c in rows}


async def _subcat_index(db: AsyncSession) -> dict[str, RiskSubCategory]:
    rows = (await db.execute(select(RiskSubCategory))).scalars().all()
    return {c.id: c for c in rows}


async def _plant_index(db: AsyncSession) -> dict[str, str]:
    rows = (await db.execute(select(Plant.id, Plant.name))).all()
    return {r[0]: r[1] for r in rows}


async def _open_treatment_counts(db: AsyncSession, risk_ids: list[str]) -> dict[str, int]:
    if not risk_ids:
        return {}
    rows = (
        await db.execute(
            select(Capa.sourceReferenceId, func.count(Capa.id))
            .where(Capa.sourceTypeCode == "RISK_TREATMENT")
            .where(Capa.sourceReferenceId.in_(risk_ids))
            .where(Capa.state.notin_(["CLOSED", "VERIFIED", "CANCELLED", "REJECTED", "CLOSED_RECURRED"]))
            .group_by(Capa.sourceReferenceId)
        )
    ).all()
    return {r[0]: r[1] for r in rows}


def _q_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_int(v: Any) -> int | None:
    """Coerce a free-form sourceMetadata value to int-or-None. CAPA
    sourceMetadata is untyped JSON, so a treatment may carry a descriptive
    string where a target score was expected — never let that 500 a list."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    try:
        return int(float(str(v).strip()))
    except (ValueError, TypeError):
        return None


async def _scope_query(db: AsyncSession, user: User):
    """Return (base_stmt, role_codes, accessible_plants). Applies plant-scope and
    the Plant-HSE-Head OPS-rollup-only restriction (test T-23)."""
    role_codes = await get_user_role_codes(db, user.id)
    accessible = await get_accessible_plants(db, user.id)  # None == all
    stmt = select(EnterpriseRisk).where(EnterpriseRisk.isDeleted.is_(False))

    is_privileged = any(
        r in role_codes for r in ("CRO", "RISK_CHAMPION", "EXECUTIVE_VIEWER", "SYSTEM_ADMIN", "ADMIN", "CORPORATE_HSE")
    )
    if "PLANT_HSE_HEAD" in role_codes and not is_privileged:
        # Site rollup OPS risks only — no strategic/financial exposure.
        ops_cat = (await db.execute(select(RiskCategory.id).where(RiskCategory.code == "OPS"))).scalar_one_or_none()
        stmt = stmt.where(EnterpriseRisk.sourceType == "HSE_ROLLUP")
        if ops_cat:
            stmt = stmt.where(EnterpriseRisk.categoryId == ops_cat)
        if accessible is not None:
            stmt = stmt.where(EnterpriseRisk.plantId.in_(accessible or ["__none__"]))
    elif "RISK_OWNER" in role_codes and not is_privileged:
        # Own risks + own plant(s).
        conds = [EnterpriseRisk.riskOwnerId == user.id]
        if accessible:
            conds.append(EnterpriseRisk.plantId.in_(accessible))
        stmt = stmt.where(or_(*conds))
    elif accessible is not None:
        # Plant-scoped but enterprise-level (plantId null) risks are global to the
        # plant-set; include them alongside the user's accessible plants.
        stmt = stmt.where(or_(EnterpriseRisk.plantId.in_(accessible or ["__none__"]), EnterpriseRisk.plantId.is_(None)))
    return stmt, role_codes, accessible


async def _serialise_list_item(
    db: AsyncSession,
    r: EnterpriseRisk,
    cats: dict[str, RiskCategory],
    subs: dict[str, RiskSubCategory],
    plants: dict[str, str],
    names: dict[str, str],
    treat_counts: dict[str, int],
) -> S.RiskListItem:
    cat = cats.get(r.categoryId)
    overdue = svc.review_overdue_days(r.nextReviewDate)
    return S.RiskListItem(
        id=r.id,
        riskCode=r.riskCode,
        title=r.title,
        categoryId=r.categoryId,
        categoryCode=cat.code if cat else None,
        categoryName=cat.name if cat else None,
        categoryColor=cat.colorHex if cat else None,
        subCategoryCode=subs[r.subCategoryId].code if r.subCategoryId and r.subCategoryId in subs else None,
        orgLevel=r.orgLevel,
        businessUnit=r.businessUnit,
        plantId=r.plantId,
        plantName=plants.get(r.plantId) if r.plantId else None,
        riskOwnerId=r.riskOwnerId,
        riskOwnerName=names.get(r.riskOwnerId),
        riskChampionId=r.riskChampionId,
        riskChampionName=names.get(r.riskChampionId),
        lifecycleState=r.lifecycleState,
        velocity=r.velocity,
        sourceType=r.sourceType,
        inherentScore=r.inherentScore,
        inherentBand=r.inherentBand,
        residualLikelihood=r.residualLikelihood,
        residualImpact=r.residualImpact,
        residualScore=r.residualScore,
        residualBand=r.residualBand,
        priorResidualScore=r.priorResidualScore,
        priorResidualBand=r.priorResidualBand,
        nextReviewDate=r.nextReviewDate,
        reviewOverdueDays=overdue,
        reviewBadge=svc.review_badge(overdue),
        openTreatments=treat_counts.get(r.id, 0),
        appetiteThreshold=r.appetiteThreshold,
        updatedAt=r.updatedAt,
    )


# ═════════════════════════════════════════════════════════════════════
# Taxonomy
# ═════════════════════════════════════════════════════════════════════
@router.get("/categories", response_model=list[S.RiskCategoryOut])
async def list_categories(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.READ")
    cats = (
        await db.execute(
            select(RiskCategory)
            .where(RiskCategory.isDeleted.is_(False))
            .options(selectinload(RiskCategory.subCategories))
            .order_by(RiskCategory.displayOrder)
        )
    ).scalars().all()
    return [S.RiskCategoryOut.model_validate(c) for c in cats]


@router.post("/categories", response_model=S.RiskCategoryOut, status_code=201)
async def create_category(
    body: S.CategoryUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.TAXONOMY_ADMIN")
    dup = (await db.execute(select(RiskCategory).where(RiskCategory.code == body.code))).scalar_one_or_none()
    if dup:
        raise HTTPException(409, f"A category with code '{body.code}' already exists.")
    cat = RiskCategory(
        code=body.code, name=body.name, description=body.description, colorHex=body.colorHex,
        displayOrder=body.displayOrder, isActive=body.isActive, isSystemCategory=False, createdBy=user.id,
    )
    db.add(cat)
    await db.commit()
    # Eager-load the (empty) subCategories relationship so model_validate doesn't
    # trigger a sync lazy-load on the async session → MissingGreenlet.
    await db.refresh(cat, attribute_names=["subCategories"])
    return S.RiskCategoryOut.model_validate(cat)


@router.patch("/categories/{cat_id}", response_model=S.RiskCategoryOut)
async def update_category(
    cat_id: str, body: S.CategoryUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.TAXONOMY_ADMIN")
    cat = await db.get(RiskCategory, cat_id)
    if not cat:
        raise HTTPException(404, "Category not found")
    cat.name, cat.description, cat.colorHex = body.name, body.description, body.colorHex
    cat.displayOrder, cat.isActive, cat.updatedBy = body.displayOrder, body.isActive, user.id
    await db.commit()
    await db.refresh(cat, attribute_names=["subCategories"])
    return S.RiskCategoryOut.model_validate(cat)


@router.post("/sub-categories", response_model=S.RiskSubCategoryOut, status_code=201)
async def create_subcategory(
    body: S.SubCategoryUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.TAXONOMY_ADMIN")
    dup = (await db.execute(select(RiskSubCategory).where(RiskSubCategory.code == body.code))).scalar_one_or_none()
    if dup:
        raise HTTPException(409, f"A sub-category with code '{body.code}' already exists.")
    parent = await db.get(RiskCategory, body.categoryId)
    if not parent:
        raise HTTPException(400, "Invalid parent category.")
    sub = RiskSubCategory(
        categoryId=body.categoryId, code=body.code, name=body.name,
        description=body.description, isActive=body.isActive, createdBy=user.id,
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return S.RiskSubCategoryOut.model_validate(sub)


# ═════════════════════════════════════════════════════════════════════
# Scoring matrix
# ═════════════════════════════════════════════════════════════════════
@router.get("/matrix", response_model=S.ScoringMatrixOut)
async def get_matrix(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "ERM.READ")
    m = await svc.get_active_matrix(db)
    if not m:
        raise HTTPException(404, "No active scoring matrix configured")
    return S.ScoringMatrixOut.model_validate(m)


@router.get("/matrix/{matrix_id}/reband-preview", response_model=S.MatrixRebandPreview)
async def reband_preview(
    matrix_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.MATRIX_ADMIN")
    n = (
        await db.execute(
            select(func.count(RiskAssessment.id)).where(RiskAssessment.matrixConfigId == matrix_id)
        )
    ).scalar_one() or 0
    return S.MatrixRebandPreview(
        affectedAssessments=n,
        message=f"{n} existing assessments will be re-banded. Scores are unchanged; bands recalculate.",
    )


@router.patch("/matrix/{matrix_id}", response_model=S.ScoringMatrixOut)
async def update_matrix(
    matrix_id: str, body: S.MatrixUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.MATRIX_ADMIN")
    m = await db.get(ScoringMatrixConfig, matrix_id)
    if not m:
        raise HTTPException(404, "Matrix not found")
    bands_changed = body.ratingBands is not None and body.ratingBands != m.ratingBands
    if body.name is not None:
        m.name = body.name
    if body.likelihoodLevels is not None:
        m.likelihoodLevels = body.likelihoodLevels
    if body.impactLevels is not None:
        m.impactLevels = body.impactLevels
    if body.ratingBands is not None:
        m.ratingBands = body.ratingBands
    if body.notes is not None:
        m.notes = body.notes
    m.updatedBy = user.id
    if bands_changed:
        m.version += 1
        # Re-band existing assessments + denormalised risk scores (scores unchanged).
        assessments = (
            await db.execute(select(RiskAssessment).where(RiskAssessment.matrixConfigId == matrix_id))
        ).scalars().all()
        for a in assessments:
            a.ratingBand = svc.band_for_score(a.totalScore, body.ratingBands)
        risks = (await db.execute(select(EnterpriseRisk))).scalars().all()
        for r in risks:
            if r.inherentScore:
                r.inherentBand = svc.band_for_score(r.inherentScore, body.ratingBands)
            if r.residualScore:
                r.residualBand = svc.band_for_score(r.residualScore, body.ratingBands)
    await db.commit()
    await db.refresh(m)
    return S.ScoringMatrixOut.model_validate(m)


# ═════════════════════════════════════════════════════════════════════
# Register — list / create / detail / update / lifecycle
# ═════════════════════════════════════════════════════════════════════
@router.get("/risks", response_model=S.RiskListResponse)
async def list_risks(
    category: str | None = Query(None),
    band: str | None = Query(None),
    state: str | None = Query(None),
    orgLevel: str | None = Query(None),
    siteId: str | None = Query(None),
    owner: str | None = Query(None),
    source: str | None = Query(None),
    overdueOnly: bool = Query(False),
    likelihood: int | None = Query(None),
    impact: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "ERM.READ")
    stmt, role_codes, accessible = await _scope_query(db, user)
    rows = (await db.execute(stmt)).scalars().all()

    cats, subs, plants = await _category_index(db), await _subcat_index(db), await _plant_index(db)
    code_to_id = {c.code: cid for cid, c in cats.items()}

    # Apply filters in Python (seed volume is small; keeps logic readable).
    def keep(r: EnterpriseRisk) -> bool:
        if category and r.categoryId != code_to_id.get(category):
            return False
        if band and (r.residualBand or "") != band:
            return False
        if state and r.lifecycleState != state:
            return False
        if orgLevel and r.orgLevel != orgLevel:
            return False
        if siteId and r.plantId != siteId:
            return False
        if owner and r.riskOwnerId != owner:
            return False
        if source and r.sourceType != source:
            return False
        if overdueOnly and svc.review_overdue_days(r.nextReviewDate) <= 0:
            return False
        if likelihood and (r.residualLikelihood or 0) != likelihood:
            return False
        if impact and (r.residualImpact or 0) != impact:
            return False
        return True

    rows = [r for r in rows if keep(r)]
    treat_counts = await _open_treatment_counts(db, [r.id for r in rows])
    names = await svc.user_name_map(db, [i for r in rows for i in (r.riskOwnerId, r.riskChampionId)])

    items = [
        await _serialise_list_item(db, r, cats, subs, plants, names, treat_counts) for r in rows
    ]
    # Sort: residual score desc, then review overdue desc.
    items.sort(key=lambda x: (-(x.residualScore or 0), -x.reviewOverdueDays))

    cat_counts: dict[str, int] = {}
    band_counts: dict[str, int] = {}
    state_counts: dict[str, int] = {}
    for it in items:
        if it.categoryCode:
            cat_counts[it.categoryCode] = cat_counts.get(it.categoryCode, 0) + 1
        if it.residualBand:
            band_counts[it.residualBand] = band_counts.get(it.residualBand, 0) + 1
        state_counts[it.lifecycleState] = state_counts.get(it.lifecycleState, 0) + 1

    return S.RiskListResponse(
        items=items, total=len(items), categoryCounts=cat_counts, bandCounts=band_counts, stateCounts=state_counts
    )


async def _next_erm_code(db: AsyncSession) -> str:
    year = _q_now().year
    count = (await db.execute(select(func.count(EnterpriseRisk.id)))).scalar_one() or 0
    return f"ERM-{year}-{(count + 1):04d}"


async def _record_assessment(
    db: AsyncSession, risk: EnterpriseRisk, body: S.AssessmentCreate, user_id: str
) -> RiskAssessment:
    """Create an assessment, flip prior current=false, validate residual<=inherent."""
    impact_scores = [{"dimension": s.dimension, "level": s.level} for s in body.impactScores]
    dom_dim, overall = svc.dominant_dimension(impact_scores)
    total = body.likelihood * overall
    bands = await svc.bands_from_active_matrix(db)
    band = svc.band_for_score(total, bands)

    if body.assessmentType == "RESIDUAL":
        inh = (
            await db.execute(
                select(RiskAssessment)
                .where(RiskAssessment.riskId == risk.id)
                .where(RiskAssessment.assessmentType == "INHERENT")
                .where(RiskAssessment.isCurrent.is_(True))
            )
        ).scalar_one_or_none()
        if inh is None:
            raise HTTPException(400, "Record an inherent assessment before a residual one.")
        if total > inh.totalScore:
            raise HTTPException(
                400, "Residual risk cannot exceed inherent risk — review existing controls."
            )

    # archive prior current of same type
    prior = (
        await db.execute(
            select(RiskAssessment)
            .where(RiskAssessment.riskId == risk.id)
            .where(RiskAssessment.assessmentType == body.assessmentType)
            .where(RiskAssessment.isCurrent.is_(True))
        )
    ).scalars().all()
    for p in prior:
        p.isCurrent = False

    matrix = await svc.get_active_matrix(db)
    a = RiskAssessment(
        riskId=risk.id,
        matrixConfigId=matrix.id if matrix else None,
        matrixVersion=matrix.version if matrix else None,
        assessmentType=body.assessmentType,
        likelihood=body.likelihood,
        impactScores=impact_scores,
        dominantImpactDimension=dom_dim,
        overallImpact=overall,
        totalScore=total,
        ratingBand=band,
        assessmentDate=_q_now(),
        assessedBy=user_id,
        rationale=body.rationale,
        isCurrent=True,
        createdBy=user_id,
    )
    db.add(a)
    await db.flush()
    await svc.recompute_risk_scores(db, risk)
    return a


@router.post("/risks", response_model=S.RiskDetail, status_code=201)
async def create_risk(
    body: S.RiskCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.CREATE", plant_id=body.plantId)
    if not body.riskOwnerId:
        raise HTTPException(400, "Risk owner is mandatory.")
    cat = await db.get(RiskCategory, body.categoryId)
    if not cat:
        raise HTTPException(400, "Invalid category")

    now = _q_now()
    review_days = body.reviewOverrideDays or 180
    risk = EnterpriseRisk(
        riskCode=await _next_erm_code(db),
        title=body.title,
        description=body.description,
        categoryId=body.categoryId,
        subCategoryId=body.subCategoryId,
        orgLevel=body.orgLevel,
        businessUnit=body.businessUnit,
        plantId=body.plantId,
        riskOwnerId=body.riskOwnerId,
        riskChampionId=body.riskChampionId,
        lifecycleState="DRAFT",
        velocity=body.velocity,
        sourceType="MANUAL",
        identifiedDate=now,
        nextReviewDate=now + timedelta(days=review_days),
        appetiteThreshold=body.appetiteThreshold,
        tags=body.tags,
        causes=body.causes,
        consequences=body.consequences,
        existingControls=body.existingControls,
        createdBy=user.id,
    )
    db.add(risk)
    await db.flush()

    if body.inherentAssessment:
        await _record_assessment(db, risk, body.inherentAssessment, user.id)
        if body.residualAssessment:
            await _record_assessment(db, risk, body.residualAssessment, user.id)
        risk.lifecycleState = "SUBMITTED"
    await db.commit()
    return await _build_detail(db, risk.id, user)


@router.get("/risks/{risk_id}", response_model=S.RiskDetail)
async def get_risk(risk_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk or risk.isDeleted:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.READ", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))
    return await _build_detail(db, risk_id, user)


@router.patch("/risks/{risk_id}", response_model=S.RiskDetail)
async def update_risk(
    risk_id: str, body: S.RiskUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk or risk.isDeleted:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.UPDATE", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))
    if body.version is not None and body.version != risk.version:
        raise HTTPException(409, "This risk was modified by someone else. Refresh and retry.")
    for f in (
        "title", "description", "categoryId", "subCategoryId", "orgLevel", "businessUnit", "plantId",
        "riskOwnerId", "riskChampionId", "velocity", "appetiteThreshold", "tags", "causes",
        "consequences", "existingControls", "nextReviewDate",
    ):
        v = getattr(body, f)
        if v is not None:
            setattr(risk, f, v)
    risk.updatedBy = user.id
    risk.version += 1
    await db.commit()
    return await _build_detail(db, risk_id, user)


@router.delete("/risks/{risk_id}")
async def delete_risk(risk_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk or risk.isDeleted:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.DELETE", plant_id=risk.plantId)
    risk.isDeleted = True
    risk.updatedBy = user.id
    await db.commit()
    return {"ok": True}


# ── Lifecycle transitions ───────────────────────────────────────────
@router.post("/risks/{risk_id}/submit", response_model=S.RiskDetail)
async def submit_risk(risk_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.UPDATE", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))
    if risk.lifecycleState != "DRAFT":
        raise HTTPException(400, f"Cannot submit from state {risk.lifecycleState}")
    risk.lifecycleState = "SUBMITTED"
    risk.updatedBy = user.id
    await db.commit()
    return await _build_detail(db, risk_id, user)


@router.post("/risks/{risk_id}/validate", response_model=S.RiskDetail)
async def validate_risk(risk_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.APPROVE", plant_id=risk.plantId)
    if risk.lifecycleState != "SUBMITTED":
        raise HTTPException(400, f"Cannot validate from state {risk.lifecycleState}")
    # require both inherent + residual present
    cur = (
        await db.execute(
            select(RiskAssessment).where(RiskAssessment.riskId == risk.id).where(RiskAssessment.isCurrent.is_(True))
        )
    ).scalars().all()
    types = {a.assessmentType for a in cur}
    if "INHERENT" not in types:
        raise HTTPException(400, "An inherent assessment is required before validation.")
    risk.lifecycleState = "ASSESSED"
    risk.updatedBy = user.id
    await svc.maybe_escalate(db, risk)
    await db.commit()
    return await _build_detail(db, risk_id, user)


@router.post("/risks/{risk_id}/accept", response_model=S.RiskDetail)
async def accept_risk(
    risk_id: str, body: S.StateActionBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.ACCEPT", plant_id=risk.plantId)  # CRO only
    if not (body.justification and body.justification.strip()):
        raise HTTPException(400, "Acceptance requires a justification.")
    risk.lifecycleState = "ACCEPTED"
    risk.acceptanceJustification = body.justification
    risk.acceptedBy = user.id
    risk.acceptedAt = _q_now()
    risk.updatedBy = user.id
    await db.commit()
    return await _build_detail(db, risk_id, user)


@router.post("/risks/{risk_id}/close", response_model=S.RiskDetail)
async def close_risk(
    risk_id: str, body: S.StateActionBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.CLOSE", plant_id=risk.plantId)  # CRO only
    if not (body.justification and body.justification.strip()):
        raise HTTPException(400, "Closure requires a justification.")
    risk.lifecycleState = "CLOSED"
    risk.closureJustification = body.justification
    risk.updatedBy = user.id
    await db.commit()
    return await _build_detail(db, risk_id, user)


@router.post("/risks/{risk_id}/monitoring", response_model=S.RiskDetail)
async def move_to_monitoring(risk_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.UPDATE", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))
    # Requires a current residual assessment recorded after the last treatment closed.
    res = (
        await db.execute(
            select(RiskAssessment)
            .where(RiskAssessment.riskId == risk.id)
            .where(RiskAssessment.assessmentType == "RESIDUAL")
            .where(RiskAssessment.isCurrent.is_(True))
        )
    ).scalar_one_or_none()
    if res is None:
        raise HTTPException(400, "Record a fresh residual assessment before moving to MONITORING.")
    risk.lifecycleState = "MONITORING"
    risk.updatedBy = user.id
    await db.commit()
    return await _build_detail(db, risk_id, user)


# ═════════════════════════════════════════════════════════════════════
# Assessments
# ═════════════════════════════════════════════════════════════════════
@router.post("/risks/{risk_id}/assessments", response_model=S.RiskAssessmentOut, status_code=201)
async def create_assessment(
    risk_id: str, body: S.AssessmentCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk or risk.isDeleted:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.ASSESS", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))
    a = await _record_assessment(db, risk, body, user.id)
    if risk.lifecycleState == "DRAFT":
        risk.lifecycleState = "ASSESSED"
    await svc.maybe_escalate(db, risk)
    risk.updatedBy = user.id
    # T2-11: a residual re-assessment can cross an appetite tolerance band —
    # run the breach engine in the same transaction, not only at nightly run.
    if body.assessmentType == "RESIDUAL":
        from app.services.erm_p2 import evaluate_appetite
        await evaluate_appetite(db)
    await db.commit()
    await db.refresh(a)
    out = S.RiskAssessmentOut.model_validate(a)
    nm = await svc.user_name_map(db, [a.assessedBy])
    out.assessedByName = nm.get(a.assessedBy)
    return out


@router.get("/risks/{risk_id}/assessments", response_model=list[S.RiskAssessmentOut])
async def list_assessments(risk_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.READ", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))
    rows = (
        await db.execute(
            select(RiskAssessment).where(RiskAssessment.riskId == risk_id).order_by(RiskAssessment.assessmentDate.desc())
        )
    ).scalars().all()
    names = await svc.user_name_map(db, [r.assessedBy for r in rows])
    out = []
    for r in rows:
        o = S.RiskAssessmentOut.model_validate(r)
        o.assessedByName = names.get(r.assessedBy)
        out.append(o)
    return out


# ═════════════════════════════════════════════════════════════════════
# Treatments (CAPA RISK_TREATMENT extension)
# ═════════════════════════════════════════════════════════════════════
async def _resolve_fallback_plant(db: AsyncSession, risk: EnterpriseRisk) -> Plant | None:
    if risk.plantId:
        p = await db.get(Plant, risk.plantId)
        if p:
            return p
    owner = await db.get(User, risk.riskOwnerId)
    if owner and owner.plantId:
        p = await db.get(Plant, owner.plantId)
        if p:
            return p
    return (await db.execute(select(Plant).order_by(Plant.code).limit(1))).scalar_one_or_none()


@router.post("/risks/{risk_id}/treatments", status_code=201)
async def create_treatment(
    risk_id: str, body: S.TreatmentCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk or risk.isDeleted:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.TREAT", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))

    if body.treatmentStrategy == "TOLERATE":
        # No CAPA actions; mandates acceptance justification + CRO sign-off (handled
        # via the /accept endpoint). Here we just stamp the justification + flag.
        if not (body.acceptanceJustification and body.acceptanceJustification.strip()):
            raise HTTPException(400, "TOLERATE requires an acceptance justification.")
        risk.acceptanceJustification = body.acceptanceJustification
        risk.updatedBy = user.id
        await db.commit()
        return {
            "ok": True,
            "strategy": "TOLERATE",
            "message": "Acceptance recorded — awaiting CRO sign-off via accept endpoint.",
        }

    # TREAT / TRANSFER / TERMINATE → spawn a CAPA on the universal engine.
    st = (
        await db.execute(select(CapaSourceType).where(CapaSourceType.code == "RISK_TREATMENT"))
    ).scalar_one_or_none()
    if st is None:
        raise HTTPException(400, "RISK_TREATMENT CAPA source type not seeded.")
    cat = await db.get(CapaSourceCategory, st.categoryId)
    plant = await _resolve_fallback_plant(db, risk)
    if plant is None:
        raise HTTPException(400, "No plant available to scope the treatment CAPA.")

    year = _q_now().year
    count = (
        await db.execute(
            select(func.count(Capa.id)).where(Capa.plantId == plant.id).where(Capa.sourceCategoryId == st.categoryId)
        )
    ).scalar_one() or 0
    capa_number = f"CAPA-{cat.prefix if cat else 'RTM'}-{year}-{plant.code}-{(count + 1):03d}"

    capa = Capa(
        capaNumber=capa_number,
        title=body.title or f"{body.treatmentStrategy.title()} — {risk.title}"[:200],
        plantId=plant.id,
        sourceCategoryId=st.categoryId,
        sourceTypeId=st.id,
        sourceTypeCode="RISK_TREATMENT",
        sourceReferenceId=risk.id,
        sourceReferenceUrl=f"/erm/risks/{risk.id}",
        sourceReferenceSummary=f"{risk.riskCode} — {risk.title}",
        sourceMetadata={
            "treatmentStrategy": body.treatmentStrategy,
            "expectedResidualReduction": body.expectedResidualReduction,
            "riskCode": risk.riskCode,
        },
        problemDescription=body.description or f"Risk treatment ({body.treatmentStrategy}) for {risk.riskCode}: {risk.title}",
        detectionMethod="ERM_TREATMENT",
        detectedAt=_q_now(),
        detectedByUserId=user.id,
        primaryCategory="Risk Treatment",
        actionType="CORRECTIVE_AND_PREVENTIVE",
        severity="HIGH" if (risk.residualBand in ("HIGH", "CRITICAL")) else "MODERATE",
        priority="HIGH",
        state="ACTIONS_PLANNED",
        stateChangedAt=_q_now(),
        stateChangedByUserId=user.id,
        closureTargetDate=body.dueDate,
        raisedByUserId=user.id,
        primaryOwnerUserId=body.primaryOwnerUserId or risk.riskOwnerId,
        createdByUserId=user.id,
    )
    db.add(capa)
    if risk.lifecycleState in ("ASSESSED", "MONITORING"):
        risk.lifecycleState = "TREATMENT_ACTIVE"
    risk.updatedBy = user.id
    await db.commit()
    await db.refresh(capa)
    return {"ok": True, "capaId": capa.id, "capaNumber": capa.capaNumber, "strategy": body.treatmentStrategy}


@router.get("/risks/{risk_id}/treatments", response_model=list[S.TreatmentOut])
async def list_risk_treatments(risk_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.READ", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))
    return await _treatments_for_risk(db, risk_id)


async def _treatments_for_risk(db: AsyncSession, risk_id: str) -> list[S.TreatmentOut]:
    rows = (
        await db.execute(
            select(Capa).where(Capa.sourceTypeCode == "RISK_TREATMENT").where(Capa.sourceReferenceId == risk_id)
        )
    ).scalars().all()
    names = await svc.user_name_map(db, [c.primaryOwnerUserId for c in rows if c.primaryOwnerUserId])
    open_states = {"DRAFT", "SUBMITTED", "UNDER_RCA", "ACTIONS_PLANNED", "ACTIONS_IN_PROGRESS", "PENDING_VERIFICATION"}
    now = _q_now()
    out = []
    for c in rows:
        meta = c.sourceMetadata or {}
        is_open = c.state in open_states
        overdue = bool(is_open and c.closureTargetDate and c.closureTargetDate.replace(tzinfo=timezone.utc) < now) if c.closureTargetDate else False
        out.append(
            S.TreatmentOut(
                id=c.id, capaNumber=c.capaNumber, title=c.title,
                treatmentStrategy=meta.get("treatmentStrategy", "TREAT"),
                state=c.state, primaryOwnerUserId=c.primaryOwnerUserId,
                primaryOwnerName=names.get(c.primaryOwnerUserId) if c.primaryOwnerUserId else None,
                closureTargetDate=c.closureTargetDate,
                expectedResidualReduction=_safe_int(meta.get("expectedResidualReduction")),
                isOpen=is_open, overdue=overdue,
            )
        )
    return out


@router.get("/treatments", response_model=S.TreatmentTrackerResponse)
async def treatment_tracker(
    strategy: str | None = Query(None),
    state: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "ERM.READ")
    rows = (await db.execute(select(Capa).where(Capa.sourceTypeCode == "RISK_TREATMENT"))).scalars().all()
    risk_ids = [c.sourceReferenceId for c in rows if c.sourceReferenceId]
    risks = {
        r.id: r
        for r in (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.id.in_(risk_ids or ["__none__"])))).scalars().all()
    }
    names = await svc.user_name_map(db, [c.primaryOwnerUserId for c in rows if c.primaryOwnerUserId])
    open_states = {"DRAFT", "SUBMITTED", "UNDER_RCA", "ACTIONS_PLANNED", "ACTIONS_IN_PROGRESS", "PENDING_VERIFICATION"}
    now = _q_now()
    q_start = _quarter_start(now)
    items, open_count, overdue_count, closed_q, closure_days = [], 0, 0, 0, []
    for c in rows:
        meta = c.sourceMetadata or {}
        strat = meta.get("treatmentStrategy", "TREAT")
        if strategy and strat != strategy:
            continue
        if state and c.state != state:
            continue
        is_open = c.state in open_states
        overdue = bool(is_open and c.closureTargetDate and c.closureTargetDate.replace(tzinfo=timezone.utc) < now) if c.closureTargetDate else False
        if is_open:
            open_count += 1
        if overdue:
            overdue_count += 1
        if c.closedAt and c.closedAt.replace(tzinfo=timezone.utc) >= q_start:
            closed_q += 1
            if c.createdAt:
                closure_days.append((c.closedAt - c.createdAt).days)
        risk = risks.get(c.sourceReferenceId)
        items.append(
            S.TreatmentTrackerRow(
                id=c.id, capaNumber=c.capaNumber, title=c.title, treatmentStrategy=strat,
                riskId=c.sourceReferenceId or "", riskCode=risk.riskCode if risk else "",
                riskTitle=risk.title if risk else "", parentResidualBand=risk.residualBand if risk else None,
                state=c.state, primaryOwnerUserId=c.primaryOwnerUserId,
                primaryOwnerName=names.get(c.primaryOwnerUserId) if c.primaryOwnerUserId else None,
                closureTargetDate=c.closureTargetDate, overdue=overdue,
                expectedResidualReduction=_safe_int(meta.get("expectedResidualReduction")),
            )
        )
    items.sort(key=lambda x: (not x.overdue, x.state))
    avg = round(sum(closure_days) / len(closure_days), 1) if closure_days else None
    return S.TreatmentTrackerResponse(
        items=items, total=len(items), openCount=open_count, overdueCount=overdue_count,
        closedThisQuarter=closed_q, avgClosureDays=avg,
    )


# ═════════════════════════════════════════════════════════════════════
# Reviews
# ═════════════════════════════════════════════════════════════════════
@router.post("/risks/{risk_id}/reviews", response_model=S.RiskReviewOut, status_code=201)
async def create_review(
    risk_id: str, body: S.ReviewCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk or risk.isDeleted:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.REVIEW", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))

    new_assessment_id = None
    if body.outcome == "RESCORED":
        if not body.newAssessment:
            raise HTTPException(400, "RESCORED outcome requires a new assessment.")
        a = await _record_assessment(db, risk, body.newAssessment, user.id)
        new_assessment_id = a.id
        await svc.maybe_escalate(db, risk)
    elif body.outcome == "ESCALATED":
        risk.lifecycleState = "ESCALATED"
        risk.escalatedAt = _q_now()
        await svc.notify_escalation(db, risk)

    review = RiskReview(
        riskId=risk.id, reviewDate=_q_now(), reviewedBy=user.id, outcome=body.outcome,
        notes=body.notes, newAssessmentId=new_assessment_id, createdBy=user.id,
    )
    db.add(review)
    # reset next review date from current residual band
    risk.nextReviewDate = await svc.next_review_date_for_band(db, risk.residualBand)
    risk.updatedBy = user.id
    # T2-11: a RESCORED review can cross an appetite band — run the breach engine.
    if body.outcome == "RESCORED":
        from app.services.erm_p2 import evaluate_appetite
        await evaluate_appetite(db)
    await db.commit()
    await db.refresh(review)
    out = S.RiskReviewOut.model_validate(review)
    nm = await svc.user_name_map(db, [review.reviewedBy])
    out.reviewedByName = nm.get(review.reviewedBy)
    return out


@router.get("/reviews/calendar", response_model=list[S.ReviewCalendarItem])
async def review_calendar(
    mine: bool = Query(False), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.READ")
    stmt, _, _ = await _scope_query(db, user)
    rows = (await db.execute(stmt)).scalars().all()
    if mine:
        rows = [r for r in rows if r.riskOwnerId == user.id or r.riskChampionId == user.id]
    names = await svc.user_name_map(db, [r.riskOwnerId for r in rows])
    out = []
    for r in rows:
        if r.lifecycleState == "CLOSED":
            continue
        overdue = svc.review_overdue_days(r.nextReviewDate)
        out.append(
            S.ReviewCalendarItem(
                riskId=r.id, riskCode=r.riskCode, title=r.title, residualBand=r.residualBand,
                nextReviewDate=r.nextReviewDate, overdueDays=overdue, reviewBadge=svc.review_badge(overdue),
                riskOwnerId=r.riskOwnerId, riskOwnerName=names.get(r.riskOwnerId),
            )
        )
    return out


# ═════════════════════════════════════════════════════════════════════
# Linkages / network graph
# ═════════════════════════════════════════════════════════════════════
@router.get("/network", response_model=S.NetworkGraph)
async def network_graph(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "ERM.READ")
    stmt, _, _ = await _scope_query(db, user)
    risks = (await db.execute(stmt)).scalars().all()
    risk_ids = {r.id for r in risks}
    cats = await _category_index(db)
    nodes = [
        S.NetworkNode(
            id=r.id, riskCode=r.riskCode, title=r.title,
            categoryCode=cats[r.categoryId].code if r.categoryId in cats else None,
            categoryColor=cats[r.categoryId].colorHex if r.categoryId in cats else None,
            residualScore=r.residualScore, residualBand=r.residualBand, lifecycleState=r.lifecycleState,
        )
        for r in risks
    ]
    links = (await db.execute(select(RiskLinkage))).scalars().all()
    edges = [
        S.NetworkEdge(id=l.id, source=l.sourceRiskId, target=l.targetRiskId, linkageType=l.linkageType, notes=l.notes)
        for l in links
        if l.sourceRiskId in risk_ids and l.targetRiskId in risk_ids
    ]
    return S.NetworkGraph(nodes=nodes, edges=edges)


@router.post("/linkages", response_model=S.RiskLinkageOut, status_code=201)
async def create_linkage(
    body: S.LinkageCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.LINK")
    if body.sourceRiskId == body.targetRiskId:
        raise HTTPException(400, "A risk cannot be linked to itself.")
    dup = (
        await db.execute(
            select(RiskLinkage)
            .where(RiskLinkage.sourceRiskId == body.sourceRiskId)
            .where(RiskLinkage.targetRiskId == body.targetRiskId)
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(409, "This linkage already exists.")
    link = RiskLinkage(
        sourceRiskId=body.sourceRiskId, targetRiskId=body.targetRiskId,
        linkageType=body.linkageType, notes=body.notes, createdBy=user.id,
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)
    return S.RiskLinkageOut.model_validate(link)


@router.delete("/linkages/{linkage_id}")
async def delete_linkage(linkage_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "ERM.LINK")
    link = await db.get(RiskLinkage, linkage_id)
    if not link:
        raise HTTPException(404, "Linkage not found")
    await db.delete(link)
    await db.commit()
    return {"ok": True}


# ═════════════════════════════════════════════════════════════════════
# Rollup engine
# ═════════════════════════════════════════════════════════════════════
@router.get("/rollup-rules", response_model=list[S.RollupRuleOut])
async def list_rollup_rules(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "ERM.READ")
    rules = (
        await db.execute(select(RollupRule).where(RollupRule.isDeleted.is_(False)))
    ).scalars().all()
    counts = {
        r[0]: r[1]
        for r in (
            await db.execute(
                select(RollupLinkage.rollupRuleId, func.count(RollupLinkage.id)).group_by(RollupLinkage.rollupRuleId)
            )
        ).all()
    }
    out = []
    for r in rules:
        o = S.RollupRuleOut.model_validate(r)
        o.linkedEntryCount = counts.get(r.id, 0)
        out.append(o)
    return out


@router.post("/rollup-rules", response_model=S.RollupRuleOut, status_code=201)
async def create_rollup_rule(
    body: S.RollupRuleUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.ROLLUP_ADMIN")
    rule = RollupRule(
        name=body.name, filterCriteria=body.filterCriteria.model_dump(exclude_none=True),
        aggregationMode=body.aggregationMode, targetSubCategoryCode=body.targetSubCategoryCode,
        scoringMode=body.scoringMode, isActive=body.isActive, createdBy=user.id,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return S.RollupRuleOut.model_validate(rule)


@router.patch("/rollup-rules/{rule_id}", response_model=S.RollupRuleOut)
async def update_rollup_rule(
    rule_id: str, body: S.RollupRuleUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.ROLLUP_ADMIN")
    rule = await db.get(RollupRule, rule_id)
    if not rule:
        raise HTTPException(404, "Rule not found")
    rule.name = body.name
    rule.filterCriteria = body.filterCriteria.model_dump(exclude_none=True)
    rule.aggregationMode = body.aggregationMode
    rule.targetSubCategoryCode = body.targetSubCategoryCode
    rule.scoringMode = body.scoringMode
    rule.isActive = body.isActive
    rule.updatedBy = user.id
    await db.commit()
    await db.refresh(rule)
    return S.RollupRuleOut.model_validate(rule)


@router.post("/rollup-rules/preview", response_model=S.RollupPreviewResult)
async def preview_rollup(
    body: S.RollupRuleUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.ROLLUP_ADMIN")
    from app.models.eai import EaiEntry, EaiStudy
    from app.models.hira import HiraEntry, HiraStudy

    crit = body.filterCriteria
    min_rank = svc._BAND_RANK.get((crit.minRiskBand or "").upper(), -1)
    modules = crit.sourceModules or ["HIRA", "EAI"]
    entries: list[S.RollupPreviewEntry] = []
    if "HIRA" in modules:
        q = select(HiraEntry, HiraStudy).join(HiraStudy, HiraStudy.id == HiraEntry.studyId).where(HiraEntry.isCurrentVersion.is_(True))
        if crit.siteIds:
            q = q.where(HiraStudy.plantId.in_(crit.siteIds))
        for e, st in (await db.execute(q)).all():
            nb = svc.normalise_band(e.residualRiskLevel)
            if min_rank >= 0 and svc._BAND_RANK.get(nb or "", -1) < min_rank:
                continue
            entries.append(S.RollupPreviewEntry(id=e.id, sourceModule="HIRA", plantId=st.plantId, activityDescription=e.activityDescription, residualBand=nb, residualScore=e.residualRiskScore))
    if "EAI" in modules:
        q = select(EaiEntry, EaiStudy).join(EaiStudy, EaiStudy.id == EaiEntry.studyId).where(EaiEntry.isCurrentVersion.is_(True))
        if crit.siteIds:
            q = q.where(EaiStudy.plantId.in_(crit.siteIds))
        for e, st in (await db.execute(q)).all():
            nb = svc.normalise_band(e.residualImpactLevel)
            if min_rank >= 0 and svc._BAND_RANK.get(nb or "", -1) < min_rank:
                continue
            entries.append(S.RollupPreviewEntry(id=e.id, sourceModule="EAI", plantId=st.plantId, activityDescription=e.activityDescription, residualBand=nb, residualScore=e.residualImpactScore))
    return S.RollupPreviewResult(matched=len(entries), entries=entries)


@router.post("/rollup-rules/{rule_id}/run", response_model=S.RollupRunResult)
async def run_rollup(rule_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "ERM.ROLLUP_ADMIN")
    rule = await db.get(RollupRule, rule_id)
    if not rule:
        raise HTTPException(404, "Rule not found")
    result = await svc.run_rollup_rule(db, rule, actor_id=user.id)
    await db.commit()
    return S.RollupRunResult(**result)


# ═════════════════════════════════════════════════════════════════════
# Dashboards / heat map
# ═════════════════════════════════════════════════════════════════════
def _quarter_start(now: datetime) -> datetime:
    q = (now.month - 1) // 3
    return datetime(now.year, q * 3 + 1, 1, tzinfo=timezone.utc)


def _empty_heatmap(bands: list[dict]) -> list[S.HeatMapCell]:
    cells = []
    for likelihood in range(1, 6):
        for impact in range(1, 6):
            score = likelihood * impact
            cells.append(S.HeatMapCell(likelihood=likelihood, impact=impact, count=0, score=score, band=svc.band_for_score(score, bands), riskIds=[]))
    return cells


def _heatmap_from(rows, kind: str, bands) -> list[S.HeatMapCell]:
    cells = {(c.likelihood, c.impact): c for c in _empty_heatmap(bands)}
    for r in rows:
        if kind == "INHERENT":
            l, i = r.inherentLikelihood, r.inherentImpact
        else:
            l, i = r.residualLikelihood, r.residualImpact
        if l and i and (l, i) in cells:
            cell = cells[(l, i)]
            cell.count += 1
            cell.riskIds.append(r.id)
    return list(cells.values())


@router.get("/dashboard/summary", response_model=S.DashboardSummary)
async def dashboard_summary(
    dimension: str | None = Query(None),
    category: str | None = Query(None),
    siteId: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "ERM.READ")
    stmt, _, _ = await _scope_query(db, user)
    rows = [r for r in (await db.execute(stmt)).scalars().all() if r.lifecycleState != "CLOSED"]
    cats = await _category_index(db)
    code_to_id = {c.code: cid for cid, c in cats.items()}
    if category:
        rows = [r for r in rows if r.categoryId == code_to_id.get(category)]
    if siteId:
        rows = [r for r in rows if r.plantId == siteId]
    bands = await svc.bands_from_active_matrix(db)

    now = _q_now()
    q_start = _quarter_start(now)
    names = await svc.user_name_map(db, [r.riskOwnerId for r in rows])
    treat_counts = await _open_treatment_counts(db, [r.id for r in rows])

    total_active = len(rows)
    crit = sum(1 for r in rows if r.residualBand == "CRITICAL")
    high = sum(1 for r in rows if r.residualBand == "HIGH")
    overdue = sum(1 for r in rows if svc.review_overdue_days(r.nextReviewDate) > 0)
    open_treat = sum(treat_counts.values())
    escalated_q = sum(1 for r in rows if r.escalatedAt and r.escalatedAt.replace(tzinfo=timezone.utc) >= q_start)

    # category bars
    bar_map: dict[str, S.CategoryBarSegment] = {}
    for r in rows:
        c = cats.get(r.categoryId)
        if not c:
            continue
        seg = bar_map.setdefault(c.code, S.CategoryBarSegment(categoryCode=c.code, categoryName=c.name, colorHex=c.colorHex))
        b = (r.residualBand or "").upper()
        if b == "LOW":
            seg.low += 1
        elif b == "MEDIUM":
            seg.medium += 1
        elif b == "HIGH":
            seg.high += 1
        elif b == "CRITICAL":
            seg.critical += 1
        seg.total += 1
    bars = sorted(bar_map.values(), key=lambda s: -s.total)

    # top 10 by residual score
    ranked = sorted(rows, key=lambda r: -(r.residualScore or 0))[:10]
    top = []
    for idx, r in enumerate(ranked, 1):
        c = cats.get(r.categoryId)
        trend, delta = "FLAT", 0
        if r.priorResidualScore is not None and r.residualScore is not None:
            delta = r.residualScore - r.priorResidualScore
            trend = "UP" if delta > 0 else ("DOWN" if delta < 0 else "FLAT")
        days_to_review = None
        if r.nextReviewDate:
            nr = r.nextReviewDate.replace(tzinfo=timezone.utc) if r.nextReviewDate.tzinfo is None else r.nextReviewDate
            days_to_review = (nr - now).days
        top.append(
            S.TopRiskRow(
                rank=idx, id=r.id, riskCode=r.riskCode, title=r.title,
                categoryCode=c.code if c else None, categoryName=c.name if c else None,
                categoryColor=c.colorHex if c else None, residualScore=r.residualScore, residualBand=r.residualBand,
                trend=trend, trendDelta=delta, riskOwnerId=r.riskOwnerId, riskOwnerName=names.get(r.riskOwnerId),
                daysToReview=days_to_review,
            )
        )

    # movement (band changed vs prior quarter)
    movement = []
    for r in rows:
        if r.priorResidualBand and r.residualBand and r.priorResidualBand != r.residualBand:
            direction = "UP" if svc._BAND_RANK.get(r.residualBand, 0) > svc._BAND_RANK.get(r.priorResidualBand, 0) else "DOWN"
            movement.append(S.MovementRow(id=r.id, riskCode=r.riskCode, title=r.title, fromBand=r.priorResidualBand, toBand=r.residualBand, direction=direction))

    return S.DashboardSummary(
        totalActiveRisks=total_active, criticalResidual=crit, highResidual=high, overdueReviews=overdue,
        openTreatments=open_treat, escalatedThisQuarter=escalated_q,
        inherentHeatMap=_heatmap_from(rows, "INHERENT", bands), residualHeatMap=_heatmap_from(rows, "RESIDUAL", bands),
        categoryBars=bars, topRisks=top, movement=movement,
    )


@router.get("/dashboard/category/{code}")
async def category_drilldown(code: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "ERM.READ")
    cat = (await db.execute(select(RiskCategory).where(RiskCategory.code == code))).scalar_one_or_none()
    if not cat:
        raise HTTPException(404, "Category not found")
    stmt, _, _ = await _scope_query(db, user)
    rows = [r for r in (await db.execute(stmt)).scalars().all() if r.categoryId == cat.id and r.lifecycleState != "CLOSED"]
    subs = await _subcat_index(db)
    sub_donut: dict[str, int] = {}
    for r in rows:
        sc = subs[r.subCategoryId].code if r.subCategoryId and r.subCategoryId in subs else "—"
        sub_donut[sc] = sub_donut.get(sc, 0) + 1
    bands = await svc.bands_from_active_matrix(db)
    return {
        "category": {"code": cat.code, "name": cat.name, "description": cat.description, "colorHex": cat.colorHex},
        "total": len(rows),
        "subCategoryDonut": [{"code": k, "count": v} for k, v in sub_donut.items()],
        "bandCounts": {b["name"]: sum(1 for r in rows if r.residualBand == b["name"]) for b in bands},
        "residualHeatMap": [c.model_dump() for c in _heatmap_from(rows, "RESIDUAL", bands)],
        "risks": [
            {"id": r.id, "riskCode": r.riskCode, "title": r.title, "residualScore": r.residualScore, "residualBand": r.residualBand, "lifecycleState": r.lifecycleState}
            for r in sorted(rows, key=lambda r: -(r.residualScore or 0))
        ],
    }


# ═════════════════════════════════════════════════════════════════════
# Board pack
# ═════════════════════════════════════════════════════════════════════
@router.get("/board-packs", response_model=list[S.BoardPackOut])
async def list_board_packs(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "ERM.READ")
    rows = (
        await db.execute(select(ErmBoardPack).where(ErmBoardPack.isDeleted.is_(False)).order_by(ErmBoardPack.createdAt.desc()))
    ).scalars().all()
    return [S.BoardPackOut.model_validate(r) for r in rows]


@router.post("/board-packs", response_model=S.BoardPackOut, status_code=201)
async def create_board_pack(
    body: S.BoardPackUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.BOARD_PACK")
    now = _q_now()
    pack = ErmBoardPack(
        title=body.title, quarterLabel=body.quarterLabel,
        periodStart=body.periodStart or _quarter_start(now), periodEnd=body.periodEnd or now,
        sections=body.sections or {}, commentary=body.commentary or {}, status="DRAFT", createdBy=user.id,
    )
    db.add(pack)
    await db.commit()
    await db.refresh(pack)
    return S.BoardPackOut.model_validate(pack)


@router.patch("/board-packs/{pack_id}", response_model=S.BoardPackOut)
async def update_board_pack(
    pack_id: str, body: S.BoardPackUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.BOARD_PACK")
    pack = await db.get(ErmBoardPack, pack_id)
    if not pack:
        raise HTTPException(404, "Board pack not found")
    pack.title = body.title
    pack.quarterLabel = body.quarterLabel
    if body.periodStart:
        pack.periodStart = body.periodStart
    if body.periodEnd:
        pack.periodEnd = body.periodEnd
    pack.sections = body.sections or pack.sections
    pack.commentary = body.commentary or pack.commentary
    pack.updatedBy = user.id
    await db.commit()
    await db.refresh(pack)
    return S.BoardPackOut.model_validate(pack)


@router.post("/board-packs/{pack_id}/publish", response_model=S.BoardPackOut)
async def publish_board_pack(pack_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "ERM.BOARD_PACK")
    pack = await db.get(ErmBoardPack, pack_id)
    if not pack:
        raise HTTPException(404, "Board pack not found")
    pack.status = "PUBLISHED"
    pack.publishedAt = _q_now()
    pack.publishedBy = user.id
    pack.generatedAt = _q_now()
    pack.snapshotHash = hashlib.sha256(f"{pack.id}:{pack.quarterLabel}:{pack.publishedAt}".encode()).hexdigest()[:16]
    await db.commit()
    await db.refresh(pack)
    return S.BoardPackOut.model_validate(pack)


@router.get("/board-packs/{pack_id}/render", response_model=S.BoardPackRender)
async def render_board_pack(pack_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "ERM.READ")
    pack = await db.get(ErmBoardPack, pack_id)
    if not pack:
        raise HTTPException(404, "Board pack not found")
    # NB: pass explicit None for the Query() filter params — calling the route
    # function directly would otherwise leave them as truthy FieldInfo defaults.
    summary = await dashboard_summary(None, None, None, user=user, db=db)
    now = _q_now()
    q_start = _quarter_start(now)
    stmt, _, _ = await _scope_query(db, user)
    rows = (await db.execute(stmt)).scalars().all()
    names = await svc.user_name_map(db, [r.acceptedBy for r in rows if r.acceptedBy])
    acceptance = [
        {"riskCode": r.riskCode, "title": r.title, "justification": r.acceptanceJustification,
         "acceptedBy": names.get(r.acceptedBy) if r.acceptedBy else None, "acceptedAt": r.acceptedAt.isoformat() if r.acceptedAt else None}
        for r in rows if r.lifecycleState == "ACCEPTED"
    ]
    escalations = [
        {"riskCode": r.riskCode, "title": r.title, "residualBand": r.residualBand, "escalatedAt": r.escalatedAt.isoformat() if r.escalatedAt else None}
        for r in rows if r.lifecycleState == "ESCALATED"
    ]
    new_risks = [
        {"riskCode": r.riskCode, "title": r.title, "residualBand": r.residualBand}
        for r in rows if r.identifiedDate and r.identifiedDate.replace(tzinfo=timezone.utc) >= q_start
    ]
    return S.BoardPackRender(
        pack=S.BoardPackOut.model_validate(pack), summary=summary, topRisks=summary.topRisks,
        acceptanceLog=acceptance, escalations=escalations, newRisks=new_risks, movement=summary.movement,
        generatedAt=now,
    )


# ═════════════════════════════════════════════════════════════════════
# Snapshots
# ═════════════════════════════════════════════════════════════════════
@router.post("/snapshots")
async def take_snapshot(
    quarterLabel: str = Query(...), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.BOARD_PACK")
    n = await svc.take_snapshot(db, quarterLabel)
    await db.commit()
    return {"ok": True, "snapshotted": n, "quarter": quarterLabel}


# ═════════════════════════════════════════════════════════════════════
# Reports — CSV exports
# ═════════════════════════════════════════════════════════════════════
@router.get("/reports/{kind}.csv")
async def export_csv(kind: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "ERM.EXPORT")
    stmt, _, _ = await _scope_query(db, user)
    rows = (await db.execute(stmt)).scalars().all()
    cats = await _category_index(db)
    buf = io.StringIO()
    w = csv.writer(buf)
    if kind == "register":
        w.writerow(["Code", "Title", "Category", "Org Level", "State", "Inherent", "Residual", "Band", "Owner", "Next Review"])
        names = await svc.user_name_map(db, [r.riskOwnerId for r in rows])
        for r in rows:
            c = cats.get(r.categoryId)
            w.writerow([r.riskCode, r.title, c.name if c else "", r.orgLevel, r.lifecycleState,
                        r.inherentScore or "", r.residualScore or "", r.residualBand or "",
                        names.get(r.riskOwnerId, ""), r.nextReviewDate.date().isoformat() if r.nextReviewDate else ""])
    elif kind == "assessments":
        w.writerow(["Risk", "Type", "Likelihood", "Impact", "Score", "Band", "Date", "Current"])
        ar = (await db.execute(select(RiskAssessment))).scalars().all()
        rmap = {r.id: r.riskCode for r in rows}
        for a in ar:
            if a.riskId not in rmap:
                continue
            w.writerow([rmap.get(a.riskId, ""), a.assessmentType, a.likelihood, a.overallImpact, a.totalScore, a.ratingBand, a.assessmentDate.date().isoformat(), a.isCurrent])
    elif kind == "treatments":
        tr = (await db.execute(select(Capa).where(Capa.sourceTypeCode == "RISK_TREATMENT"))).scalars().all()
        rmap = {r.id: r for r in rows}
        w.writerow(["CAPA", "Strategy", "Risk", "State", "Due", "Expected Reduction"])
        for c in tr:
            meta = c.sourceMetadata or {}
            rr = rmap.get(c.sourceReferenceId)
            w.writerow([c.capaNumber, meta.get("treatmentStrategy", ""), rr.riskCode if rr else "", c.state,
                        c.closureTargetDate.date().isoformat() if c.closureTargetDate else "", meta.get("expectedResidualReduction", "")])
    elif kind == "escalations":
        w.writerow(["Code", "Title", "Residual Band", "Escalated At"])
        for r in rows:
            if r.lifecycleState == "ESCALATED" or r.escalatedAt:
                w.writerow([r.riskCode, r.title, r.residualBand or "", r.escalatedAt.isoformat() if r.escalatedAt else ""])
    elif kind == "acceptances":
        w.writerow(["Code", "Title", "Justification", "Accepted By", "Accepted At"])
        names = await svc.user_name_map(db, [r.acceptedBy for r in rows if r.acceptedBy])
        for r in rows:
            if r.lifecycleState == "ACCEPTED":
                w.writerow([r.riskCode, r.title, r.acceptanceJustification or "", names.get(r.acceptedBy, "") if r.acceptedBy else "", r.acceptedAt.isoformat() if r.acceptedAt else ""])
    else:
        raise HTTPException(404, f"Unknown report kind '{kind}'")
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=erm-{kind}.csv"},
    )


# ═════════════════════════════════════════════════════════════════════
# Detail builder
# ═════════════════════════════════════════════════════════════════════
async def _build_detail(db: AsyncSession, risk_id: str, user: User) -> S.RiskDetail:
    # populate_existing forces a full reload so server-evaluated columns
    # (createdAt / updatedAt = func.now()) are present even when this runs right
    # after a commit on a freshly-inserted row in the identity map — otherwise
    # accessing the expired attr triggers a sync lazy-load → MissingGreenlet.
    r = (
        await db.execute(
            select(EnterpriseRisk)
            .where(EnterpriseRisk.id == risk_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Risk not found")
    cats, subs, plants = await _category_index(db), await _subcat_index(db), await _plant_index(db)
    treat_counts = await _open_treatment_counts(db, [r.id])
    names = await svc.user_name_map(db, [r.riskOwnerId, r.riskChampionId, r.acceptedBy or ""])
    base = await _serialise_list_item(db, r, cats, subs, plants, names, treat_counts)

    assessments = (
        await db.execute(select(RiskAssessment).where(RiskAssessment.riskId == r.id).order_by(RiskAssessment.assessmentDate.desc()))
    ).scalars().all()
    a_names = await svc.user_name_map(db, [a.assessedBy for a in assessments])

    def a_out(a):
        o = S.RiskAssessmentOut.model_validate(a)
        o.assessedByName = a_names.get(a.assessedBy)
        return o

    cur_inh = next((a_out(a) for a in assessments if a.assessmentType == "INHERENT" and a.isCurrent), None)
    cur_res = next((a_out(a) for a in assessments if a.assessmentType == "RESIDUAL" and a.isCurrent), None)

    # linkages
    links = (
        await db.execute(select(RiskLinkage).where(or_(RiskLinkage.sourceRiskId == r.id, RiskLinkage.targetRiskId == r.id)))
    ).scalars().all()
    other_ids = {l.targetRiskId if l.sourceRiskId == r.id else l.sourceRiskId for l in links}
    other = {
        x.id: x
        for x in (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.id.in_(other_ids or ["__none__"])))).scalars().all()
    }
    linkage_out = [
        {
            "id": l.id, "linkageType": l.linkageType, "notes": l.notes,
            "direction": "OUT" if l.sourceRiskId == r.id else "IN",
            "otherRiskId": (l.targetRiskId if l.sourceRiskId == r.id else l.sourceRiskId),
            "otherRiskCode": other.get(l.targetRiskId if l.sourceRiskId == r.id else l.sourceRiskId).riskCode if other.get(l.targetRiskId if l.sourceRiskId == r.id else l.sourceRiskId) else None,
            "otherRiskTitle": other.get(l.targetRiskId if l.sourceRiskId == r.id else l.sourceRiskId).title if other.get(l.targetRiskId if l.sourceRiskId == r.id else l.sourceRiskId) else None,
        }
        for l in links
    ]

    # reviews
    reviews = (
        await db.execute(select(RiskReview).where(RiskReview.riskId == r.id).order_by(RiskReview.reviewDate.desc()))
    ).scalars().all()
    rev_names = await svc.user_name_map(db, [rv.reviewedBy for rv in reviews])
    reviews_out = [
        {"id": rv.id, "reviewDate": rv.reviewDate.isoformat(), "reviewedBy": rv.reviewedBy,
         "reviewedByName": rev_names.get(rv.reviewedBy), "outcome": rv.outcome, "notes": rv.notes}
        for rv in reviews
    ]

    # contributing operational entries
    rollups = (await db.execute(select(RollupLinkage).where(RollupLinkage.enterpriseRiskId == r.id))).scalars().all()
    contributing = [
        S.ContributingEntry(
            id=rl.id, sourceModule=rl.sourceModule, sourceRegisterEntryId=rl.sourceRegisterEntryId,
            sourceRef=rl.sourceRef, contributingScore=rl.contributingScore, contributingBand=rl.contributingBand,
            drilldownUrl=f"/{'hira' if rl.sourceModule == 'HIRA' else 'eai'}",
        )
        for rl in rollups
    ]

    treatments = await _treatments_for_risk(db, r.id)

    detail = S.RiskDetail(
        **base.model_dump(),
        description=r.description,
        tags=r.tags or [], causes=r.causes or [], consequences=r.consequences or [], existingControls=r.existingControls or [],
        identifiedDate=r.identifiedDate, rollupRuleId=r.rollupRuleId,
        closureJustification=r.closureJustification, acceptanceJustification=r.acceptanceJustification,
        acceptedBy=r.acceptedBy, acceptedByName=names.get(r.acceptedBy) if r.acceptedBy else None,
        acceptedAt=r.acceptedAt, escalatedAt=r.escalatedAt, isRollup=(r.sourceType == "HSE_ROLLUP"),
        version=r.version, currentInherent=cur_inh, currentResidual=cur_res,
        assessmentHistory=[a_out(a) for a in assessments], treatments=treatments,
        linkages=linkage_out, reviews=reviews_out, contributingEntries=contributing, createdAt=r.createdAt,
    )
    return detail
