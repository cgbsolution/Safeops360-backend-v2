"""EPC Gate Clearance router. Mounts at /api/epc/gate.

This is the real-time enforcement layer. The POST /check endpoint must complete
in < 2 seconds because a gate guard is waiting. All 8 checks run against data
already loaded in memory after a minimal set of DB queries. A GateClearanceCheck
record is persisted for audit; a GatePass is auto-issued when the worker clears.

Override flow: HSE manager can override a NOT_CLEARED check — this creates a
new GatePass with the overrideApplied flag set on the original check record.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timezone

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
    GateClearanceCheck,
    GatePass,
    MobilizationRecord,
    SiteComplianceConfig,
    SiteInduction,
)
from app.models.user import User
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/epc/gate", tags=["epc-gate"])


# ─── RBAC helper ─────────────────────────────────────────────────────────────


async def _require(db: AsyncSession, user: User, permission_code: str) -> None:
    result = await can(db, user.id, permission_code, PermissionContext())
    if not result.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            result.reason or f"Missing permission {permission_code}",
        )


# ─── Request bodies ───────────────────────────────────────────────────────────


class GateCheckBody(BaseModel):
    siteId: str
    workerIdentifier: str  # workerCode (exact) or name (partial)
    checkMethod: str = "manual"  # "manual" | "qr_scan" | "biometric"


class OverrideBody(BaseModel):
    checkId: str
    reason: str
    authorityRole: str


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _end_of_today_utc() -> datetime:
    """Return 23:59:59 today in UTC."""
    today = datetime.now(timezone.utc).date()
    return datetime.combine(today, time(23, 59, 59), tzinfo=timezone.utc)


async def _generate_pass_number(db: AsyncSession, site_code: str) -> str:
    today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    count_result = await db.execute(select(func.count(GatePass.id)))
    count = (count_result.scalar_one() or 0) + 1
    return f"GP-{site_code}-{today_str}-{count:04d}"


def _check_result_dict(check: GateClearanceCheck) -> dict:
    return {
        "id": check.id,
        "siteId": check.siteId,
        "contractorWorkerId": check.contractorWorkerId,
        "workerName": check.workerName,
        "workerCode": check.workerCode,
        "contractorCompanyName": check.contractorCompanyName,
        "checkMethod": check.checkMethod,
        "overallResult": check.overallResult,
        "checks": check.checks or {},
        "blockingIssues": check.blockingIssues or [],
        "warningIssues": check.warningIssues or [],
        "gatePassIssued": check.gatePassIssued,
        "gatePassId": check.gatePassId,
        "overrideApplied": check.overrideApplied,
        "checkCompletedAt": check.checkCompletedAt.isoformat() if check.checkCompletedAt else None,
        "processingDurationMs": check.processingDurationMs,
        "createdAt": check.createdAt.isoformat() if check.createdAt else None,
    }


def _pass_dict(gate_pass: GatePass) -> dict:
    return {
        "id": gate_pass.id,
        "passNumber": gate_pass.passNumber,
        "siteId": gate_pass.siteId,
        "contractorWorkerId": gate_pass.contractorWorkerId,
        "workerName": gate_pass.workerName,
        "workerCode": gate_pass.workerCode,
        "workerPhotoUrl": gate_pass.workerPhotoUrl,
        "primaryTrade": gate_pass.primaryTrade,
        "contractorCompanyName": gate_pass.contractorCompanyName,
        "passType": gate_pass.passType,
        "validFrom": gate_pass.validFrom.isoformat() if gate_pass.validFrom else None,
        "validUntil": gate_pass.validUntil.isoformat() if gate_pass.validUntil else None,
        "authorizedAreas": gate_pass.authorizedAreas or [],
        "authorizedTrades": gate_pass.authorizedTrades or [],
        "status": gate_pass.status,
        "qrCodeData": gate_pass.qrCodeData,
        "generatedAt": gate_pass.generatedAt.isoformat() if gate_pass.generatedAt else None,
    }


async def _run_eight_checks(
    db: AsyncSession,
    worker: ContractorWorker,
    company: ContractorCompany,
    site_id: str,
    now: datetime,
) -> dict:
    """Run all 8 gate clearance checks. Returns {check_name: {result, detail}} dict."""
    checks: dict[str, dict] = {}

    # a. mobilization
    active_mob = (
        await db.execute(
            select(MobilizationRecord).where(
                MobilizationRecord.contractorWorkerId == worker.id,
                MobilizationRecord.siteId == site_id,
                MobilizationRecord.status == "active",
            )
        )
    ).scalar_one_or_none()
    checks["mobilization"] = {
        "result": "pass" if active_mob else "fail",
        "detail": "Active mobilization found" if active_mob else "No active mobilization for this site",
    }

    # b. site_induction
    latest_induction = (
        await db.execute(
            select(SiteInduction)
            .where(
                SiteInduction.contractorWorkerId == worker.id,
                SiteInduction.siteId == site_id,
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
            checks["site_induction"] = {
                "result": "fail",
                "detail": "Site induction has expired",
                "validUntil": valid_until.isoformat(),
            }
        elif days_remaining <= 14:
            checks["site_induction"] = {
                "result": "warn",
                "detail": f"Site induction expires in {days_remaining} days",
                "validUntil": valid_until.isoformat(),
                "daysRemaining": days_remaining,
            }
        else:
            checks["site_induction"] = {
                "result": "pass",
                "detail": "Site induction valid",
                "validUntil": valid_until.isoformat(),
                "daysRemaining": days_remaining,
            }
    else:
        checks["site_induction"] = {
            "result": "fail",
            "detail": "No site induction on record for this site",
        }

    # c. competency
    worker_competencies: list = worker.competencyRecords or []
    site_config_result = await db.execute(
        select(SiteComplianceConfig).where(SiteComplianceConfig.siteId == site_id)
    )
    site_cfg = site_config_result.scalar_one_or_none()
    mandatory_competencies: list = (site_cfg.mandatoryTraining or []) if site_cfg else []

    comp_blocks, comp_warns = [], []
    for req in mandatory_competencies:
        applies_to = req.get("applies_to_trades", [])
        if applies_to and worker.primaryTrade not in applies_to:
            continue
        req_code = req.get("program_code", "")
        found = next((c for c in worker_competencies if c.get("competencyCode") == req_code), None)
        if not found:
            comp_blocks.append(f"{req_code}: Not recorded")
        elif found.get("status") not in ("valid", "current"):
            comp_blocks.append(f"{req_code}: {found.get('status', 'invalid')}")
        elif found.get("validUntil"):
            exp = datetime.fromisoformat(found["validUntil"].replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            days_left = (exp - now).days
            if days_left < 0:
                comp_blocks.append(f"{req_code}: Expired {abs(days_left)} days ago")
            elif days_left <= 14:
                comp_warns.append(f"{req_code}: Expires in {days_left} days")

    checks["competency"] = {
        "result": "fail" if comp_blocks else ("warn" if comp_warns else "pass"),
        "blocking_competencies": comp_blocks,
        "warning_competencies": comp_warns,
        "detail": f"{len(comp_blocks)} blocking gaps" if comp_blocks else (f"{len(comp_warns)} expiring soon" if comp_warns else "All competencies current"),
    }

    # d. training
    worker_certs: list = worker.trainingCertificates or []
    # Re-use site_cfg from competency check above
    mandatory_training: list = (site_cfg.mandatoryTraining or []) if site_cfg else []

    train_blocks, train_warns = [], []
    for req in mandatory_training:
        applies_to = req.get("applies_to_trades", [])
        if applies_to and worker.primaryTrade not in applies_to:
            continue
        req_code = req.get("program_code", "")
        found = next((c for c in worker_certs if c.get("programCode") == req_code), None)
        if not found:
            train_blocks.append(f"{req_code}: Not completed")
        elif found.get("status") not in ("active", "valid", "ACTIVE"):
            train_blocks.append(f"{req_code}: {found.get('status', 'invalid')}")
        elif found.get("validUntil"):
            exp = datetime.fromisoformat(found["validUntil"].replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            days_left = (exp - now).days
            if days_left < 0:
                train_blocks.append(f"{req_code}: Expired {abs(days_left)} days ago")
            elif days_left <= 14:
                train_warns.append(f"{req_code}: Expires in {days_left} days")

    checks["training"] = {
        "result": "fail" if train_blocks else ("warn" if train_warns else "pass"),
        "blocking_programs": train_blocks,
        "warning_programs": train_warns,
        "detail": f"{len(train_blocks)} training gaps" if train_blocks else (f"{len(train_warns)} expiring soon" if train_warns else "All training current"),
    }

    # e. ppe
    worker_ppe: list = worker.ppeIssuances or []
    min_ppe_reqs: list = (site_cfg.minimumPpeRequirements or []) if site_cfg else []

    ppe_blocks, ppe_warns = [], []
    for req in min_ppe_reqs:
        req_code = req.get("ppe_type_code", "")
        req_name = req.get("ppe_type_name", req_code)
        found = next((p for p in worker_ppe if p.get("ppeTypeCode") == req_code), None)
        if not found:
            ppe_blocks.append(f"{req_name}: Not issued")
        elif found.get("status") not in ("active", "valid"):
            ppe_blocks.append(f"{req_name}: {found.get('status', 'invalid')}")
        elif found.get("expiresAt"):
            exp = datetime.fromisoformat(found["expiresAt"].replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            days_left = (exp - now).days
            if days_left < 0:
                ppe_blocks.append(f"{req_name}: Expired {abs(days_left)} days ago")
            elif days_left <= 7:
                ppe_warns.append(f"{req_name}: Expires in {days_left} days")

    checks["ppe"] = {
        "result": "fail" if ppe_blocks else ("warn" if ppe_warns else "pass"),
        "blocking_ppe": ppe_blocks,
        "warning_ppe": ppe_warns,
        "detail": f"{len(ppe_blocks)} PPE gaps" if ppe_blocks else (f"{len(ppe_warns)} expiring soon" if ppe_warns else "All required PPE current" if min_ppe_reqs else "No PPE requirements configured"),
    }

    # f. medical_fitness
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
            "validUntil": med_valid.isoformat(),
        }
    else:
        checks["medical_fitness"] = {
            "result": "fail",
            "detail": "No medical fitness certificate on record",
        }

    # g. suspension
    checks["suspension"] = {
        "result": "pass" if worker.overallStatus == "active" else "fail",
        "detail": (
            "Worker is active"
            if worker.overallStatus == "active"
            else f"Worker status is '{worker.overallStatus}'"
        ),
    }

    # h. contractor_company_status
    approved_statuses = {"approved", "conditionally_approved"}
    checks["contractor_company_status"] = {
        "result": "pass" if company.prequalificationStatus in approved_statuses else "fail",
        "detail": f"Company prequalification status: {company.prequalificationStatus}",
        "prequalificationStatus": company.prequalificationStatus,
    }

    return checks


async def _issue_gate_pass(
    db: AsyncSession,
    site: ConstructionSite,
    check: GateClearanceCheck,
    worker: ContractorWorker,
    company: ContractorCompany,
    pass_type: str = "daily",
    override: bool = False,
) -> GatePass:
    """Create and persist a GatePass. Updates the check record with gatePassId."""
    now = datetime.now(timezone.utc)
    pass_number = await _generate_pass_number(db, site.siteCode)

    # Build authorized areas from active mobilization
    active_mob = (
        await db.execute(
            select(MobilizationRecord).where(
                MobilizationRecord.contractorWorkerId == worker.id,
                MobilizationRecord.siteId == site.id,
                MobilizationRecord.status == "active",
            )
        )
    ).scalar_one_or_none()

    authorized_areas = [active_mob.workArea] if active_mob and active_mob.workArea else []
    authorized_trades = (
        [active_mob.tradeAtSite] if active_mob and active_mob.tradeAtSite else [worker.primaryTrade]
    )

    qr_data = json.dumps({
        "passNumber": pass_number,
        "workerId": worker.id,
        "workerCode": worker.workerCode,
        "siteId": site.id,
        "validUntil": _end_of_today_utc().isoformat(),
        "override": override,
    })

    gate_pass = GatePass(
        tenantId=worker.tenantId,
        siteId=site.id,
        clearanceCheckId=check.id,
        contractorWorkerId=worker.id,
        workerName=worker.fullName,
        workerCode=worker.workerCode,
        workerPhotoUrl=worker.photoUrl,
        primaryTrade=worker.primaryTrade,
        contractorCompanyName=company.name,
        passNumber=pass_number,
        passType=pass_type,
        validFrom=now,
        validUntil=_end_of_today_utc(),
        authorizedAreas=authorized_areas,
        authorizedTrades=[t for t in authorized_trades if t],
        status="active",
        qrCodeData=qr_data,
        generatedAt=now,
    )
    db.add(gate_pass)
    await db.flush()  # get gate_pass.id without committing

    check.gatePassIssued = True
    check.gatePassId = gate_pass.id
    return gate_pass


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.post("/check", status_code=status.HTTP_201_CREATED)
async def gate_check(
    body: GateCheckBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Run gate clearance check. Target: < 2 seconds end-to-end."""
    await _require(db, user, "EPC.READ")

    start_time = datetime.now(timezone.utc)
    now = start_time

    # Load site
    site = await db.get(ConstructionSite, body.siteId)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Construction site not found")

    # Find worker by workerCode (exact) or fullName (partial)
    worker = (
        await db.execute(
            select(ContractorWorker).where(ContractorWorker.workerCode == body.workerIdentifier)
        )
    ).scalar_one_or_none()

    if worker is None:
        # Try partial name match
        workers = (
            await db.execute(
                select(ContractorWorker).where(
                    ContractorWorker.fullName.ilike(f"%{body.workerIdentifier}%")
                )
            )
        ).scalars().all()
        if not workers:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"No worker found matching '{body.workerIdentifier}'",
            )
        if len(workers) > 1:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Multiple workers match '{body.workerIdentifier}'. Use exact worker code.",
            )
        worker = workers[0]

    company = await db.get(ContractorCompany, worker.contractorCompanyId)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contractor company not found")

    # Run all 8 checks
    checks = await _run_eight_checks(db, worker, company, body.siteId, now)

    # Determine overall result
    blocking = [k for k, c in checks.items() if c["result"] == "fail"]
    warnings = [k for k, c in checks.items() if c["result"] == "warn"]

    if blocking:
        overall = "not_cleared"
    elif warnings:
        overall = "cleared_with_warnings"
    else:
        overall = "cleared"

    end_time = datetime.now(timezone.utc)
    duration_ms = int((end_time - start_time).total_seconds() * 1000)

    # Create GateClearanceCheck record
    check = GateClearanceCheck(
        tenantId=worker.tenantId,
        siteId=body.siteId,
        contractorWorkerId=worker.id,
        workerName=worker.fullName,
        workerCode=worker.workerCode,
        contractorCompanyName=company.name,
        checkRequestedAt=start_time,
        checkMethod=body.checkMethod,
        checks=checks,
        overallResult=overall,
        blockingIssues=blocking,
        warningIssues=warnings,
        gatePassIssued=False,
        overrideApplied=False,
        checkCompletedAt=end_time,
        processingDurationMs=duration_ms,
    )
    db.add(check)
    await db.flush()

    # Issue gate pass if cleared
    gate_pass = None
    if overall in ("cleared", "cleared_with_warnings"):
        gate_pass = await _issue_gate_pass(db, site, check, worker, company)

    await db.commit()
    await db.refresh(check)

    result = _check_result_dict(check)
    if gate_pass:
        await db.refresh(gate_pass)
        result["gatePass"] = _pass_dict(gate_pass)

    return result


@router.get("/log")
async def gate_log(
    siteId: str = Query(...),
    date: str | None = Query(None, description="YYYY-MM-DD, defaults to today"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Gate log for a site on a given date."""
    await _require(db, user, "EPC.READ")

    if date:
        try:
            log_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid date format. Use YYYY-MM-DD")
    else:
        log_date = datetime.now(timezone.utc).date()

    # Date range in UTC
    day_start = datetime.combine(log_date, time(0, 0, 0), tzinfo=timezone.utc)
    day_end = datetime.combine(log_date, time(23, 59, 59), tzinfo=timezone.utc)

    checks = (
        await db.execute(
            select(GateClearanceCheck)
            .where(
                GateClearanceCheck.siteId == siteId,
                GateClearanceCheck.createdAt >= day_start,
                GateClearanceCheck.createdAt <= day_end,
            )
            .order_by(GateClearanceCheck.createdAt.desc())
        )
    ).scalars().all()

    cleared = sum(1 for c in checks if c.overallResult in ("cleared", "cleared_with_warnings"))
    not_cleared = sum(1 for c in checks if c.overallResult == "not_cleared")

    return {
        "siteId": siteId,
        "date": log_date.isoformat(),
        "totalChecks": len(checks),
        "cleared": cleared,
        "notCleared": not_cleared,
        "entries": [_check_result_dict(c) for c in checks],
    }


@router.post("/override")
async def override_check(
    body: OverrideBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Apply HSE manager override to a not-cleared check. Issues a gate pass with override flag."""
    await _require(db, user, "EPC.GATE_OVERRIDE")

    check = await db.get(GateClearanceCheck, body.checkId)
    if check is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Gate clearance check not found")

    if check.overallResult != "not_cleared":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Override can only be applied to not_cleared checks. Current result: {check.overallResult}",
        )

    if check.overrideApplied:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Override has already been applied to this check")

    site = await db.get(ConstructionSite, check.siteId)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Construction site not found")

    worker = await db.get(ContractorWorker, check.contractorWorkerId)
    if worker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Worker not found")

    company = await db.get(ContractorCompany, worker.contractorCompanyId)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contractor company not found")

    now = datetime.now(timezone.utc)
    check.overrideApplied = True
    check.overrideByUserId = user.id
    check.overrideAt = now
    check.overrideReason = body.reason
    check.overrideAuthorityRole = body.authorityRole
    check.overallResult = "cleared_with_override"

    await db.flush()

    gate_pass = await _issue_gate_pass(db, site, check, worker, company, override=True)

    await db.commit()
    await db.refresh(check)
    await db.refresh(gate_pass)

    return {
        "message": "Override applied and gate pass issued",
        "checkId": check.id,
        "overrideApplied": True,
        "gatePass": _pass_dict(gate_pass),
    }


@router.get("/active-passes/{site_id}")
async def active_passes(
    site_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """All active gate passes for a site (workers currently on site today)."""
    await _require(db, user, "EPC.READ")

    site = await db.get(ConstructionSite, site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Construction site not found")

    now = datetime.now(timezone.utc)
    passes = (
        await db.execute(
            select(GatePass).where(
                GatePass.siteId == site_id,
                GatePass.status == "active",
                GatePass.validUntil >= now,
            ).order_by(GatePass.generatedAt.desc())
        )
    ).scalars().all()

    return {
        "siteId": site_id,
        "siteCode": site.siteCode,
        "siteName": site.siteName,
        "activePassCount": len(passes),
        "passes": [_pass_dict(p) for p in passes],
    }

