"""SLA sweeps for workflow tasks and inspection scheduling.

Direct port of `WorkflowEngine.sweepOverdue` and
`WorkflowEngine.sweepInspectionStatus` from the Next.js side. They ran there as
a side effect of rendering /inbox, /ptw and /inspections — meaning a permit only
expired, and an approval only escalated, if somebody happened to open a page.
Here they are ordinary scheduler jobs, so the clock runs whether anyone is
looking or not.

The thresholds are the ones the TypeScript used, unchanged:
  * a task is OVERDUE once `dueAt` has passed;
  * it ESCALATES 24h after that, spawning a parallel task for the step's
    escalation role, itself due in 24h and flagged URGENT;
  * an inspection goes DUE within 3 days of its scheduled date, and OVERDUE
    once that date has passed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.workflow import (
    Action,
    TaskStatus,
    WorkflowDefinition,
    WorkflowHistory,
    WorkflowInstance,
    WorkflowStep,
    WorkflowTask,
)

log = logging.getLogger("safeops360.workflow_sweeps")

ESCALATE_AFTER = timedelta(hours=24)
ESCALATION_DUE_IN = timedelta(hours=24)
INSPECTION_DUE_WINDOW = timedelta(days=3)

_OPEN_TASK_STATUSES = (TaskStatus.PENDING.value, "OVERDUE", "ESCALATED")


def _history(task: WorkflowTask, comment: str) -> WorkflowHistory:
    """An ESCALATED trail entry attributed to the assignee who missed the SLA.

    Attribution is deliberate: the escalation happened *to* their task, and the
    audit trail has no concept of a system actor, so recording it against the
    holder keeps the chain readable.
    """
    return WorkflowHistory(
        instanceId=task.instanceId,
        stepId=task.stepId,
        stepName=task.stepName,
        action=Action.ESCALATED,
        performedById=task.assignedToId,
        comments=comment,
    )


async def sweep_overdue(db: AsyncSession, *, now: datetime | None = None) -> dict[str, Any]:
    """Flip PENDING → OVERDUE, then OVERDUE → ESCALATED with a parallel task."""
    now = now or datetime.now(timezone.utc)
    flipped_to_overdue = 0
    flipped_to_escalated = 0
    escalation_tasks_created = 0

    # 1. PENDING → OVERDUE
    result = await db.execute(
        update(WorkflowTask)
        .where(WorkflowTask.status == TaskStatus.PENDING.value)
        .where(WorkflowTask.dueAt.is_not(None))
        .where(WorkflowTask.dueAt < now)
        .values(status="OVERDUE")
    )
    flipped_to_overdue = int(result.rowcount or 0)

    # 2. OVERDUE → ESCALATED, 24h after the due date
    overdue_long = (
        await db.execute(
            select(WorkflowTask)
            .where(WorkflowTask.status == "OVERDUE")
            .where(WorkflowTask.dueAt.is_not(None))
            .where(WorkflowTask.dueAt < now - ESCALATE_AFTER)
        )
    ).scalars().all()

    from app.services.workflow_engine import _resolve_assignee

    for task in overdue_long:
        task.status = "ESCALATED"
        flipped_to_escalated += 1

        instance = await db.get(WorkflowInstance, task.instanceId)
        if instance is None:
            continue

        step = (
            await db.execute(
                select(WorkflowStep)
                .where(WorkflowStep.definitionId == instance.definitionId)
                .where(WorkflowStep.id == task.stepId)
            )
        ).scalar_one_or_none()

        if step is None or not step.escalationRole:
            db.add(
                _history(
                    task,
                    "Auto-escalated — overdue more than 24h. "
                    "No escalation role configured on this step.",
                )
            )
            continue

        # Scope the role lookup to the INITIATOR's plant. Without it the
        # resolver returns the first holder globally, which on a multi-plant
        # tenant is usually a shared admin account rather than the plant-local
        # role holder who can actually act.
        initiator = await db.get(User, instance.initiatedById)
        escalation_user_id = await _resolve_assignee(
            db,
            approver_role=step.escalationRole,
            approver_field=None,
            approver_user_id=None,
            approver_group_roles=None,
            record_data={},
            initiator_id=instance.initiatedById,
            plant_id=initiator.plantId if initiator else None,
        )

        if not escalation_user_id or escalation_user_id == task.assignedToId:
            db.add(
                _history(
                    task,
                    "Auto-escalated — could not find a distinct user with role "
                    f"{step.escalationRole}.",
                )
            )
            continue

        # Idempotence: a re-run must not pile up duplicate escalation tasks for
        # the same step and person.
        existing = (
            await db.execute(
                select(WorkflowTask.id)
                .where(WorkflowTask.instanceId == task.instanceId)
                .where(WorkflowTask.stepId == task.stepId)
                .where(WorkflowTask.assignedToId == escalation_user_id)
                .where(WorkflowTask.status.in_(_OPEN_TASK_STATUSES))
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing:
            continue

        db.add(
            WorkflowTask(
                instanceId=task.instanceId,
                stepId=task.stepId,
                stepName=f"[Escalation] {task.stepName}",
                taskType=task.taskType,
                module=task.module,
                recordId=task.recordId,
                recordNumber=task.recordNumber,
                recordTitle=task.recordTitle,
                assignedToId=escalation_user_id,
                dueAt=now + ESCALATION_DUE_IN,
                status=TaskStatus.PENDING.value,
                priority="URGENT",
            )
        )
        escalation_tasks_created += 1
        db.add(
            _history(
                task,
                f"Auto-escalated to {step.escalationRole} (24h+ overdue). "
                "Parallel task created.",
            )
        )

    await db.flush()
    return {
        "flippedToOverdue": flipped_to_overdue,
        "flippedToEscalated": flipped_to_escalated,
        "escalationTasksCreated": escalation_tasks_created,
    }


async def sweep_inspection_status(
    db: AsyncSession, *, now: datetime | None = None
) -> dict[str, Any]:
    """Advance inspection scheduling states against the clock."""
    now = now or datetime.now(timezone.utc)
    from app.models.equipment import Inspection

    async def _flip(from_status: str, to_status: str, *conditions) -> int:
        stmt = update(Inspection).where(Inspection.status == from_status)
        for c in conditions:
            stmt = stmt.where(c)
        res = await db.execute(stmt.values(status=to_status))
        return int(res.rowcount or 0)

    # Past-due first, so a record that is both "within 3 days" and "already
    # past" lands on OVERDUE rather than being pulled back to DUE.
    scheduled_overdue = await _flip(
        "SCHEDULED", "OVERDUE", Inspection.scheduledDate < now
    )
    due_overdue = await _flip("DUE", "OVERDUE", Inspection.scheduledDate < now)
    scheduled_due = await _flip(
        "SCHEDULED",
        "DUE",
        Inspection.scheduledDate >= now,
        Inspection.scheduledDate <= now + INSPECTION_DUE_WINDOW,
    )

    await db.flush()
    return {
        "scheduledToOverdue": scheduled_overdue,
        "dueToOverdue": due_overdue,
        "scheduledToDue": scheduled_due,
    }
