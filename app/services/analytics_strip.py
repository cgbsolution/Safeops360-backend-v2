"""Analytics-strip metrics — the server-side half of the module landing-page
KPI band.

Every module page renders the same strip layout (3 KPI tiles │ 12-point
sparkline │ alert chips). Until now each strip was computed in the Next.js
layer by querying Prisma directly, which meant the frontend held a second
copy of every module's business rules (what counts as "open", which statuses
are terminal, how the NM:LTI ratio is derived) AND a second database
connection.

This module owns those rules. It returns RAW METRICS ONLY — numbers and
bucket counts. Tile labels, colours, hrefs, badge tone and delta wording stay
in the frontend where they belong; the pure helpers in
`src/lib/dashboard/strip.ts` turn these numbers into a rendered strip.

Scoping note — this is a deliberate behaviour change, and a correction. The
old Prisma strips scoped by a hard-coded role-name allowlist
(`GROUP_WIDE_ROLES = ADMIN | CORPORATE_HSE | CEO | MD | DIRECTOR` in
`lib/dashboard/scope.ts`), so a role granted ALL_PLANTS through RBAC but not
named in that list saw a strip narrower than the list below it. Here every
strip scopes through the same `can()` + `get_accessible_plants()` path the
module's list endpoint uses, so the strip and the list can no longer disagree.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capa import Capa
from app.models.incident import Incident, IncidentCapa
from app.models.moc import ChangeRequest
from app.models.near_miss import NearMiss
from app.models.observation import Observation
from app.models.permit import Permit
from app.models.ppe import PpeItem
from app.models.user import User
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
)

# Row ceiling on the "fetch and derive in Python" queries. Matches the `take:
# 5000` the Prisma strips used, so a tenant large enough to truncate sees the
# same numbers it saw before rather than a silent change.
_ROW_CAP = 5000
_ITEM_CAP = 10_000

DAY = timedelta(days=1)


class StripDenied(Exception):
    """Caller holds no read grant on the module (or a grant that resolves to an
    empty plant set). The router turns this into an honest zeroed strip rather
    than a 403 — a user who cannot read the module still lands on the page and
    must see *something* truthful.

    Carries the module's ZEROED PAYLOAD, not just a flag. The response shape
    stays identical whether or not the caller is denied, so a strip component
    never has to branch on `denied` to avoid rendering `undefined` into a tile.
    """

    def __init__(self, empty: dict[str, Any]) -> None:
        super().__init__("No read grant for this module")
        self.empty = empty


def _empty(counts: dict[str, Any], buckets, kind: str = "month") -> dict[str, Any]:
    """Build a denial payload: the module's metrics at zero/None, plus a flat
    sparkline over the real buckets so the band keeps its shape."""
    payload = {
        **counts,
        "trendCounts": [0] * len(buckets),
        "bucketStarts": _iso(buckets),
    }
    if kind != "month":
        payload["bucketKind"] = kind
    return payload


# ── shared time helpers ──────────────────────────────────────────────


def _month_start(dt: datetime, offset: int = 0) -> datetime:
    """Start of the month `offset` months away from `dt`, in UTC."""
    total = dt.year * 12 + (dt.month - 1) + offset
    return datetime(total // 12, total % 12 + 1, 1, tzinfo=timezone.utc)


def month_buckets(now: datetime, count: int = 12) -> list[tuple[datetime, datetime]]:
    """`count` monthly [start, end) buckets, oldest → newest, the last being
    the month containing `now`."""
    return [
        (_month_start(now, -i), _month_start(now, -i + 1))
        for i in range(count - 1, -1, -1)
    ]


def week_buckets(now: datetime, count: int = 12) -> list[tuple[datetime, datetime]]:
    """`count` rolling 7-day [start, end) buckets ending at `now`. PTW reads
    week-over-week, not month-over-month."""
    return [
        (now - (i + 1) * 7 * DAY, now - i * 7 * DAY) for i in range(count - 1, -1, -1)
    ]


def _bucket_counts(values: Sequence[datetime | None], buckets) -> list[int]:
    counts = [0] * len(buckets)
    for v in values:
        if v is None:
            continue
        v = _aware(v)
        for i, (start, end) in enumerate(buckets):
            if start <= v < end:
                counts[i] += 1
                break
    return counts


def _aware(dt: datetime) -> datetime:
    """Postgres columns are `timestamptz`, but a driver/session can still hand
    back a naive datetime. Comparing naive to aware raises TypeError and would
    500 the whole strip, so normalise to UTC at the boundary."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _iso(buckets) -> list[str]:
    """Bucket starts as ISO strings. The frontend formats its own labels from
    these — sending pre-formatted labels would bake this server's locale into
    the response."""
    return [start.isoformat() for start, _ in buckets]


def _avg_days(pairs: list[tuple[datetime, datetime]]) -> int | None:
    if not pairs:
        return None
    total = sum((_aware(to) - _aware(frm)).total_seconds() for frm, to in pairs)
    return round(total / len(pairs) / 86_400)


def _pct(part: int, whole: int) -> int | None:
    return round(part / whole * 100) if whole else None


# ── shared scoping ───────────────────────────────────────────────────


async def _scope(
    db: AsyncSession,
    user: User,
    permission: str,
    model,
    stmt: Select,
    own_records_cols: Sequence[Any] = (),
) -> Select | None:
    """Apply the module's read scope to `stmt`.

    Returns None when the caller can see nothing — either no grant at all or a
    grant that resolves to an empty plant set. Callers turn None into a zeroed
    strip. Mirrors the list endpoints exactly (`can()` → `get_accessible_plants()`
    → OWN_RECORDS narrowing) so the strip's numbers always match the list's.
    """
    check = await can(db, user.id, permission, PermissionContext())
    if not check.allowed:
        return None

    plants = await get_accessible_plants(db, user.id)
    if plants is not None:
        if not plants:
            return None
        stmt = stmt.where(model.plantId.in_(plants))

    if check.matched_scope == "OWN_RECORDS" and own_records_cols:
        clause = own_records_cols[0] == user.id
        for col in own_records_cols[1:]:
            clause = clause | (col == user.id)
        stmt = stmt.where(clause)

    return stmt


async def _count(db: AsyncSession, stmt: Select) -> int:
    """COUNT(*) over an already-scoped SELECT, without materialising rows."""
    return (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()


# ── Observations ─────────────────────────────────────────────────────

HIGH_SEVERITIES = ("HIGH", "CRITICAL")


async def observation_strip(db: AsyncSession, user: User, now: datetime) -> dict[str, Any]:
    buckets = month_buckets(now)
    base = await _scope(
        db,
        user,
        "OBSERVATION.READ",
        Observation,
        select(Observation),
        (Observation.observerId, Observation.responsiblePersonId),
    )
    if base is None:
        raise StripDenied(
            _empty(
                {
                    "open": 0, "overdue": 0, "highSeverity": 0, "openedThisMonth": 0,
                    "closedMTD": 0, "closedPrevSamePoint": 0,
                    "avg90": None, "avgPrev90": None, "onTimePct": None,
                },
                buckets,
            )
        )

    twelve_months_ago = buckets[0][0]
    start_of_month = _month_start(now)
    start_of_last_month = _month_start(now, -1)
    start_90 = now - 90 * DAY
    start_180 = now - 180 * DAY
    # Compare MTD against the SAME elapsed slice of last month. A mid-month MTD
    # measured against a whole prior month always reads as a false ↓100%.
    same_point_last_month_end = start_of_last_month + (now - start_of_month)

    open_rows = (
        await db.execute(
            base.with_only_columns(Observation.severity, Observation.targetDate)
            .where(Observation.status != "CLOSED")
            .limit(_ROW_CAP)
        )
    ).all()

    trend_rows = (
        await db.execute(
            base.with_only_columns(Observation.date)
            .where(Observation.date >= twelve_months_ago)
            .limit(_ROW_CAP)
        )
    ).all()

    # `closedAt` is set on workflow closure, but seed-closed rows can have it
    # null. Falling back to `updatedAt` is what keeps "Closed MTD" from
    # silently reading 0 on a demo tenant.
    closed_rows = (
        await db.execute(
            base.with_only_columns(
                Observation.date,
                Observation.closedAt,
                Observation.updatedAt,
                Observation.targetDate,
            )
            .where(Observation.status == "CLOSED")
            .where(
                func.coalesce(Observation.closedAt, Observation.updatedAt) >= start_180
            )
            .limit(_ROW_CAP)
        )
    ).all()

    severity_of = lambda r: r.severity.value if hasattr(r.severity, "value") else r.severity
    overdue = sum(1 for r in open_rows if r.targetDate and _aware(r.targetDate) < now)
    high_severity = sum(1 for r in open_rows if severity_of(r) in HIGH_SEVERITIES)

    trend_counts = _bucket_counts([r.date for r in trend_rows], buckets)

    closed = [
        (r.date, _aware(r.closedAt or r.updatedAt), r.targetDate) for r in closed_rows
    ]
    closed_mtd = [c for c in closed if c[1] >= start_of_month]
    closed_prev_same_point = sum(
        1 for c in closed if start_of_last_month <= c[1] < same_point_last_month_end
    )

    last_90 = [c for c in closed if c[1] >= start_90]
    prev_90 = [c for c in closed if start_180 <= c[1] < start_90]

    with_target = [c for c in closed_mtd if c[2]]
    on_time = sum(1 for c in with_target if c[1] <= _aware(c[2]))

    return {
        "open": len(open_rows),
        "overdue": overdue,
        "highSeverity": high_severity,
        "openedThisMonth": trend_counts[-1],
        "closedMTD": len(closed_mtd),
        "closedPrevSamePoint": closed_prev_same_point,
        "avg90": _avg_days([(c[0], c[1]) for c in last_90]),
        "avgPrev90": _avg_days([(c[0], c[1]) for c in prev_90]),
        "onTimePct": _pct(on_time, len(with_target)),
        "trendCounts": trend_counts,
        "bucketStarts": _iso(buckets),
    }


# ── Near-Miss ────────────────────────────────────────────────────────


async def near_miss_strip(db: AsyncSession, user: User, now: datetime) -> dict[str, Any]:
    buckets = month_buckets(now)
    base = await _scope(
        db,
        user,
        "NEAR_MISS.READ",
        NearMiss,
        select(NearMiss),
        (NearMiss.reporterId, NearMiss.actionOwnerId),
    )
    if base is None:
        raise StripDenied(
            _empty(
                {
                    "nm12": 0, "thisMonth": 0, "prevWindowCount": 0,
                    "sameMonthLastYear": 0, "uninvestigated": 0, "lti12": 0,
                },
                buckets,
            )
        )

    twelve_months_ago = buckets[0][0]
    twenty_four_months_ago = _month_start(now, -23)
    start_of_month = _month_start(now)
    start_month_last_year = _month_start(now, -12)
    start_next_month_last_year = _month_start(now, -11)
    seven_days_ago = now - 7 * DAY

    trend_rows = (
        await db.execute(
            base.with_only_columns(NearMiss.date)
            .where(NearMiss.date >= twelve_months_ago)
            .limit(_ROW_CAP)
        )
    ).all()

    prev_window = await _count(
        db,
        base.where(NearMiss.date >= twenty_four_months_ago).where(
            NearMiss.date < twelve_months_ago
        ),
    )
    same_month_last_year = await _count(
        db,
        base.where(NearMiss.date >= start_month_last_year).where(
            NearMiss.date < start_next_month_last_year
        ),
    )
    # Uninvestigated: still REPORTED, nobody owns the action, older than 7 days.
    uninvestigated = await _count(
        db,
        base.where(NearMiss.status == "REPORTED")
        .where(NearMiss.actionOwnerId.is_(None))
        .where(NearMiss.date < seven_days_ago),
    )

    # LTI count for the NM:LTI pyramid ratio. Scoped through INCIDENT.READ, not
    # NEAR_MISS.READ — a user who can see near misses but not incidents must not
    # learn the LTI count through this ratio.
    lti_base = await _scope(
        db, user, "INCIDENT.READ", Incident, select(Incident), (Incident.reporterId,)
    )
    lti_12 = (
        0
        if lti_base is None
        else await _count(
            db,
            lti_base.where(Incident.type == "LTI").where(Incident.date >= twelve_months_ago),
        )
    )

    trend_counts = _bucket_counts([r.date for r in trend_rows], buckets)

    return {
        "nm12": len(trend_rows),
        "thisMonth": sum(1 for r in trend_rows if _aware(r.date) >= start_of_month),
        "prevWindowCount": prev_window,
        "sameMonthLastYear": same_month_last_year,
        "uninvestigated": uninvestigated,
        "lti12": lti_12,
        "trendCounts": trend_counts,
        "bucketStarts": _iso(buckets),
    }


# ── Incidents ────────────────────────────────────────────────────────


async def incident_strip(db: AsyncSession, user: User, now: datetime) -> dict[str, Any]:
    buckets = month_buckets(now)
    base = await _scope(
        db, user, "INCIDENT.READ", Incident, select(Incident), (Incident.reporterId,)
    )
    if base is None:
        raise StripDenied(
            _empty(
                {
                    "open": 0, "stalled": 0, "ltiOpen": 0, "openedThisMonth": 0,
                    "closedMTD": 0, "closedPrevCount": 0,
                    "avgDays": None, "linkagePct": None,
                },
                buckets,
            )
        )

    # Soft-deleted incidents are never readable at any scope.
    base = base.where(Incident.isDeleted.is_(False))

    twelve_months_ago = buckets[0][0]
    start_of_month = _month_start(now)
    start_of_last_month = _month_start(now, -1)
    stalled_before = now - 30 * DAY
    lti_open_before = now - 10 * DAY

    open_rows = (
        await db.execute(
            base.with_only_columns(Incident.date, Incident.type)
            .where(Incident.status != "CLOSED")
            .limit(_ROW_CAP)
        )
    ).all()

    trend_rows = (
        await db.execute(
            base.with_only_columns(Incident.date)
            .where(Incident.date >= twelve_months_ago)
            .limit(_ROW_CAP)
        )
    ).all()

    closed_mtd_rows = (
        await db.execute(
            base.with_only_columns(Incident.date, Incident.closedAt)
            .where(Incident.status == "CLOSED")
            .where(Incident.closedAt >= start_of_month)
            .limit(_ROW_CAP)
        )
    ).all()

    closed_prev = await _count(
        db,
        base.where(Incident.status == "CLOSED")
        .where(Incident.closedAt >= start_of_last_month)
        .where(Incident.closedAt < start_of_month),
    )

    closed_12m = base.where(Incident.status == "CLOSED").where(
        Incident.closedAt >= twelve_months_ago
    )
    closed_total = await _count(db, closed_12m)
    closed_with_capa = await _count(
        db,
        closed_12m.where(
            Incident.id.in_(select(IncidentCapa.incidentId).distinct())
        ),
    )

    type_of = lambda r: r.type.value if hasattr(r.type, "value") else r.type
    stalled = sum(1 for r in open_rows if _aware(r.date) < stalled_before)
    lti_open = sum(
        1
        for r in open_rows
        if type_of(r) in ("LTI", "FATALITY") and _aware(r.date) < lti_open_before
    )

    trend_counts = _bucket_counts([r.date for r in trend_rows], buckets)

    return {
        "open": len(open_rows),
        "stalled": stalled,
        "ltiOpen": lti_open,
        "openedThisMonth": trend_counts[-1],
        "closedMTD": len(closed_mtd_rows),
        "closedPrevCount": closed_prev,
        "avgDays": _avg_days(
            [(r.date, r.closedAt) for r in closed_mtd_rows if r.closedAt]
        ),
        "linkagePct": _pct(closed_with_capa, closed_total),
        "trendCounts": trend_counts,
        "bucketStarts": _iso(buckets),
    }


# ── CAPA ─────────────────────────────────────────────────────────────

# "Open" = not in any terminal or rejected state.
CAPA_CLOSED_STATES = ("CLOSED", "CLOSED_RECURRED", "REJECTED", "CANCELLED")


async def capa_strip(db: AsyncSession, user: User, now: datetime) -> dict[str, Any]:
    buckets = month_buckets(now)
    base = await _scope(db, user, "CAPA.READ", Capa, select(Capa))
    if base is None:
        raise StripDenied(
            _empty(
                {
                    "open": 0, "overdue": 0, "criticalOverdue": 0,
                    "openedThisMonth": 0, "closedMTD": 0, "closedPrev": 0,
                    "effPct": None,
                },
                buckets,
            )
        )

    twelve_months_ago = buckets[0][0]
    start_of_month = _month_start(now)
    start_of_last_month = _month_start(now, -1)
    ninety_days_ago = now - 90 * DAY

    open_rows = (
        await db.execute(
            base.with_only_columns(Capa.severity, Capa.closureTargetDate)
            .where(Capa.state.notin_(CAPA_CLOSED_STATES))
            .limit(_ROW_CAP)
        )
    ).all()

    trend_rows = (
        await db.execute(
            base.with_only_columns(Capa.createdAt)
            .where(Capa.createdAt >= twelve_months_ago)
            .limit(_ROW_CAP)
        )
    ).all()

    closed_mtd = await _count(db, base.where(Capa.closedAt >= start_of_month))
    closed_prev = await _count(
        db,
        base.where(Capa.closedAt >= start_of_last_month).where(
            Capa.closedAt < start_of_month
        ),
    )

    eff_rows = (
        await db.execute(
            base.with_only_columns(Capa.verificationResult)
            .where(Capa.verificationCompletedAt >= ninety_days_ago)
            .limit(_ROW_CAP)
        )
    ).all()

    overdue = sum(
        1 for r in open_rows if r.closureTargetDate and _aware(r.closureTargetDate) < now
    )
    critical_overdue = sum(
        1
        for r in open_rows
        if r.severity in HIGH_SEVERITIES
        and r.closureTargetDate
        and _aware(r.closureTargetDate) < now
    )

    trend_counts = _bucket_counts([r.createdAt for r in trend_rows], buckets)

    return {
        "open": len(open_rows),
        "overdue": overdue,
        "criticalOverdue": critical_overdue,
        "openedThisMonth": trend_counts[-1],
        "closedMTD": closed_mtd,
        "closedPrev": closed_prev,
        "effPct": _pct(
            sum(1 for r in eff_rows if r.verificationResult == "EFFECTIVE"), len(eff_rows)
        ),
        "trendCounts": trend_counts,
        "bucketStarts": _iso(buckets),
    }


# ── PPE ──────────────────────────────────────────────────────────────

# In service = physically fielded equipment (not retired, lost, stolen, or
# recalled out of use).
PPE_IN_SERVICE = ("in_stock", "issued", "under_inspection", "under_repair", "quarantined")


async def ppe_strip(
    db: AsyncSession, user: User, now: datetime, plant_id: str
) -> dict[str, Any]:
    """PPE is always viewed one plant at a time (the module page gates on plant
    selection), so `plant_id` is required — but it is still checked against the
    caller's accessible plants rather than trusted from the query string."""
    buckets = month_buckets(now)
    base = await _scope(db, user, "PPE.READ", PpeItem, select(PpeItem))
    if base is None:
        raise StripDenied(
            _empty(
                {
                    "itemsInService": 0, "inspectionOverdue": 0,
                    "compliancePct": None, "recallsActive": 0,
                    "serviceLifeEnding": 0,
                },
                buckets,
            )
        )
    base = base.where(PpeItem.plantId == plant_id)

    twelve_months_ago = buckets[0][0]
    in_90_days = now + 90 * DAY

    in_service = base.where(PpeItem.status.in_(PPE_IN_SERVICE))

    items_in_service = await _count(db, in_service)
    inspection_overdue = await _count(
        db, in_service.where(PpeItem.nextInspectionDueDate < now)
    )
    recalls_active = await _count(db, base.where(PpeItem.batchUnderRecall.is_(True)))
    service_life_ending = await _count(
        db,
        in_service.where(PpeItem.serviceLifeEndDate >= now).where(
            PpeItem.serviceLifeEndDate <= in_90_days
        ),
    )

    # "People Compliance" is a serviceability proxy: the share of ISSUED (held)
    # items that are neither inspection-overdue nor past service life. The full
    # person-vs-required-PPE matrix lives behind the PPE requirement profiles.
    issued_rows = (
        await db.execute(
            base.with_only_columns(
                PpeItem.nextInspectionDueDate, PpeItem.serviceLifeEndDate
            )
            .where(PpeItem.status == "issued")
            .limit(_ITEM_CAP)
        )
    ).all()
    valid_held = sum(
        1
        for r in issued_rows
        if (not r.nextInspectionDueDate or _aware(r.nextInspectionDueDate) >= now)
        and (not r.serviceLifeEndDate or _aware(r.serviceLifeEndDate) >= now)
    )

    trend_rows = (
        await db.execute(
            base.with_only_columns(PpeItem.commissionedAt)
            .where(PpeItem.commissionedAt >= twelve_months_ago)
            .limit(_ITEM_CAP)
        )
    ).all()

    return {
        "itemsInService": items_in_service,
        "inspectionOverdue": inspection_overdue,
        "compliancePct": _pct(valid_held, len(issued_rows)),
        "recallsActive": recalls_active,
        "serviceLifeEnding": service_life_ending,
        "trendCounts": _bucket_counts([r.commissionedAt for r in trend_rows], buckets),
        "bucketStarts": _iso(buckets),
    }


# ── PTW ──────────────────────────────────────────────────────────────

PTW_ACTIVE_STATUSES = ("ACTIVE", "SAFETY_APPROVED", "PLANT_HEAD_APPROVED")
PTW_TERMINAL_STATUSES = ("CLOSED", "EXPIRED", "REJECTED")


async def ptw_strip(db: AsyncSession, user: User, now: datetime) -> dict[str, Any]:
    buckets = week_buckets(now)  # permits read week-over-week, not monthly
    base = await _scope(db, user, "PTW.READ", Permit, select(Permit))
    if base is None:
        raise StripDenied(
            _empty(
                {
                    "activeCount": 0, "closedMTD": 0, "closedLastMonth": 0,
                    "activatedThisMonth": 0, "onTimePct": None,
                    "avgCycleHours": None, "overdue": 0, "competencyBlocks": 0,
                },
                buckets,
                kind="week",
            )
        )

    twelve_weeks_ago = buckets[0][0]
    start_of_month = _month_start(now)
    start_of_last_month = _month_start(now, -1)

    active_count = await _count(db, base.where(Permit.status.in_(PTW_ACTIVE_STATUSES)))

    closed_rows = (
        await db.execute(
            base.with_only_columns(Permit.closedAt, Permit.validTo)
            .where(Permit.closedAt >= start_of_month)
            .limit(_ROW_CAP)
        )
    ).all()

    closed_last_month = await _count(
        db,
        base.where(Permit.closedAt >= start_of_last_month).where(
            Permit.closedAt < start_of_month
        ),
    )

    activated_rows = (
        await db.execute(
            base.with_only_columns(Permit.createdAt, Permit.activatedAt)
            .where(Permit.activatedAt >= start_of_month)
            .limit(_ROW_CAP)
        )
    ).all()

    overdue = await _count(
        db,
        base.where(Permit.status.notin_(PTW_TERMINAL_STATUSES)).where(
            Permit.validTo < now
        ),
    )

    trend_rows = (
        await db.execute(
            base.with_only_columns(Permit.createdAt)
            .where(Permit.createdAt >= twelve_weeks_ago)
            .limit(_ROW_CAP)
        )
    ).all()

    on_time = sum(
        1
        for r in closed_rows
        if r.closedAt and r.validTo and _aware(r.closedAt) <= _aware(r.validTo)
    )

    cycle_hours = [
        (_aware(r.activatedAt) - _aware(r.createdAt)).total_seconds() / 3600
        for r in activated_rows
        if r.activatedAt
    ]

    return {
        "activeCount": active_count,
        "closedMTD": len(closed_rows),
        "closedLastMonth": closed_last_month,
        "activatedThisMonth": len(activated_rows),
        "onTimePct": _pct(on_time, len(closed_rows)),
        "avgCycleHours": round(sum(cycle_hours) / len(cycle_hours)) if cycle_hours else None,
        "overdue": overdue,
        # No competency-gate-block model exists yet — reported as 0 so the chip
        # renders honestly rather than being hidden.
        "competencyBlocks": 0,
        "trendCounts": _bucket_counts([r.createdAt for r in trend_rows], buckets),
        "bucketStarts": _iso(buckets),
        "bucketKind": "week",
    }


# ── MOC ──────────────────────────────────────────────────────────────


async def moc_strip(db: AsyncSession, user: User, now: datetime) -> dict[str, Any]:
    buckets = month_buckets(now)
    base = await _scope(db, user, "MOC.READ", ChangeRequest, select(ChangeRequest))
    if base is None:
        raise StripDenied(
            _empty(
                {
                    "activeMocs": 0, "awaitingApproval": 0, "overdue": 0,
                    "tempExpiring": 0,
                },
                buckets,
            )
        )

    twelve_months_ago = buckets[0][0]
    in_30_days = now + 30 * DAY

    # The 18-state lifecycle is a lowercase string; closed states contain
    # "closed" and approval-pending states contain "approval". Derived in
    # Python rather than SQL so the rule stays readable and matches the
    # frontend's previous behaviour exactly.
    status_rows = (
        await db.execute(
            base.with_only_columns(
                ChangeRequest.status,
                ChangeRequest.isTemporary,
                ChangeRequest.temporaryExpiryDate,
                ChangeRequest.targetCompletionDate,
            ).limit(_ITEM_CAP)
        )
    ).all()

    trend_rows = (
        await db.execute(
            base.with_only_columns(ChangeRequest.initiatedAt)
            .where(ChangeRequest.initiatedAt >= twelve_months_ago)
            .limit(_ITEM_CAP)
        )
    ).all()

    is_open = lambda s: "closed" not in s and s != "draft"

    active = sum(1 for r in status_rows if is_open(r.status))
    awaiting = sum(1 for r in status_rows if "approval" in r.status)
    overdue = sum(
        1
        for r in status_rows
        if is_open(r.status)
        and r.targetCompletionDate
        and _aware(r.targetCompletionDate) < now
    )
    temp_expiring = sum(
        1
        for r in status_rows
        if r.isTemporary
        and r.temporaryExpiryDate
        and now < _aware(r.temporaryExpiryDate) <= in_30_days
    )

    return {
        "activeMocs": active,
        "awaitingApproval": awaiting,
        "overdue": overdue,
        "tempExpiring": temp_expiring,
        "trendCounts": _bucket_counts([r.initiatedAt for r in trend_rows], buckets),
        "bucketStarts": _iso(buckets),
    }


# ── Training ─────────────────────────────────────────────────────────


async def training_strip(db: AsyncSession, user: User, now: datetime) -> dict[str, Any]:
    """Training compliance + certificate health.

    TrainingRecord and TrainingCertificate carry no plantId of their own — both
    hang off a person — so the plant scope is applied through the holder's
    plant, matching what the page's relation filters did.
    """
    from app.models.training import TrainingCertificate, TrainingRecord

    buckets = month_buckets(now)
    empty = {
        "compliancePct": 0, "validPairs": 0, "applicablePairs": 0,
        "validCerts": 0, "expiredCerts": 0, "expiringCerts": 0,
    }
    check = await can(db, user.id, "TRAINING.READ", PermissionContext())
    if not check.allowed:
        raise StripDenied(_empty(empty, buckets))
    plants = await get_accessible_plants(db, user.id)
    if plants is not None and not plants:
        raise StripDenied(_empty(empty, buckets))

    def _by_holder(stmt, holder_col):
        if plants is None:
            return stmt
        return stmt.where(
            holder_col.in_(select(User.id).where(User.plantId.in_(plants)))
        )

    twelve_months_ago = buckets[0][0]
    in_30 = now + 30 * DAY

    # Compliance basis: the LATEST record per (employee, programme) pair. An
    # employee who re-sat a lapsed course is compliant on the new result, not
    # counted twice.
    record_rows = (
        await db.execute(
            _by_holder(
                select(
                    TrainingRecord.employeeId,
                    TrainingRecord.programId,
                    TrainingRecord.date,
                    TrainingRecord.passed,
                    TrainingRecord.validUntil,
                ),
                TrainingRecord.employeeId,
            ).limit(_ITEM_CAP)
        )
    ).all()
    latest: dict[tuple[str, str], Any] = {}
    for r in record_rows:
        key = (r.employeeId, r.programId)
        prev = latest.get(key)
        if prev is None or _aware(r.date) > _aware(prev.date):
            latest[key] = r
    valid_pairs = sum(
        1 for r in latest.values() if r.passed and r.validUntil and _aware(r.validUntil) > now
    )

    trend_rows = (
        await db.execute(
            _by_holder(select(TrainingRecord.date), TrainingRecord.employeeId)
            .where(TrainingRecord.date >= twelve_months_ago)
            .limit(_ITEM_CAP)
        )
    ).all()

    cert_rows = (
        await db.execute(
            _by_holder(
                select(TrainingCertificate.status, TrainingCertificate.validTo),
                TrainingCertificate.userId,
            ).limit(_ITEM_CAP)
        )
    ).all()
    valid_certs = sum(1 for c in cert_rows if c.status == "ACTIVE")
    expired_certs = sum(1 for c in cert_rows if c.status in ("EXPIRED", "LAPSED"))
    expiring_certs = sum(
        1
        for c in cert_rows
        if c.status == "EXPIRING_SOON"
        or (c.validTo and now < _aware(c.validTo) <= in_30)
    )

    return {
        "compliancePct": _pct(valid_pairs, len(latest)) or 0,
        "validPairs": valid_pairs,
        "applicablePairs": len(latest),
        "validCerts": valid_certs,
        "expiredCerts": expired_certs,
        "expiringCerts": expiring_certs,
        "trendCounts": _bucket_counts([r.date for r in trend_rows], buckets),
        "bucketStarts": _iso(buckets),
    }


# ── Skill matrix (competency) ────────────────────────────────────────

# State groupings — must stay identical to the skill-matrix grid's STATE_META,
# or the strip and the matrix below it report different totals.
COMPETENCY_VALID = "validated_active"
COMPETENCY_EXPIRING = "expiring_soon"
COMPETENCY_EXPIRED = ("expired_in_grace", "expired_revoked", "lapsed_requires_full_redo")
COMPETENCY_IN_PROGRESS = (
    "in_training",
    "training_complete_pending_assessment",
    "under_assessment",
)
COMPETENCY_NOT_STARTED = ("not_yet_attempted",)
COMPETENCY_SUSPENDED = "suspended"


async def skill_matrix_strip(db: AsyncSession, user: User, now: datetime) -> dict[str, Any]:
    from app.models.competency_matrix import CompetencyRecord

    buckets = month_buckets(now)
    empty = {
        "validityPct": 0, "valid": 0, "applicable": 0,
        "suspended": 0, "inProgress": 0, "expiring": 0, "expired": 0,
    }
    base = await _scope(db, user, "SKILL_MATRIX.READ", CompetencyRecord, select(CompetencyRecord))
    if base is None:
        raise StripDenied(_empty(empty, buckets))

    twelve_months_ago = buckets[0][0]
    in_30 = now + 30 * DAY

    state_rows = (
        await db.execute(
            base.with_only_columns(
                CompetencyRecord.state, CompetencyRecord.validUntil
            ).limit(_ITEM_CAP * 2)
        )
    ).all()
    trend_rows = (
        await db.execute(
            base.with_only_columns(CompetencyRecord.createdAt)
            .where(CompetencyRecord.createdAt >= twelve_months_ago)
            .limit(_ITEM_CAP * 2)
        )
    ).all()

    valid = sum(1 for r in state_rows if r.state == COMPETENCY_VALID)
    suspended = sum(1 for r in state_rows if r.state == COMPETENCY_SUSPENDED)
    in_progress = sum(1 for r in state_rows if r.state in COMPETENCY_IN_PROGRESS)
    not_started = sum(1 for r in state_rows if r.state in COMPETENCY_NOT_STARTED)
    expired = sum(1 for r in state_rows if r.state in COMPETENCY_EXPIRED)
    expiring = sum(
        1
        for r in state_rows
        if r.state == COMPETENCY_EXPIRING
        or (r.validUntil and now < _aware(r.validUntil) <= in_30)
    )
    # Validity is measured against APPLICABLE competencies — a person who has
    # not yet attempted one isn't "non-compliant", they're unassessed, and
    # counting them would make every new hire drag the number down.
    applicable = len(state_rows) - not_started

    return {
        "validityPct": _pct(valid, applicable) or 0,
        "valid": valid,
        "applicable": applicable,
        "suspended": suspended,
        "inProgress": in_progress,
        "expiring": expiring,
        "expired": expired,
        "trendCounts": _bucket_counts([r.createdAt for r in trend_rows], buckets),
        "bucketStarts": _iso(buckets),
    }
