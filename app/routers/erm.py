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
from app.services.access_scope import build_query_scope
from app.services.capa_spawn import next_capa_number
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
        r in role_codes for r in ("CRO", "RISK_CHAMPION", "EXECUTIVE_VIEWER", "ADMIN", "CORPORATE_HSE")
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
        inherentExpectedLossInr=r.inherentExpectedLossInr,
        residualExpectedLossInr=r.residualExpectedLossInr,
        residualWorstLossInr=r.residualWorstLossInr,
        controlEffectivenessPct=r.controlEffectivenessPct,
        derivedResidualScore=r.derivedResidualScore,
        derivedResidualBand=r.derivedResidualBand,
        residualIsOverride=bool(r.residualIsOverride),
        residualOverrideVariance=r.residualOverrideVariance,
        controlAlert=bool(r.controlAlert),
        kriAlert=bool(r.kriAlert),
        incidentAlert=bool(getattr(r, "incidentAlert", False)),
        targetScore=r.targetScore,
        targetBand=r.targetBand,
        targetExpectedLossInr=r.targetExpectedLossInr,
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


# Canonical enterprise Business Unit / Department master. Kept as a curated set
# (union'd with any values already in the register) so the field is structured —
# a dropdown on create and a first-class filter, not a free-text box.
CANONICAL_BUSINESS_UNITS = [
    "Manufacturing & Supply Chain",
    "Corporate Strategy",
    "Finance & Treasury",
    "IT & Digital",
    "Legal & Compliance",
    "Compliance & Sustainability",
    "Human Resources",
    "Sales & Marketing",
    "Research & Development",
    "Procurement",
]


@router.get("/business-units", response_model=list[str])
async def list_business_units(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Structured Business Unit / Department options — the canonical master
    union'd with any distinct values already present on risks."""
    await _require(db, user, "ERM.READ")
    used = (
        await db.execute(
            select(EnterpriseRisk.businessUnit)
            .where(EnterpriseRisk.businessUnit.is_not(None))
            .where(EnterpriseRisk.isDeleted.is_(False))
            .distinct()
        )
    ).scalars().all()
    seen, out = set(), []
    for bu in CANONICAL_BUSINESS_UNITS + [u for u in used if u]:
        if bu and bu not in seen:
            seen.add(bu)
            out.append(bu)
    return sorted(out)


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


@router.patch("/sub-categories/{sub_id}", response_model=S.RiskSubCategoryOut)
async def update_subcategory(
    sub_id: str, body: S.SubCategoryUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.TAXONOMY_ADMIN")
    sub = await db.get(RiskSubCategory, sub_id)
    if not sub:
        raise HTTPException(404, "Sub-category not found")
    if body.name is not None:
        sub.name = body.name
    if body.description is not None:
        sub.description = body.description
    if body.isActive is not None:
        sub.isActive = body.isActive
    sub.updatedBy = user.id
    await db.commit()
    await db.refresh(sub)
    return S.RiskSubCategoryOut.model_validate(sub)


@router.delete("/sub-categories/{sub_id}")
async def delete_subcategory(
    sub_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require(db, user, "ERM.TAXONOMY_ADMIN")
    sub = await db.get(RiskSubCategory, sub_id)
    if not sub:
        raise HTTPException(404, "Sub-category not found")
    in_use = (
        await db.execute(
            select(func.count()).select_from(EnterpriseRisk)
            .where(EnterpriseRisk.subCategoryId == sub_id).where(EnterpriseRisk.isDeleted.is_(False))
        )
    ).scalar() or 0
    if in_use:
        raise HTTPException(409, f"Cannot delete — {in_use} risk(s) use this sub-category. Deactivate it instead.")
    await db.delete(sub)
    await db.commit()
    return {"ok": True}


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
    businessUnit: str | None = Query(None),
    search: str | None = Query(None),
    overdueOnly: bool = Query(False),
    likelihood: int | None = Query(None),
    impact: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "ERM.READ")
    stmt, role_codes, accessible = await _scope_query(db, user)
    # Newest-created first — platform-wide register convention. This query
    # previously had no ORDER BY at all, so the register's row order was
    # whatever Postgres happened to return: unstable between page loads and
    # with no guarantee a freshly-raised risk appeared anywhere near the top.
    stmt = stmt.order_by(EnterpriseRisk.createdAt.desc(), EnterpriseRisk.id.desc())
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
        if businessUnit and (r.businessUnit or "") != businessUnit:
            return False
        if search:
            q = search.strip().lower()
            hay = f"{r.riskCode or ''} {r.title or ''} {r.description or ''}".lower()
            if q and q not in hay:
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
    """Create an assessment, flip prior current=false, validate residual<=inherent.
    Computes monetary expected loss; for a control-derived residual, the
    likelihood/impact fall out of mapped control effectiveness (not typed in)."""
    bands = await svc.bands_from_active_matrix(db)

    inh = None
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

    # Control effectiveness (snapshot) — drives a derived residual + override variance.
    eff = await svc.control_effectiveness(db, risk.id) if body.assessmentType == "RESIDUAL" else None

    likelihood = body.likelihood
    if body.assessmentType == "RESIDUAL" and body.deriveFromControls:
        d = svc.derive_residual_from_controls(inh.likelihood, inh.overallImpact, eff, bands)
        likelihood = d["likelihood"]
        dom_dim = inh.dominantImpactDimension
        overall = d["impact"]
        impact_scores = [{"dimension": dom_dim, "level": overall}]
    else:
        impact_scores = [{"dimension": s.dimension, "level": s.level} for s in body.impactScores]
        if not impact_scores:
            raise HTTPException(
                400, "impactScores is required (or set deriveFromControls=true for a residual)."
            )
        dom_dim, overall = svc.dominant_dimension(impact_scores)

    total = likelihood * overall
    band = svc.band_for_score(total, bands)

    if body.assessmentType == "RESIDUAL" and total > inh.totalScore:
        raise HTTPException(
            400, "Residual risk cannot exceed inherent risk — review existing controls."
        )

    # ── Monetary expected loss ──
    likelihood_pct = body.likelihoodPct if body.likelihoodPct is not None else svc.default_likelihood_pct(likelihood)
    fin_best, fin_expected, fin_worst = body.financialBestInr, body.financialExpectedInr, body.financialWorstInr
    if body.assessmentType == "RESIDUAL" and body.deriveFromControls and eff is not None:
        # Derived residual inherits the inherent ₹ impact attenuated by mitigating controls.
        atten = 1.0 - eff["mitigating"]
        if fin_expected is None and inh.financialExpectedInr is not None:
            fin_expected = round(inh.financialExpectedInr * atten)
        if fin_worst is None and inh.financialWorstInr is not None:
            fin_worst = round(inh.financialWorstInr * atten)
        if fin_best is None and inh.financialBestInr is not None:
            fin_best = round(inh.financialBestInr * atten)
    el = svc.expected_loss(likelihood_pct, fin_expected)
    uel = svc.unexpected_loss(likelihood_pct, fin_expected, fin_worst)

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
        likelihood=likelihood,
        impactScores=impact_scores,
        dominantImpactDimension=dom_dim,
        overallImpact=overall,
        totalScore=total,
        ratingBand=band,
        likelihoodPct=likelihood_pct,
        financialBestInr=fin_best,
        financialExpectedInr=fin_expected,
        financialWorstInr=fin_worst,
        expectedLossInr=el,
        unexpectedLossInr=uel,
        timeHorizon=body.timeHorizon,
        derivedFromControls=bool(body.assessmentType == "RESIDUAL" and body.deriveFromControls),
        controlEffectivenessPct=round(eff["combined"] * 100.0, 1) if eff is not None else None,
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
    # Automated notification (§8b) — alert the risk owner on assignment (in-app + email).
    from app.services.erm_notifications import notify_risk_owner_assigned
    await notify_risk_owner_assigned(
        db, risk_id=risk.id, risk_code=risk.riskCode, risk_title=risk.title, owner_user_id=risk.riskOwnerId
    )
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
    # Closed-loop gate: a current residual assessment must exist AND have been
    # recorded AFTER the latest treatment closed — proving the post-mitigation
    # re-score actually measured the new residual rather than reusing a stale one.
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
    last_closed = (
        await db.execute(
            select(func.max(Capa.closedAt))
            .where(Capa.sourceTypeCode == "RISK_TREATMENT")
            .where(Capa.sourceReferenceId == risk.id)
            .where(Capa.state.in_(("CLOSED", "VERIFIED")))
        )
    ).scalar_one_or_none()
    if last_closed is not None:
        res_date = res.assessmentDate.replace(tzinfo=timezone.utc) if res.assessmentDate.tzinfo is None else res.assessmentDate
        closed_aware = last_closed.replace(tzinfo=timezone.utc) if last_closed.tzinfo is None else last_closed
        if res_date < closed_aware:
            raise HTTPException(
                400,
                "Re-assess residual risk AFTER the treatment closed — the current residual predates the latest treatment closure, so the reduction is unproven.",
            )
        # Loop closed → record what the mitigation actually achieved.
        await svc.reconcile_treatment_closures(db)
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
    # Override governance (probe-1 hardening): a residual asserted materially more
    # OPTIMISTIC than the control-derived residual must be both justified AND signed
    # off by a risk approver — a typed-in residual can no longer silently beat controls.
    if body.assessmentType == "RESIDUAL" and not body.deriveFromControls:
        var = risk.residualOverrideVariance  # asserted − derived
        if var is not None and var <= -svc.OVERRIDE_TOLERANCE:
            gap = abs(var)
            if not (body.overrideJustification and body.overrideJustification.strip()):
                raise HTTPException(
                    400,
                    detail={
                        "code": "OVERRIDE_JUSTIFICATION_REQUIRED",
                        "message": (
                            f"This residual is {gap} points more optimistic than your controls justify "
                            f"(control-derived residual = {risk.derivedResidualScore}). Add a short justification "
                            f"for the override, or map/strengthen controls so the residual is supported."
                        ),
                        "gap": gap,
                        "derivedResidualScore": risk.derivedResidualScore,
                    },
                )
            approver = await can(db, user.id, "ERM.APPROVE", PermissionContext(plant_id=risk.plantId))
            if not approver.allowed:
                raise HTTPException(
                    403,
                    detail={
                        "code": "OVERRIDE_APPROVAL_REQUIRED",
                        "message": (
                            f"A residual {gap} points below the control-derived value "
                            f"({risk.derivedResidualScore}) must be signed off by a risk approver "
                            f"(ERM.APPROVE). Strengthen/evidence the controls, or have the CRO approve the override."
                        ),
                        "gap": gap,
                        "derivedResidualScore": risk.derivedResidualScore,
                    },
                )
            a.rationale = f"{a.rationale}\n[OVERRIDE approved by {user.id}: {body.overrideJustification.strip()}]"
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
# Derived residual · Target · Enterprise exposure (ADVANCED)
# ═════════════════════════════════════════════════════════════════════
@router.get("/risks/{risk_id}/history")
async def risk_history(risk_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """§9 — the full audit trail for one risk: creation/updates, assessment changes
    and mitigation (treatment) updates, each with user name, date/time and the
    before/after detail. Gated on ERM.READ (the platform-wide audit viewer is
    compliance-role-gated; this is the risk-scoped, owner-visible view)."""
    from sqlalchemy import or_

    from app.models.audit_log import AuditLog

    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk or risk.isDeleted:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.READ", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))
    assess_ids = (await db.execute(select(RiskAssessment.id).where(RiskAssessment.riskId == risk_id))).scalars().all()
    capa_ids = (
        await db.execute(
            select(Capa.id).where(Capa.sourceTypeCode == "RISK_TREATMENT").where(Capa.sourceReferenceId == risk_id)
        )
    ).scalars().all()
    conds = [(AuditLog.entityType == "EnterpriseRisk") & (AuditLog.entityId == risk_id)]
    if assess_ids:
        conds.append((AuditLog.entityType == "RiskAssessment") & (AuditLog.entityId.in_(list(assess_ids))))
    if capa_ids:
        conds.append((AuditLog.entityType == "Capa") & (AuditLog.entityId.in_(list(capa_ids))))
    rows = (
        await db.execute(select(AuditLog).where(or_(*conds)).order_by(AuditLog.timestamp.desc()).limit(500))
    ).scalars().all()
    names = await svc.user_name_map(db, [r.actorId for r in rows if r.actorId and not str(r.actorId).startswith("SYSTEM")])
    label = {"EnterpriseRisk": "Risk", "RiskAssessment": "Assessment", "Capa": "Mitigation"}
    entries = [
        {
            "id": r.id, "entityType": r.entityType, "entityLabel": label.get(r.entityType, r.entityType),
            "action": r.action, "actorId": r.actorId,
            "actorName": names.get(r.actorId or "", r.actorId), "actorType": r.actorType,
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            "changedFields": r.changedFields, "before": r.before, "after": r.after, "reason": r.reason,
        }
        for r in rows
    ]
    return {"riskId": risk_id, "entries": entries, "total": len(entries)}


@router.get("/risks/{risk_id}/derived-residual", response_model=S.DerivedResidualOut)
async def get_derived_residual(
    risk_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk or risk.isDeleted:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.READ", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))
    return await _derived_residual_out(db, risk)


@router.post("/risks/{risk_id}/target", response_model=S.RiskDetail)
async def set_risk_target(
    risk_id: str, body: S.TargetSet, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk or risk.isDeleted:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.ASSESS", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))
    bands = await svc.bands_from_active_matrix(db)
    svc.set_target(
        risk, body.targetLikelihood, body.targetImpact, bands,
        target_date=body.targetDate, rationale=body.targetRationale,
        financial_expected_inr=body.financialExpectedInr, likelihood_pct=body.likelihoodPct,
    )
    if risk.residualScore is not None and risk.targetScore is not None and risk.targetScore > risk.residualScore:
        raise HTTPException(400, "Target risk should be at or below the current residual — you cannot target a worse position.")
    risk.updatedBy = user.id
    await db.commit()
    return await _build_detail(db, risk_id, user)


@router.put("/risks/{risk_id}/bowtie", response_model=S.RiskDetail)
async def set_bowtie(
    risk_id: str, body: S.BowtieModel, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Persist the structured bow-tie (threats → top event → consequences, with
    preventive/mitigating barriers, each optionally linked to a Control)."""
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk or risk.isDeleted:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.UPDATE", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))
    # Enrich barriers that reference a control with its current code/rating.
    payload = body.model_dump()
    ctrl_ids = [
        b.get("controlId")
        for grp in (payload.get("threats", []) + payload.get("consequences", []))
        for b in (grp.get("preventiveBarriers", []) + grp.get("mitigatingBarriers", []))
        if b.get("controlId")
    ]
    if ctrl_ids:
        from app.models.erm_t3 import Control
        ctrls = {c.id: c for c in (await db.execute(select(Control).where(Control.id.in_(ctrl_ids)))).scalars().all()}
        for grp in payload.get("threats", []) + payload.get("consequences", []):
            for b in grp.get("preventiveBarriers", []) + grp.get("mitigatingBarriers", []):
                c = ctrls.get(b.get("controlId"))
                if c:
                    b["controlCode"] = c.controlCode
    risk.bowtie = payload
    risk.updatedBy = user.id
    await db.commit()
    return await _build_detail(db, risk_id, user)


@router.put("/risks/{risk_id}/three-lines", response_model=S.RiskDetail)
async def set_three_lines(
    risk_id: str, body: S.ThreeLinesUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Assign three-lines-of-defence accountability (1st line = owner/management,
    2nd line = risk/compliance oversight, 3rd line = independent assurance)."""
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk or risk.isDeleted:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.UPDATE", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))
    risk.firstLineOwnerId = body.firstLineOwnerId
    risk.secondLineOwnerId = body.secondLineOwnerId
    risk.thirdLineAssurance = body.thirdLineAssurance
    risk.updatedBy = user.id
    await db.commit()
    return await _build_detail(db, risk_id, user)


@router.get("/exposure-by-factory")
async def exposure_by_factory(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """I-17 / P2-5 — ₹ enterprise-risk exposure per factory (Σ residual expected
    loss of HIGH+CRITICAL risks), ranked. Powers the MIS widget + factory cards."""
    await _require(db, user, "ERM.READ")
    scope = await build_query_scope(db, user.id, "ERM.READ")
    plants = await _plant_index(db)
    q = (
        select(EnterpriseRisk).where(EnterpriseRisk.isDeleted.is_(False))
        .where(EnterpriseRisk.lifecycleState != "CLOSED")
        .where(EnterpriseRisk.residualBand.in_(("HIGH", "CRITICAL")))
        .where(EnterpriseRisk.plantId.is_not(None))
    )
    q = scope.apply(q, EnterpriseRisk)
    risks = (await db.execute(q)).scalars().all()
    by_site: dict[str, dict[str, Any]] = {}
    for r in risks:
        d = by_site.setdefault(r.plantId, {"plantId": r.plantId, "plantName": plants.get(r.plantId, r.plantId),
                                           "totalExposureInr": 0.0, "criticalRiskCount": 0, "highRiskCount": 0})
        d["totalExposureInr"] += r.residualExpectedLossInr or 0
        if r.residualBand == "CRITICAL":
            d["criticalRiskCount"] += 1
        else:
            d["highRiskCount"] += 1
    rows = sorted(by_site.values(), key=lambda x: x["totalExposureInr"], reverse=True)
    for d in rows:
        d["totalExposureInr"] = round(d["totalExposureInr"])
        d["worstBand"] = "CRITICAL" if d["criticalRiskCount"] > 0 else "HIGH"
    return {"factories": rows, "totalExposureInr": round(sum(d["totalExposureInr"] for d in rows))}


@router.post("/risks/sync-incident-alerts")
async def sync_incident_alerts(
    lookbackDays: int = Query(120, ge=7, le=730),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """I-04 — flag OPS risks whose site had an LTI/Critical incident (on-demand;
    also runs on the nightly scheduler and on incident closure)."""
    await _require(db, user, "ERM.UPDATE")
    res = await svc.sync_incident_risk_alerts(db, lookback_days=lookbackDays)
    await db.commit()
    return res


@router.get("/exposure", response_model=S.EnterpriseExposureResponse)
async def enterprise_exposure(
    plant_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Total enterprise risk exposure in ₹ (Σ residual expected loss), the risks
    that drive most of it, concentration by site & category, and a Herfindahl
    concentration index. Answers the CRO's 'what's our exposure and what drives it'."""
    await _require(db, user, "ERM.READ")
    cats = await _category_index(db)
    plants = await _plant_index(db)
    q = (
        select(EnterpriseRisk)
        .where(EnterpriseRisk.isDeleted.is_(False))
        .where(EnterpriseRisk.lifecycleState != "CLOSED")
    )
    if plant_id:
        q = q.where(EnterpriseRisk.plantId == plant_id)
    risks = (await db.execute(q)).scalars().all()

    total_el = sum(r.residualExpectedLossInr or 0 for r in risks)
    total_worst = sum(r.residualWorstLossInr or 0 for r in risks)
    quantified = [r for r in risks if (r.residualExpectedLossInr or 0) > 0]
    unquantified = [r for r in risks if not (r.residualExpectedLossInr or 0) > 0]

    drivers = sorted(quantified, key=lambda r: r.residualExpectedLossInr or 0, reverse=True)
    top_rows: list[S.ExposureRow] = []
    cum = 0.0
    for i, r in enumerate(drivers[:10], start=1):
        el = r.residualExpectedLossInr or 0
        pct = round(el * 100.0 / total_el, 1) if total_el else 0.0
        cum += pct
        cat = cats.get(r.categoryId)
        top_rows.append(S.ExposureRow(
            rank=i, id=r.id, riskCode=r.riskCode, title=r.title,
            categoryCode=cat.code if cat else None, categoryName=cat.name if cat else None,
            residualBand=r.residualBand, residualExpectedLossInr=el,
            residualWorstLossInr=r.residualWorstLossInr or 0,
            pctOfTotal=pct, cumulativePct=round(cum, 1),
        ))

    by_cat: dict[str, dict[str, Any]] = {}
    for r in quantified:
        cat = cats.get(r.categoryId)
        key = cat.code if cat else "UNCATEGORISED"
        d = by_cat.setdefault(key, {"name": cat.name if cat else "Uncategorised", "color": cat.colorHex if cat else None, "count": 0, "el": 0.0})
        d["count"] += 1
        d["el"] += r.residualExpectedLossInr or 0
    cat_rows = [
        S.ExposureByCategory(
            categoryCode=k, categoryName=v["name"], colorHex=v["color"], riskCount=v["count"],
            expectedLossInr=v["el"], pctOfTotal=round(v["el"] * 100.0 / total_el, 1) if total_el else 0.0,
        )
        for k, v in sorted(by_cat.items(), key=lambda kv: kv[1]["el"], reverse=True)
    ]

    by_site: dict[str | None, dict[str, Any]] = {}
    for r in quantified:
        d = by_site.setdefault(r.plantId, {"count": 0, "el": 0.0, "els": []})
        d["count"] += 1
        d["el"] += r.residualExpectedLossInr or 0
        d["els"].append(r.residualExpectedLossInr or 0)
    site_rows = []
    for pid, v in sorted(by_site.items(), key=lambda kv: kv[1]["el"], reverse=True):
        site_total = v["el"] or 1
        hhi = round(sum((x / site_total) ** 2 for x in v["els"]), 3)
        site_rows.append(S.ExposureBySite(
            plantId=pid, plantName=(plants.get(pid) if pid else "Enterprise (no site)"),
            riskCount=v["count"], expectedLossInr=v["el"], concentrationIndex=hhi,
        ))

    portfolio_hhi = round(sum(((r.residualExpectedLossInr or 0) / total_el) ** 2 for r in quantified), 3) if total_el else 0.0
    top5 = sum((r.residualExpectedLossInr or 0) for r in drivers[:5])
    return S.EnterpriseExposureResponse(
        totalExpectedLossInr=total_el, totalWorstLossInr=total_worst,
        quantifiedRiskCount=len(quantified), unquantifiedRiskCount=len(unquantified),
        topDrivers=top_rows, byCategory=cat_rows, bySite=site_rows,
        portfolioConcentrationIndex=portfolio_hhi,
        top5SharePct=round(top5 * 100.0 / total_el, 1) if total_el else 0.0,
    )


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
    # Robust sequence: highest existing suffix + 1 (scans soft-deleted rows too).
    # The old count()+1 collided with an existing capaNumber after any CAPA delete.
    capa_number = await next_capa_number(
        db, prefix=f"CAPA-{cat.prefix if cat else 'RTM'}-{year}-{plant.code}-",
        plant_id=plant.id, category_id=st.categoryId,
    )

    capa = Capa(
        capaNumber=capa_number,
        title=body.title or f"{body.treatmentStrategy.title()} — {risk.title}"[:200],
        plantId=plant.id,
        sourceCategoryId=st.categoryId,
        sourceTypeId=st.id,
        sourceTypeCode="RISK_TREATMENT",
        sourceReferenceId=risk.id,
        sourceReferenceUrl=f"/erm/register/{risk.id}",  # real risk-detail route (was /erm/risks → 404)
        sourceReferenceSummary=f"{risk.riskCode} — {risk.title}",
        sourceMetadata={
            "treatmentStrategy": body.treatmentStrategy,
            "expectedResidualReduction": body.expectedResidualReduction,
            "completionPercent": body.completionPercent or 0,
            "riskCode": risk.riskCode,
            "costInr": body.costInr,
            "transferPolicyId": body.transferPolicyId,
            # Baseline residual snapshot — the closed-loop compares the residual
            # AFTER closure against this to prove (not assume) the risk fell.
            "baselineResidualScore": risk.residualScore,
            "baselineResidualBand": risk.residualBand,
            "baselineResidualExpectedLossInr": risk.residualExpectedLossInr,
            "baselineCapturedAt": _q_now().isoformat(),
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
    # Automated notification (§8b) — alert the treatment owner on assignment.
    from app.services.erm_notifications import notify_treatment_owner_assigned
    await notify_treatment_owner_assigned(db, capa=capa)
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
    # current residual of the parent risk — used to compute the achieved reduction live.
    parent = await db.get(EnterpriseRisk, risk_id)
    cur_residual = parent.residualScore if parent else None
    out = []
    for c in rows:
        meta = c.sourceMetadata or {}
        is_open = c.state in open_states
        overdue = bool(is_open and c.closureTargetDate and c.closureTargetDate.replace(tzinfo=timezone.utc) < now) if c.closureTargetDate else False
        achieved = None if is_open else svc.achieved_reduction(meta, cur_residual)
        expected = _safe_int(meta.get("expectedResidualReduction"))
        out.append(
            S.TreatmentOut(
                id=c.id, capaNumber=c.capaNumber, title=c.title,
                treatmentStrategy=meta.get("treatmentStrategy", "TREAT"),
                state=c.state, primaryOwnerUserId=c.primaryOwnerUserId,
                primaryOwnerName=names.get(c.primaryOwnerUserId) if c.primaryOwnerUserId else None,
                closureTargetDate=c.closureTargetDate,
                expectedResidualReduction=expected,
                achievedResidualReduction=achieved,
                reductionShortfall=(achieved is not None and expected is not None and achieved < expected) or None,
                baselineResidualScore=_safe_int(meta.get("baselineResidualScore")),
                costInr=meta.get("costInr"),
                expectedLossReductionInr=meta.get("expectedLossReductionInr"),
                riskReductionPerRupee=meta.get("riskReductionPerRupee"),
                transferPolicyId=meta.get("transferPolicyId"),
                completionPercent=_safe_int(meta.get("completionPercent")) or 0,
                isOpen=is_open, overdue=overdue,
            )
        )
    return out


async def _auto_apply_post_mitigation_residual(
    db: AsyncSession, risk: EnterpriseRisk, capa: Capa, meta: dict, actor_id: str
) -> bool:
    """§7d — when a treatment reaches 100%, AUTOMATICALLY record a fresh RESIDUAL
    assessment that applies the treatment's expected reduction, so the residual
    score/band (and the heat map) fall on their own post-mitigation. It's a real
    recorded assessment, so it stays governed and audit-trailed."""
    expected = _safe_int(meta.get("expectedResidualReduction"))
    if not expected or expected <= 0:
        return False
    # Baseline = current residual if one exists; otherwise fall back to the
    # current inherent assessment. This makes the residual drop on the NATURAL
    # path too (create → assess inherent → mitigate → complete), where no
    # residual has been recorded yet — previously this silently no-op'd.
    cur = (
        await db.execute(
            select(RiskAssessment)
            .where(RiskAssessment.riskId == risk.id)
            .where(RiskAssessment.assessmentType == "RESIDUAL")
            .where(RiskAssessment.isCurrent.is_(True))
        )
    ).scalar_one_or_none()
    baseline = cur
    if baseline is None:
        baseline = (
            await db.execute(
                select(RiskAssessment)
                .where(RiskAssessment.riskId == risk.id)
                .where(RiskAssessment.assessmentType == "INHERENT")
                .where(RiskAssessment.isCurrent.is_(True))
            )
        ).scalar_one_or_none()
    if baseline is None or not baseline.totalScore:
        return False
    new_score = max(1, baseline.totalScore - expected)
    if new_score >= baseline.totalScore:
        return False
    likelihood, impact = svc.factor_score(new_score)
    dim = baseline.dominantImpactDimension or "Financial"
    try:
        ac = S.AssessmentCreate(
            assessmentType="RESIDUAL",
            likelihood=likelihood,
            impactScores=[S.ImpactScore(dimension=dim, level=impact)],
            deriveFromControls=False,
            rationale=f"Auto: post-mitigation residual recalculation — treatment {capa.capaNumber} reached 100% completion.",
        )
        await _record_assessment(db, risk, ac, actor_id)
    except Exception:
        return False
    meta["residualAutoAppliedAt"] = _q_now().isoformat()
    capa.sourceMetadata = dict(meta)
    return True


@router.patch("/treatments/{capa_id}/progress")
async def update_treatment_progress(
    capa_id: str, body: S.TreatmentProgress, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """§7c — track mitigation % completion. Reaching 100% triggers §7d automatic
    residual recalculation on the parent risk."""
    capa = await db.get(Capa, capa_id)
    if not capa or capa.sourceTypeCode != "RISK_TREATMENT":
        raise HTTPException(404, "Treatment not found")
    risk = await db.get(EnterpriseRisk, capa.sourceReferenceId) if capa.sourceReferenceId else None
    await _require(
        db, user, "ERM.TREAT",
        plant_id=risk.plantId if risk else None,
        record_id=risk.id if risk else None,
        record=_owner_record(risk) if risk else None,
    )
    meta = dict(capa.sourceMetadata or {})
    prev = _safe_int(meta.get("completionPercent")) or 0
    meta["completionPercent"] = body.completionPercent
    if body.note:
        plog = list(meta.get("progressLog") or [])
        plog.append({"at": _q_now().isoformat(), "by": user.id, "pct": body.completionPercent, "note": body.note})
        meta["progressLog"] = plog[-20:]
    capa.sourceMetadata = meta
    residual_recalculated = False
    state_advanced = False
    if body.completionPercent >= 100 and prev < 100 and risk is not None and not meta.get("residualAutoAppliedAt"):
        residual_recalculated = await _auto_apply_post_mitigation_residual(db, risk, capa, meta, user.id)
    # Reaching 100% means the mitigation WORK is done — advance the treatment out
    # of the planning/in-progress state into PENDING_VERIFICATION so the tracker
    # reflects it (full closure still requires a governance verification step).
    if body.completionPercent >= 100 and capa.state in {"ACTIONS_PLANNED", "ACTIONS_IN_PROGRESS"}:
        capa.state = "PENDING_VERIFICATION"
        meta = dict(capa.sourceMetadata or {})
        meta["completedAt"] = _q_now().isoformat()
        capa.sourceMetadata = meta
        state_advanced = True
    await db.commit()
    return {
        "ok": True,
        "completionPercent": body.completionPercent,
        "residualRecalculated": residual_recalculated,
        "stateAdvanced": state_advanced,
        "state": capa.state,
    }


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
    total_el_reduction = total_cost = 0.0
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
        cur_residual = risk.residualScore if risk else None
        achieved = None if is_open else svc.achieved_reduction(meta, cur_residual)
        expected = _safe_int(meta.get("expectedResidualReduction"))
        el_red = meta.get("expectedLossReductionInr")
        cost = meta.get("costInr")
        if el_red:
            total_el_reduction += el_red
        if cost:
            total_cost += cost
        items.append(
            S.TreatmentTrackerRow(
                id=c.id, capaNumber=c.capaNumber, title=c.title, treatmentStrategy=strat,
                riskId=c.sourceReferenceId or "", riskCode=risk.riskCode if risk else "",
                riskTitle=risk.title if risk else "", parentResidualBand=risk.residualBand if risk else None,
                state=c.state, primaryOwnerUserId=c.primaryOwnerUserId,
                primaryOwnerName=names.get(c.primaryOwnerUserId) if c.primaryOwnerUserId else None,
                closureTargetDate=c.closureTargetDate, overdue=overdue,
                expectedResidualReduction=expected,
                achievedResidualReduction=achieved,
                reductionShortfall=(achieved is not None and expected is not None and achieved < expected) or None,
                costInr=cost, expectedLossReductionInr=el_red,
                riskReductionPerRupee=meta.get("riskReductionPerRupee"),
                completionPercent=_safe_int(meta.get("completionPercent")) or 0,
            )
        )
    items.sort(key=lambda x: (not x.overdue, x.state))
    avg = round(sum(closure_days) / len(closure_days), 1) if closure_days else None
    return S.TreatmentTrackerResponse(
        items=items, total=len(items), openCount=open_count, overdueCount=overdue_count,
        closedThisQuarter=closed_q, avgClosureDays=avg,
        totalExpectedLossReductionInr=round(total_el_reduction),
        totalTreatmentCostInr=round(total_cost),
        portfolioRiskReductionPerRupee=round(total_el_reduction / total_cost, 2) if total_cost else None,
    )


@router.post("/treatments/reconcile")
async def reconcile_treatments(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Measure achieved-vs-expected residual reduction + ₹ per ₹ across all CLOSED
    treatments (on-demand job — the platform has no scheduler). CRO / Risk Champion."""
    await _require(db, user, "ERM.TREAT")
    res = await svc.reconcile_treatment_closures(db)
    await db.commit()
    return res


@router.post("/treatments/escalate-overdue")
async def escalate_overdue_treatments(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Escalate overdue treatments up the OWNER→CHAMPION→CRO ladder by days-overdue
    and parent-risk severity (on-demand job). CRO / Risk Champion."""
    await _require(db, user, "ERM.TREAT")
    res = await svc.escalate_overdue_treatments(db)
    await db.commit()
    return res


@router.get("/risks/{risk_id}/horizon")
async def risk_horizon(risk_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Project residual exposure over 1/3/5-year horizons (risk is not point-in-time)."""
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk or risk.isDeleted:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.READ", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))
    return await svc.horizon_projection(db, risk_id)


@router.get("/risks/{risk_id}/stability")
async def risk_stability(risk_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Longitudinal residual stability — did it fall and STAY down, or rebound?"""
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk or risk.isDeleted:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.READ", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))
    return await svc.residual_stability(db, risk_id)


@router.post("/jobs/run-all")
async def run_all_jobs(
    includeModuleFed: bool = Query(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run every on-demand ERM engine in one pass (scheduler substitute — point a cron
    at this). includeModuleFed re-reads live KRI metrics. CRO / Risk Champion."""
    await _require(db, user, "ERM.TREAT")
    res = await svc.run_all_jobs(db, include_module_fed=includeModuleFed)
    await db.commit()
    return res


@router.post("/control-alerts/sync")
async def sync_control_alerts(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Re-evaluate which risks have a deficient mapped control and flag them for
    reassessment (on-demand job). Also runs inline when a control test is recorded."""
    await _require(db, user, "ERM.ASSESS")
    res = await svc.sync_control_alerts(db)
    await db.commit()
    return res


@router.post("/kri-alerts/sync")
async def sync_kri_alerts(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Re-evaluate which risks have a RED linked KRI and flag them for reassessment
    (on-demand; also runs inline when a KRI reading is recorded)."""
    await _require(db, user, "ERM.ASSESS")
    res = await svc.sync_kri_alerts(db)
    await db.commit()
    return res


@router.get("/frameworks/coverage", response_model=S.FrameworkCoverageResponse)
async def frameworks_coverage(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """ISO 31000 / COSO ERM 2017 / SEBI LODR Reg 21 clause-by-clause coverage map —
    proves framework alignment with the endpoint/feature evidence for each clause."""
    await _require(db, user, "ERM.READ")
    return S.FrameworkCoverageResponse(**svc.framework_coverage())


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
    # reset next review date from current residual band, compressed by risk velocity
    risk.nextReviewDate = await svc.next_review_date_for_band(db, risk.residualBand, velocity=risk.velocity)
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
        S.NetworkEdge(
            id=l.id, source=l.sourceRiskId, target=l.targetRiskId, linkageType=l.linkageType, notes=l.notes,
            correlationStrength=l.correlationStrength, impactFactor=l.impactFactor,
        )
        for l in links
        if l.sourceRiskId in risk_ids and l.targetRiskId in risk_ids
    ]
    return S.NetworkGraph(nodes=nodes, edges=edges)


@router.get("/risks/{risk_id}/propagation", response_model=S.PropagationResult)
async def risk_propagation(risk_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """If this risk materialises, which linked risks move and by how much ₹ (probe 3)."""
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk or risk.isDeleted:
        raise HTTPException(404, "Risk not found")
    await _require(db, user, "ERM.READ", plant_id=risk.plantId, record_id=risk.id, record=_owner_record(risk))
    return S.PropagationResult(**await svc.risk_propagation(db, risk_id))


@router.get("/portfolio/correlated-exposure", response_model=S.CorrelatedExposureResponse)
async def correlated_exposure(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Portfolio exposure WITH interdependencies vs the naive independent sum — the
    contagion exposure a naive Σ hides (probes 3, 9)."""
    await _require(db, user, "ERM.READ")
    res = await svc.correlated_exposure(db)
    res["topContagionSources"] = [S.PropagationResult(**p) for p in res["topContagionSources"]]
    return S.CorrelatedExposureResponse(**res)


@router.get("/portfolio/monte-carlo", response_model=S.MonteCarloResponse)
async def portfolio_monte_carlo(
    iterations: int = Query(10000, ge=1000, le=100000),
    seed: int = Query(42),
    plant_id: str | None = Query(None),
    correlate: bool = Query(True),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Monte-Carlo aggregate-loss distribution + VaR (P90/P95/P99) over the portfolio's
    residual loss profiles — the probabilistic capability behind the heat map (probe 8).
    correlate=True drives contagion (induced firing + amplification) via the RiskLinkage
    graph so VaR reflects correlation, and reports the independent-vs-correlated tail delta."""
    await _require(db, user, "ERM.READ")
    return S.MonteCarloResponse(**await svc.monte_carlo_portfolio(db, iterations=iterations, seed=seed, plant_id=plant_id, correlate=correlate))


@router.get("/portfolio/reverse-stress", response_model=S.ReverseStressResponse)
async def portfolio_reverse_stress(
    threshold_inr: float = Query(..., gt=0),
    plant_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reverse stress test — the smallest combination of risks whose simultaneous
    materialisation breaches a ₹ loss threshold (probe 8: 'what combination breaks us')."""
    await _require(db, user, "ERM.READ")
    return S.ReverseStressResponse(**await svc.reverse_stress(db, threshold_inr, plant_id=plant_id))


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
        linkageType=body.linkageType, notes=body.notes,
        correlationStrength=body.correlationStrength, impactFactor=body.impactFactor,
        createdBy=user.id,
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
    med = sum(1 for r in rows if r.residualBand == "MEDIUM")
    low = sum(1 for r in rows if r.residualBand == "LOW")

    # Mitigation progress % + overdue actions (§1d) across this scope's treatments.
    _open_states = {"DRAFT", "SUBMITTED", "UNDER_RCA", "ACTIONS_PLANNED", "ACTIONS_IN_PROGRESS", "PENDING_VERIFICATION"}
    treat_rows = (
        await db.execute(
            select(Capa)
            .where(Capa.sourceTypeCode == "RISK_TREATMENT")
            .where(Capa.sourceReferenceId.in_([r.id for r in rows] or ["__none__"]))
        )
    ).scalars().all()
    open_pcts: list[int] = []
    overdue_treat = 0
    for c in treat_rows:
        if c.state not in _open_states:
            continue
        m = c.sourceMetadata or {}
        open_pcts.append(_safe_int(m.get("completionPercent")) or 0)
        due = c.closureTargetDate
        if due and (due.replace(tzinfo=timezone.utc) if due.tzinfo is None else due) < now:
            overdue_treat += 1
    mitigation_pct = round(sum(open_pcts) / len(open_pcts), 1) if open_pcts else 0.0

    # Department / Business-Unit summary (§1b)
    dept_map: dict[str, S.DepartmentBarSegment] = {}
    for r in rows:
        bu = r.businessUnit or "Unassigned"
        seg = dept_map.setdefault(bu, S.DepartmentBarSegment(businessUnit=bu))
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
    dept_bars = sorted(dept_map.values(), key=lambda s: -s.total)

    # Top root causes (§1c) — best-effort from approved-RCA causal analytics.
    top_causes: list[S.RootCauseSummary] = []
    try:
        from app.services.access_scope import build_query_scope
        from app.services.rca_analytics import compute_cause_analytics
        rca_scope = await build_query_scope(db, user.id, "RCA.READ")
        ca = await compute_cause_analytics(db, rca_scope)
        for cz in (ca.get("causes") or [])[:5]:
            top_causes.append(
                S.RootCauseSummary(
                    label=cz.get("subCauseName") or cz.get("subCauseCode") or "—",
                    categoryCode=cz.get("categoryCode") or None,
                    categoryName=cz.get("categoryName") or None,
                    occurrences=cz.get("occurrences") or 0,
                    riskReach=cz.get("riskReach") or 0,
                    isRecurringDriver=bool(cz.get("isRecurringDriver")),
                )
            )
    except Exception:
        top_causes = []

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

    # §7.5 Pending approvals — items awaiting a governance decision (risks
    # submitted for validation, treatments awaiting verification).
    pending_items: list[S.PendingApprovalItem] = []
    for r in rows:
        if r.lifecycleState == "SUBMITTED":
            pending_items.append(S.PendingApprovalItem(
                type="RISK", code=r.riskCode, title=r.title, href=f"/erm/register/{r.id}",
                state=r.lifecycleState, ownerName=names.get(r.riskOwnerId),
            ))
    risk_by_id = {r.id: r for r in rows}
    for c in treat_rows:
        if c.state == "PENDING_VERIFICATION":
            rk = risk_by_id.get(c.sourceReferenceId)
            pending_items.append(S.PendingApprovalItem(
                type="TREATMENT", code=c.capaNumber, title=c.title,
                href=(f"/erm/register/{rk.id}" if rk else "/erm/treatments"),
                state=c.state,
            ))
    pending_items = pending_items[:25]

    return S.DashboardSummary(
        totalActiveRisks=total_active, criticalResidual=crit, highResidual=high,
        mediumResidual=med, lowResidual=low, overdueReviews=overdue,
        openTreatments=open_treat, overdueTreatments=overdue_treat, mitigationProgressPct=mitigation_pct,
        escalatedThisQuarter=escalated_q,
        pendingApprovals=len(pending_items), pendingApprovalItems=pending_items,
        inherentHeatMap=_heatmap_from(rows, "INHERENT", bands), residualHeatMap=_heatmap_from(rows, "RESIDUAL", bands),
        categoryBars=bars, departmentBars=dept_bars, topRootCauses=top_causes, topRisks=top, movement=movement,
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
    # ₹ exposure + VaR for the board (SEBI LODR Reg 21 — quantified exposure).
    exposure = await enterprise_exposure(None, user=user, db=db)
    monte_carlo = S.MonteCarloResponse(**await svc.monte_carlo_portfolio(db, iterations=5000))
    return S.BoardPackRender(
        pack=S.BoardPackOut.model_validate(pack), summary=summary, topRisks=summary.topRisks,
        acceptanceLog=acceptance, escalations=escalations, newRisks=new_risks, movement=summary.movement,
        exposure=exposure, monteCarlo=monte_carlo,
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
# Reports — CSV / Excel (.xlsx) / PDF exports  (Page Industries §10)
# ═════════════════════════════════════════════════════════════════════
_REPORT_KINDS = {
    "register": "Risk Register",
    "assessments": "Assessment History",
    "treatments": "Mitigation Action Tracker",
    "overdue": "Overdue Action Report",
    "department": "Department-wise Risk Report",
    "heatmap": "Risk Heat Map Report",
    "escalations": "Escalation Log",
    "acceptances": "Risk Acceptance Log",
}


async def _gather_report(kind: str, db: AsyncSession, user: User) -> tuple[str, list[str], list[list]]:
    """Build (title, headers, rows) for an ERM report — the single source of truth
    behind the CSV / Excel / PDF renderers."""
    title = _REPORT_KINDS.get(kind)
    if title is None:
        raise HTTPException(404, f"Unknown report kind '{kind}'")
    stmt, _, _ = await _scope_query(db, user)
    rows = (await db.execute(stmt)).scalars().all()
    cats = await _category_index(db)
    out: list[list] = []

    if kind == "register":
        headers = ["Code", "Title", "Category", "Business Unit", "Org Level", "State", "Inherent", "Residual", "Band", "Owner", "Next Review"]
        names = await svc.user_name_map(db, [r.riskOwnerId for r in rows])
        for r in rows:
            c = cats.get(r.categoryId)
            out.append([r.riskCode, r.title, c.name if c else "", r.businessUnit or "", r.orgLevel, r.lifecycleState,
                        r.inherentScore or "", r.residualScore or "", r.residualBand or "",
                        names.get(r.riskOwnerId, ""), r.nextReviewDate.date().isoformat() if r.nextReviewDate else ""])

    elif kind == "assessments":
        headers = ["Risk", "Type", "Likelihood", "Impact", "Score", "Band", "Date", "Current"]
        ar = (await db.execute(select(RiskAssessment))).scalars().all()
        rmap = {r.id: r.riskCode for r in rows}
        for a in ar:
            if a.riskId not in rmap:
                continue
            out.append([rmap.get(a.riskId, ""), a.assessmentType, a.likelihood, a.overallImpact, a.totalScore, a.ratingBand, a.assessmentDate.date().isoformat(), a.isCurrent])

    elif kind == "treatments":
        headers = ["CAPA", "Strategy", "Risk", "Owner", "State", "% Complete", "Due", "Expected Reduction", "Achieved Reduction"]
        tr = (await db.execute(select(Capa).where(Capa.sourceTypeCode == "RISK_TREATMENT"))).scalars().all()
        rmap = {r.id: r for r in rows}
        names = await svc.user_name_map(db, [c.primaryOwnerUserId for c in tr if c.primaryOwnerUserId])
        for c in tr:
            meta = c.sourceMetadata or {}
            rr = rmap.get(c.sourceReferenceId)
            out.append([c.capaNumber, meta.get("treatmentStrategy", ""), rr.riskCode if rr else "",
                        names.get(c.primaryOwnerUserId, "") if c.primaryOwnerUserId else "", c.state,
                        meta.get("completionPercent", 0),
                        c.closureTargetDate.date().isoformat() if c.closureTargetDate else "",
                        meta.get("expectedResidualReduction", ""), meta.get("achievedResidualReduction", "")])

    elif kind == "overdue":
        headers = ["CAPA", "Risk", "Title", "Owner", "Strategy", "State", "Due", "Days Overdue", "% Complete"]
        _open = {"DRAFT", "SUBMITTED", "UNDER_RCA", "ACTIONS_PLANNED", "ACTIONS_IN_PROGRESS", "PENDING_VERIFICATION"}
        tr = (await db.execute(select(Capa).where(Capa.sourceTypeCode == "RISK_TREATMENT").where(Capa.state.in_(_open)))).scalars().all()
        rmap = {r.id: r for r in rows}
        names = await svc.user_name_map(db, [c.primaryOwnerUserId for c in tr if c.primaryOwnerUserId])
        now = _q_now()
        for c in tr:
            if not c.closureTargetDate:
                continue
            due = c.closureTargetDate.replace(tzinfo=timezone.utc) if c.closureTargetDate.tzinfo is None else c.closureTargetDate
            if due >= now:
                continue
            rr = rmap.get(c.sourceReferenceId)
            meta = c.sourceMetadata or {}
            out.append([c.capaNumber, rr.riskCode if rr else "", c.title,
                        names.get(c.primaryOwnerUserId, "") if c.primaryOwnerUserId else "",
                        meta.get("treatmentStrategy", ""), c.state, due.date().isoformat(), (now - due).days,
                        meta.get("completionPercent", 0)])
        out.sort(key=lambda x: -(x[7] if isinstance(x[7], int) else 0))

    elif kind == "department":
        headers = ["Department / Business Unit", "Total", "Critical", "High", "Medium", "Low"]
        agg: dict[str, dict] = {}
        for r in rows:
            if r.lifecycleState == "CLOSED":
                continue
            bu = r.businessUnit or "Unassigned"
            a = agg.setdefault(bu, {"total": 0, "CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0})
            a["total"] += 1
            b = (r.residualBand or "").upper()
            if b in a:
                a[b] += 1
        for bu, a in sorted(agg.items(), key=lambda kv: -kv[1]["total"]):
            out.append([bu, a["total"], a["CRITICAL"], a["HIGH"], a["MEDIUM"], a["LOW"]])

    elif kind == "heatmap":
        headers = ["Likelihood", "Impact", "Score", "Band", "Count", "Risk Codes"]
        bands = await svc.bands_from_active_matrix(db)
        active = [r for r in rows if r.lifecycleState != "CLOSED"]
        code_by_id = {r.id: r.riskCode for r in active}
        for cell in sorted(_heatmap_from(active, "RESIDUAL", bands), key=lambda c: (-c.likelihood, -c.impact)):
            if cell.count == 0:
                continue
            out.append([cell.likelihood, cell.impact, cell.score, cell.band, cell.count,
                        ", ".join(code_by_id.get(i, "") for i in cell.riskIds)])

    elif kind == "escalations":
        headers = ["Code", "Title", "Residual Band", "Escalated At"]
        for r in rows:
            if r.lifecycleState == "ESCALATED" or r.escalatedAt:
                out.append([r.riskCode, r.title, r.residualBand or "", r.escalatedAt.isoformat() if r.escalatedAt else ""])

    elif kind == "acceptances":
        headers = ["Code", "Title", "Justification", "Accepted By", "Accepted At"]
        names = await svc.user_name_map(db, [r.acceptedBy for r in rows if r.acceptedBy])
        for r in rows:
            if r.lifecycleState == "ACCEPTED":
                out.append([r.riskCode, r.title, r.acceptanceJustification or "", names.get(r.acceptedBy, "") if r.acceptedBy else "", r.acceptedAt.isoformat() if r.acceptedAt else ""])
    else:  # pragma: no cover — guarded by _REPORT_KINDS above
        raise HTTPException(404, f"Unknown report kind '{kind}'")

    return title, headers, out


def _report_csv(headers: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for row in rows:
        w.writerow(row)
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens UTF-8 cleanly


def _report_xlsx(title: str, headers: list[str], rows: list[list]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1E3A5F")
    ws.append(headers)
    for ci in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=ci)
        cell.font = header_font
        cell.fill = header_fill
    for row in rows:
        ws.append(["" if v is None else v for v in row])
    for ci, h in enumerate(headers, 1):
        width = max([len(str(h))] + [len(str(r[ci - 1])) for r in rows]) if rows else len(str(h))
        ws.column_dimensions[ws.cell(row=1, column=ci).column_letter].width = min(max(width + 2, 10), 60)
    ws.freeze_panes = "A2"
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def _lat1(v: Any) -> str:
    """fpdf2 core fonts are latin-1 only; ₹, em-dashes etc. would crash them."""
    return ("" if v is None else str(v)).encode("latin-1", "replace").decode("latin-1")


def _report_pdf(title: str, headers: list[str], rows: list[list]) -> bytes:
    from fpdf import FPDF

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, _lat1(f"SafeOps360 — {title}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8)
    pdf.cell(0, 6, _lat1(f"Generated {_q_now().date().isoformat()} · {len(rows)} rows"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    epw = pdf.w - 2 * pdf.l_margin
    col_w = epw / max(len(headers), 1)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(30, 58, 95)
    pdf.set_text_color(255, 255, 255)
    for h in headers:
        pdf.cell(col_w, 7, _lat1(h)[:28], border=1, fill=True)
    pdf.ln()
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 7)
    for row in rows:
        for v in row:
            pdf.cell(col_w, 6, _lat1(v)[:32], border=1)
        pdf.ln()
    return bytes(pdf.output())


@router.get("/reports/{kind}.csv")
async def export_csv(kind: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "ERM.EXPORT")
    _title, headers, rows = await _gather_report(kind, db, user)
    return Response(
        content=_report_csv(headers, rows), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=erm-{kind}.csv"},
    )


@router.get("/reports/{kind}.xlsx")
async def export_xlsx(kind: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "ERM.EXPORT")
    title, headers, rows = await _gather_report(kind, db, user)
    return Response(
        content=_report_xlsx(title, headers, rows),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=erm-{kind}.xlsx"},
    )


@router.get("/reports/{kind}.pdf")
async def export_pdf(kind: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "ERM.EXPORT")
    title, headers, rows = await _gather_report(kind, db, user)
    return Response(
        content=_report_pdf(title, headers, rows), media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=erm-{kind}.pdf"},
    )


# ═════════════════════════════════════════════════════════════════════
# Detail builder
# ═════════════════════════════════════════════════════════════════════
async def _derived_residual_out(db: AsyncSession, r: EnterpriseRisk) -> S.DerivedResidualOut:
    """Assemble the control-DERIVED residual evidence: effectiveness split, derived
    score/band, ₹ reduction the control environment buys (probe 10), and the
    asserted-vs-derived override variance (probe 1)."""
    bands = await svc.bands_from_active_matrix(db)
    eff = await svc.control_effectiveness(db, r.id)
    out = S.DerivedResidualOut(
        inherentLikelihood=r.inherentLikelihood,
        inherentImpact=r.inherentImpact,
        inherentScore=r.inherentScore,
        preventiveEffectivenessPct=round(eff["preventive"] * 100.0, 1),
        mitigatingEffectivenessPct=round(eff["mitigating"] * 100.0, 1),
        combinedEffectivenessPct=round(eff["combined"] * 100.0, 1),
        assertedResidualScore=r.residualScore,
        overrideVariance=r.residualOverrideVariance,
        mappedControlCount=eff["mappedCount"],
        ratedControlCount=eff["ratedCount"],
        backTestedControlCount=eff.get("backTestedCount", 0),
        contributingControls=[S.ControlContribution(**c) for c in eff["contributing"]],
        inherentExpectedLossInr=r.inherentExpectedLossInr,
    )
    if r.inherentLikelihood and r.inherentImpact:
        d = svc.derive_residual_from_controls(r.inherentLikelihood, r.inherentImpact, eff, bands)
        out.derivedLikelihood = d["likelihood"]
        out.derivedImpact = d["impact"]
        out.derivedResidualScore = d["score"]
        out.derivedResidualBand = d["band"]
    if r.inherentExpectedLossInr is not None:
        # Residual EL without controls = inherent EL; with controls = attenuated.
        derived_el = round(r.inherentExpectedLossInr * (1.0 - eff["combined"]))
        out.derivedResidualExpectedLossInr = derived_el
        out.controlRiskReductionInr = round(r.inherentExpectedLossInr - derived_el)
    return out


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

    derived_residual = await _derived_residual_out(db, r)
    tldof_names = await svc.user_name_map(db, [r.firstLineOwnerId or "", r.secondLineOwnerId or ""])

    # I-14: appetite breaches this risk triggered (open/under-review) → detail chip
    open_breaches: list[dict[str, Any]] = []
    try:
        from app.models.erm_p2 import AppetiteBreach
        brs = (
            await db.execute(
                select(AppetiteBreach).where(AppetiteBreach.status.in_(("OPEN", "UNDER_REVIEW", "TREATMENT_MANDATED", "TEMPORARILY_ACCEPTED")))
            )
        ).scalars().all()
        for b in brs:
            if r.id in (b.triggeringEntityIds or []):
                open_breaches.append({"id": b.id, "bandType": b.bandType, "status": b.status,
                                      "observedValue": b.observedValue, "thresholdValue": b.thresholdValue})
    except Exception:
        pass

    detail = S.RiskDetail(
        **base.model_dump(),
        description=r.description,
        tags=r.tags or [], causes=r.causes or [], consequences=r.consequences or [], existingControls=r.existingControls or [],
        identifiedDate=r.identifiedDate, rollupRuleId=r.rollupRuleId,
        closureJustification=r.closureJustification, acceptanceJustification=r.acceptanceJustification,
        acceptedBy=r.acceptedBy, acceptedByName=names.get(r.acceptedBy) if r.acceptedBy else None,
        acceptedAt=r.acceptedAt, escalatedAt=r.escalatedAt, isRollup=(r.sourceType == "HSE_ROLLUP"),
        version=r.version, currentInherent=cur_inh, currentResidual=cur_res,
        targetLikelihood=r.targetLikelihood, targetImpact=r.targetImpact,
        targetDate=r.targetDate, targetRationale=r.targetRationale, controlAlertAt=r.controlAlertAt,
        incidentAlertReason=r.incidentAlertReason, openAppetiteBreaches=open_breaches,
        derivedResidual=derived_residual,
        bowtie=(S.BowtieModel(**r.bowtie) if r.bowtie else None),
        firstLineOwnerId=r.firstLineOwnerId, firstLineOwnerName=tldof_names.get(r.firstLineOwnerId or ""),
        secondLineOwnerId=r.secondLineOwnerId, secondLineOwnerName=tldof_names.get(r.secondLineOwnerId or ""),
        thirdLineAssurance=r.thirdLineAssurance,
        assessmentHistory=[a_out(a) for a in assessments], treatments=treatments,
        linkages=linkage_out, reviews=reviews_out, contributingEntries=contributing, createdAt=r.createdAt,
    )
    return detail
