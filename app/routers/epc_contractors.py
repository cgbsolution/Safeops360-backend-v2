"""EPC Contractor Companies router. Mounts at /api/epc/contractors.

Manages ContractorCompany records including prequalification lifecycle. The
prequalification gate controls whether a company's workers can be mobilized
to construction sites. Performance summary aggregates all companies for the
corporate dashboard view.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.epc import ContractorCompany, ContractorWorker
from app.models.user import User
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/epc/contractors", tags=["epc-contractors"])


# ─── RBAC helper ─────────────────────────────────────────────────────────────


async def _require(db: AsyncSession, user: User, permission_code: str) -> None:
    result = await can(db, user.id, permission_code, PermissionContext())
    if not result.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            result.reason or f"Missing permission {permission_code}",
        )


# ─── Request bodies ───────────────────────────────────────────────────────────


class ContractorCompanyCreate(BaseModel):
    name: str
    code: str | None = None
    tradeName: str | None = None
    registrationNumber: str | None = None
    panNumber: str | None = None
    gstNumber: str | None = None
    tradeCategories: list = []
    sizeCategory: str | None = None
    representativeName: str | None = None
    representativePhone: str | None = None
    representativeEmail: str | None = None
    safetyOfficerName: str | None = None
    safetyOfficerPhone: str | None = None
    complianceDocuments: list = []


class ContractorCompanyUpdate(BaseModel):
    name: str | None = None
    tradeName: str | None = None
    registrationNumber: str | None = None
    panNumber: str | None = None
    gstNumber: str | None = None
    tradeCategories: list | None = None
    sizeCategory: str | None = None
    representativeName: str | None = None
    representativePhone: str | None = None
    representativeEmail: str | None = None
    safetyOfficerName: str | None = None
    safetyOfficerPhone: str | None = None
    complianceDocuments: list | None = None


class PrequalifyBody(BaseModel):
    status: str
    score: float | None = None
    validUntil: str | None = None  # ISO date string


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _company_dict(company: ContractorCompany, worker_count: int = 0) -> dict:
    return {
        "id": company.id,
        "companyCode": company.code,
        "companyName": company.name,
        "tradeName": company.tradeName,
        "registrationNumber": company.registrationNumber,
        "panNumber": company.panNumber,
        "gstNumber": company.gstNumber,
        "tradeCategories": company.tradeCategories or [],
        "sizeCategory": company.sizeCategory,
        "prequalificationStatus": company.prequalificationStatus,
        "prequalificationValidUntil": (
            company.prequalificationValidUntil.isoformat()
            if company.prequalificationValidUntil
            else None
        ),
        "prequalificationScore": company.prequalificationScore,
        "prequalificationReviewedById": company.prequalificationReviewedById,
        "prequalificationReviewedAt": (
            company.prequalificationReviewedAt.isoformat()
            if company.prequalificationReviewedAt
            else None
        ),
        "complianceDocuments": company.complianceDocuments or [],
        "representativeName": company.representativeName,
        "representativePhone": company.representativePhone,
        "representativeEmail": company.representativeEmail,
        "safetyOfficerName": company.safetyOfficerName,
        "safetyOfficerPhone": company.safetyOfficerPhone,
        "suspensionHistory": company.suspensionHistory or [],
        "workerCount": worker_count,
        "createdAt": company.createdAt.isoformat() if company.createdAt else None,
        "updatedAt": company.updatedAt.isoformat() if company.updatedAt else None,
    }


async def _generate_company_code(db: AsyncSession) -> str:
    count_result = await db.execute(select(func.count(ContractorCompany.id)))
    count = (count_result.scalar_one() or 0) + 1
    return f"CC-{count:04d}"


async def _get_worker_count(db: AsyncSession, company_id: str) -> int:
    result = await db.execute(
        select(func.count(ContractorWorker.id)).where(
            ContractorWorker.contractorCompanyId == company_id
        )
    )
    return result.scalar_one() or 0


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get("/performance-summary")
async def performance_summary(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Aggregate performance across all contractor companies (top 10 and bottom 10 by score)."""
    await _require(db, user, "EPC.READ")

    companies = (
        await db.execute(
            select(ContractorCompany).where(
                ContractorCompany.prequalificationScore.isnot(None)
            )
        )
    ).scalars().all()

    scored = sorted(
        [c for c in companies if c.prequalificationScore is not None],
        key=lambda c: c.prequalificationScore or 0,
        reverse=True,
    )

    def _slim(c: ContractorCompany) -> dict:
        return {
            "id": c.id,
            "companyCode": c.code,
            "companyName": c.name,
            "prequalificationScore": c.prequalificationScore,
            "prequalificationStatus": c.prequalificationStatus,
            "prequalificationValidUntil": (
                c.prequalificationValidUntil.isoformat()
                if c.prequalificationValidUntil
                else None
            ),
        }

    top10 = [_slim(c) for c in scored[:10]]
    bottom10 = [_slim(c) for c in scored[-10:][::-1]] if len(scored) > 10 else []

    unscored_count_result = await db.execute(
        select(func.count(ContractorCompany.id)).where(
            ContractorCompany.prequalificationScore.is_(None)
        )
    )
    unscored = unscored_count_result.scalar_one() or 0

    return {
        "totalCompanies": len(companies) + unscored,
        "scoredCompanies": len(companies),
        "unscoredCompanies": unscored,
        "top10": top10,
        "bottom10": bottom10,
    }


@router.get("/")
async def list_contractors(
    prequalificationStatus: str | None = Query(None),
    search: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.READ")
    stmt = select(ContractorCompany)
    if prequalificationStatus:
        stmt = stmt.where(ContractorCompany.prequalificationStatus == prequalificationStatus)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            ContractorCompany.name.ilike(pattern)
            | ContractorCompany.code.ilike(pattern)
        )
    companies = (
        await db.execute(stmt.order_by(ContractorCompany.name.asc()))
    ).scalars().all()

    out: list[dict] = []
    for company in companies:
        wc = await _get_worker_count(db, company.id)
        out.append(_company_dict(company, wc))
    return {"count": len(out), "contractors": out}


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_contractor(
    body: ContractorCompanyCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.CREATE")

    company_code = body.code
    if not company_code:
        company_code = await _generate_company_code(db)

    existing = (
        await db.execute(
            select(ContractorCompany).where(ContractorCompany.code == company_code)
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Company code '{company_code}' already exists"
        )

    company = ContractorCompany(
        code=company_code,
        name=body.name,
        tradeName=body.tradeName,
        registrationNumber=body.registrationNumber,
        panNumber=body.panNumber,
        gstNumber=body.gstNumber,
        tradeCategories=body.tradeCategories or [],
        sizeCategory=body.sizeCategory,
        representativeName=body.representativeName,
        representativePhone=body.representativePhone,
        representativeEmail=body.representativeEmail,
        safetyOfficerName=body.safetyOfficerName,
        safetyOfficerPhone=body.safetyOfficerPhone,
        complianceDocuments=body.complianceDocuments or [],
    )
    db.add(company)
    await db.commit()
    await db.refresh(company)
    return _company_dict(company, 0)


@router.get("/{company_id}")
async def get_contractor(
    company_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.READ")
    company = await db.get(ContractorCompany, company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contractor company not found")
    wc = await _get_worker_count(db, company_id)
    return _company_dict(company, wc)


@router.patch("/{company_id}")
async def update_contractor(
    company_id: str,
    body: ContractorCompanyUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.UPDATE")
    company = await db.get(ContractorCompany, company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contractor company not found")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(company, field, value)

    await db.commit()
    await db.refresh(company)
    wc = await _get_worker_count(db, company_id)
    return _company_dict(company, wc)


@router.post("/{company_id}/prequalify")
async def prequalify_contractor(
    company_id: str,
    body: PrequalifyBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.UPDATE")
    company = await db.get(ContractorCompany, company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contractor company not found")

    company.prequalificationStatus = body.status
    if body.score is not None:
        company.prequalificationScore = body.score
    if body.validUntil:
        try:
            company.prequalificationValidUntil = datetime.fromisoformat(body.validUntil).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid validUntil date format")
    company.prequalificationReviewedById = user.id
    company.prequalificationReviewedAt = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(company)
    wc = await _get_worker_count(db, company_id)
    return _company_dict(company, wc)


@router.get("/{company_id}/workers")
async def list_company_workers(
    company_id: str,
    search: str | None = Query(None),
    overallStatus: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.READ")
    company = await db.get(ContractorCompany, company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contractor company not found")

    stmt = select(ContractorWorker).where(
        ContractorWorker.contractorCompanyId == company_id
    )
    if overallStatus:
        stmt = stmt.where(ContractorWorker.overallStatus == overallStatus)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            ContractorWorker.fullName.ilike(pattern)
            | ContractorWorker.workerCode.ilike(pattern)
        )
    workers = (await db.execute(stmt.order_by(ContractorWorker.fullName.asc()))).scalars().all()

    out = [
        {
            "id": w.id,
            "workerCode": w.workerCode,
            "fullName": w.fullName,
            "primaryTrade": w.primaryTrade,
            "overallStatus": w.overallStatus,
            "gender": w.gender,
            "mobileNumber": w.mobileNumber,
            "currentMedicalValidUntil": (
                w.currentMedicalValidUntil.isoformat() if w.currentMedicalValidUntil else None
            ),
            "biometricEnrolled": w.biometricEnrolled,
            "createdAt": w.createdAt.isoformat() if w.createdAt else None,
        }
        for w in workers
    ]
    return {"companyId": company_id, "count": len(out), "workers": out}

