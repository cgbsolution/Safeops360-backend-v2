"""EPC Mobilization router. Mounts at /api/epc/mobilization.

Controls the contractor worker mobilization lifecycle: initiation → automated
pre-checks → approval → active → demobilised / suspended. Pre-mobilisation
checks run automatically on initiation and their results are stored in the
preMobilisationChecks JSONB field. The /site/{site_id}/roster endpoint provides
the current active headcount at any site.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.epc import (
    ConstructionSite,
    ContractorCompany,
    ContractorWorker,
    MobilizationRecord,
)
from app.models.user import User
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/epc/mobilization", tags=["epc-mobilization"])


# ─── RBAC helper ─────────────────────────────────────────────────────────────


async def _require(db: AsyncSession, user: User, permission_code: str) -> None:
    result = await can(db, user.id, permission_code, PermissionContext())
    if not result.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            result.reason or f"Missing permission {permission_code}",
        )


# ─── Request bodies ───────────────────────────────────────────────────────────


class MobilizationCreate(BaseModel):
    contractorWorkerId: str
    siteId: str
    mobilizationType: str | None = None
    tradeAtSite: str | None = None
    workArea: str | None = None
    mobilisationDate: datetime | None = None
    plannedDemobilisationDate: datetime | None = None
    reportingSupervisorUserId: str | None = None
    contractorCoordinatorUserId: str | None = None


class MobilizationUpdate(BaseModel):
    tradeAtSite: str | None = None
    workArea: str | None = None
    mobilisationDate: datetime | None = None
    plannedDemobilisationDate: datetime | None = None
    reportingSupervisorUserId: str | None = None
    contractorCoordinatorUserId: str | None = None
    approvalConditions: str | None = None


class ApproveBody(BaseModel):
    approvalConditions: str | None = None


class DemobiliseBody(BaseModel):
    reason: str
    notes: str | None = None


class SuspendBody(BaseModel):
    reason: str


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _mob_dict(mob: MobilizationRecord) -> dict:
    return {
        "id": mob.id,
        "tenantId": mob.tenantId,
        "mobilizationNumber": mob.mobilizationNumber,
        "contractorWorkerId": mob.contractorWorkerId,
        "contractorCompanyId": mob.contractorCompanyId,
        "siteId": mob.siteId,
        "mobilizationType": mob.mobilizationType,
        "tradeAtSite": mob.tradeAtSite,
        "workArea": mob.workArea,
        "reportingSupervisorUserId": mob.reportingSupervisorUserId,
        "contractorCoordinatorUserId": mob.contractorCoordinatorUserId,
        "mobilisationDate": mob.mobilisationDate.isoformat() if mob.mobilisationDate else None,
        "plannedDemobilisationDate": (
            mob.plannedDemobilisationDate.isoformat() if mob.plannedDemobilisationDate else None
        ),
        "actualDemobilisationDate": (
            mob.actualDemobilisationDate.isoformat() if mob.actualDemobilisationDate else None
        ),
        "preMobilisationChecks": mob.preMobilisationChecks or {},
        "status": mob.status,
        "approvedById": mob.approvedById,
        "approvedAt": mob.approvedAt.isoformat() if mob.approvedAt else None,
        "approvalConditions": mob.approvalConditions,
        "demobilisationReason": mob.demobilisationReason,
        "demobilisationClearance": mob.demobilisationClearance,
        "performanceNotes": mob.performanceNotes or [],
        "createdAt": mob.createdAt.isoformat() if mob.createdAt else None,
        "updatedAt": mob.updatedAt.isoformat() if mob.updatedAt else None,
        "createdById": mob.createdById,
    }


async def _generate_mob_number(db: AsyncSession, site_code: str) -> str:
    year = datetime.now(timezone.utc).year
    count_result = await db.execute(select(func.count(MobilizationRecord.id)))
    count = (count_result.scalar_one() or 0) + 1
    return f"MOB-{site_code}-{year}-{count:04d}"


async def _run_pre_checks(
    db: AsyncSession,
    worker: ContractorWorker,
    company: ContractorCompany,
    site_id: str,
) -> dict:
    """Run automated pre-mobilisation checks. Returns {check_name: {result, detail}} dict."""
    now = datetime.now(timezone.utc)
    checks: dict[str, dict] = {}

    # 1. contractor_company_active
    approved_statuses = {"approved", "conditionally_approved"}
    checks["contractor_company_active"] = {
        "result": "pass" if company.prequalificationStatus in approved_statuses else "fail",
        "detail": f"Company prequalification status: {company.prequalificationStatus}",
        "checkedAt": now.isoformat(),
    }

    # 2. medical_fitness_valid
    if worker.currentMedicalValidUntil:
        med_valid = worker.currentMedicalValidUntil
        if med_valid.tzinfo is None:
            med_valid = med_valid.replace(tzinfo=timezone.utc)
        checks["medical_fitness_valid"] = {
            "result": "pass" if med_valid > now else "fail",
            "detail": (
                f"Medical certificate valid until {med_valid.date().isoformat()}"
                if med_valid > now
                else f"Medical certificate expired on {med_valid.date().isoformat()}"
            ),
            "checkedAt": now.isoformat(),
        }
    else:
        checks["medical_fitness_valid"] = {
            "result": "fail",
            "detail": "No medical fitness certificate on record",
            "checkedAt": now.isoformat(),
        }

    # 3. worker_not_suspended
    checks["worker_not_suspended"] = {
        "result": "pass" if worker.overallStatus == "active" else "fail",
        "detail": (
            "Worker is active"
            if worker.overallStatus == "active"
            else f"Worker status is {worker.overallStatus}"
        ),
        "checkedAt": now.isoformat(),
    }

    # 4. existing_active_mobilization
    existing_active = (
        await db.execute(
            select(MobilizationRecord).where(
                MobilizationRecord.contractorWorkerId == worker.id,
                MobilizationRecord.siteId == site_id,
                MobilizationRecord.status == "active",
            )
        )
    ).scalar_one_or_none()
    checks["existing_active_mobilization"] = {
        "result": "fail" if existing_active else "pass",
        "detail": (
            f"Worker already has an active mobilization at this site "
            f"(mob: {existing_active.mobilizationNumber})"
            if existing_active
            else "No conflicting active mobilization"
        ),
        "checkedAt": now.isoformat(),
    }

    return checks


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get("/")
async def list_mobilizations(
    siteId: str | None = Query(None),
    contractorCompanyId: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.READ")
    stmt = select(MobilizationRecord)
    if siteId:
        stmt = stmt.where(MobilizationRecord.siteId == siteId)
    if contractorCompanyId:
        stmt = stmt.where(MobilizationRecord.contractorCompanyId == contractorCompanyId)
    if status_filter:
        stmt = stmt.where(MobilizationRecord.status == status_filter)
    mobs = (
        await db.execute(stmt.order_by(MobilizationRecord.createdAt.desc()))
    ).scalars().all()
    return {"count": len(mobs), "mobilizations": [_mob_dict(m) for m in mobs]}


@router.post("/", status_code=status.HTTP_201_CREATED)
async def initiate_mobilization(
    body: MobilizationCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.CREATE")

    worker = await db.get(ContractorWorker, body.contractorWorkerId)
    if worker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contractor worker not found")

    site = await db.get(ConstructionSite, body.siteId)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Construction site not found")

    company = await db.get(ContractorCompany, worker.contractorCompanyId)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contractor company not found")

    # Run automated checks
    checks = await _run_pre_checks(db, worker, company, body.siteId)

    # Determine status based on check results
    all_pass = all(c["result"] == "pass" for c in checks.values())
    any_fail = any(c["result"] == "fail" for c in checks.values())

    if any_fail:
        mob_status = "pending_checks"
    else:
        mob_status = "checks_complete_pending_approval"

    mob_number = await _generate_mob_number(db, site.siteCode)

    mob = MobilizationRecord(
        mobilizationNumber=mob_number,
        contractorWorkerId=body.contractorWorkerId,
        contractorCompanyId=worker.contractorCompanyId,
        siteId=body.siteId,
        mobilizationType=body.mobilizationType,
        tradeAtSite=body.tradeAtSite or worker.primaryTrade,
        workArea=body.workArea,
        reportingSupervisorUserId=body.reportingSupervisorUserId,
        contractorCoordinatorUserId=body.contractorCoordinatorUserId,
        mobilisationDate=body.mobilisationDate,
        plannedDemobilisationDate=body.plannedDemobilisationDate,
        preMobilisationChecks=checks,
        status=mob_status,
        createdById=user.id,
    )
    db.add(mob)
    await db.commit()
    await db.refresh(mob)

    result = _mob_dict(mob)
    result["checksRan"] = len(checks)
    result["checksFailed"] = sum(1 for c in checks.values() if c["result"] == "fail")
    return result


@router.get("/site/{site_id}/roster")
async def site_roster(
    site_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Current active worker roster at a site (active mobilizations only)."""
    await _require(db, user, "EPC.READ")
    site = await db.get(ConstructionSite, site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Construction site not found")

    mobs = (
        await db.execute(
            select(MobilizationRecord).where(
                MobilizationRecord.siteId == site_id,
                MobilizationRecord.status == "active",
            )
        )
    ).scalars().all()

    roster = []
    for m in mobs:
        worker = await db.get(ContractorWorker, m.contractorWorkerId)
        company = await db.get(ContractorCompany, m.contractorCompanyId)
        roster.append({
            "mobilizationId": m.id,
            "mobilizationNumber": m.mobilizationNumber,
            "workerId": m.contractorWorkerId,
            "workerCode": worker.workerCode if worker else None,
            "workerName": worker.fullName if worker else None,
            "primaryTrade": m.tradeAtSite,
            "workArea": m.workArea,
            "companyName": company.name if company else None,
            "mobilisationDate": m.mobilisationDate.isoformat() if m.mobilisationDate else None,
            "plannedDemobilisationDate": (
                m.plannedDemobilisationDate.isoformat() if m.plannedDemobilisationDate else None
            ),
        })

    return {
        "siteId": site_id,
        "siteCode": site.siteCode,
        "siteName": site.siteName,
        "activeWorkerCount": len(roster),
        "roster": roster,
    }


@router.get("/{mob_id}")
async def get_mobilization(
    mob_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.READ")
    mob = await db.get(MobilizationRecord, mob_id)
    if mob is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mobilization record not found")
    return _mob_dict(mob)


@router.patch("/{mob_id}")
async def update_mobilization(
    mob_id: str,
    body: MobilizationUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.UPDATE")
    mob = await db.get(MobilizationRecord, mob_id)
    if mob is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mobilization record not found")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(mob, field, value)

    await db.commit()
    await db.refresh(mob)
    return _mob_dict(mob)


@router.post("/{mob_id}/approve")
async def approve_mobilization(
    mob_id: str,
    body: ApproveBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.UPDATE")
    mob = await db.get(MobilizationRecord, mob_id)
    if mob is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mobilization record not found")

    if mob.status not in ("pending_checks", "checks_complete_pending_approval"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot approve mobilization in status '{mob.status}'",
        )

    mob.status = "active"
    mob.approvedById = user.id
    mob.approvedAt = datetime.now(timezone.utc)
    if body.approvalConditions:
        mob.approvalConditions = body.approvalConditions

    await db.commit()
    await db.refresh(mob)
    return _mob_dict(mob)


@router.post("/{mob_id}/demobilise")
async def demobilise(
    mob_id: str,
    body: DemobiliseBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.UPDATE")
    mob = await db.get(MobilizationRecord, mob_id)
    if mob is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mobilization record not found")

    if mob.status not in ("active", "suspended"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot demobilise from status '{mob.status}'",
        )

    mob.status = "demobilised"
    mob.demobilisationReason = body.reason
    mob.actualDemobilisationDate = datetime.now(timezone.utc)
    if body.notes:
        mob.performanceNotes = (mob.performanceNotes or []) + [
            {"note": body.notes, "addedById": user.id, "addedAt": datetime.now(timezone.utc).isoformat()}
        ]

    await db.commit()
    await db.refresh(mob)
    return _mob_dict(mob)


@router.post("/{mob_id}/suspend")
async def suspend_mobilization(
    mob_id: str,
    body: SuspendBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.UPDATE")
    mob = await db.get(MobilizationRecord, mob_id)
    if mob is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mobilization record not found")

    if mob.status != "active":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot suspend mobilization in status '{mob.status}'",
        )

    mob.status = "suspended"
    mob.performanceNotes = (mob.performanceNotes or []) + [
        {
            "note": f"Suspended: {body.reason}",
            "addedById": user.id,
            "addedAt": datetime.now(timezone.utc).isoformat(),
            "type": "suspension",
        }
    ]

    await db.commit()
    await db.refresh(mob)
    return _mob_dict(mob)

