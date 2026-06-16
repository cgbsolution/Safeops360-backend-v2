"""Dashboard overview router. Mounts at /api/dashboard.

One endpoint — GET /api/dashboard/overview — returns the full payload that
the mobile EHS dashboard needs in a single round-trip:
  * 10 headline KPIs (days since last LTI, LTIFR/TRIR 12mo, active permits,
    obs MTD / open / closed, near-miss 12mo, training/inspection compliance)
  * 6-month observations + near-miss trend for the line chart
  * Rolling-12-month Heinrich pyramid
  * Top unsafe categories (top 6)
  * Recent activity feed across modules

Computation mirrors the web `src/app/(dashboard)/dashboard/page.tsx` so the
mobile + web numbers stay aligned.

Enum-typed columns (Observation.type/category/status, Incident.type, etc.)
are read via `cast(..., String)` so unknown values that exist in
seeded/production data (OTHERS, EMERGENCY_PREP, …) don't 500 the read path
through SQLAlchemy's strict native-enum coercion.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import String, cast, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.equipment import Inspection
from app.models.incident import Incident
from app.models.near_miss import NearMiss
from app.models.observation import Observation
from app.models.permit import Permit
from app.models.training import TrainingRecord
from app.models.user import User
from app.schemas.dashboard import (
    DashboardKpis,
    DashboardOverview,
    HeinrichLevel,
    RecentActivityItem,
    TopUnsafeCategory,
    TrendPoint,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _humanize(s: str) -> str:
    return s.replace("_", " ").title()


def _month_label(d: datetime) -> str:
    return d.strftime("%b %y")


def _start_of_month(now: datetime, months_offset: int = 0) -> datetime:
    year = now.year
    month = now.month + months_offset
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    # Match the DB's tz convention: most date columns are read back
    # tz-naive (Prisma writes timestamp without tz). Returning naive
    # datetimes here lets `o.date >= start_of_month` compare cleanly.
    return datetime(year, month, 1)


def _as_naive(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.replace(tzinfo=None) if d.tzinfo is not None else d


@router.get("/overview", response_model=DashboardOverview)
async def dashboard_overview(
    user: User = Depends(get_current_user),  # noqa: ARG001 — auth gate only
    db: AsyncSession = Depends(get_db),
) -> DashboardOverview:
    now_aware = datetime.now(timezone.utc)
    now = now_aware.replace(tzinfo=None)  # comparisons use naive throughout
    start_of_month = _start_of_month(now)
    six_months_ago = _start_of_month(now, -5)
    twelve_months_ago = _start_of_month(now, -11)

    # --- Bounded reads --------------------------------------------------------
    # All enum-typed columns are cast to String so unknown values don't 500
    # the read path (e.g. seeded "OTHERS" / "EMERGENCY_PREP" not in the Python
    # enum).

    obs_rows = (
        await db.execute(
            select(
                Observation.id,
                Observation.number,
                Observation.date,
                cast(Observation.type, String).label("type"),
                cast(Observation.category, String).label("category"),
                cast(Observation.status, String).label("status"),
                Observation.description,
            )
            .where(Observation.date >= six_months_ago)
            .order_by(Observation.date.desc())
            .limit(300)
        )
    ).all()

    nm_rows = (
        await db.execute(
            select(NearMiss.id, NearMiss.number, NearMiss.date, NearMiss.description)
            .where(NearMiss.date >= six_months_ago)
            .order_by(NearMiss.date.desc())
            .limit(300)
        )
    ).all()

    permit_rows = (
        await db.execute(
            select(
                Permit.id,
                Permit.number,
                cast(Permit.type, String).label("type"),
                cast(Permit.status, String).label("status"),
                Permit.scopeOfWork,
                Permit.createdAt,
            )
            .order_by(Permit.createdAt.desc())
            .limit(150)
        )
    ).all()

    incident_rows = (
        await db.execute(
            select(
                Incident.id,
                Incident.number,
                Incident.date,
                cast(Incident.type, String).label("type"),
                Incident.description,
            )
            .where(Incident.date >= twelve_months_ago)
            .order_by(Incident.date.desc())
            .limit(200)
        )
    ).all()

    training_rows = (
        await db.execute(
            select(
                TrainingRecord.id,
                TrainingRecord.date,
                TrainingRecord.passed,
                TrainingRecord.validUntil,
            )
            .order_by(TrainingRecord.date.desc())
            .limit(300)
        )
    ).all()

    inspection_rows = (
        await db.execute(
            select(
                Inspection.id,
                cast(Inspection.status, String).label("status"),
                Inspection.scheduledDate,
            )
            .order_by(Inspection.scheduledDate.desc())
            .limit(150)
        )
    ).all()

    # The SQLAlchemy `Manhours` model is out of sync with the actual DB
    # (Prisma owns the schema and uses employeeHours / contractorHours /
    # rwcCount / fatalityCount which the SA model misnames). Use raw SQL
    # against the columns that actually exist so the dashboard isn't
    # blocked on the model fix.
    manhours = (
        await db.execute(
            text(
                'SELECT "ltiCount", "mtcCount", "rwcCount", "fatalityCount", '
                '"employeeHours", "contractorHours" '
                'FROM "Manhours" ORDER BY year DESC, month DESC LIMIT 36'
            )
        )
    ).all()

    # --- KPI calculations -----------------------------------------------------
    obs_mtd = sum(1 for o in obs_rows if o.date >= start_of_month)
    obs_open = sum(1 for o in obs_rows if o.status != "CLOSED")
    obs_closed = sum(1 for o in obs_rows if o.status == "CLOSED")
    active_permits = sum(
        1 for p in permit_rows if p.status in ("ACTIVE", "SAFETY_APPROVED")
    )

    total_lti = sum(int(r[0] or 0) for r in manhours)
    total_mtc = sum(int(r[1] or 0) for r in manhours)
    total_rwc = sum(int(r[2] or 0) for r in manhours)
    total_fatal = sum(int(r[3] or 0) for r in manhours)
    total_hours = sum(int((r[4] or 0) + (r[5] or 0)) for r in manhours)
    ltifr12 = round((total_lti * 1_000_000) / total_hours, 2) if total_hours > 0 else 0.0
    total_recordable = total_lti + total_mtc + total_rwc + total_fatal
    trir12 = round((total_recordable * 200_000) / total_hours, 2) if total_hours > 0 else 0.0

    lti_incidents = [i for i in incident_rows if i.type in ("LTI", "FATALITY")]
    days_since_last_lti = (
        (now - _as_naive(lti_incidents[0].date)).days  # type: ignore[operator]
        if lti_incidents
        else 365
    )

    training_total = len(training_rows)
    valid_training = sum(
        1 for t in training_rows if t.passed and t.validUntil and t.validUntil > now
    )
    training_compliance = (
        round((valid_training / training_total) * 100) if training_total > 0 else 0
    )

    inspection_done = sum(1 for i in inspection_rows if i.status == "COMPLETED")
    inspection_compliance = (
        round((inspection_done / len(inspection_rows)) * 100) if inspection_rows else 0
    )

    kpis = DashboardKpis(
        daysSinceLastLti=days_since_last_lti,
        ltifr12mo=ltifr12,
        trir12mo=trir12,
        activePermits=active_permits,
        observationsMtd=obs_mtd,
        observationsOpen=obs_open,
        observationsClosed=obs_closed,
        nearMiss12mo=sum(1 for n in nm_rows if n.date >= twelve_months_ago),
        trainingCompliancePct=training_compliance,
        inspectionCompliancePct=inspection_compliance,
    )

    # --- 6-month observations / near-miss trend -------------------------------
    trend: list[TrendPoint] = []
    for i in range(5, -1, -1):
        m_start = _start_of_month(now, -i)
        m_end = _start_of_month(now, -i + 1)
        trend.append(
            TrendPoint(
                month=_month_label(m_start),
                observations=sum(1 for o in obs_rows if m_start <= o.date < m_end),
                nearMiss=sum(1 for n in nm_rows if m_start <= n.date < m_end),
            )
        )

    # --- Heinrich pyramid (rolling 12 months) --------------------------------
    inc_last12 = [i for i in incident_rows if i.date >= twelve_months_ago]
    obs_unsafe_last12 = sum(
        1
        for o in obs_rows
        if o.date >= twelve_months_ago and o.type in ("UNSAFE_ACT", "UNSAFE_CONDITION")
    )
    nm_last12 = sum(1 for n in nm_rows if n.date >= twelve_months_ago)
    heinrich = [
        HeinrichLevel(
            level="Fatality",
            count=sum(1 for i in inc_last12 if i.type == "FATALITY"),
            color="#7f1d1d",
        ),
        HeinrichLevel(
            level="LTI",
            count=sum(1 for i in inc_last12 if i.type == "LTI"),
            color="#dc2626",
        ),
        HeinrichLevel(
            level="RWC + MTC",
            count=sum(1 for i in inc_last12 if i.type in ("RWC", "MTC")),
            color="#ea580c",
        ),
        HeinrichLevel(
            level="First Aid",
            count=sum(1 for i in inc_last12 if i.type == "FIRST_AID"),
            color="#f59e0b",
        ),
        HeinrichLevel(level="Near Miss", count=nm_last12, color="#3b82f6"),
        HeinrichLevel(
            level="Unsafe Acts/Conds", count=obs_unsafe_last12, color="#7c3aed"
        ),
    ]

    # --- Top unsafe categories from observations ------------------------------
    cat_counts: dict[str, int] = {}
    for o in obs_rows:
        if o.type in ("UNSAFE_ACT", "UNSAFE_CONDITION") and o.category:
            label = _humanize(str(o.category))
            cat_counts[label] = cat_counts.get(label, 0) + 1
    top_unsafe = [
        TopUnsafeCategory(category=k, count=v)
        for k, v in sorted(cat_counts.items(), key=lambda kv: kv[1], reverse=True)[:6]
    ]

    # --- Recent activity feed -------------------------------------------------
    # Each row carries `recordId` + `module` so the mobile dashboard can
    # deep-link directly to the matching detail screen on tap.
    feed: list[RecentActivityItem] = []
    for o in obs_rows[:4]:
        desc = o.description or ""
        feed.append(
            RecentActivityItem(
                type="Observation",
                title=desc[:80] + ("…" if len(desc) > 80 else ""),
                meta=o.number,
                date=o.date,
                tone="primary",
                recordId=o.id,
                module="OBSERVATION",
            )
        )
    for n in nm_rows[:3]:
        desc = n.description or ""
        feed.append(
            RecentActivityItem(
                type="Near Miss",
                title=desc[:80] + ("…" if len(desc) > 80 else ""),
                meta=n.number,
                date=n.date,
                tone="warning",
                recordId=n.id,
                module="NEAR_MISS",
            )
        )
    for p in permit_rows[:3]:
        scope = p.scopeOfWork or ""
        feed.append(
            RecentActivityItem(
                type="Permit",
                title=scope[:80],
                meta=f"{p.number} · {_humanize(p.type)} · {_humanize(p.status)}",
                date=p.createdAt,
                tone="info",
                recordId=p.id,
                module="PTW",
            )
        )
    for i in incident_rows[:2]:
        desc = i.description or ""
        feed.append(
            RecentActivityItem(
                type="Incident",
                title=desc[:80],
                meta=f"{i.number} · {_humanize(i.type)}",
                date=i.date,
                tone="danger",
                recordId=i.id,
                module="INCIDENT",
            )
        )
    feed.sort(key=lambda x: x.date, reverse=True)
    recent_activity = feed[:12]

    return DashboardOverview(
        asOf=now,
        kpis=kpis,
        trend6mo=trend,
        heinrich=heinrich,
        topUnsafe=top_unsafe,
        recentActivity=recent_activity,
    )
