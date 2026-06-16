"""EPC Sites router. Mounts at /api/epc/sites.

CRUD for ConstructionSite records. Each site has a unique siteCode, tracks the
full project lifecycle (awarded → active → completed / suspended), and exposes
a computed currentWorkforceCount from active MobilizationRecords. Workforce
breakdown is available via the /workforce-count sub-resource.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.epc import ConstructionSite, MobilizationRecord
from app.models.user import User
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/epc/sites", tags=["epc-sites"])


# ─── RBAC helper ─────────────────────────────────────────────────────────────


async def _require(db: AsyncSession, user: User, permission_code: str) -> None:
    result = await can(db, user.id, permission_code, PermissionContext())
    if not result.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            result.reason or f"Missing permission {permission_code}",
        )


# ─── Request bodies ───────────────────────────────────────────────────────────


class SiteCreate(BaseModel):
    siteName: str
    siteCode: str | None = None
    projectNumber: str | None = None
    clientName: str | None = None
    clientContactName: str | None = None
    clientContactEmail: str | None = None
    clientProjectManager: str | None = None
    clientSafetyDocUrl: str | None = None
    address: str | None = None
    district: str | None = None
    state: str | None = None
    lat: float | None = None
    lng: float | None = None
    projectType: str | None = None
    scopeDescription: str | None = None
    contractValue: float | None = None
    contractCurrency: str = "INR"
    status: str = "awarded_setup"
    awardDate: datetime | None = None
    plannedStartDate: datetime | None = None
    plannedCompletionDate: datetime | None = None
    peakWorkforcePlanned: int = 0
    siteManagerUserId: str | None = None
    siteHseManagerUserId: str | None = None
    siteQualityManagerUserId: str | None = None
    corporateHseOwnerUserId: str | None = None
    statutoryApprovals: list = []


class SiteUpdate(BaseModel):
    siteName: str | None = None
    projectNumber: str | None = None
    clientName: str | None = None
    clientContactName: str | None = None
    clientContactEmail: str | None = None
    clientProjectManager: str | None = None
    clientSafetyDocUrl: str | None = None
    address: str | None = None
    district: str | None = None
    state: str | None = None
    lat: float | None = None
    lng: float | None = None
    projectType: str | None = None
    scopeDescription: str | None = None
    contractValue: float | None = None
    contractCurrency: str | None = None
    status: str | None = None
    awardDate: datetime | None = None
    plannedStartDate: datetime | None = None
    plannedCompletionDate: datetime | None = None
    actualStartDate: datetime | None = None
    actualCompletionDate: datetime | None = None
    peakWorkforcePlanned: int | None = None
    siteManagerUserId: str | None = None
    siteHseManagerUserId: str | None = None
    siteQualityManagerUserId: str | None = None
    corporateHseOwnerUserId: str | None = None
    statutoryApprovals: list | None = None


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _site_dict(site: ConstructionSite, workforce_count: int = 0) -> dict:
    return {
        "id": site.id,
        "tenantId": site.tenantId,
        "siteCode": site.siteCode,
        "siteName": site.siteName,
        "projectNumber": site.projectNumber,
        "clientName": site.clientName,
        "clientContactName": site.clientContactName,
        "clientContactEmail": site.clientContactEmail,
        "clientProjectManager": site.clientProjectManager,
        "clientSafetyDocUrl": site.clientSafetyDocUrl,
        "address": site.address,
        "district": site.district,
        "state": site.state,
        "lat": site.lat,
        "lng": site.lng,
        "projectType": site.projectType,
        "scopeDescription": site.scopeDescription,
        "contractValue": site.contractValue,
        "contractCurrency": site.contractCurrency,
        "status": site.status,
        "awardDate": site.awardDate.isoformat() if site.awardDate else None,
        "plannedStartDate": site.plannedStartDate.isoformat() if site.plannedStartDate else None,
        "plannedCompletionDate": site.plannedCompletionDate.isoformat() if site.plannedCompletionDate else None,
        "actualStartDate": site.actualStartDate.isoformat() if site.actualStartDate else None,
        "actualCompletionDate": site.actualCompletionDate.isoformat() if site.actualCompletionDate else None,
        "peakWorkforcePlanned": site.peakWorkforcePlanned,
        "siteManagerUserId": site.siteManagerUserId,
        "siteHseManagerUserId": site.siteHseManagerUserId,
        "siteQualityManagerUserId": site.siteQualityManagerUserId,
        "corporateHseOwnerUserId": site.corporateHseOwnerUserId,
        "statutoryApprovals": site.statutoryApprovals or [],
        "currentWorkforceCount": workforce_count,
        "createdAt": site.createdAt.isoformat() if site.createdAt else None,
        "updatedAt": site.updatedAt.isoformat() if site.updatedAt else None,
        "createdById": site.createdById,
    }


async def _active_workforce_count(db: AsyncSession, site_id: str) -> int:
    result = await db.execute(
        select(func.count(MobilizationRecord.id)).where(
            MobilizationRecord.siteId == site_id,
            MobilizationRecord.status == "active",
        )
    )
    return result.scalar_one() or 0


async def _generate_site_code(db: AsyncSession) -> str:
    count_result = await db.execute(select(func.count(ConstructionSite.id)))
    count = (count_result.scalar_one() or 0) + 1
    return f"SITE-{count:04d}"


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get("/")
async def list_sites(
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.READ")
    stmt = select(ConstructionSite)
    if status_filter:
        stmt = stmt.where(ConstructionSite.status == status_filter)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            ConstructionSite.siteName.ilike(pattern)
            | ConstructionSite.siteCode.ilike(pattern)
        )
    sites = (await db.execute(stmt.order_by(ConstructionSite.createdAt.desc()))).scalars().all()

    out: list[dict] = []
    for site in sites:
        count = await _active_workforce_count(db, site.id)
        out.append(_site_dict(site, count))
    return {"count": len(out), "sites": out}


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_site(
    body: SiteCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.CREATE")

    site_code = body.siteCode
    if not site_code:
        site_code = await _generate_site_code(db)

    # Check for duplicate siteCode
    existing = (
        await db.execute(select(ConstructionSite).where(ConstructionSite.siteCode == site_code))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Site code '{site_code}' already exists")

    site = ConstructionSite(
        siteCode=site_code,
        siteName=body.siteName,
        projectNumber=body.projectNumber,
        clientName=body.clientName,
        clientContactName=body.clientContactName,
        clientContactEmail=body.clientContactEmail,
        clientProjectManager=body.clientProjectManager,
        clientSafetyDocUrl=body.clientSafetyDocUrl,
        address=body.address,
        district=body.district,
        state=body.state,
        lat=body.lat,
        lng=body.lng,
        projectType=body.projectType,
        scopeDescription=body.scopeDescription,
        contractValue=body.contractValue,
        contractCurrency=body.contractCurrency,
        status=body.status,
        awardDate=body.awardDate,
        plannedStartDate=body.plannedStartDate,
        plannedCompletionDate=body.plannedCompletionDate,
        peakWorkforcePlanned=body.peakWorkforcePlanned,
        siteManagerUserId=body.siteManagerUserId,
        siteHseManagerUserId=body.siteHseManagerUserId,
        siteQualityManagerUserId=body.siteQualityManagerUserId,
        corporateHseOwnerUserId=body.corporateHseOwnerUserId,
        statutoryApprovals=body.statutoryApprovals or [],
        createdById=user.id,
    )
    db.add(site)
    await db.commit()
    await db.refresh(site)
    return _site_dict(site, 0)


@router.get("/{site_id}")
async def get_site(
    site_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.READ")
    site = await db.get(ConstructionSite, site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Construction site not found")
    count = await _active_workforce_count(db, site_id)
    return _site_dict(site, count)


@router.patch("/{site_id}")
async def update_site(
    site_id: str,
    body: SiteUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.UPDATE")
    site = await db.get(ConstructionSite, site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Construction site not found")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(site, field, value)

    await db.commit()
    await db.refresh(site)
    count = await _active_workforce_count(db, site_id)
    return _site_dict(site, count)


@router.get("/{site_id}/workforce-count")
async def workforce_count(
    site_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.READ")
    site = await db.get(ConstructionSite, site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Construction site not found")

    active_result = await db.execute(
        select(func.count(MobilizationRecord.id)).where(
            MobilizationRecord.siteId == site_id,
            MobilizationRecord.status == "active",
        )
    )
    active = active_result.scalar_one() or 0

    pending_result = await db.execute(
        select(func.count(MobilizationRecord.id)).where(
            MobilizationRecord.siteId == site_id,
            MobilizationRecord.status.in_([
                "pending_checks",
                "checks_complete_pending_approval",
            ]),
        )
    )
    pending = pending_result.scalar_one() or 0

    total_result = await db.execute(
        select(func.count(MobilizationRecord.id)).where(
            MobilizationRecord.siteId == site_id,
        )
    )
    total = total_result.scalar_one() or 0

    return {"siteId": site_id, "active": active, "pending": pending, "total": total}

