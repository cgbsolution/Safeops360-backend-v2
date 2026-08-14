"""Helpers shared by the module register (list) endpoints.

Every module's list screen renders the same two things beside its rows:

  * a status filter bar with a count per status, computed over the caller's
    WHOLE accessible set — not just the page of rows returned, or the tab
    counts would change as you paged;
  * a workflow chip per row showing the step a record is currently sitting on,
    which lives in WorkflowInstance rather than on the record itself.

The Next.js pages used to do both for themselves in Prisma — a `groupBy` plus a
second `findMany` over WorkflowInstance keyed by the row ids. Doing it here
keeps the list endpoint a single round-trip for the page and stops each module
re-deriving "which step is this on" slightly differently.
"""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workflow import WorkflowInstance


async def status_counts(db: AsyncSession, scoped: Select, status_col) -> dict[str, int]:
    """`{status: count}` over an already-scoped SELECT.

    `scoped` must carry the caller's access filter and nothing else — no
    status/type narrowing, no LIMIT — because these counts drive the filter
    tabs and must describe the full accessible set.
    """
    # The grouping column has to be re-resolved against the subquery — the
    # model-level column would reference the outer table and produce a
    # cartesian join instead of grouping the scoped set.
    sub = scoped.subquery()
    col = sub.c[status_col.key]
    rows = (await db.execute(select(col, func.count()).select_from(sub).group_by(col))).all()
    return {(s.value if hasattr(s, "value") else str(s)): int(n) for s, n in rows}


async def workflow_chips(
    db: AsyncSession, module: str, record_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """`{recordId: {status, currentStepName}}` for the rows on this page.

    Records with no workflow instance are simply absent from the map — the UI
    falls back to the record's own status, which is the correct display for
    modules and legacy rows that never entered the engine.
    """
    if not record_ids:
        return {}
    rows = (
        await db.execute(
            select(
                WorkflowInstance.recordId,
                WorkflowInstance.status,
                WorkflowInstance.currentStepName,
            )
            .where(WorkflowInstance.module == module)
            .where(WorkflowInstance.recordId.in_(list(record_ids)))
        )
    ).all()
    return {
        rid: {"status": status, "currentStepName": step}
        for rid, status, step in rows
    }


async def workflow_bottleneck(
    db: AsyncSession, module: str, open_record_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """"Where it's stuck": per-step dwell over the OPEN backlog.

    days-in-step = now − the record's last workflow transition
    (WorkflowHistory.performedAt), falling back to when the instance was
    initiated for a record that has not moved yet. Sorted slowest-first.

    Matches the bottleneck insight the backend already surfaces, so the panel
    on a list screen and the insight card above it quote the same number
    instead of disagreeing by a day.
    """
    if not open_record_ids:
        return []

    instances = (
        await db.execute(
            select(
                WorkflowInstance.id,
                WorkflowInstance.currentStepName,
                WorkflowInstance.initiatedAt,
            )
            .where(WorkflowInstance.module == module)
            .where(WorkflowInstance.recordId.in_(list(open_record_ids)))
            .where(WorkflowInstance.status == "IN_PROGRESS")
        )
    ).all()
    if not instances:
        return []

    from app.models.workflow import WorkflowHistory

    # Ascending, so the last write per instance is its most recent transition —
    # i.e. when it entered the step it is sitting on now.
    history = (
        await db.execute(
            select(WorkflowHistory.instanceId, WorkflowHistory.performedAt)
            .where(WorkflowHistory.instanceId.in_([i[0] for i in instances]))
            .order_by(WorkflowHistory.performedAt.asc())
        )
    ).all()
    entered_at: dict[str, Any] = {}
    for instance_id, performed_at in history:
        entered_at[instance_id] = performed_at

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    agg: dict[str, dict[str, float]] = {}
    for instance_id, step_name, initiated_at in instances:
        if not step_name:
            continue
        entered = entered_at.get(instance_id) or initiated_at
        if entered.tzinfo is None:
            entered = entered.replace(tzinfo=timezone.utc)
        days = max(0, (now - entered).days)
        bucket = agg.setdefault(step_name, {"count": 0, "totalDays": 0})
        bucket["count"] += 1
        bucket["totalDays"] += days

    return sorted(
        (
            {
                "step": step,
                "count": int(v["count"]),
                "avgDays": round(v["totalDays"] / v["count"], 1),
            }
            for step, v in agg.items()
        ),
        key=lambda r: r["avgDays"],
        reverse=True,
    )
