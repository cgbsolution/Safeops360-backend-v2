"""Manhours KPI engine — computes the registry's definitions against real data.

Port of `lib/manhours/kpi-engine.ts`. The engine knows how to *fetch*; the
registry knows what the formulas *are*. Adding a KPI is a registry edit unless
it needs a genuinely new fetch strategy.

Every result carries its own audit trail — numerator, denominator, multiplier,
the formula string and the ids of the records that contributed — because these
numbers end up in statutory returns and an inspector is entitled to ask which
incidents make up an LTIFR of 2.4.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.manhours_kpi_registry import (
    FATALITY_DAY_CHARGE,
    KPI_CODES,
    KPI_REGISTRY,
    RECORDABLE_TYPES,
    KpiDefinition,
    band_for,
)


@dataclass
class KpiResult:
    kpiCode: str
    kpiName: str
    value: float | None
    formattedValue: str
    numerator: float
    denominator: float
    formula: str
    band: str | None
    higherIsBetter: bool
    period: dict[str, Any]
    scope: dict[str, Any]
    computedAt: str
    # Ids of the records behind the numerator, so a drill-down can list them.
    sourceRecordIds: list[str] = field(default_factory=list)
    statutoryReference: str | None = None
    exclusionRules: list[str] = field(default_factory=list)
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kpiCode": self.kpiCode,
            "kpiName": self.kpiName,
            "value": self.value,
            "formattedValue": self.formattedValue,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "formula": self.formula,
            "band": self.band,
            "higherIsBetter": self.higherIsBetter,
            "period": self.period,
            "scope": self.scope,
            "computedAt": self.computedAt,
            "sourceRecordIds": self.sourceRecordIds,
            "statutoryReference": self.statutoryReference,
            "exclusionRules": self.exclusionRules,
            "note": self.note,
        }


def _format(value: float | None, fmt: str) -> str:
    if value is None:
        return "—"
    if fmt == "integer":
        return f"{int(round(value)):,}"
    if fmt == "percent":
        return f"{value:.1f}%"
    if fmt == "currency_indian":
        # Indian grouping (lakh/crore) is a display concern; the plain
        # thousands separator is close enough here and unambiguous.
        return f"₹{value:,.0f}"
    return f"{value:.2f}"


class KpiEngine:
    """Computes one or many KPIs for a plant + reporting month."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._cache: dict[str, KpiResult] = {}

    # ── Period + scope helpers ───────────────────────────────────────

    @staticmethod
    def _bounds(year: int, month: int) -> tuple[datetime, datetime]:
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        end = (
            datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            if month == 12
            else datetime(year, month + 1, 1, tzinfo=timezone.utc)
        )
        return start, end

    async def _exposure_hours(self, plant_id: str, year: int, month: int) -> float:
        """The NET exposure figure from that month's submission.

        Returns 0 when no submission exists — which the caller renders as "no
        data" rather than as a zero rate. Falling back to gross hours here is
        exactly the bug IS 3786 compliance exists to prevent.
        """
        from app.models.manhours_submission import ManhoursSubmission

        return float(
            (
                await self.db.execute(
                    select(ManhoursSubmission.netExposureHours)
                    .where(ManhoursSubmission.plantId == plant_id)
                    .where(ManhoursSubmission.reportingYear == year)
                    .where(ManhoursSubmission.reportingMonth == month)
                )
            ).scalar_one_or_none()
            or 0
        )

    # ── Numerator strategies ─────────────────────────────────────────

    async def _module_count(
        self, definition: KpiDefinition, plant_id: str, start: datetime, end: datetime
    ) -> tuple[float, list[str]]:
        from app.models.incident import Incident
        from app.models.near_miss import NearMiss
        from app.models.observation import Observation

        source = definition.numerator.source
        if source == "incident":
            stmt = (
                select(Incident.id)
                .where(Incident.plantId == plant_id)
                .where(Incident.isDeleted.is_(False))
                .where(Incident.date >= start)
                .where(Incident.date < end)
            )
            if definition.numerator.types:
                stmt = stmt.where(Incident.type.in_(definition.numerator.types))
        elif source == "nearMiss":
            stmt = (
                select(NearMiss.id)
                .where(NearMiss.plantId == plant_id)
                .where(NearMiss.date >= start)
                .where(NearMiss.date < end)
            )
        elif source == "observation":
            stmt = (
                select(Observation.id)
                .where(Observation.plantId == plant_id)
                .where(Observation.date >= start)
                .where(Observation.date < end)
            )
        else:
            raise ValueError(f"MODULE_COUNT has no resolver for source: {source}")

        ids = [r[0] for r in (await self.db.execute(stmt)).all()]
        return float(len(ids)), ids

    async def _days_since(
        self, definition: KpiDefinition, plant_id: str, now: datetime
    ) -> tuple[float, list[str]]:
        """Days since the most recent matching record — a streak, so it is
        measured against NOW rather than the reporting period."""
        from app.models.incident import Incident

        stmt = (
            select(Incident.id, Incident.date)
            .where(Incident.plantId == plant_id)
            .where(Incident.isDeleted.is_(False))
            .order_by(Incident.date.desc())
            .limit(1)
        )
        if definition.numerator.types:
            stmt = stmt.where(Incident.type.in_(definition.numerator.types))
        row = (await self.db.execute(stmt)).first()
        if row is None:
            # No qualifying incident on record. There is no honest streak to
            # report, so this is "no data", not "zero days".
            return 0.0, []
        last = row[1]
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return float(max(0, (now - last).days)), [row[0]]

    async def _severity_numerator(
        self, plant_id: str, start: datetime, end: datetime
    ) -> tuple[float, list[str]]:
        """IS 3786: SUM(lost days on LTIs) + 6000 × fatalities."""
        from app.models.incident import Incident

        rows = (
            await self.db.execute(
                select(Incident.id, Incident.type, Incident.lostDays)
                .where(Incident.plantId == plant_id)
                .where(Incident.isDeleted.is_(False))
                .where(Incident.date >= start)
                .where(Incident.date < end)
                .where(Incident.type.in_(("LTI", "FATALITY")))
            )
        ).all()
        days = 0.0
        for _id, itype, lost in rows:
            value = itype.value if hasattr(itype, "value") else itype
            days += FATALITY_DAY_CHARGE if value == "FATALITY" else (lost or 0)
        return days, [r[0] for r in rows]

    async def _capa_closure(
        self, plant_id: str, start: datetime, end: datetime
    ) -> tuple[float, list[str]]:
        """On-time closure % across the three CAPA child tables.

        Scoped to CAPAs *due* in the period — the right denominator for
        "how well did we close what was owed this month".
        """
        from app.models.incident import Incident, IncidentCapa
        from app.models.near_miss import NearMiss
        from app.models.near_miss_children import NearMissCapa

        pairs: list[tuple[str, Any, Any]] = []

        nm_rows = (
            await self.db.execute(
                select(NearMissCapa.id, NearMissCapa.targetDate, NearMissCapa.completedAt)
                .join(NearMiss, NearMiss.id == NearMissCapa.nearMissId)
                .where(NearMiss.plantId == plant_id)
                .where(NearMissCapa.targetDate >= start)
                .where(NearMissCapa.targetDate < end)
            )
        ).all()
        pairs.extend((r[0], r[1], r[2]) for r in nm_rows)

        inc_rows = (
            await self.db.execute(
                select(IncidentCapa.id, IncidentCapa.targetDate, IncidentCapa.completedAt)
                .join(Incident, Incident.id == IncidentCapa.incidentId)
                .where(Incident.plantId == plant_id)
                .where(IncidentCapa.targetDate >= start)
                .where(IncidentCapa.targetDate < end)
            )
        ).all()
        pairs.extend((r[0], r[1], r[2]) for r in inc_rows)

        try:
            from app.models.inspection_finding import InspectionFindingCapa

            fn_rows = (
                await self.db.execute(
                    select(
                        InspectionFindingCapa.id,
                        InspectionFindingCapa.dueDate,
                        InspectionFindingCapa.completedAt,
                    )
                    .where(InspectionFindingCapa.dueDate >= start)
                    .where(InspectionFindingCapa.dueDate < end)
                )
            ).all()
            pairs.extend((r[0], r[1], r[2]) for r in fn_rows)
        except Exception:  # noqa: BLE001 — table/model absent on older deployments
            pass

        if not pairs:
            return 0.0, []
        on_time = sum(1 for _id, due, done in pairs if done and due and done <= due)
        return (on_time / len(pairs)) * 100, [p[0] for p in pairs]

    async def _training_compliance(self, plant_id: str) -> tuple[float, list[str]]:
        """% of (employee, programme) pairs whose LATEST record is passed and
        still valid. Measured as-of-now, not period-bounded — `validUntil`
        already carries currency, and a retake collapses to its newest attempt.
        """
        from app.models.training import TrainingRecord
        from app.models.user import User

        rows = (
            await self.db.execute(
                select(
                    TrainingRecord.id,
                    TrainingRecord.employeeId,
                    TrainingRecord.programId,
                    TrainingRecord.date,
                    TrainingRecord.passed,
                    TrainingRecord.validUntil,
                ).where(
                    TrainingRecord.employeeId.in_(
                        select(User.id).where(User.plantId == plant_id)
                    )
                )
            )
        ).all()
        if not rows:
            return 0.0, []

        latest: dict[tuple[str, str], Any] = {}
        for r in rows:
            key = (r[1], r[2])
            prev = latest.get(key)
            if prev is None or r[3] > prev[3]:
                latest[key] = r

        now = datetime.now(timezone.utc)
        valid = 0
        for r in latest.values():
            valid_until = r[5]
            if valid_until is not None and valid_until.tzinfo is None:
                valid_until = valid_until.replace(tzinfo=timezone.utc)
            if r[4] and valid_until and valid_until > now:
                valid += 1
        return (valid / len(latest)) * 100, [r[0] for r in latest.values()]

    async def _inspection_compliance(
        self, plant_id: str, start: datetime, end: datetime
    ) -> tuple[float, list[str]]:
        from app.models.equipment import Inspection

        rows = (
            await self.db.execute(
                select(Inspection.id, Inspection.status)
                .where(Inspection.plantId == plant_id)
                .where(Inspection.scheduledDate >= start)
                .where(Inspection.scheduledDate < end)
            )
        ).all()
        if not rows:
            return 0.0, []
        completed = sum(1 for _id, s in rows if s == "COMPLETED")
        return (completed / len(rows)) * 100, [r[0] for r in rows]

    async def _ptw_flra_compliance(
        self, plant_id: str, start: datetime, end: datetime
    ) -> tuple[float, list[str]]:
        """% of permits raised in the period that have a linked FLRA.

        Anything below 100% is a process failure rather than a performance
        band — which is why the registry gives this a fixed target.
        """
        from app.models.flra import FLRA
        from app.models.permit import Permit

        permit_ids = [
            r[0]
            for r in (
                await self.db.execute(
                    select(Permit.id)
                    .where(Permit.plantId == plant_id)
                    .where(Permit.createdAt >= start)
                    .where(Permit.createdAt < end)
                )
            ).all()
        ]
        if not permit_ids:
            return 0.0, []
        linked = {
            r[0]
            for r in (
                await self.db.execute(
                    select(FLRA.permitId).where(FLRA.permitId.in_(permit_ids))
                )
            ).all()
            if r[0]
        }
        return (len(linked) / len(permit_ids)) * 100, permit_ids

    async def _cost_of_incidents(
        self, plant_id: str, start: datetime, end: datetime
    ) -> tuple[float, list[str]]:
        from app.models.incident import Incident

        rows = (
            await self.db.execute(
                select(Incident.id, Incident.costTotal)
                .where(Incident.plantId == plant_id)
                .where(Incident.isDeleted.is_(False))
                .where(Incident.date >= start)
                .where(Incident.date < end)
                .where(Incident.costTotal.is_not(None))
            )
        ).all()
        return float(sum(float(r[1] or 0) for r in rows)), [r[0] for r in rows]

    # ── Public API ───────────────────────────────────────────────────

    async def compute(self, code: str, plant_id: str, year: int, month: int) -> KpiResult:
        """One KPI. Results are memoised per engine instance so the derived
        KPIs don't recompute their inputs."""
        cache_key = f"{code}:{plant_id}:{year}:{month}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        definition = KPI_REGISTRY[code]
        start, end = self._bounds(year, month)
        now = datetime.now(timezone.utc)
        period = {"year": year, "month": month}
        scope = {"plantId": plant_id}
        spec = definition.numerator

        numerator = 0.0
        ids: list[str] = []
        note: str | None = None

        if spec.kind == "MODULE_COUNT":
            numerator, ids = await self._module_count(definition, plant_id, start, end)
        elif spec.kind == "DAYS_SINCE":
            numerator, ids = await self._days_since(definition, plant_id, now)
            if not ids:
                note = "No qualifying incident on record."
        elif spec.kind == "CUSTOM":
            handlers = {
                "SEVERITY_NUMERATOR": lambda: self._severity_numerator(plant_id, start, end),
                "CAPA_CLOSURE": lambda: self._capa_closure(plant_id, start, end),
                "TRAINING_COMPLIANCE": lambda: self._training_compliance(plant_id),
                "INSPECTION_COMPLIANCE": lambda: self._inspection_compliance(plant_id, start, end),
                "PTW_FLRA_COMPLIANCE": lambda: self._ptw_flra_compliance(plant_id, start, end),
                "COST_OF_INCIDENTS": lambda: self._cost_of_incidents(plant_id, start, end),
            }
            handler = handlers.get(spec.tag or "")
            if handler is None:
                raise ValueError(f"Unknown CUSTOM numerator tag: {spec.tag}")
            numerator, ids = await handler()
        elif spec.kind == "DERIVED":
            value = await self._derived(definition, plant_id, year, month)
            result = KpiResult(
                kpiCode=code,
                kpiName=definition.name,
                value=value,
                formattedValue=_format(value, definition.display_format),
                numerator=value if value is not None else 0,
                denominator=1,
                formula=definition.formula,
                band=band_for(definition, value),
                higherIsBetter=definition.higher_is_better,
                period=period,
                scope=scope,
                computedAt=now.isoformat(),
                statutoryReference=definition.statutory_reference,
                exclusionRules=list(definition.exclusion_rules),
            )
            self._cache[cache_key] = result
            return result

        denominator = 1.0
        value: float | None
        if definition.denominator == "EXPOSURE_HOURS":
            denominator = await self._exposure_hours(plant_id, year, month)
            if denominator <= 0:
                # No exposure hours means the month has not been reported. A
                # rate computed against zero is not "0.0", it is unknown —
                # returning 0 would read as a perfect safety record.
                value = None
                note = "No locked exposure hours for this period."
            else:
                value = (numerator / denominator) * definition.multiplier
        else:
            value = numerator * definition.multiplier

        result = KpiResult(
            kpiCode=code,
            kpiName=definition.name,
            value=value,
            formattedValue=_format(value, definition.display_format),
            numerator=numerator,
            denominator=denominator,
            formula=definition.formula,
            band=band_for(definition, value),
            higherIsBetter=definition.higher_is_better,
            period=period,
            scope=scope,
            computedAt=now.isoformat(),
            sourceRecordIds=ids[:500],
            statutoryReference=definition.statutory_reference,
            exclusionRules=list(definition.exclusion_rules),
            note=note,
        )
        self._cache[cache_key] = result
        return result

    async def _derived(
        self, definition: KpiDefinition, plant_id: str, year: int, month: int
    ) -> float | None:
        tag = definition.numerator.tag
        if tag == "FSI":
            ltifr = await self.compute("LTIFR", plant_id, year, month)
            severity = await self.compute("SEVERITY_RATE", plant_id, year, month)
            if ltifr.value is None or severity.value is None:
                return None
            product = ltifr.value * severity.value
            return math.sqrt(product / 1000) if product > 0 else 0.0

        if tag == "HEINRICH_RATIO":
            # Absolute counts, not rates: both rates share the same exposure
            # denominator so it would cancel anyway, and counts are what a
            # drill-down can actually list.
            from app.models.incident import Incident
            from app.models.near_miss import NearMiss

            start, end = self._bounds(year, month)
            nm = int(
                (
                    await self.db.execute(
                        select(func.count())
                        .select_from(NearMiss)
                        .where(NearMiss.plantId == plant_id)
                        .where(NearMiss.date >= start)
                        .where(NearMiss.date < end)
                    )
                ).scalar_one()
            )
            inc = int(
                (
                    await self.db.execute(
                        select(func.count())
                        .select_from(Incident)
                        .where(Incident.plantId == plant_id)
                        .where(Incident.isDeleted.is_(False))
                        .where(Incident.date >= start)
                        .where(Incident.date < end)
                        .where(Incident.type.in_(RECORDABLE_TYPES))
                    )
                ).scalar_one()
            )
            # No recordable incidents is the best possible month, but the
            # ratio is undefined — reported as None, not as infinity or 0.
            return (nm / inc) if inc > 0 else None

        raise ValueError(f"Unknown DERIVED tag: {tag}")

    async def compute_all(
        self, plant_id: str, year: int, month: int, codes: tuple[str, ...] = KPI_CODES
    ) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for code in codes:
            out[code] = (await self.compute(code, plant_id, year, month)).to_dict()
        return out
