"""EPC Dashboard router. Mounts at /api/epc/dashboard.

Multi-site corporate dashboard providing a health-at-a-glance view across all
construction sites. The health indicator logic:
  red  — site suspended, or workers with expired inductions at this site
  amber — pending mobilizations > 10, or site demobilising
  green — all clear

The single-site summary card covers today's gate activity, pending mobilizations,
and expired induction count — all the data needed for the site tile on a PM's
dashboard.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
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
    SiteInduction,
)
from app.models.user import User
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/epc/dashboard", tags=["epc-dashboard"])


# ─── RBAC helper ─────────────────────────────────────────────────────────────


async def _require(db: AsyncSession, user: User, permission_code: str) -> None:
    result = await can(db, user.id, permission_code, PermissionContext())
    if not result.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            result.reason or f"Missing permission {permission_code}",
        )


# ─── Health indicator logic ───────────────────────────────────────────────────


async def _health_indicator(
    db: AsyncSession,
    site: ConstructionSite,
    now: datetime,
) -> tuple[str, str]:
    """Return (indicator: 'red'|'amber'|'green', reason: str)."""

    # Red: site suspended
    if site.status == "suspended":
        return "red", "Site is suspended"

    # Red: any workers with expired inductions at this site
    expired_induction_count_result = await db.execute(
        select(func.count(SiteInduction.id)).where(
            SiteInduction.siteId == site.id,
            SiteInduction.validUntil < now,
            SiteInduction.isExpired.is_(False),  # not yet marked expired
        )
    )
    expired_inductions = expired_induction_count_result.scalar_one() or 0
    if expired_inductions > 0:
        return "red", f"{expired_inductions} worker(s) with expired site induction"

    # Amber: site demobilising
    if site.status == "demobilising":
        return "amber", "Site is demobilising"

    # Amber: pending mobilizations > 10
    pending_count_result = await db.execute(
        select(func.count(MobilizationRecord.id)).where(
            MobilizationRecord.siteId == site.id,
            MobilizationRecord.status.in_([
                "pending_checks",
                "checks_complete_pending_approval",
            ]),
        )
    )
    pending_count = pending_count_result.scalar_one() or 0
    if pending_count > 10:
        return "amber", f"{pending_count} mobilizations pending approval"

    return "green", "All clear"


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get("/")
async def corporate_dashboard(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Corporate multi-site dashboard."""
    await _require(db, user, "EPC.READ")
    now = datetime.now(timezone.utc)

    sites = (await db.execute(select(ConstructionSite))).scalars().all()

    # Summary counts
    active_sites = sum(1 for s in sites if s.status in ("mobilising", "active", "peak_construction"))

    total_active_workers_result = await db.execute(
        select(func.count(MobilizationRecord.id)).where(
            MobilizationRecord.status == "active"
        )
    )
    total_active_workers = total_active_workers_result.scalar_one() or 0

    total_companies_result = await db.execute(select(func.count(ContractorCompany.id)))
    total_companies = total_companies_result.scalar_one() or 0

    open_passes_result = await db.execute(
        select(func.count(GatePass.id)).where(
            GatePass.status == "active",
            GatePass.validUntil >= now,
        )
    )
    open_passes = open_passes_result.scalar_one() or 0

    # Per-site breakdown
    site_data: list[dict] = []
    for site in sites:
        # Current workforce count (active mobilizations)
        wf_result = await db.execute(
            select(func.count(MobilizationRecord.id)).where(
                MobilizationRecord.siteId == site.id,
                MobilizationRecord.status == "active",
            )
        )
        wf_count = wf_result.scalar_one() or 0

        # Active contractor companies at this site
        active_cos_result = await db.execute(
            select(func.count(func.distinct(MobilizationRecord.contractorCompanyId))).where(
                MobilizationRecord.siteId == site.id,
                MobilizationRecord.status == "active",
            )
        )
        active_cos = active_cos_result.scalar_one() or 0

        # Pending mobilizations
        pending_result = await db.execute(
            select(func.count(MobilizationRecord.id)).where(
                MobilizationRecord.siteId == site.id,
                MobilizationRecord.status.in_([
                    "pending_checks",
                    "checks_complete_pending_approval",
                ]),
            )
        )
        pending = pending_result.scalar_one() or 0

        indicator, reason = await _health_indicator(db, site, now)

        site_data.append({
            "id": site.id,
            "siteCode": site.siteCode,
            "siteName": site.siteName,
            "clientName": site.clientName,
            "state": site.state,
            "status": site.status,
            "currentWorkforceCount": wf_count,
            "activeContractorCompanies": active_cos,
            "pendingMobilizations": pending,
            "healthIndicator": indicator,
            "healthReason": reason,
        })

    # Contractor performance: top 5 and bottom 5 by prequalificationScore
    scored_companies = (
        await db.execute(
            select(ContractorCompany).where(
                ContractorCompany.prequalificationScore.isnot(None)
            ).order_by(ContractorCompany.prequalificationScore.desc())
        )
    ).scalars().all()

    def _co_slim(c: ContractorCompany) -> dict:
        return {
            "id": c.id,
            "companyCode": c.code,
            "companyName": c.name,
            "prequalificationScore": c.prequalificationScore,
            "prequalificationStatus": c.prequalificationStatus,
        }

    top5 = [_co_slim(c) for c in scored_companies[:5]]
    bottom5 = [_co_slim(c) for c in scored_companies[-5:][::-1]] if len(scored_companies) > 5 else []

    return {
        "generatedAt": now.isoformat(),
        "summary": {
            "activeSites": active_sites,
            "totalSites": len(sites),
            "totalActiveWorkers": total_active_workers,
            "totalContractorCompanies": total_companies,
            "openGatePasses": open_passes,
        },
        "sites": site_data,
        "contractorPerformance": {
            "top5": top5,
            "bottom5": bottom5,
        },
    }


@router.get("/sites/{site_id}/summary")
async def site_summary(
    site_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Single site summary card data."""
    await _require(db, user, "EPC.READ")
    now = datetime.now(timezone.utc)

    site = await db.get(ConstructionSite, site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Construction site not found")

    # Active workforce count
    wf_result = await db.execute(
        select(func.count(MobilizationRecord.id)).where(
            MobilizationRecord.siteId == site_id,
            MobilizationRecord.status == "active",
        )
    )
    active_workers = wf_result.scalar_one() or 0

    # Today's gate activity
    today_start = datetime.combine(now.date(), time(0, 0, 0), tzinfo=timezone.utc)
    today_end = datetime.combine(now.date(), time(23, 59, 59), tzinfo=timezone.utc)

    today_checks = (
        await db.execute(
            select(GateClearanceCheck).where(
                GateClearanceCheck.siteId == site_id,
                GateClearanceCheck.createdAt >= today_start,
                GateClearanceCheck.createdAt <= today_end,
            )
        )
    ).scalars().all()

    today_cleared = sum(
        1 for c in today_checks if c.overallResult in ("cleared", "cleared_with_warnings", "cleared_with_override")
    )
    today_not_cleared = sum(1 for c in today_checks if c.overallResult == "not_cleared")

    # Pending mobilizations
    pending_result = await db.execute(
        select(func.count(MobilizationRecord.id)).where(
            MobilizationRecord.siteId == site_id,
            MobilizationRecord.status.in_([
                "pending_checks",
                "checks_complete_pending_approval",
            ]),
        )
    )
    pending_mobilizations = pending_result.scalar_one() or 0

    # Expired inductions (not yet flagged as expired)
    expired_ind_result = await db.execute(
        select(func.count(SiteInduction.id)).where(
            SiteInduction.siteId == site_id,
            SiteInduction.validUntil < now,
            SiteInduction.isExpired.is_(False),
        )
    )
    expired_inductions = expired_ind_result.scalar_one() or 0

    indicator, health_reason = await _health_indicator(db, site, now)

    return {
        "site": {
            "id": site.id,
            "siteCode": site.siteCode,
            "siteName": site.siteName,
            "clientName": site.clientName,
            "state": site.state,
            "status": site.status,
            "plannedCompletionDate": (
                site.plannedCompletionDate.isoformat() if site.plannedCompletionDate else None
            ),
            "peakWorkforcePlanned": site.peakWorkforcePlanned,
        },
        "workforce": {
            "activeWorkers": active_workers,
            "capacityUtilization": (
                round(active_workers / site.peakWorkforcePlanned * 100, 1)
                if site.peakWorkforcePlanned
                else None
            ),
        },
        "todayGateActivity": {
            "date": now.date().isoformat(),
            "totalChecks": len(today_checks),
            "cleared": today_cleared,
            "notCleared": today_not_cleared,
        },
        "pendingMobilizations": pending_mobilizations,
        "expiredInductions": expired_inductions,
        "health": {
            "indicator": indicator,
            "reason": health_reason,
        },
    }


@router.get("/sites/{site_id}/performance")
async def site_performance(
    site_id: str,
    days: int = Query(default=30, ge=7, le=90),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """30-day rolling performance metrics for a site card — gate activity
    by day, mobilization breakdown, induction compliance, contractor scores."""
    await _require(db, user, "EPC.READ")
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    # Gate activity
    gate_result = await db.execute(
        select(GateClearanceCheck).where(
            GateClearanceCheck.siteId == site_id,
            GateClearanceCheck.createdAt >= since,
        ).order_by(GateClearanceCheck.createdAt.asc())
    )
    gate_checks = gate_result.scalars().all()

    # Group by date
    from collections import defaultdict
    daily: dict = defaultdict(lambda: {"cleared": 0, "warnings": 0, "rejected": 0})
    for gc in gate_checks:
        day = gc.checkCompletedAt.strftime("%Y-%m-%d") if gc.checkCompletedAt else gc.createdAt.strftime("%Y-%m-%d")
        if gc.overallResult == "cleared":
            daily[day]["cleared"] += 1
        elif gc.overallResult == "cleared_with_warnings":
            daily[day]["warnings"] += 1
        else:
            daily[day]["rejected"] += 1

    gate_trend = [{"date": d, **counts} for d, counts in sorted(daily.items())]

    # Mobilization breakdown
    mob_result = await db.execute(
        select(MobilizationRecord).where(MobilizationRecord.siteId == site_id)
    )
    mobs = mob_result.scalars().all()
    mob_breakdown = {}
    for m in mobs:
        mob_breakdown[m.status] = mob_breakdown.get(m.status, 0) + 1

    # Induction compliance
    active_mobs = [m for m in mobs if m.status == "active"]
    inducted_ids = set()
    if active_mobs:
        worker_ids = [m.contractorWorkerId for m in active_mobs]
        ind_result = await db.execute(
            select(SiteInduction).where(
                SiteInduction.siteId == site_id,
                SiteInduction.contractorWorkerId.in_(worker_ids),
                SiteInduction.validUntil >= now,
                SiteInduction.isExpired == False,
            )
        )
        inductions = ind_result.scalars().all()
        inducted_ids = {i.contractorWorkerId for i in inductions}

    total_active = len(active_mobs)
    inducted_count = len(inducted_ids)
    induction_pct = round((inducted_count / total_active * 100) if total_active else 0, 1)

    # Total gate stats
    total_gate = len(gate_checks)
    cleared_total = sum(1 for g in gate_checks if g.overallResult == "cleared")
    warn_total = sum(1 for g in gate_checks if g.overallResult == "cleared_with_warnings")
    reject_total = sum(1 for g in gate_checks if g.overallResult == "not_cleared")
    rejection_rate = round((reject_total / total_gate * 100) if total_gate else 0, 1)

    return {
        "siteId": site_id,
        "periodDays": days,
        "gateTrend": gate_trend,
        "gateSummary": {
            "total": total_gate,
            "cleared": cleared_total,
            "clearedWithWarnings": warn_total,
            "rejected": reject_total,
            "rejectionRatePct": rejection_rate,
        },
        "mobilizationBreakdown": mob_breakdown,
        "inductionCompliance": {
            "totalActive": total_active,
            "inducted": inducted_count,
            "compliancePct": induction_pct,
        },
        # LTIFR/TRIR deferred — requires incident module integration
        "ltifr": None,
        "trir": None,
        "ltifr_note": "Requires incident data integration (EPC Pass 3)",
    }

