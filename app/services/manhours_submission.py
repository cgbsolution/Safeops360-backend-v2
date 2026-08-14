"""Manhours submission — aggregates and the state machine.

Port of `lib/manhours/aggregate.ts`, `server.ts` and `workflow.ts`.

Why this module drives its own state machine instead of using
`workflow_engine.py`: the Manhours lifecycle has two rules the generic engine
does not model.

  1. **A rejection returns to DRAFT, it does not terminate.** The generic engine
     treats REJECTED as an end state. A statutory monthly return cannot end
     there — the month still has to be reported, so a rejection has to bounce
     the record back for correction.
  2. **Lock / unlock / re-lock cycles.** A locked return is a reported figure;
     reopening it is an audited event with a reason and a captured diff, and the
     KPI snapshot must be re-frozen on every re-lock.

It still writes to the SAME WorkflowInstance / WorkflowTask / WorkflowHistory
tables, so Manhours tasks appear in the shared inbox exactly like any other
module's.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manhours_submission import (
    EDITABLE_STATUSES,
    ManhoursEmployeeCategory,
    ManhoursSubmission,
    ManhoursUnlockEvent,
)
from app.models.plant import Plant
from app.models.user import User
from app.models.workflow import (
    Action,
    WorkflowDefinition,
    WorkflowHistory,
    WorkflowInstance,
    WorkflowStep,
    WorkflowTask,
)

WORKFLOW_MODULE = "MANHOURS"

# Step labels, matching seed-workflows so the shared tracker renders the
# sequence consistently.
STEP_MAKER = "Plant HSE Enters"
STEP_CHECKER = "Plant Head Reviews"
STEP_CLOSURE = "Corporate HSE Locks"

# 48h for Plant Head review; Corporate lock is a soft 7-day target because
# they batch the month's returns.
PLANT_HEAD_SLA_HOURS = 48
CORPORATE_LOCK_SLA_HOURS = 7 * 24


class ManhoursStatusError(Exception):
    """Raised when a transition or edit is attempted from the wrong state."""

    def __init__(self, status: str, message: str | None = None) -> None:
        self.status = status
        super().__init__(
            message
            or (
                f"Submission is in {status} state — edits are no longer allowed. "
                "Use the unlock workflow if changes are required."
            )
        )


def assert_editable(submission: ManhoursSubmission) -> None:
    if submission.status not in EDITABLE_STATUSES:
        raise ManhoursStatusError(submission.status)


# ── Aggregates ───────────────────────────────────────────────────────


def category_total_hours(regular: float | None, overtime: float | None) -> float:
    return (regular or 0) + (overtime or 0)


def recompute_aggregates(
    categories: Iterable[ManhoursEmployeeCategory], submission: ManhoursSubmission
) -> dict[str, float | int]:
    """Re-derive the roll-ups from current category + deduction state."""
    perm = contr = train = 0.0
    perm_end = train_end = contract_end = 0

    for c in categories:
        total = category_total_hours(c.regularHours, c.overtimeHours)
        if c.categoryType == "PERMANENT":
            perm += total
            perm_end += c.endOfPeriodHeadcount or 0
        elif c.categoryType == "CONTRACT":
            contr += total
            contract_end += c.endOfPeriodHeadcount or 0
        else:
            train += total
            train_end += c.endOfPeriodHeadcount or 0

    all_hours = perm + contr + train
    deductions = (
        (submission.hoursAnnualLeave or 0)
        + (submission.hoursSickLeave or 0)
        + (submission.hoursTraining or 0)
        + (submission.hoursMaternityLeave or 0)
        + (submission.hoursOther or 0)
    )

    return {
        "totalManhoursPermanent": perm,
        "totalManhoursContract": contr,
        "totalManhoursTrainee": train,
        "totalManhoursAll": all_hours,
        # Trainees count as employee strength — they are on payroll. Visitors
        # are excluded; they live in their own record.
        "totalEmployeeStrength": perm_end + train_end,
        "totalContractorStrength": contract_end,
        "hoursDeductionsTotal": deductions,
        # IS 3786: net = gross − deductions. Never negative, so a data-entry
        # slip that over-declares deductions can't produce a KPI denominator
        # below zero and flip every rate's sign.
        "netExposureHours": max(0.0, all_hours - deductions),
    }


async def refresh_aggregates(db: AsyncSession, submission_id: str) -> ManhoursSubmission:
    """Recompute and persist the roll-ups. Call after every category mutation
    and after any PATCH that touched a deduction field."""
    submission = await db.get(ManhoursSubmission, submission_id)
    if submission is None:
        raise ManhoursStatusError("MISSING", "Submission not found")

    categories = (
        await db.execute(
            select(ManhoursEmployeeCategory).where(
                ManhoursEmployeeCategory.submissionId == submission_id
            )
        )
    ).scalars().all()

    for key, value in recompute_aggregates(categories, submission).items():
        setattr(submission, key, value)
    await db.flush()
    return submission


def build_submission_number(plant_code: str, year: int, month: int) -> str:
    """MH-YYYY-PLANT-MM. Assigned at the DRAFT → SUBMITTED transition."""
    return f"MH-{year}-{plant_code}-{month:02d}"


def period_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """Inclusive start, exclusive end of a reporting month, in UTC."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)
    return start, end


# ── Workflow plumbing ────────────────────────────────────────────────


async def _load_definition(db: AsyncSession) -> tuple[WorkflowDefinition, list[WorkflowStep]]:
    definition = (
        await db.execute(
            select(WorkflowDefinition)
            .where(WorkflowDefinition.module == WORKFLOW_MODULE)
            .where(WorkflowDefinition.isActive.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none()
    if definition is None:
        raise ManhoursStatusError(
            "NO_DEFINITION",
            "No active MANHOURS workflow definition. Seed it before submitting.",
        )
    steps = (
        await db.execute(
            select(WorkflowStep)
            .where(WorkflowStep.definitionId == definition.id)
            .order_by(WorkflowStep.sequence)
        )
    ).scalars().all()
    return definition, list(steps)


def _step(steps: list[WorkflowStep], step_type: str) -> WorkflowStep:
    for s in steps:
        value = s.stepType.value if hasattr(s.stepType, "value") else s.stepType
        if value == step_type:
            return s
    raise ManhoursStatusError(
        "NO_STEP", f"MANHOURS workflow definition has no {step_type} step."
    )


async def _ensure_instance(
    db: AsyncSession, submission: ManhoursSubmission, initiator_id: str
) -> WorkflowInstance:
    instance = (
        await db.execute(
            select(WorkflowInstance)
            .where(WorkflowInstance.module == WORKFLOW_MODULE)
            .where(WorkflowInstance.recordId == submission.id)
        )
    ).scalar_one_or_none()
    if instance is not None:
        return instance

    definition, _steps = await _load_definition(db)
    instance = WorkflowInstance(
        definitionId=definition.id,
        module=WORKFLOW_MODULE,
        recordId=submission.id,
        recordNumber=submission.submissionNumber,
        initiatedById=initiator_id,
        status="IN_PROGRESS",
    )
    db.add(instance)
    await db.flush()
    return instance


async def _find_role_holder(db: AsyncSession, role_code: str, plant_id: str | None) -> str | None:
    """A user holding `role_code`, preferring one at the given plant.

    Plant-first matters on a multi-plant tenant: the global fallback is often a
    shared corporate account, and assigning the review task there means the
    person who actually knows the plant's numbers never sees it.
    """
    from app.models.user import Role, UserRole

    base = (
        select(User.id)
        .join(UserRole, UserRole.userId == User.id)
        .join(Role, Role.id == UserRole.roleId)
        .where(Role.code == role_code)
        .where(Role.isActive.is_(True))
    )
    if plant_id:
        found = (
            await db.execute(base.where(User.plantId == plant_id).limit(1))
        ).scalar_one_or_none()
        if found:
            return found
    return (await db.execute(base.limit(1))).scalar_one_or_none()


async def _close_open_tasks(db: AsyncSession, instance_id: str, step_id: str | None = None) -> None:
    """Complete the tasks a transition supersedes, so the inbox doesn't keep
    showing an action that has already been taken."""
    stmt = (
        select(WorkflowTask)
        .where(WorkflowTask.instanceId == instance_id)
        .where(WorkflowTask.status.in_(("PENDING", "OVERDUE", "ESCALATED")))
    )
    if step_id:
        stmt = stmt.where(WorkflowTask.stepId == step_id)
    for task in (await db.execute(stmt)).scalars().all():
        task.status = "COMPLETED"
        task.completedAt = datetime.now(timezone.utc)


def _history(
    instance_id: str,
    step: WorkflowStep,
    action: Action,
    performed_by: str,
    comments: str | None = None,
    to_status: str | None = None,
) -> WorkflowHistory:
    return WorkflowHistory(
        instanceId=instance_id,
        stepId=step.id,
        stepName=step.name,
        action=action,
        performedById=performed_by,
        comments=comments,
        toStatus=to_status,
    )


# -- State transitions ------------------------------------------------


async def submit_for_review(
    db: AsyncSession, *, submission_id: str, initiator_id: str
) -> dict[str, Any]:
    """DRAFT -> UNDER_REVIEW. Assigns the submission number and raises the
    Plant Head approval task."""
    submission = await db.get(ManhoursSubmission, submission_id)
    if submission is None:
        raise ManhoursStatusError("MISSING", "Submission not found")
    if submission.status not in ("DRAFT", "UNLOCKED_FOR_REVISION"):
        raise ManhoursStatusError(
            submission.status,
            f"Cannot submit a submission in {submission.status} state (expected DRAFT).",
        )

    plant_head_id = await _find_role_holder(db, "PLANT_HEAD", submission.plantId)
    if not plant_head_id:
        raise ManhoursStatusError(
            submission.status,
            "No Plant Head found for this plant. Assign a user with role "
            "PLANT_HEAD before submitting.",
        )

    # The number is minted here, not at create: a DRAFT saved and resumed
    # repeatedly would otherwise collide on the unique index.
    if not submission.submissionNumber:
        plant = await db.get(Plant, submission.plantId)
        submission.submissionNumber = build_submission_number(
            plant.code if plant else "NA",
            submission.reportingYear,
            submission.reportingMonth,
        )

    _definition, steps = await _load_definition(db)
    maker, checker = _step(steps, "MAKER"), _step(steps, "CHECKER")
    instance = await _ensure_instance(db, submission, initiator_id)
    instance.recordNumber = submission.submissionNumber
    instance.status = "IN_PROGRESS"
    instance.currentStepId = checker.id
    instance.currentStepName = checker.name
    instance.completedAt = None

    title = f"Manhours {submission.reportingMonth:02d}/{submission.reportingYear}"
    db.add(_history(instance.id, maker, Action.SUBMITTED, initiator_id, to_status="IN_PROGRESS"))
    db.add(
        WorkflowTask(
            instanceId=instance.id,
            stepId=checker.id,
            stepName=checker.name,
            taskType="APPROVAL",
            module=WORKFLOW_MODULE,
            recordId=submission.id,
            recordNumber=submission.submissionNumber,
            recordTitle=title,
            assignedToId=plant_head_id,
            dueAt=datetime.now(timezone.utc) + timedelta(hours=PLANT_HEAD_SLA_HOURS),
            status="PENDING",
            priority="NORMAL",
        )
    )
    submission.status = "UNDER_REVIEW"
    submission.submittedById = initiator_id
    submission.submittedAt = datetime.now(timezone.utc)
    await db.flush()
    return {"instanceId": instance.id, "submissionNumber": submission.submissionNumber}


async def plant_head_approve(
    db: AsyncSession, *, submission_id: str, approver_id: str, notes: str | None
) -> dict[str, Any]:
    """UNDER_REVIEW -> APPROVED, raising the Corporate HSE lock task."""
    submission = await db.get(ManhoursSubmission, submission_id)
    if submission is None:
        raise ManhoursStatusError("MISSING", "Submission not found")
    if submission.status != "UNDER_REVIEW":
        raise ManhoursStatusError(
            submission.status,
            f"Cannot approve a submission in {submission.status} state (expected UNDER_REVIEW).",
        )

    _definition, steps = await _load_definition(db)
    checker, closure = _step(steps, "CHECKER"), _step(steps, "CLOSURE")
    instance = await _ensure_instance(db, submission, approver_id)

    await _close_open_tasks(db, instance.id, checker.id)
    db.add(_history(instance.id, checker, Action.APPROVED, approver_id, notes))

    corporate_id = await _find_role_holder(db, "CORPORATE_HSE", None)
    if corporate_id:
        title = f"Manhours {submission.reportingMonth:02d}/{submission.reportingYear}"
        db.add(
            WorkflowTask(
                instanceId=instance.id,
                stepId=closure.id,
                stepName=closure.name,
                taskType="APPROVAL",
                module=WORKFLOW_MODULE,
                recordId=submission.id,
                recordNumber=submission.submissionNumber,
                recordTitle=title,
                assignedToId=corporate_id,
                dueAt=datetime.now(timezone.utc) + timedelta(hours=CORPORATE_LOCK_SLA_HOURS),
                status="PENDING",
                priority="NORMAL",
            )
        )
    instance.currentStepId = closure.id
    instance.currentStepName = closure.name

    submission.status = "APPROVED"
    submission.reviewedById = approver_id
    submission.reviewedAt = datetime.now(timezone.utc)
    submission.reviewerNotes = notes
    submission.reviewDecision = "APPROVED"
    await db.flush()
    return {"status": submission.status}


async def plant_head_reject(
    db: AsyncSession, *, submission_id: str, reviewer_id: str, decision: str, notes: str
) -> dict[str, Any]:
    """UNDER_REVIEW -> DRAFT.

    Deliberately NOT terminal. The month still has to be reported, so a
    rejection returns the record for correction rather than closing it.
    """
    if not notes or len(notes.strip()) < 5:
        raise ManhoursStatusError("INVALID", "Reject / return reason must be at least 5 characters.")
    if decision not in ("REJECTED", "RETURNED_FOR_REVISION"):
        raise ManhoursStatusError("INVALID", f"Unknown review decision: {decision}")

    submission = await db.get(ManhoursSubmission, submission_id)
    if submission is None:
        raise ManhoursStatusError("MISSING", "Submission not found")
    if submission.status != "UNDER_REVIEW":
        raise ManhoursStatusError(
            submission.status,
            f"Cannot reject a submission in {submission.status} state (expected UNDER_REVIEW).",
        )

    _definition, steps = await _load_definition(db)
    checker = _step(steps, "CHECKER")
    instance = await _ensure_instance(db, submission, reviewer_id)

    await _close_open_tasks(db, instance.id, checker.id)
    db.add(
        _history(
            instance.id,
            checker,
            Action.REJECTED,
            reviewer_id,
            f"[{decision}] {notes}",
            to_status="REJECTED",
        )
    )
    # The INSTANCE goes REJECTED so the shared tracker reflects it; the
    # SUBMISSION goes back to DRAFT. Re-submitting reuses this instance.
    instance.status = "REJECTED"
    instance.currentStepName = "Rejected - returned to HSE Manager"

    submission.status = "DRAFT"
    submission.reviewedById = reviewer_id
    submission.reviewedAt = datetime.now(timezone.utc)
    submission.reviewerNotes = notes
    submission.reviewDecision = decision
    await db.flush()
    return {"status": submission.status}


async def unlock(
    db: AsyncSession, *, submission_id: str, unlocker_id: str, reason: str
) -> dict[str, Any]:
    """LOCKED -> UNLOCKED_FOR_REVISION, opening an audit event."""
    if not reason or len(reason.strip()) < 10:
        raise ManhoursStatusError(
            "INVALID",
            "Unlock reason must be at least 10 characters - this becomes the audit record.",
        )
    submission = await db.get(ManhoursSubmission, submission_id)
    if submission is None:
        raise ManhoursStatusError("MISSING", "Submission not found")
    if submission.status != "LOCKED":
        raise ManhoursStatusError(
            submission.status,
            f"Cannot unlock a submission in {submission.status} state (expected LOCKED).",
        )

    # A reopen-from-reopen would leave two open events and make the
    # before/after diff meaningless.
    open_event = (
        await db.execute(
            select(ManhoursUnlockEvent)
            .where(ManhoursUnlockEvent.submissionId == submission_id)
            .where(ManhoursUnlockEvent.reLockedAt.is_(None))
            .limit(1)
        )
    ).scalar_one_or_none()
    if open_event is not None:
        raise ManhoursStatusError(
            submission.status,
            "This submission already has an open unlock event. Re-lock first.",
        )

    event = ManhoursUnlockEvent(
        submissionId=submission_id, unlockedById=unlocker_id, reason=reason
    )
    db.add(event)

    _definition, steps = await _load_definition(db)
    closure = _step(steps, "CLOSURE")
    instance = (
        await db.execute(
            select(WorkflowInstance)
            .where(WorkflowInstance.module == WORKFLOW_MODULE)
            .where(WorkflowInstance.recordId == submission_id)
        )
    ).scalar_one_or_none()
    if instance is not None:
        instance.status = "IN_PROGRESS"
        instance.currentStepId = closure.id
        instance.currentStepName = "Re-lock pending"
        instance.completedAt = None
        db.add(
            _history(
                instance.id,
                closure,
                Action.REASSIGNED,
                unlocker_id,
                f"[UNLOCK] {reason}",
                to_status="IN_PROGRESS",
            )
        )

    submission.status = "UNLOCKED_FOR_REVISION"
    await db.flush()
    return {"unlockEventId": event.id}


# -- Lock-time KPI snapshot ------------------------------------------


async def capture_kpi_snapshot(
    db: AsyncSession, *, submission_id: str, captured_by_id: str
) -> dict[str, Any]:
    """Freeze the period's KPIs into the submission.

    This is the audit-defensibility contract: "what was our LTIFR in March
    2026" must return the same answer forever. Later re-renders read THIS
    snapshot; the engine is never re-run for a historical period, so a
    subsequent reclassification of a source incident cannot rewrite a figure
    that has already been reported.

    The registry version is stamped in so an auditor can tell which generation
    of the formulas produced any given historical number.
    """
    from app.services.manhours_kpi_engine import KpiEngine
    from app.services.manhours_kpi_registry import REGISTRY_VERSION

    submission = await db.get(ManhoursSubmission, submission_id)
    if submission is None:
        raise ManhoursStatusError("MISSING", "Submission not found")

    engine = KpiEngine(db)
    kpis = await engine.compute_all(
        submission.plantId, submission.reportingYear, submission.reportingMonth
    )
    return {
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "capturedById": captured_by_id,
        "registryVersion": REGISTRY_VERSION,
        "scope": {"plantId": submission.plantId},
        "period": {
            "year": submission.reportingYear,
            "month": submission.reportingMonth,
        },
        "kpis": kpis,
    }


async def corporate_lock(
    db: AsyncSession, *, submission_id: str, locker_id: str, notes: str | None
) -> dict[str, Any]:
    """APPROVED -> LOCKED, capturing the KPI snapshot in the same unit of work.

    Snapshot and status flip must land together: a LOCKED row without a
    snapshot is a reported figure with no evidence behind it.
    """
    submission = await db.get(ManhoursSubmission, submission_id)
    if submission is None:
        raise ManhoursStatusError("MISSING", "Submission not found")
    if submission.status != "APPROVED":
        raise ManhoursStatusError(
            submission.status,
            f"Cannot lock a submission in {submission.status} state (expected APPROVED).",
        )

    snapshot = await capture_kpi_snapshot(
        db, submission_id=submission_id, captured_by_id=locker_id
    )

    _definition, steps = await _load_definition(db)
    closure = _step(steps, "CLOSURE")
    instance = await _ensure_instance(db, submission, locker_id)
    await _close_open_tasks(db, instance.id, closure.id)
    db.add(_history(instance.id, closure, Action.APPROVED, locker_id, notes, to_status="COMPLETED"))
    instance.status = "COMPLETED"
    instance.currentStepName = "Locked"
    instance.completedAt = datetime.now(timezone.utc)

    submission.status = "LOCKED"
    submission.lockedById = locker_id
    submission.lockedAt = datetime.now(timezone.utc)
    submission.lockNotes = notes
    submission.kpiSnapshot = snapshot
    await db.flush()
    return {"status": submission.status, "kpiSnapshot": snapshot}


async def relock(
    db: AsyncSession, *, submission_id: str, locker_id: str, notes: str | None
) -> dict[str, Any]:
    """UNLOCKED_FOR_REVISION -> LOCKED.

    Takes a FRESH snapshot: the whole point of the unlock was to correct the
    data, so the historical KPIs must reflect the correction. The PREVIOUS
    snapshot is preserved in the unlock event's changeLog, so the audit trail
    holds both the before and the after.
    """
    submission = await db.get(ManhoursSubmission, submission_id)
    if submission is None:
        raise ManhoursStatusError("MISSING", "Submission not found")
    if submission.status != "UNLOCKED_FOR_REVISION":
        raise ManhoursStatusError(
            submission.status,
            f"Cannot re-lock a submission in {submission.status} state "
            "(expected UNLOCKED_FOR_REVISION).",
        )

    open_event = (
        await db.execute(
            select(ManhoursUnlockEvent)
            .where(ManhoursUnlockEvent.submissionId == submission_id)
            .where(ManhoursUnlockEvent.reLockedAt.is_(None))
            .order_by(ManhoursUnlockEvent.unlockedAt.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if open_event is None:
        raise ManhoursStatusError(
            submission.status,
            "No open unlock event found for this submission - state is inconsistent.",
        )

    previous = submission.kpiSnapshot
    fresh = await capture_kpi_snapshot(
        db, submission_id=submission_id, captured_by_id=locker_id
    )

    open_event.reLockedAt = datetime.now(timezone.utc)
    open_event.reLockedById = locker_id
    open_event.changeLog = {"before": previous, "after": fresh}

    _definition, steps = await _load_definition(db)
    closure = _step(steps, "CLOSURE")
    instance = await _ensure_instance(db, submission, locker_id)
    await _close_open_tasks(db, instance.id, closure.id)
    db.add(
        _history(
            instance.id,
            closure,
            Action.APPROVED,
            locker_id,
            f"[RE-LOCK] {notes or ''}".strip(),
            to_status="COMPLETED",
        )
    )
    instance.status = "COMPLETED"
    instance.currentStepName = "Locked"
    instance.completedAt = datetime.now(timezone.utc)

    submission.status = "LOCKED"
    submission.lockedById = locker_id
    submission.lockedAt = datetime.now(timezone.utc)
    submission.lockNotes = notes
    submission.kpiSnapshot = fresh
    await db.flush()
    return {"status": submission.status, "unlockEventId": open_event.id}
