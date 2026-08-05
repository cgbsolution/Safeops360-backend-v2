"""Feature 8 — Cost-of-unsafety.

Turns per-incident cost fields into a derived, defensible total and a plant-level
rolling-12-month rollup a CFO can act on. Rates are NEVER hardcoded — downtime
and investigation-labor costs use the per-plant `PlantCostConfig`; with no
config, the derived components are zero and `costConfidence = 'estimated'`, so
the number never silently uses a generic default.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capa import Capa
from app.models.incident import Incident, IncidentInvestigationMember
from app.models.incident_intel import PlantCostConfig

# Estimated investigation effort per team member when no timesheet exists (the
# investigation-team model logs no hours). Deliberately conservative.
_DEFAULT_INVESTIGATION_HOURS_PER_MEMBER = 8.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


async def plant_config(db: AsyncSession, plant_id: str | None) -> PlantCostConfig | None:
    if not plant_id:
        return None
    return (
        await db.execute(select(PlantCostConfig).where(PlantCostConfig.plantId == plant_id))
    ).scalar_one_or_none()


async def compute_cost_impact(
    db: AsyncSession, incident: Incident, *, downtime_hours: float | None = None
) -> dict[str, Any]:
    """Derive `incident.costImpact` from the per-incident cost fields + the
    plant's cost config. Returns (and persists) the breakdown. Caller commits."""
    cfg = await plant_config(db, incident.plantId)

    # Direct repair — manual entry at investigation stage.
    direct_repair = _f(incident.costPropertyDamage)

    # Downtime — hours × plant hourly production value. If explicit lost-production
    # cost was entered, trust it; else derive from downtime hours × config rate.
    if _f(incident.costLostProduction) > 0:
        downtime_cost = _f(incident.costLostProduction)
        downtime_basis = "entered"
    elif cfg and downtime_hours:
        downtime_cost = downtime_hours * _f(cfg.hourlyProductionValue)
        downtime_basis = "derived"
    else:
        downtime_cost = 0.0
        downtime_basis = "unavailable"

    # Investigation labor — team size × estimated hours × loaded rate (config).
    members = (
        await db.execute(
            select(IncidentInvestigationMember).where(IncidentInvestigationMember.incidentId == incident.id)
        )
    ).scalars().all()
    labor_cost = 0.0
    if cfg:
        rates = cfg.loadedLaborRateByRole or {}
        for m in members:
            rate = _f(rates.get(m.role)) or _f(cfg.defaultLaborRate)
            labor_cost += _DEFAULT_INVESTIGATION_HOURS_PER_MEMBER * rate

    insurance = _f(incident.costInsurance) if incident.costInsurance is not None else None

    total = direct_repair + downtime_cost + labor_cost + _f(insurance)
    # Confirmed only when every component is a real entered figure (no estimation).
    confirmed = (
        downtime_basis != "derived"
        and labor_cost == 0.0  # no estimated labor mixed in
        and cfg is not None
    )
    detail = {
        "directRepairCost": round(direct_repair, 2),
        "estimatedDowntimeCost": round(downtime_cost, 2),
        "downtimeBasis": downtime_basis,
        "investigationLaborCost": round(labor_cost, 2),
        "estimatedInsuranceImpact": round(insurance, 2) if insurance is not None else None,
        "totalCost": round(total, 2),
        "currency": (cfg.currency if cfg else "INR"),
        "costConfidence": "confirmed" if confirmed else "estimated",
        "hasPlantConfig": cfg is not None,
        "computedAt": _now().isoformat(),
    }
    incident.costImpact = detail
    return detail


async def plant_rollup(db: AsyncSession, plant_id: str, *, months: int = 12) -> dict[str, Any]:
    """Rolling-window cost-of-unsafety rollup for a plant: total, by incident
    type, by area, month-over-month, and the preventive-CAPA cost comparison."""
    cutoff = _now() - timedelta(days=int(months * 30.4))
    incidents = (
        await db.execute(
            select(Incident)
            .where(Incident.plantId == plant_id)
            .where(Incident.deletedAt.is_(None))
            .where(Incident.date >= cutoff)
        )
    ).scalars().all()

    def incident_total(i: Incident) -> float:
        if i.costImpact and i.costImpact.get("totalCost") is not None:
            return _f(i.costImpact["totalCost"])
        return _f(i.costTotal)  # fall back to the raw entered rollup

    total = 0.0
    by_type: dict[str, float] = {}
    by_area: dict[str, float] = {}
    by_month: dict[str, float] = {}
    contributing = 0
    for i in incidents:
        c = incident_total(i)
        if c <= 0:
            continue
        contributing += 1
        total += c
        t = i.type.value if i.type else "UNKNOWN"
        by_type[t] = by_type.get(t, 0.0) + c
        a = i.areaId or "unassigned"
        by_area[a] = by_area.get(a, 0.0) + c
        month = (i.date or _now()).strftime("%Y-%m")
        by_month[month] = by_month.get(month, 0.0) + c

    # Preventive-CAPA cost for the plant — the "what prevention would have cost".
    capa_rows = (
        await db.execute(
            select(Capa.estimatedActionsCost, Capa.actualCost)
            .where(Capa.plantId == plant_id)
            .where(Capa.isDeleted.is_(False))
            .where(Capa.createdAt >= cutoff)
        )
    ).all()
    capa_preventive = sum(_f(r[1]) or _f(r[0]) for r in capa_rows)

    cfg = await plant_config(db, plant_id)
    return {
        "plantId": plant_id,
        "windowMonths": months,
        "totalCost": round(total, 2),
        "incidentCount": len(incidents),
        "contributingCount": contributing,
        "byType": {k: round(v, 2) for k, v in sorted(by_type.items(), key=lambda x: -x[1])},
        "byArea": {k: round(v, 2) for k, v in sorted(by_area.items(), key=lambda x: -x[1])},
        "byMonth": {k: round(by_month[k], 2) for k in sorted(by_month)},
        "capaPreventiveCost": round(capa_preventive, 2),
        "currency": (cfg.currency if cfg else "INR"),
        "hasPlantConfig": cfg is not None,
    }
