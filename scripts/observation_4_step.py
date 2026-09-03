"""Safety Observation: collapse the workflow to 4 steps.

"Section Head Review" is removed. Its only real output was naming the action
owner, and that is now a field on the maker form (Observation.responsiblePersonId,
set by the observer at submit). What was left of the step was a 24-hour approval
gate standing between somebody reporting a hazard and anybody being asked to fix
it. The chain becomes:

    1 MAKER          Submitted by Observer
    2 ASSIGNEE_TASK  Action Owner Executes      (ACTION_OWNER, 168h, esc HSE_MANAGER)
    3 VERIFIER       HSE Officer Verification   (SAFETY_OFFICER, 24h)
    4 CLOSURE        HSE Manager Closure        (HSE_MANAGER)

Verification and closure are untouched: they are still separate, independent
hands. The review that matters is on the fix, not on the report.

Design notes
------------
* The step row is DELETED and the steps after it are RE-SEQUENCED IN PLACE —
  their primary keys are preserved. Deleting and recreating the whole step set
  (what seed_workflows.upsert_definition does) would mint new ids and orphan
  every in-flight instance's currentStepId / WorkflowTask.stepId.
* The pre-change definition is snapshotted into WorkflowDefinitionVersion first,
  so Configuration → Workflows → History can show and restore the 5-step
  version. Same table the admin UI writes to.
* In-flight work is the whole difficulty here, so it is an explicit choice
  rather than a default — see --in-flight below. Nothing is approved on anyone's
  behalf: an observation parked on Section Head Review has NOT been reviewed,
  and writing an APPROVED history row for it would put a decision nobody made
  into an EHS audit trail. The migrate strategy records Action.REASSIGNED with a
  note naming this script, which is what actually happened.

--in-flight strategies
----------------------
  abort    (default) Report what is parked on the step and change nothing.
  migrate            Move each parked instance to "Action Owner Executes":
                     close its open review tasks as SKIPPED, write a REASSIGNED
                     history entry, and create the execution task through the
                     engine's own task builder so the assignee, SLA, escalation
                     and record-status sync all resolve exactly as they would
                     for a new observation. Observations with no action owner
                     resolve to the observer (engine fallback) — those are
                     listed explicitly before anything is written.

Run:
    python -m scripts.observation_4_step                              # dry run, report only
    python -m scripts.observation_4_step --in-flight=migrate          # dry run, full plan
    python -m scripts.observation_4_step --in-flight=migrate --apply  # do it
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.db import AsyncSessionLocal
from app.models.observation import Observation
from app.models.user import User
from app.models.workflow import (
    Action,
    TaskStatus,
    WorkflowDefinition,
    WorkflowDefinitionVersion,
    WorkflowHistory,
    WorkflowInstance,
    WorkflowStep,
    WorkflowTask,
)
from app.services import workflow_engine as engine

DROP_STEP_NAME = "Section Head Review"
NEW_DESCRIPTION = (
    "Observation lifecycle: Observer → Action Owner → HSE Officer → HSE Manager"
)
CHANGE_NOTE = (
    "Safety Observation reduced to a 4-step chain — Section Head Review removed. "
    "The action owner is now named by the observer on the submission form, which "
    "was the step's only output; the remaining 24h approval gate only delayed the fix."
)
MIGRATE_NOTE = (
    "Step removed by workflow redesign (Section Head Review). Not reviewed and not "
    "approved — moved straight to execution by scripts/observation_4_step.py."
)
OPEN_STATUSES = {"PENDING", "OVERDUE", "ESCALATED"}


def _log(msg: str = "") -> None:
    print(msg, flush=True)


async def _editor_id(db) -> str | None:
    """Someone to attribute the definition version to. The HSE Manager who owns
    the workflow config is the closest thing to an author for a scripted edit."""
    return (
        await db.execute(
            select(User.id).where(User.role == "HSE_MANAGER").order_by(User.email).limit(1)
        )
    ).scalar_one_or_none()


# ─────────────────────────────────────────────────────────────────────────
# Part A — survey what the change would disturb
# ─────────────────────────────────────────────────────────────────────────
async def survey(db) -> tuple[WorkflowDefinition, WorkflowStep | None, list[WorkflowInstance]]:
    definition = (
        await db.execute(
            select(WorkflowDefinition)
            .options(selectinload(WorkflowDefinition.steps))
            .where(
                WorkflowDefinition.module == "OBSERVATION",
                WorkflowDefinition.recordType.is_(None),
            )
        )
    ).scalar_one()

    steps = sorted(definition.steps, key=lambda s: s.sequence)
    _log(f"\n[A] {definition.name} ({definition.id}) — {len(steps)} steps")
    for s in steps:
        st = s.stepType.value if hasattr(s.stepType, "value") else s.stepType
        _log(f"      {s.sequence}. {st:14s} {s.name!r}")

    victim = next((s for s in steps if s.name == DROP_STEP_NAME), None)
    if victim is None:
        _log(f"    → {DROP_STEP_NAME!r} already gone; definition left as-is.")
        return definition, None, []

    parked = (
        await db.execute(
            select(WorkflowInstance)
            .where(WorkflowInstance.currentStepId == victim.id)
            .order_by(WorkflowInstance.initiatedAt)
        )
    ).scalars().all()
    open_tasks = (
        await db.execute(
            select(WorkflowTask).where(
                WorkflowTask.stepId == victim.id,
                WorkflowTask.status.in_(OPEN_STATUSES),
            )
        )
    ).scalars().all()

    _log(
        f"\n    in flight on {DROP_STEP_NAME!r}: "
        f"{len(parked)} instance(s), {len(open_tasks)} open task(s)"
    )
    for inst in parked:
        obs = await db.get(Observation, inst.recordId)
        owner = None
        if obs is not None and obs.responsiblePersonId:
            owner = await db.get(User, obs.responsiblePersonId)
        observer = await db.get(User, obs.observerId) if obs is not None else None
        who = (
            f"owner {owner.name}"
            if owner is not None
            else f"NO ACTION OWNER → falls back to observer {observer.name if observer else '?'}"
        )
        _log(f"      · {obs.number if obs else inst.recordId} — {who}")

    return definition, victim, list(parked)


# ─────────────────────────────────────────────────────────────────────────
# Part B — move in-flight work off the step
# ─────────────────────────────────────────────────────────────────────────
async def migrate_in_flight(
    db, *, definition: WorkflowDefinition, victim: WorkflowStep,
    parked: list[WorkflowInstance], actor_id: str, apply: bool,
) -> None:
    """Re-point each parked instance at the ASSIGNEE_TASK step.

    Deliberately NOT engine.approve(): nobody approved these. The open review
    tasks are closed as SKIPPED and the reason is recorded as REASSIGNED, then
    the execution task is built by the engine's own `_create_task_for_step` so
    assignee resolution, SLA, escalation and record-status sync behave exactly
    as they do for a new observation.
    """
    target = next(
        s for s in definition.steps
        if (s.stepType.value if hasattr(s.stepType, "value") else s.stepType) == "ASSIGNEE_TASK"
    )
    _log(f"\n[B] move {len(parked)} instance(s) → {target.name!r}")

    now = datetime.now(timezone.utc)
    for inst in parked:
        obs = await db.get(Observation, inst.recordId)
        tasks = (
            await db.execute(
                select(WorkflowTask).where(
                    WorkflowTask.instanceId == inst.id,
                    WorkflowTask.stepId == victim.id,
                    WorkflowTask.status.in_(OPEN_STATUSES),
                )
            )
        ).scalars().all()
        _log(
            f"      · {obs.number if obs else inst.recordId}: "
            f"skip {len(tasks)} open task(s), create execution task"
        )
        if not apply:
            continue

        for t in tasks:
            t.status = TaskStatus.SKIPPED.value
            t.completedAt = now
        # WorkflowHistory.performedById is NOT NULL with an FK to User, so a
        # system migration still needs an attributable actor. The workflow owner
        # (HSE Manager) is the closest thing to one; REASSIGNED plus the note
        # below is what stops this reading as an approval they gave.
        db.add(
            WorkflowHistory(
                instanceId=inst.id,
                stepId=victim.id,
                stepName=victim.name,
                action=Action.REASSIGNED.value,
                performedById=actor_id,
                comments=MIGRATE_NOTE,
            )
        )
        await db.flush()

        record_data = await engine._enrich_record_data(
            db, module="OBSERVATION", record_id=inst.recordId, base={}
        )
        inst.currentStepId = target.id
        inst.currentStepName = target.name
        await db.flush()

        await engine._create_task_for_step(
            db,
            instance=inst,
            step=target,
            record_data=record_data,
            record_number=obs.number if obs else None,
            record_title=(obs.description[:120] if obs and obs.description else None),
            module="OBSERVATION",
            record_id=inst.recordId,
            initiator_id=inst.initiatedById,
            plant_id=obs.plantId if obs else None,
        )
        await engine._sync_record_status(
            db,
            module="OBSERVATION",
            record_id=inst.recordId,
            next_step_type=(
                target.stepType.value if hasattr(target.stepType, "value") else target.stepType
            ),
            instance_completed=False,
            actor_id=None,
            next_step_name=target.name,
        )
        await db.flush()


# ─────────────────────────────────────────────────────────────────────────
# Part C — reshape the definition: 5 steps → 4
# ─────────────────────────────────────────────────────────────────────────
async def reshape_definition(
    db, *, definition: WorkflowDefinition, victim: WorkflowStep,
    editor_id: str | None, apply: bool,
) -> None:
    steps = sorted(definition.steps, key=lambda s: s.sequence)
    snapshot = {
        "name": definition.name,
        "description": definition.description,
        "module": definition.module,
        "recordType": definition.recordType,
        "isActive": definition.isActive,
        "steps": [
            {
                "sequence": st.sequence,
                "stepType": st.stepType.value if hasattr(st.stepType, "value") else st.stepType,
                "name": st.name,
                "approverRole": st.approverRole,
                "approverField": st.approverField,
                "approverUserId": st.approverUserId,
                "approverGroupRoles": st.approverGroupRoles,
                "slaHours": st.slaHours,
                "slaUnit": st.slaUnit,
                "escalationRole": st.escalationRole,
                "isOptional": st.isOptional,
                "conditionExpr": st.conditionExpr,
                "notes": st.notes,
            }
            for st in steps
        ],
    }
    last_version = (
        await db.execute(
            select(WorkflowDefinitionVersion.version)
            .where(WorkflowDefinitionVersion.definitionId == definition.id)
            .order_by(WorkflowDefinitionVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none() or 0

    survivors = [s for s in steps if s.id != victim.id]
    _log(f"\n[C] snapshot 5-step chain as version {last_version + 1}")
    _log(f"    → delete step {victim.sequence}. {victim.name!r} ({victim.id})")
    for i, s in enumerate(survivors, start=1):
        if s.sequence != i:
            _log(f"    → re-sequence {s.name!r}: {s.sequence} → {i}")
    _log(f"    → description → {NEW_DESCRIPTION!r}")

    if not apply:
        return

    db.add(
        WorkflowDefinitionVersion(
            definitionId=definition.id,
            version=last_version + 1,
            snapshot=json.dumps(snapshot),
            editedById=editor_id,
            changeNote=CHANGE_NOTE,
        )
    )
    await db.delete(victim)
    await db.flush()
    for i, s in enumerate(survivors, start=1):
        s.sequence = i
    definition.description = NEW_DESCRIPTION
    await db.flush()


async def main() -> None:
    apply = "--apply" in sys.argv
    strategy = "abort"
    for arg in sys.argv[1:]:
        if arg.startswith("--in-flight="):
            strategy = arg.split("=", 1)[1].strip()
    if strategy not in ("abort", "migrate"):
        raise SystemExit(f"Unknown --in-flight strategy {strategy!r}. Use abort | migrate.")

    _log(f"Safety Observation → 4 steps  (in-flight={strategy}, {'APPLY' if apply else 'dry run'})")

    async with AsyncSessionLocal() as db:
        definition, victim, parked = await survey(db)
        if victim is None:
            return

        actor_id = await _editor_id(db)
        if parked and strategy == "migrate" and not actor_id:
            raise SystemExit(
                "ABORT: no HSE_MANAGER user to attribute the migration history to. "
                "WorkflowHistory.performedById is NOT NULL."
            )

        if parked and strategy == "abort":
            _log(
                f"\nABORT: {len(parked)} instance(s) sit on {DROP_STEP_NAME!r}. Removing the "
                "step now would strand them with a currentStepId that no longer exists.\n"
                "Re-run with --in-flight=migrate to move them to 'Action Owner Executes',\n"
                "or clear them through the UI first."
            )
            return

        if parked:
            await migrate_in_flight(
                db, definition=definition, victim=victim, parked=parked,
                actor_id=actor_id, apply=apply,
            )

        await reshape_definition(
            db, definition=definition, victim=victim,
            editor_id=actor_id, apply=apply,
        )

        if apply:
            await db.commit()
            _log("\nCommitted.")
        else:
            await db.rollback()
            _log("\nDry run — nothing written. Re-run with --apply.")


if __name__ == "__main__":
    asyncio.run(main())
