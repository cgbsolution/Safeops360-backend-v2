"""Risk Aggregation Dashboard — Phase 2.

Plant-level read-only aggregates across HIRA + EAI for the executive
risk picture. 8 widgets per HIRA Phase 2 spec §6.2:

  1. /dashboard/top-risks           — top 10 risks across HIRA+EAI
  2. /dashboard/risk-trend          — aggregate residual score over time
  3. /dashboard/heatmap             — area × residual-level grid
  4. /dashboard/control-effectiveness — aggregate control effectiveness
  5. /dashboard/coverage            — % depts with active studies (HIRA + EAI)
  6. /dashboard/review-compliance   — % on-time, overdue aging
  7. /dashboard/top-hazards         — most frequent hazard / aspect categories
  8. /dashboard/incident-linkage    — last 90d incidents mapped to entries

Plus a persona-configurable default-dashboard endpoint.

All endpoints are read-only and cache-friendly (no writes). 15-min
client-side cache hint via the standard FastAPI response so the UI can
poll without churn.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import HTTPException, status

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.models.eai import (
    EaiAspect,
    EaiAspectCategory,
    EaiEntry,
    EaiEntryAspect,
    EaiEntryControl,
    EaiFeatureFlag,
    EaiStudy,
)
from app.models.hira import (
    HiraEntry,
    HiraEntryControl,
    HiraEntryHazard,
    HiraHazard,
    HiraStudy,
)
from app.models.incident import Incident
from app.models.masters import Department

router = APIRouter(prefix="/api/risk-dashboard", tags=["risk-dashboard"])


async def resolve_plant_id(
    plantId: str | None = Query(None),
    user: User = Depends(get_current_user),
) -> str:
    """Pick the plant the caller is asking about.

    Web callers explicitly pass `?plantId=…`. Mobile / lightweight clients
    omit it and we fall back to the caller's home plant — saves the mobile
    UI from having to round-trip the user object before each widget.
    """
    plant_id = (plantId or user.plantId or "").strip()
    if not plant_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "plantId required (caller has no home plant)",
        )
    return plant_id


# ─────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────


class TopRiskRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    type: Literal["HIRA", "EAI"]
    moduleNumber: str
    sequenceNumber: int
    activityDescription: str
    areaId: str | None
    departmentId: str | None
    residualLevel: str | None
    residualScore: int | None
    lastReviewedAt: datetime | None
    nextReviewDue: datetime | None


class RiskTrendPoint(BaseModel):
    period: str  # e.g. "2026-04"
    meanResidualScore: float
    entryCount: int
    significantOrCriticalCount: int


class HeatmapCell(BaseModel):
    areaId: str | None
    areaName: str | None
    low: int = 0
    moderate: int = 0
    high: int = 0
    critical: int = 0


class ControlEffectivenessRow(BaseModel):
    hierarchy: str
    total: int
    effective: int
    partial: int
    ineffective: int
    notVerified: int
    effectivenessPercent: float


class CoverageStats(BaseModel):
    departmentsTotal: int
    hiraCoverageDepts: int
    eaiCoverageDepts: int
    hiraCoveragePercent: float
    eaiCoveragePercent: float


class ReviewComplianceStats(BaseModel):
    totalActive: int
    overdueCount: int
    overduePercent: float
    overdueAgingBuckets: dict[str, int]  # "0-30", "31-90", "90+"


class TopCategoryRow(BaseModel):
    source: Literal["HIRA", "EAI"]
    category: str
    count: int


class IncidentLinkageRow(BaseModel):
    incidentId: str
    incidentNumber: str | None
    occurredAt: datetime | None
    severity: str | None
    linkedHiraEntryIds: list[str] = []
    linkedEaiEntryIds: list[str] = []


class PersonaDashboardConfig(BaseModel):
    persona: str
    widgets: list[str]


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


_RISK_RANK = {"LOW": 0, "MODERATE": 1, "HIGH": 2, "CRITICAL": 3, "SIGNIFICANT": 2, "MAJOR": 3}


async def _eai_enabled_for(db: AsyncSession, plant_id: str) -> bool:
    flag = (
        await db.execute(select(EaiFeatureFlag).where(EaiFeatureFlag.plantId == plant_id))
    ).scalar_one_or_none()
    return bool(flag and flag.eaiRegisterEnabled)


# ─────────────────────────────────────────────────────────────────────
# 1. Top Risks
# ─────────────────────────────────────────────────────────────────────


@router.get("/top-risks", response_model=list[TopRiskRow])
async def top_risks(
    plantId: str = Depends(resolve_plant_id),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    rows: list[TopRiskRow] = []

    hira_rows = (
        await db.execute(
            select(HiraEntry, HiraStudy)
            .join(HiraStudy, HiraStudy.id == HiraEntry.studyId)
            .where(HiraStudy.plantId == plantId)
            .where(HiraEntry.isCurrentVersion.is_(True))
        )
    ).all()
    for entry, study in hira_rows:
        rows.append(
            TopRiskRow(
                id=entry.id,
                type="HIRA",
                moduleNumber=study.number,
                sequenceNumber=entry.sequenceNumber,
                activityDescription=entry.activityDescription,
                areaId=entry.areaId,
                departmentId=study.departmentId,
                residualLevel=entry.residualRiskLevel,
                residualScore=entry.residualRiskScore,
                lastReviewedAt=entry.lastReviewedAt,
                nextReviewDue=entry.nextReviewDue,
            )
        )

    if await _eai_enabled_for(db, plantId):
        eai_rows = (
            await db.execute(
                select(EaiEntry, EaiStudy)
                .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
                .where(EaiStudy.plantId == plantId)
                .where(EaiEntry.isCurrentVersion.is_(True))
            )
        ).all()
        for entry, study in eai_rows:
            rows.append(
                TopRiskRow(
                    id=entry.id,
                    type="EAI",
                    moduleNumber=study.number,
                    sequenceNumber=entry.sequenceNumber,
                    activityDescription=entry.activityDescription,
                    areaId=entry.areaId,
                    departmentId=study.departmentId,
                    residualLevel=entry.residualImpactLevel,
                    residualScore=entry.residualImpactScore,
                    lastReviewedAt=entry.lastReviewedAt,
                    nextReviewDue=entry.nextReviewDue,
                )
            )

    def sort_key(r: TopRiskRow) -> tuple:
        return (
            -_RISK_RANK.get((r.residualLevel or "").upper(), 0),
            -(r.residualScore or 0),
        )

    rows.sort(key=sort_key)
    return rows[:limit]


# ─────────────────────────────────────────────────────────────────────
# 2. Risk Reduction Trend
# ─────────────────────────────────────────────────────────────────────


@router.get("/risk-trend", response_model=list[RiskTrendPoint])
async def risk_trend(
    plantId: str = Depends(resolve_plant_id),
    months: int = Query(12, ge=1, le=36),
    db: AsyncSession = Depends(get_db),
):
    # Snapshot approach: bucket entries by updatedAt month and compute mean
    # residual. For exact historical trend, EaiVersion / HiraVersion would
    # need a time-series scan — punt that to a separate metrics aggregator job.
    now = datetime.now(timezone.utc)
    eai_enabled = await _eai_enabled_for(db, plantId)
    buckets: dict[str, dict[str, Any]] = {}

    for i in range(months):
        period_start = (now - timedelta(days=30 * (months - i)))
        period_label = period_start.strftime("%Y-%m")
        buckets[period_label] = {"scores": [], "significant": 0}

    hira_rows = (
        await db.execute(
            select(HiraEntry.updatedAt, HiraEntry.residualRiskScore, HiraEntry.residualRiskLevel)
            .join(HiraStudy, HiraStudy.id == HiraEntry.studyId)
            .where(HiraStudy.plantId == plantId)
        )
    ).all()
    for updated_at, score, level in hira_rows:
        if score is None or updated_at is None:
            continue
        period = updated_at.strftime("%Y-%m")
        if period not in buckets:
            continue
        buckets[period]["scores"].append(score)
        if level in ("HIGH", "CRITICAL"):
            buckets[period]["significant"] += 1

    if eai_enabled:
        eai_rows = (
            await db.execute(
                select(EaiEntry.updatedAt, EaiEntry.residualImpactScore, EaiEntry.residualSignificant)
                .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
                .where(EaiStudy.plantId == plantId)
            )
        ).all()
        for updated_at, score, significant in eai_rows:
            if score is None or updated_at is None:
                continue
            period = updated_at.strftime("%Y-%m")
            if period not in buckets:
                continue
            buckets[period]["scores"].append(score)
            if significant:
                buckets[period]["significant"] += 1

    points: list[RiskTrendPoint] = []
    for period, data in sorted(buckets.items()):
        scores = data["scores"]
        mean = sum(scores) / len(scores) if scores else 0.0
        points.append(
            RiskTrendPoint(
                period=period,
                meanResidualScore=round(mean, 2),
                entryCount=len(scores),
                significantOrCriticalCount=data["significant"],
            )
        )
    return points


# ─────────────────────────────────────────────────────────────────────
# 3. Heatmap (area × residual level)
# ─────────────────────────────────────────────────────────────────────


@router.get("/heatmap", response_model=list[HeatmapCell])
async def heatmap(
    plantId: str = Depends(resolve_plant_id), db: AsyncSession = Depends(get_db),
):
    from app.models.plant import Area

    areas = (
        await db.execute(select(Area).where(Area.plantId == plantId))
    ).scalars().all()
    cells: dict[str | None, HeatmapCell] = {
        a.id: HeatmapCell(areaId=a.id, areaName=a.name) for a in areas
    }
    cells[None] = HeatmapCell(areaId=None, areaName="Unassigned")

    eai_enabled = await _eai_enabled_for(db, plantId)

    hira_rows = (
        await db.execute(
            select(HiraEntry.areaId, HiraEntry.residualRiskLevel)
            .join(HiraStudy, HiraStudy.id == HiraEntry.studyId)
            .where(HiraStudy.plantId == plantId)
            .where(HiraEntry.isCurrentVersion.is_(True))
        )
    ).all()
    for area_id, level in hira_rows:
        cell = cells.get(area_id) or cells[None]
        if level == "LOW":
            cell.low += 1
        elif level == "MODERATE":
            cell.moderate += 1
        elif level == "HIGH":
            cell.high += 1
        elif level == "CRITICAL":
            cell.critical += 1

    if eai_enabled:
        eai_rows = (
            await db.execute(
                select(EaiEntry.areaId, EaiEntry.residualImpactLevel)
                .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
                .where(EaiStudy.plantId == plantId)
                .where(EaiEntry.isCurrentVersion.is_(True))
            )
        ).all()
        for area_id, level in eai_rows:
            cell = cells.get(area_id) or cells[None]
            if level == "LOW":
                cell.low += 1
            elif level == "MODERATE":
                cell.moderate += 1
            elif level == "SIGNIFICANT":
                cell.high += 1
            elif level == "MAJOR":
                cell.critical += 1

    return [c for c in cells.values() if (c.low + c.moderate + c.high + c.critical) > 0]


# ─────────────────────────────────────────────────────────────────────
# 4. Control Effectiveness
# ─────────────────────────────────────────────────────────────────────


@router.get("/control-effectiveness", response_model=list[ControlEffectivenessRow])
async def control_effectiveness(
    plantId: str = Depends(resolve_plant_id), db: AsyncSession = Depends(get_db),
):
    buckets: dict[str, dict[str, int]] = {}

    def _bump(hierarchy: str, key: str) -> None:
        b = buckets.setdefault(
            hierarchy,
            {"total": 0, "effective": 0, "partial": 0, "ineffective": 0, "notVerified": 0},
        )
        b["total"] += 1
        b[key] += 1

    hira_controls = (
        await db.execute(
            select(HiraEntryControl.hierarchy, HiraEntryControl.effectiveness)
            .join(HiraEntry, HiraEntry.id == HiraEntryControl.entryId)
            .join(HiraStudy, HiraStudy.id == HiraEntry.studyId)
            .where(HiraStudy.plantId == plantId)
        )
    ).all()
    for hierarchy, effectiveness in hira_controls:
        if effectiveness == "EFFECTIVE":
            _bump(hierarchy, "effective")
        elif effectiveness == "PARTIALLY_EFFECTIVE":
            _bump(hierarchy, "partial")
        elif effectiveness == "INEFFECTIVE":
            _bump(hierarchy, "ineffective")
        else:
            _bump(hierarchy, "notVerified")

    if await _eai_enabled_for(db, plantId):
        eai_controls = (
            await db.execute(
                select(EaiEntryControl.hierarchy, EaiEntryControl.effectiveness)
                .join(EaiEntry, EaiEntry.id == EaiEntryControl.entryId)
                .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
                .where(EaiStudy.plantId == plantId)
            )
        ).all()
        for hierarchy, effectiveness in eai_controls:
            if effectiveness == "EFFECTIVE":
                _bump(hierarchy, "effective")
            elif effectiveness == "PARTIALLY_EFFECTIVE":
                _bump(hierarchy, "partial")
            elif effectiveness == "INEFFECTIVE":
                _bump(hierarchy, "ineffective")
            else:
                _bump(hierarchy, "notVerified")

    rows: list[ControlEffectivenessRow] = []
    for hierarchy, b in buckets.items():
        eff_pct = (b["effective"] / b["total"] * 100.0) if b["total"] else 0.0
        rows.append(
            ControlEffectivenessRow(
                hierarchy=hierarchy,
                total=b["total"],
                effective=b["effective"],
                partial=b["partial"],
                ineffective=b["ineffective"],
                notVerified=b["notVerified"],
                effectivenessPercent=round(eff_pct, 1),
            )
        )
    rows.sort(key=lambda r: r.hierarchy)
    return rows


# ─────────────────────────────────────────────────────────────────────
# 5. Coverage
# ─────────────────────────────────────────────────────────────────────


@router.get("/coverage", response_model=CoverageStats)
async def coverage(plantId: str = Depends(resolve_plant_id), db: AsyncSession = Depends(get_db)):
    total = (
        await db.execute(
            select(func.count(Department.id)).where(Department.plantId == plantId)
        )
    ).scalar_one() or 0

    hira_depts = (
        await db.execute(
            select(func.count(func.distinct(HiraStudy.departmentId)))
            .where(HiraStudy.plantId == plantId)
            .where(HiraStudy.status.in_(["ACTIVE", "APPROVED"]))
            .where(HiraStudy.departmentId.isnot(None))
        )
    ).scalar_one() or 0

    eai_depts = 0
    if await _eai_enabled_for(db, plantId):
        eai_depts = (
            await db.execute(
                select(func.count(func.distinct(EaiStudy.departmentId)))
                .where(EaiStudy.plantId == plantId)
                .where(EaiStudy.status.in_(["ACTIVE", "APPROVED"]))
                .where(EaiStudy.departmentId.isnot(None))
            )
        ).scalar_one() or 0

    return CoverageStats(
        departmentsTotal=total,
        hiraCoverageDepts=hira_depts,
        eaiCoverageDepts=eai_depts,
        hiraCoveragePercent=round(hira_depts / total * 100, 1) if total else 0.0,
        eaiCoveragePercent=round(eai_depts / total * 100, 1) if total else 0.0,
    )


# ─────────────────────────────────────────────────────────────────────
# 6. Review Compliance
# ─────────────────────────────────────────────────────────────────────


@router.get("/review-compliance", response_model=ReviewComplianceStats)
async def review_compliance(plantId: str = Depends(resolve_plant_id), db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)

    total_hira = (
        await db.execute(
            select(func.count(HiraEntry.id))
            .join(HiraStudy, HiraStudy.id == HiraEntry.studyId)
            .where(HiraStudy.plantId == plantId)
            .where(HiraEntry.status == "ACTIVE")
        )
    ).scalar_one() or 0

    overdue_hira_rows = (
        await db.execute(
            select(HiraEntry.nextReviewDue)
            .join(HiraStudy, HiraStudy.id == HiraEntry.studyId)
            .where(HiraStudy.plantId == plantId)
            .where(HiraEntry.status == "ACTIVE")
            .where(HiraEntry.nextReviewDue.isnot(None))
            .where(HiraEntry.nextReviewDue < now)
        )
    ).scalars().all()

    total = total_hira
    overdue: list[datetime] = list(overdue_hira_rows)

    if await _eai_enabled_for(db, plantId):
        total_eai = (
            await db.execute(
                select(func.count(EaiEntry.id))
                .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
                .where(EaiStudy.plantId == plantId)
                .where(EaiEntry.status == "ACTIVE")
            )
        ).scalar_one() or 0
        total += total_eai

        overdue_eai_rows = (
            await db.execute(
                select(EaiEntry.nextReviewDue)
                .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
                .where(EaiStudy.plantId == plantId)
                .where(EaiEntry.status == "ACTIVE")
                .where(EaiEntry.nextReviewDue.isnot(None))
                .where(EaiEntry.nextReviewDue < now)
            )
        ).scalars().all()
        overdue.extend(overdue_eai_rows)

    buckets = {"0-30": 0, "31-90": 0, "90+": 0}
    for due in overdue:
        days_overdue = (now - due).days
        if days_overdue <= 30:
            buckets["0-30"] += 1
        elif days_overdue <= 90:
            buckets["31-90"] += 1
        else:
            buckets["90+"] += 1

    return ReviewComplianceStats(
        totalActive=total,
        overdueCount=len(overdue),
        overduePercent=round(len(overdue) / total * 100, 1) if total else 0.0,
        overdueAgingBuckets=buckets,
    )


# ─────────────────────────────────────────────────────────────────────
# 7. Top Categories
# ─────────────────────────────────────────────────────────────────────


@router.get("/top-categories", response_model=list[TopCategoryRow])
async def top_categories(plantId: str = Depends(resolve_plant_id), db: AsyncSession = Depends(get_db)):
    rows: list[TopCategoryRow] = []

    hira_cats = (
        await db.execute(
            select(HiraHazard.category, func.count(HiraEntryHazard.id))
            .join(HiraEntryHazard, HiraEntryHazard.hazardId == HiraHazard.id)
            .join(HiraEntry, HiraEntry.id == HiraEntryHazard.entryId)
            .join(HiraStudy, HiraStudy.id == HiraEntry.studyId)
            .where(HiraStudy.plantId == plantId)
            .group_by(HiraHazard.category)
            .order_by(func.count(HiraEntryHazard.id).desc())
            .limit(10)
        )
    ).all()
    for cat, count in hira_cats:
        rows.append(TopCategoryRow(source="HIRA", category=cat, count=int(count)))

    if await _eai_enabled_for(db, plantId):
        eai_cats = (
            await db.execute(
                select(EaiAspectCategory.code, func.count(EaiEntryAspect.id))
                .join(EaiAspect, EaiAspect.categoryId == EaiAspectCategory.id)
                .join(EaiEntryAspect, EaiEntryAspect.aspectId == EaiAspect.id)
                .join(EaiEntry, EaiEntry.id == EaiEntryAspect.entryId)
                .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
                .where(EaiStudy.plantId == plantId)
                .group_by(EaiAspectCategory.code)
                .order_by(func.count(EaiEntryAspect.id).desc())
                .limit(10)
            )
        ).all()
        for cat, count in eai_cats:
            rows.append(TopCategoryRow(source="EAI", category=cat, count=int(count)))

    return rows


# ─────────────────────────────────────────────────────────────────────
# 8. Incident Linkage (last 90d)
# ─────────────────────────────────────────────────────────────────────


@router.get("/incident-linkage", response_model=list[IncidentLinkageRow])
async def incident_linkage(plantId: str = Depends(resolve_plant_id), db: AsyncSession = Depends(get_db)):
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    rows = (
        await db.execute(
            select(Incident)
            .where(Incident.plantId == plantId)
            .where(Incident.incidentDateTime >= cutoff)
            .order_by(Incident.incidentDateTime.desc())
            .limit(50)
        )
    ).scalars().all()

    out: list[IncidentLinkageRow] = []
    for inc in rows:
        # Heuristic linkage: HIRA entries with same plant area + active status
        hira_ids: list[str] = []
        if getattr(inc, "areaId", None):
            hira_matches = (
                await db.execute(
                    select(HiraEntry.id)
                    .join(HiraStudy, HiraStudy.id == HiraEntry.studyId)
                    .where(HiraStudy.plantId == plantId)
                    .where(HiraEntry.areaId == inc.areaId)
                    .where(HiraEntry.isCurrentVersion.is_(True))
                    .limit(5)
                )
            ).scalars().all()
            hira_ids = list(hira_matches)

        out.append(
            IncidentLinkageRow(
                incidentId=inc.id,
                incidentNumber=getattr(inc, "number", None),
                occurredAt=getattr(inc, "incidentDateTime", None),
                severity=getattr(inc, "severity", None),
                linkedHiraEntryIds=hira_ids,
                linkedEaiEntryIds=[],
            )
        )
    return out


# ─────────────────────────────────────────────────────────────────────
# Persona dashboard config
# ─────────────────────────────────────────────────────────────────────


_PERSONA_DEFAULTS: dict[str, list[str]] = {
    "PLANT_HEAD": ["top-risks", "risk-trend", "coverage", "review-compliance", "incident-linkage"],
    "CORPORATE_HSE": ["risk-trend", "coverage", "top-categories", "review-compliance"],
    "HSE_MANAGER": ["top-risks", "review-compliance", "control-effectiveness", "heatmap"],
    "DEPARTMENT_HEAD": ["top-risks", "review-compliance"],
    "ENVIRONMENT_MANAGER": ["top-risks", "top-categories", "review-compliance", "control-effectiveness"],
}


@router.get("/persona-config", response_model=PersonaDashboardConfig)
async def persona_config(persona: str = Query(...)):
    widgets = _PERSONA_DEFAULTS.get(
        persona.upper(),
        ["top-risks", "risk-trend", "coverage", "review-compliance"],
    )
    return PersonaDashboardConfig(persona=persona.upper(), widgets=widgets)
