"""EPC Contractor Workers router. Mounts at /api/epc/workers.

Manages ContractorWorker records. Workers are registered once and can be
mobilized to multiple sites over time. Duplicate detection uses aadhaarLast4 +
contractorCompanyId to catch duplicate registrations. The /gate-status endpoint
performs a dry-run clearance check to preview what would happen at the gate
without creating any records.
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
    ContractorCompany,
    ContractorWorker,
    GateClearanceCheck,
    MobilizationRecord,
    SiteInduction,
)
from app.models.user import User
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/epc/workers", tags=["epc-workers"])


# ─── RBAC helper ─────────────────────────────────────────────────────────────


async def _require(db: AsyncSession, user: User, permission_code: str) -> None:
    result = await can(db, user.id, permission_code, PermissionContext())
    if not result.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            result.reason or f"Missing permission {permission_code}",
        )


# ─── Request bodies ───────────────────────────────────────────────────────────


class WorkerCreate(BaseModel):
    contractorCompanyId: str
    fullName: str
    workerCode: str | None = None
    dateOfBirth: datetime | None = None
    gender: str | None = None
    bloodGroup: str | None = None
    photoUrl: str | None = None
    aadhaarLast4: str | None = None
    panNumber: str | None = None
    pfUanNumber: str | None = None
    esicNumber: str | None = None
    mobileNumber: str | None = None
    emergencyContactName: str | None = None
    emergencyContactPhone: str | None = None
    emergencyContactRelation: str | None = None
    homeAddress: str | None = None
    homeDistrict: str | None = None
    homeState: str | None = None
    primaryTrade: str | None = None
    secondaryTrades: list = []
    yearsExperience: int | None = None
    educationLevel: str | None = None
    itiTrade: str | None = None
    itiCertificateUrl: str | None = None
    currentMedicalValidUntil: datetime | None = None


class WorkerUpdate(BaseModel):
    fullName: str | None = None
    dateOfBirth: datetime | None = None
    gender: str | None = None
    bloodGroup: str | None = None
    photoUrl: str | None = None
    aadhaarLast4: str | None = None
    aadhaarVerified: bool | None = None
    panNumber: str | None = None
    pfUanNumber: str | None = None
    esicNumber: str | None = None
    mobileNumber: str | None = None
    emergencyContactName: str | None = None
    emergencyContactPhone: str | None = None
    emergencyContactRelation: str | None = None
    homeAddress: str | None = None
    homeDistrict: str | None = None
    homeState: str | None = None
    primaryTrade: str | None = None
    secondaryTrades: list | None = None
    yearsExperience: int | None = None
    educationLevel: str | None = None
    itiTrade: str | None = None
    itiCertificateUrl: str | None = None
    currentMedicalValidUntil: datetime | None = None
    overallStatus: str | None = None
    biometricEnrolled: bool | None = None
    medicalFitnessRecords: list | None = None


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _worker_dict(worker: ContractorWorker, active_mobilizations: int = 0) -> dict:
    return {
        "id": worker.id,
        "tenantId": worker.tenantId,
        "contractorCompanyId": worker.contractorCompanyId,
        "workerCode": worker.workerCode,
        "fullName": worker.fullName,
        "dateOfBirth": worker.dateOfBirth.isoformat() if worker.dateOfBirth else None,
        "gender": worker.gender,
        "bloodGroup": worker.bloodGroup,
        "photoUrl": worker.photoUrl,
        "aadhaarLast4": worker.aadhaarLast4,
        "aadhaarVerified": worker.aadhaarVerified,
        "panNumber": worker.panNumber,
        "pfUanNumber": worker.pfUanNumber,
        "esicNumber": worker.esicNumber,
        "mobileNumber": worker.mobileNumber,
        "emergencyContactName": worker.emergencyContactName,
        "emergencyContactPhone": worker.emergencyContactPhone,
        "emergencyContactRelation": worker.emergencyContactRelation,
        "homeAddress": worker.homeAddress,
        "homeDistrict": worker.homeDistrict,
        "homeState": worker.homeState,
        "primaryTrade": worker.primaryTrade,
        "secondaryTrades": worker.secondaryTrades or [],
        "yearsExperience": worker.yearsExperience,
        "educationLevel": worker.educationLevel,
        "itiTrade": worker.itiTrade,
        "itiCertificateUrl": worker.itiCertificateUrl,
        "medicalFitnessRecords": worker.medicalFitnessRecords or [],
        "currentMedicalValidUntil": (
            worker.currentMedicalValidUntil.isoformat() if worker.currentMedicalValidUntil else None
        ),
        "overallStatus": worker.overallStatus,
        "biometricEnrolled": worker.biometricEnrolled,
        "activeMobilizations": active_mobilizations,
        "createdAt": worker.createdAt.isoformat() if worker.createdAt else None,
        "updatedAt": worker.updatedAt.isoformat() if worker.updatedAt else None,
        "createdById": worker.createdById,
    }


async def _generate_worker_code(db: AsyncSession) -> str:
    year = datetime.now(timezone.utc).year
    count_result = await db.execute(select(func.count(ContractorWorker.id)))
    count = (count_result.scalar_one() or 0) + 1
    return f"CW-{year}-{count:04d}"


async def _active_mob_count(db: AsyncSession, worker_id: str) -> int:
    result = await db.execute(
        select(func.count(MobilizationRecord.id)).where(
            MobilizationRecord.contractorWorkerId == worker_id,
            MobilizationRecord.status == "active",
        )
    )
    return result.scalar_one() or 0


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get("/")
async def list_workers(
    contractorCompanyId: str | None = Query(None),
    search: str | None = Query(None),
    overallStatus: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.READ")
    stmt = select(ContractorWorker)
    if contractorCompanyId:
        stmt = stmt.where(ContractorWorker.contractorCompanyId == contractorCompanyId)
    if overallStatus:
        stmt = stmt.where(ContractorWorker.overallStatus == overallStatus)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            ContractorWorker.fullName.ilike(pattern)
            | ContractorWorker.workerCode.ilike(pattern)
        )
    workers = (await db.execute(stmt.order_by(ContractorWorker.fullName.asc()))).scalars().all()

    out: list[dict] = []
    for w in workers:
        amc = await _active_mob_count(db, w.id)
        out.append(_worker_dict(w, amc))
    return {"count": len(out), "workers": out}


@router.post("/", status_code=status.HTTP_201_CREATED)
async def register_worker(
    body: WorkerCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.CREATE")

    # Verify contractor company exists
    company = await db.get(ContractorCompany, body.contractorCompanyId)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contractor company not found")

    # Duplicate detection: same aadhaarLast4 within same company
    if body.aadhaarLast4:
        existing = (
            await db.execute(
                select(ContractorWorker).where(
                    ContractorWorker.contractorCompanyId == body.contractorCompanyId,
                    ContractorWorker.aadhaarLast4 == body.aadhaarLast4,
                )
            )
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"A worker with Aadhaar last 4 '{body.aadhaarLast4}' already exists "
                f"in this contractor company (workerCode: {existing.workerCode})",
            )

    worker_code = body.workerCode
    if not worker_code:
        worker_code = await _generate_worker_code(db)

    # Check uniqueness of workerCode
    existing_code = (
        await db.execute(
            select(ContractorWorker).where(ContractorWorker.workerCode == worker_code)
        )
    ).scalar_one_or_none()
    if existing_code:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Worker code '{worker_code}' already exists"
        )

    worker = ContractorWorker(
        contractorCompanyId=body.contractorCompanyId,
        workerCode=worker_code,
        fullName=body.fullName,
        dateOfBirth=body.dateOfBirth,
        gender=body.gender,
        bloodGroup=body.bloodGroup,
        photoUrl=body.photoUrl,
        aadhaarLast4=body.aadhaarLast4,
        panNumber=body.panNumber,
        pfUanNumber=body.pfUanNumber,
        esicNumber=body.esicNumber,
        mobileNumber=body.mobileNumber,
        emergencyContactName=body.emergencyContactName,
        emergencyContactPhone=body.emergencyContactPhone,
        emergencyContactRelation=body.emergencyContactRelation,
        homeAddress=body.homeAddress,
        homeDistrict=body.homeDistrict,
        homeState=body.homeState,
        primaryTrade=body.primaryTrade,
        secondaryTrades=body.secondaryTrades or [],
        yearsExperience=body.yearsExperience,
        educationLevel=body.educationLevel,
        itiTrade=body.itiTrade,
        itiCertificateUrl=body.itiCertificateUrl,
        currentMedicalValidUntil=body.currentMedicalValidUntil,
        createdById=user.id,
    )
    db.add(worker)
    await db.commit()
    await db.refresh(worker)
    return _worker_dict(worker, 0)


@router.get("/{worker_id}")
async def get_worker(
    worker_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.READ")
    worker = await db.get(ContractorWorker, worker_id)
    if worker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Worker not found")

    # Mobilization history
    mobilizations = (
        await db.execute(
            select(MobilizationRecord)
            .where(MobilizationRecord.contractorWorkerId == worker_id)
            .order_by(MobilizationRecord.createdAt.desc())
        )
    ).scalars().all()

    # Induction records
    inductions = (
        await db.execute(
            select(SiteInduction)
            .where(SiteInduction.contractorWorkerId == worker_id)
            .order_by(SiteInduction.conductedAt.desc())
        )
    ).scalars().all()

    active_mobs = sum(1 for m in mobilizations if m.status == "active")
    detail = _worker_dict(worker, active_mobs)
    detail["mobilizationHistory"] = [
        {
            "id": m.id,
            "mobilizationNumber": m.mobilizationNumber,
            "siteId": m.siteId,
            "status": m.status,
            "tradeAtSite": m.tradeAtSite,
            "mobilisationDate": m.mobilisationDate.isoformat() if m.mobilisationDate else None,
            "actualDemobilisationDate": (
                m.actualDemobilisationDate.isoformat() if m.actualDemobilisationDate else None
            ),
        }
        for m in mobilizations
    ]
    detail["inductionRecords"] = [
        {
            "id": ind.id,
            "siteId": ind.siteId,
            "inductionType": ind.inductionType,
            "conductedAt": ind.conductedAt.isoformat() if ind.conductedAt else None,
            "validUntil": ind.validUntil.isoformat() if ind.validUntil else None,
            "isExpired": ind.isExpired,
            "assessmentPassed": ind.assessmentPassed,
        }
        for ind in inductions
    ]
    return detail


@router.patch("/{worker_id}")
async def update_worker(
    worker_id: str,
    body: WorkerUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "EPC.UPDATE")
    worker = await db.get(ContractorWorker, worker_id)
    if worker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Worker not found")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(worker, field, value)

    await db.commit()
    await db.refresh(worker)
    amc = await _active_mob_count(db, worker_id)
    return _worker_dict(worker, amc)


class BulkWorkerRow(BaseModel):
    fullName: str
    contractorCompanyId: str
    primaryTrade: str
    mobileNumber: str
    aadhaarLast4: str | None = None
    dateOfBirth: str | None = None  # ISO date string
    gender: str | None = None
    yearsExperience: int = 0
    educationLevel: str | None = None
    emergencyContactName: str = ""
    emergencyContactPhone: str = ""
    homeState: str | None = None

class BulkImportBody(BaseModel):
    workers: list[BulkWorkerRow]
    siteId: str | None = None  # if provided, auto-create mobilization for each worker


@router.post("/bulk-import")
async def bulk_import_workers(
    body: BulkImportBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Bulk register contractor workers. Idempotent — existing workers (matched
    by aadhaarLast4 + contractorCompanyId) are updated, not duplicated."""
    await _require(db, user, "EPC.CREATE")
    now = datetime.now(timezone.utc)
    year = now.year

    created, updated, errors = 0, 0, []

    for i, row in enumerate(body.workers):
        try:
            # Dedup check: aadhaar last 4 + company
            existing = None
            if row.aadhaarLast4:
                result = await db.execute(
                    select(ContractorWorker).where(
                        ContractorWorker.aadhaarLast4 == row.aadhaarLast4,
                        ContractorWorker.contractorCompanyId == row.contractorCompanyId,
                    )
                )
                existing = result.scalar_one_or_none()

            if existing:
                # Update
                existing.fullName = row.fullName
                existing.primaryTrade = row.primaryTrade
                existing.mobileNumber = row.mobileNumber
                existing.yearsExperience = row.yearsExperience
                updated += 1
            else:
                # Create
                count_result = await db.execute(select(func.count(ContractorWorker.id)))
                count = (count_result.scalar_one() or 0) + 1
                worker_code = f"CW-{year}-{count:04d}"

                dob = None
                if row.dateOfBirth:
                    try:
                        dob = datetime.fromisoformat(row.dateOfBirth)
                    except ValueError:
                        pass

                worker = ContractorWorker(
                    contractorCompanyId=row.contractorCompanyId,
                    workerCode=worker_code,
                    fullName=row.fullName,
                    primaryTrade=row.primaryTrade,
                    mobileNumber=row.mobileNumber,
                    aadhaarLast4=row.aadhaarLast4,
                    dateOfBirth=dob,
                    gender=row.gender,
                    yearsExperience=row.yearsExperience,
                    educationLevel=row.educationLevel,
                    emergencyContactName=row.emergencyContactName,
                    emergencyContactPhone=row.emergencyContactPhone,
                    homeState=row.homeState,
                    createdById=user.id,
                )
                db.add(worker)
                created += 1

        except Exception as exc:  # noqa: BLE001
            errors.append({"row": i + 1, "name": row.fullName, "error": str(exc)})

    await db.commit()
    return {
        "created": created,
        "updated": updated,
        "errors": errors,
        "total_processed": len(body.workers),
    }


@router.get("/{worker_id}/gate-status")
async def gate_status(
    worker_id: str,
    siteId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Dry-run gate clearance check — preview result without creating any records."""
    await _require(db, user, "EPC.READ")
    worker = await db.get(ContractorWorker, worker_id)
    if worker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Worker not found")

    company = await db.get(ContractorCompany, worker.contractorCompanyId)
    now = datetime.now(timezone.utc)
    checks: dict[str, dict] = {}

    # a. mobilization check
    active_mob = (
        await db.execute(
            select(MobilizationRecord).where(
                MobilizationRecord.contractorWorkerId == worker_id,
                MobilizationRecord.siteId == siteId,
                MobilizationRecord.status == "active",
            )
        )
    ).scalar_one_or_none()
    checks["mobilization"] = {
        "result": "pass" if active_mob else "fail",
        "detail": "Active mobilization found" if active_mob else "No active mobilization for this site",
    }

    # b. site_induction check
    latest_induction = (
        await db.execute(
            select(SiteInduction)
            .where(
                SiteInduction.contractorWorkerId == worker_id,
                SiteInduction.siteId == siteId,
            )
            .order_by(SiteInduction.conductedAt.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if latest_induction and latest_induction.validUntil:
        valid_until = latest_induction.validUntil
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)
        days_remaining = (valid_until - now).days
        if days_remaining < 0:
            checks["site_induction"] = {"result": "fail", "detail": "Site induction has expired"}
        elif days_remaining <= 14:
            checks["site_induction"] = {
                "result": "warn",
                "detail": f"Site induction expires in {days_remaining} days",
            }
        else:
            checks["site_induction"] = {"result": "pass", "detail": "Site induction valid"}
    else:
        checks["site_induction"] = {"result": "fail", "detail": "No site induction on record"}

    # c–e. Placeholders for future integration
    checks["competency"] = {"result": "pass", "detail": "Skill matrix integration pending (next pass)"}
    checks["training"] = {"result": "pass", "detail": "Training records integration pending (next pass)"}
    checks["ppe"] = {"result": "pass", "detail": "PPE compliance integration pending (next pass)"}

    # f. medical fitness
    if worker.currentMedicalValidUntil:
        med_valid = worker.currentMedicalValidUntil
        if med_valid.tzinfo is None:
            med_valid = med_valid.replace(tzinfo=timezone.utc)
        checks["medical_fitness"] = {
            "result": "pass" if med_valid > now else "fail",
            "detail": (
                f"Medical certificate valid until {med_valid.date().isoformat()}"
                if med_valid > now
                else f"Medical certificate expired on {med_valid.date().isoformat()}"
            ),
        }
    else:
        checks["medical_fitness"] = {"result": "fail", "detail": "No medical fitness certificate on record"}

    # g. suspension check
    checks["suspension"] = {
        "result": "pass" if worker.overallStatus == "active" else "fail",
        "detail": (
            "Worker status is active"
            if worker.overallStatus == "active"
            else f"Worker status is {worker.overallStatus}"
        ),
    }

    # h. contractor company status
    approved_statuses = {"approved", "conditionally_approved"}
    if company:
        checks["contractor_company_status"] = {
            "result": "pass" if company.prequalificationStatus in approved_statuses else "fail",
            "detail": f"Company prequalification status: {company.prequalificationStatus}",
        }
    else:
        checks["contractor_company_status"] = {"result": "fail", "detail": "Contractor company not found"}

    # Determine overall result
    results = [c["result"] for c in checks.values()]
    blocking = [k for k, c in checks.items() if c["result"] == "fail"]
    warnings = [k for k, c in checks.items() if c["result"] == "warn"]

    if blocking:
        overall = "not_cleared"
    elif warnings:
        overall = "cleared_with_warnings"
    else:
        overall = "cleared"

    return {
        "workerId": worker_id,
        "workerCode": worker.workerCode,
        "workerName": worker.fullName,
        "siteId": siteId,
        "isDryRun": True,
        "overallResult": overall,
        "checks": checks,
        "blockingIssues": blocking,
        "warningIssues": warnings,
    }

