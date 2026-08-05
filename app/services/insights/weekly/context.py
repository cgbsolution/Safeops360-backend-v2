"""Weekly Insight Engine — context loader (spec §3).

Pulls the tenant/plant record set ONCE (no per-record queries — spec §3, §14
"complete without N+1"), plus the workflow-step timing, name lookups and monthly
submission history the generators need. Everything downstream reads this in
memory. Airgap-safe: DB reads only, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String, cast, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.observation import Observation
from app.models.workflow import WorkflowHistory, WorkflowInstance
from app.services.insights.common import as_naive

_WINDOW_DAYS = 210          # 7 months — covers signals + the 6-month reporting baseline
_UNSAFE_TYPES = ("UNSAFE_ACT", "UNSAFE_CONDITION")


@dataclass
class ObsRec:
    id: str
    number: str
    date: datetime | None
    plantId: str
    areaId: str | None
    category: str
    # STOP sub-category — None on safe observations and on legacy at-risk rows
    # the taxonomy migration left for review.
    subCategoryCode: str | None
    type: str
    severity: str
    status: str
    createdAt: datetime | None
    closedAt: datetime | None
    ownerId: str | None
    capaId: str | None

    @property
    def is_open(self) -> bool:
        return (self.status or "") != "CLOSED"

    @property
    def is_unsafe(self) -> bool:
        return (self.type or "").startswith("UNSAFE")


@dataclass
class GeneratorContext:
    module: str
    plant: str | None
    now: datetime
    week_of: datetime
    records: list[ObsRec]
    plant_names: dict[str, str]
    area_names: dict[str, str]
    # recordId → (currentStepName, days_in_step) for OPEN in-progress instances
    current_step: dict[str, tuple[str, int]]
    # stepName → module-wide avg days-in-step over CLOSED hops (the bottleneck norm)
    step_norm: dict[str, float]
    # category → median closure days (ageing baseline, §5)
    category_median_closure: dict[str, float]
    prior_snapshots: dict[str, Any] = field(default_factory=dict)  # identityKey → prior InsightSnapshot

    def plant_name(self, pid: str) -> str:
        return self.plant_names.get(pid, "this plant")

    def area_name(self, aid: str | None) -> str:
        return self.area_names.get(aid or "", "Unassigned area")

    @property
    def open_records(self) -> list[ObsRec]:
        return [r for r in self.records if r.is_open]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


async def load_context(
    db: AsyncSession, *, module: str, plant: str | None, now: datetime, week_of: datetime
) -> GeneratorContext:
    window_start = now - timedelta(days=_WINDOW_DAYS)

    stmt = (
        select(
            Observation.id,
            Observation.number,
            Observation.date,
            Observation.plantId,
            Observation.areaId,
            cast(Observation.category, String).label("category"),
            Observation.subCategoryCode,
            cast(Observation.type, String).label("type"),
            cast(Observation.severity, String).label("severity"),
            cast(Observation.status, String).label("status"),
            Observation.createdAt,
            Observation.closedAt,
            Observation.responsiblePersonId,
            Observation.capaId,
        )
        .where(Observation.date >= window_start)
        .order_by(Observation.date.desc())
        .limit(3000)
    )
    if plant:
        stmt = stmt.where(Observation.plantId == plant)
    rows = (await db.execute(stmt)).all()
    records = [
        ObsRec(
            id=r.id, number=r.number, date=as_naive(r.date), plantId=r.plantId, areaId=r.areaId,
            category=r.category or "OTHER", subCategoryCode=r.subCategoryCode,
            type=r.type or "", severity=r.severity or "LOW",
            status=r.status or "OPEN", createdAt=as_naive(r.createdAt), closedAt=as_naive(r.closedAt),
            ownerId=r.responsiblePersonId, capaId=r.capaId,
        )
        for r in rows
    ]

    plant_names: dict[str, str] = dict((await db.execute(text('SELECT id, name FROM "Plant"'))).all())
    area_names: dict[str, str] = dict((await db.execute(text('SELECT id, name FROM "Area"'))).all())

    current_step, step_norm = await _workflow_timing(db, records, now)
    category_median_closure = _category_medians(records)

    return GeneratorContext(
        module=module, plant=plant, now=now, week_of=week_of, records=records,
        plant_names=plant_names, area_names=area_names, current_step=current_step,
        step_norm=step_norm, category_median_closure=category_median_closure,
    )


def _category_medians(records: list[ObsRec]) -> dict[str, float]:
    by_cat: dict[str, list[float]] = {}
    for r in records:
        if r.closedAt and r.date:
            days = (r.closedAt - r.date).total_seconds() / 86400
            if days >= 0:
                by_cat.setdefault(r.category, []).append(days)
    return {c: m for c, v in by_cat.items() if (m := _median(v)) is not None}


async def _workflow_timing(
    db: AsyncSession, records: list[ObsRec], now: datetime
) -> tuple[dict[str, tuple[str, int]], dict[str, float]]:
    """current_step per open record (days since entering the step) + the
    module-wide avg days-in-step norm over completed hops. Two queries, bounded
    to the loaded ids — never an N+1."""
    ids = [r.id for r in records]
    if not ids:
        return {}, {}
    inst_rows = (
        await db.execute(
            select(
                WorkflowInstance.id, WorkflowInstance.recordId, WorkflowInstance.currentStepName,
                WorkflowInstance.initiatedAt, WorkflowInstance.status,
            ).where(WorkflowInstance.module == "OBSERVATION", WorkflowInstance.recordId.in_(ids))
        )
    ).all()
    if not inst_rows:
        return {}, {}
    inst_ids = [i.id for i in inst_rows]
    hist_rows = (
        await db.execute(
            select(WorkflowHistory.instanceId, WorkflowHistory.stepName, WorkflowHistory.performedAt)
            .where(WorkflowHistory.instanceId.in_(inst_ids))
            .order_by(WorkflowHistory.performedAt)
        )
    ).all()
    hist_by_inst: dict[str, list[Any]] = {}
    for h in hist_rows:
        hist_by_inst.setdefault(h.instanceId, []).append(h)

    open_ids = {r.id for r in records if r.is_open}
    current_step: dict[str, tuple[str, int]] = {}
    dwell_samples: dict[str, list[float]] = {}
    for inst in inst_rows:
        entered = as_naive(inst.initiatedAt)
        for h in hist_by_inst.get(inst.id, []):
            pa = as_naive(h.performedAt)
            if entered is not None and pa is not None and pa >= entered:
                dwell_samples.setdefault(h.stepName, []).append((pa - entered).total_seconds() / 86400)
            if pa is not None:
                entered = pa
        if (inst.status or "") == "IN_PROGRESS" and inst.currentStepName and entered is not None and inst.recordId in open_ids:
            days = int((now - entered).total_seconds() // 86400)
            current_step[inst.recordId] = (inst.currentStepName, max(days, 0))

    step_norm = {step: (sum(v) / len(v)) for step, v in dwell_samples.items() if v}
    return current_step, step_norm
