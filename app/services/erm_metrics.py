"""ERM Phase 2 — Metric Provider Catalogue.

Each MODULE_FED KRI reads a live metric through a provider registered here. The
constraint is "no direct table access into source schemas scattered through the
codebase" — every cross-module metric is encapsulated in one provider, declared
in METRIC_PROVIDERS, and surfaced to the KRI admin screen via the catalogue API.

Provider contract: async compute(db, period_end: datetime) -> float | None
(None = no data available for the period). Metrics aggregate across all plants
(the demo tenant = Meridian NW+SW); plant-scoping a KRI is a Phase-3 refinement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Manhours-based (raw SQL — the Manhours SQLAlchemy model is out of sync with
#    the DB column names; dashboard.py does the same). ────────────────────────
async def _manhours_window(db: AsyncSession, period_end: datetime) -> dict[str, int]:
    ey, em = period_end.year, period_end.month
    end_idx = ey * 12 + em
    start_idx = end_idx - 12  # rolling 12 months
    rows = (
        await db.execute(
            text(
                'SELECT COALESCE(SUM("ltiCount"),0), COALESCE(SUM("mtcCount"),0), '
                'COALESCE(SUM("rwcCount"),0), COALESCE(SUM("fatalityCount"),0), '
                'COALESCE(SUM("employeeHours"),0), COALESCE(SUM("contractorHours"),0) '
                'FROM "Manhours" WHERE (year*12 + month) > :s AND (year*12 + month) <= :e'
            ),
            {"s": start_idx, "e": end_idx},
        )
    ).first()
    lti, mtc, rwc, fatal, emp_h, con_h = (int(x or 0) for x in (rows or (0, 0, 0, 0, 0, 0)))
    return {"lti": lti, "mtc": mtc, "rwc": rwc, "fatal": fatal, "hours": emp_h + con_h}


async def ltifr_12m(db: AsyncSession, period_end: datetime) -> float | None:
    m = await _manhours_window(db, period_end)
    if m["hours"] <= 0:
        return None
    return round((m["lti"] + m["fatal"]) * 1_000_000 / m["hours"], 2)


async def trir_12m(db: AsyncSession, period_end: datetime) -> float | None:
    m = await _manhours_window(db, period_end)
    if m["hours"] <= 0:
        return None
    return round((m["lti"] + m["mtc"] + m["rwc"] + m["fatal"]) * 200_000 / m["hours"], 2)


async def near_miss_ratio(db: AsyncSession, period_end: datetime) -> float | None:
    """Near-miss : incident ratio for the trailing 30 days (higher = better
    reporting culture → LOWER_IS_WORSE)."""
    from app.models.incident import Incident
    from app.models.near_miss import NearMiss

    from datetime import timedelta
    start = period_end - timedelta(days=30)
    nm = (await db.execute(select(func.count()).select_from(NearMiss).where(NearMiss.createdAt >= start))).scalar() or 0
    inc = (await db.execute(select(func.count()).select_from(Incident).where(Incident.createdAt >= start))).scalar() or 0
    if inc <= 0:
        return float(nm) if nm else None
    return round(nm / inc, 2)


# ── CAPA-based ──────────────────────────────────────────────────────────────
_CAPA_OPEN = ("DRAFT", "SUBMITTED", "UNDER_RCA", "ACTIONS_PLANNED", "ACTIONS_IN_PROGRESS", "PENDING_VERIFICATION")


async def capa_overdue_pct(db: AsyncSession, period_end: datetime) -> float | None:
    from app.models.capa import Capa

    total_open = (await db.execute(select(func.count()).select_from(Capa).where(Capa.state.in_(_CAPA_OPEN)))).scalar() or 0
    if total_open <= 0:
        return 0.0
    overdue = (
        await db.execute(
            select(func.count()).select_from(Capa).where(Capa.state.in_(_CAPA_OPEN)).where(Capa.closureTargetDate < period_end)
        )
    ).scalar() or 0
    return round(overdue * 100 / total_open, 1)


async def capa_avg_closure_days(db: AsyncSession, period_end: datetime) -> float | None:
    from datetime import timedelta

    from app.models.capa import Capa

    start = period_end - timedelta(days=90)
    rows = (
        await db.execute(
            select(Capa.createdAt, Capa.closedAt)
            .where(Capa.closedAt.is_not(None))
            .where(Capa.closedAt >= start)
            .where(Capa.closedAt <= period_end)
        )
    ).all()
    if not rows:
        return None
    days = [(c - cr).days for cr, c in rows if c and cr]
    return round(sum(days) / len(days), 1) if days else None


# ── Audit-based ─────────────────────────────────────────────────────────────
async def audit_nc_rate(db: AsyncSession, period_end: datetime) -> float | None:
    """Non-conformances per conducted audit over the last 90 days — sourced from
    the centralised CAMS engine (TC-14: CAMS feeds the ERM KRI). Counts every
    audit AND inspection (one engine), so the KRI reflects the whole programme."""
    from datetime import timedelta

    from app.models.cams import CamsEngagement, CamsFinding

    start = period_end - timedelta(days=90)
    conducted = ("FIELDWORK_COMPLETE", "FINDINGS_REVIEW", "REPORT_ISSUED", "CLOSED")
    eng_ids = (
        await db.execute(
            select(CamsEngagement.id)
            .where(CamsEngagement.isDeleted.is_(False))
            .where(CamsEngagement.status.in_(conducted))
            .where(CamsEngagement.conductedDate >= start)
            .where(CamsEngagement.conductedDate <= period_end)
        )
    ).scalars().all()
    if not eng_ids:
        return None
    ncs = (
        await db.execute(
            select(func.count()).select_from(CamsFinding)
            .where(CamsFinding.engagementId.in_(list(eng_ids)))
            .where(CamsFinding.isDeleted.is_(False))
            .where(CamsFinding.severity.in_(("MINOR_NC", "MAJOR_NC", "CRITICAL_NC")))
        )
    ).scalar() or 0
    return round(ncs / len(eng_ids), 1)


async def audit_overdue(db: AsyncSession, period_end: datetime) -> float | None:
    """Scheduled CAMS engagements past their planned date but not yet conducted
    (TC-14: CAMS feeds the ERM KRI)."""
    from app.models.cams import CamsEngagement

    n = (
        await db.execute(
            select(func.count()).select_from(CamsEngagement)
            .where(CamsEngagement.isDeleted.is_(False))
            .where(CamsEngagement.plannedDate < period_end)
            .where(CamsEngagement.status.in_(("PLANNED", "SCHEDULED", "IN_PROGRESS")))
        )
    ).scalar() or 0
    return float(n)


# ── Training / competency ───────────────────────────────────────────────────
async def competency_currency_pct(db: AsyncSession, period_end: datetime) -> float | None:
    from app.models.competency_matrix import Competency, CompetencyRecord

    total = (
        await db.execute(
            select(func.count()).select_from(CompetencyRecord)
            .join(Competency, Competency.id == CompetencyRecord.competencyId)
            .where(Competency.category == "safety_critical")
        )
    ).scalar() or 0
    if total <= 0:
        return None
    current = (
        await db.execute(
            select(func.count()).select_from(CompetencyRecord)
            .join(Competency, Competency.id == CompetencyRecord.competencyId)
            .where(Competency.category == "safety_critical")
            .where(CompetencyRecord.state == "validated")
            .where((CompetencyRecord.validUntil.is_(None)) | (CompetencyRecord.validUntil > period_end))
        )
    ).scalar() or 0
    return round(current * 100 / total, 1)


# ── Phase-2 self-referential metrics ────────────────────────────────────────
async def compliance_overdue_obligations(db: AsyncSession, period_end: datetime) -> float | None:
    from app.models.erm_p2 import LegalObligation

    n = (
        await db.execute(
            select(func.count()).select_from(LegalObligation)
            .where(LegalObligation.status == "OVERDUE")
            .where(LegalObligation.isDeleted.is_(False))
        )
    ).scalar() or 0
    return float(n)


async def loss_net_quarter(db: AsyncSession, period_end: datetime) -> float | None:
    from app.models.erm_p2 import LossEvent

    q = (period_end.month - 1) // 3
    qstart = datetime(period_end.year, q * 3 + 1, 1, tzinfo=timezone.utc)
    total = (
        await db.execute(
            select(func.coalesce(func.sum(LossEvent.netLossInr), 0.0))
            .where(LossEvent.isNearMiss.is_(False))
            .where(LossEvent.status.in_(("QUANTIFIED", "CLOSED")))
            .where(LossEvent.eventDate >= qstart)
            .where(LossEvent.eventDate <= period_end)
            .where(LossEvent.isDeleted.is_(False))
        )
    ).scalar() or 0.0
    return round(float(total) / 100_000.0, 1)  # ₹ → ₹ Lakh


@dataclass(frozen=True)
class MetricProvider:
    key: str
    source_module: str
    label: str
    unit: str
    direction: str
    frequency: str
    compute: Callable[[AsyncSession, datetime], Awaitable[float | None]]


METRIC_PROVIDERS: dict[str, MetricProvider] = {
    p.key: p
    for p in [
        MetricProvider("incident.ltifr_12m", "Incident Investigation", "LTIFR — rolling 12 months", "per mn manhours", "HIGHER_IS_WORSE", "MONTHLY", ltifr_12m),
        MetricProvider("incident.trir_12m", "Incident Investigation", "TRIR — rolling 12 months", "per mn manhours", "HIGHER_IS_WORSE", "MONTHLY", trir_12m),
        MetricProvider("incident.near_miss_ratio", "Safety Observation", "Near-miss : incident ratio", "ratio", "LOWER_IS_WORSE", "MONTHLY", near_miss_ratio),
        MetricProvider("capa.overdue_pct", "CAPA Universal", "% CAPA items overdue", "%", "HIGHER_IS_WORSE", "MONTHLY", capa_overdue_pct),
        MetricProvider("capa.avg_closure_days", "CAPA Universal", "Average CAPA closure days", "days", "HIGHER_IS_WORSE", "QUARTERLY", capa_avg_closure_days),
        MetricProvider("audit.nc_rate", "Audit Management", "Non-conformances per audit", "NCs/audit", "HIGHER_IS_WORSE", "QUARTERLY", audit_nc_rate),
        MetricProvider("audit.overdue_audits", "Audit Management", "Overdue scheduled audits", "count", "HIGHER_IS_WORSE", "MONTHLY", audit_overdue),
        MetricProvider("training.competency_currency_pct", "Skill Matrix", "% safety-critical competencies current", "%", "LOWER_IS_WORSE", "MONTHLY", competency_currency_pct),
        MetricProvider("compliance.overdue_obligations", "Compliance Register", "Overdue statutory obligations", "count", "HIGHER_IS_WORSE", "MONTHLY", compliance_overdue_obligations),
        MetricProvider("loss.net_loss_quarter", "Loss Event DB", "Net loss — current quarter", "₹ Lakh", "HIGHER_IS_WORSE", "QUARTERLY", loss_net_quarter),
    ]
}


def catalogue() -> list[dict]:
    return [
        {"key": p.key, "sourceModule": p.source_module, "label": p.label, "unit": p.unit, "direction": p.direction, "frequency": p.frequency}
        for p in METRIC_PROVIDERS.values()
    ]
