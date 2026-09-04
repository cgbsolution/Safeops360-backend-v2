"""Safety Observation: verification returns to the observer, closure goes to the Plant Head.

Two changes to steps 3 and 4 of "Safety Observation — Standard Workflow":

    3 VERIFIER  HSE Officer Verification (SAFETY_OFFICER)  →  Observer Verification (ORIGINATOR)
    4 CLOSURE   HSE Manager Closure      (HSE_MANAGER)     →  Plant Head Closure   (PLANT_HEAD)

Why verification goes back to the raiser: the person who reported the hazard is
the one who knows what "fixed" looks like for it, and they are standing in the
area. A Safety Officer verifying a report they never saw is checking a
photograph against a description. This makes the loop close where it opened.

Why closure moves to the Plant Head: closure is the accountability signature on
the plant's own record, not an HSE administrative act.

Design notes
------------
* Step ROWS ARE UPDATED IN PLACE — no delete, no re-sequence, no new ids. Only
  the approver fields and display names change. That is what keeps the 22
  in-flight instances valid: their currentStepId and every WorkflowTask.stepId
  still point at rows that exist.

* Changing a definition does NOT retarget tasks that are already assigned. An
  observation sitting on the verifier step right now has a real, open task in a
  real person's inbox; silently moving it would make someone's queue change
  under them with no audit trail. So existing tasks are reported, and moved only
  when asked — see --in-flight below.

* The pre-change definition is snapshotted into WorkflowDefinitionVersion first,
  so Configuration → Workflows → History can show and restore it. Same table the
  admin UI writes to.

--in-flight strategies
----------------------
  report      (default) List every open task on the two changed steps and change
              no task. The definition is still updated, so everything that
              reaches verification FROM NOW ON routes to the observer.
  reassign    Additionally retarget each open task on those steps to the newly
              resolved assignee, closing nothing and approving nothing: the task
              keeps its id, SLA and history, and a REASSIGNED entry naming this
              script is written. Tasks already held by the correct person are
              left untouched rather than rewritten to themselves.

Run:
    python -m scripts.observation_verifier_closure                          # dry run
    python -m scripts.observation_verifier_closure --apply                  # change the definition
    python -m scripts.observation_verifier_closure --in-flight=reassign --apply
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
    WorkflowDefinition,
    WorkflowDefinitionVersion,
    WorkflowHistory,
    WorkflowInstance,
    WorkflowStep,
    WorkflowTask,
)
from app.services import workflow_engine as engine

MODULE = "OBSERVATION"

# The two steps being retargeted, keyed by stepType — matching on the display
# name would break the moment someone renames a step in the admin UI, which is
# exactly the sort of edit this script has to survive.
VERIFIER_NAME = "Observer Verification"
CLOSURE_NAME = "Plant Head Closure"

NEW_DESCRIPTION = "Observation lifecycle: Observer → Action Owner → Observer verifies → Plant Head closes"

CHANGE_NOTE = (
    "Verification returned to the observer who raised the observation "
    "(approverField=ORIGINATOR, was approverRole=SAFETY_OFFICER); closure moved "
    "to the Plant Head (approverRole=PLANT_HEAD, was HSE_MANAGER)."
)

OPEN_TASK_STATUSES = ("PENDING", "OVERDUE", "ESCALATED")


# A Windows console defaults to cp1252, which cannot encode the arrows and
# check marks this report is built from — printing one raises UnicodeEncodeError
# and takes the whole run down before anything is written. Reconfigure once here
# rather than stripping the characters out of every message.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover - non-standard stdout
    pass


def _log(msg: str) -> None:
    print(msg, flush=True)


async def _editor_id(db) -> str | None:
    """Someone to attribute the definition version to — WorkflowDefinitionVersion
    .editedById is NOT NULL, so a scripted edit still needs an author. The HSE
    Manager who owns the workflow config is the closest thing to one."""
    return (
        await db.execute(
            select(User.id).where(User.role == "HSE_MANAGER").order_by(User.email).limit(1)
        )
    ).scalar_one_or_none()


async def _load_definition(db) -> WorkflowDefinition:
    definition = (
        await db.execute(
            select(WorkflowDefinition)
            .where(WorkflowDefinition.module == MODULE)
            .where(WorkflowDefinition.isActive.is_(True))
            .options(selectinload(WorkflowDefinition.steps))
        )
    ).scalars().first()
    if definition is None:
        raise SystemExit(f"No active {MODULE} workflow definition found.")
    return definition


def _snapshot(definition: WorkflowDefinition) -> dict:
    return {
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
            for st in sorted(definition.steps, key=lambda s: s.sequence)
        ],
    }


async def reshape_definition(db, *, definition: WorkflowDefinition, editor_id: str | None, apply: bool):
    steps = sorted(definition.steps, key=lambda s: s.sequence)

    def _by_type(step_type: str) -> WorkflowStep | None:
        for st in steps:
            raw = st.stepType.value if hasattr(st.stepType, "value") else st.stepType
            if raw == step_type:
                return st
        return None

    verifier = _by_type("VERIFIER")
    closure = _by_type("CLOSURE")
    if verifier is None or closure is None:
        raise SystemExit("Definition has no VERIFIER and/or CLOSURE step — nothing to retarget.")

    _log("\n[A] definition changes")
    _log(
        f"    step {verifier.sequence} VERIFIER {verifier.name!r}: "
        f"approverRole={verifier.approverRole!r} → None, approverField=None → 'ORIGINATOR', "
        f"name → {VERIFIER_NAME!r}"
    )
    _log(
        f"    step {closure.sequence} CLOSURE  {closure.name!r}: "
        f"approverRole={closure.approverRole!r} → 'PLANT_HEAD', name → {CLOSURE_NAME!r}"
    )
    _log(f"    description → {NEW_DESCRIPTION!r}")

    if not apply:
        return verifier, closure

    last_version = (
        await db.execute(
            select(WorkflowDefinitionVersion.version)
            .where(WorkflowDefinitionVersion.definitionId == definition.id)
            .order_by(WorkflowDefinitionVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none() or 0

    db.add(
        WorkflowDefinitionVersion(
            definitionId=definition.id,
            version=last_version + 1,
            snapshot=json.dumps(_snapshot(definition)),
            editedById=editor_id,
            changeNote=CHANGE_NOTE,
        )
    )

    # ORIGINATOR resolves to Observation.observerId — see
    # workflow_engine._resolve_assignee, which falls back to the instance
    # initiator when the record carries no observer.
    verifier.approverRole = None
    verifier.approverField = "ORIGINATOR"
    verifier.approverUserId = None
    verifier.approverGroupRoles = None
    verifier.name = VERIFIER_NAME

    # PLANT_HEAD resolves per record: _find_user_by_roles prefers a holder of the
    # role at the record's own plant before falling back to a globally scoped one.
    closure.approverRole = "PLANT_HEAD"
    closure.approverField = None
    closure.approverUserId = None
    closure.approverGroupRoles = None
    closure.name = CLOSURE_NAME

    definition.description = NEW_DESCRIPTION
    await db.flush()
    _log(f"    ✓ applied, previous chain snapshotted as version {last_version + 1}")
    return verifier, closure


async def handle_in_flight(db, *, definition, verifier, closure, strategy: str, apply: bool):
    """Report — and optionally retarget — open tasks sitting on the two steps."""
    step_ids = {verifier.id: verifier, closure.id: closure}
    tasks = (
        await db.execute(
            select(WorkflowTask)
            .where(WorkflowTask.module == MODULE)
            .where(WorkflowTask.stepId.in_(list(step_ids)))
            .where(WorkflowTask.status.in_(OPEN_TASK_STATUSES))
        )
    ).scalars().all()

    _log(f"\n[B] in-flight tasks on the two changed steps: {len(tasks)}")
    if not tasks:
        _log("    nothing parked — every future observation uses the new routing.")
        return

    moves: list[tuple[WorkflowTask, str, str]] = []
    for t in tasks:
        step = step_ids[t.stepId]
        obs = await db.get(Observation, t.recordId)
        if obs is None:
            _log(f"    ! task {t.id} references a missing observation {t.recordId} — skipped")
            continue
        record_data = {
            "observerId": obs.observerId,
            "responsiblePersonId": obs.responsiblePersonId,
            "actionOwnerId": obs.responsiblePersonId,
            "plantId": obs.plantId,
        }
        resolved = await engine._resolve_assignee(
            db,
            approver_role=step.approverRole,
            approver_field=step.approverField,
            approver_user_id=step.approverUserId,
            approver_group_roles=step.approverGroupRoles,
            record_data=record_data,
            initiator_id=obs.observerId,
            plant_id=obs.plantId,
        )
        holder = await db.get(User, t.assignedToId)
        target = await db.get(User, resolved) if resolved else None
        holder_name = holder.name if holder else t.assignedToId
        if resolved is None:
            _log(f"    ! {obs.number} {step.name}: no assignee resolves — leaving with {holder_name}")
            continue
        if resolved == t.assignedToId:
            _log(f"    = {obs.number} {step.name}: already {holder_name}")
            continue
        _log(
            f"    → {obs.number} {step.name}: {holder_name} → {target.name if target else resolved}"
        )
        moves.append((t, resolved, obs.number))

    if strategy != "reassign":
        _log(
            f"\n    {len(moves)} task(s) would move. These are left alone: an open task is in "
            "someone's\n    inbox right now. Re-run with --in-flight=reassign to retarget them."
        )
        return

    if not apply:
        _log(f"\n    [dry run] {len(moves)} task(s) would be reassigned.")
        return

    now = datetime.now(timezone.utc)
    for task, new_assignee, number in moves:
        old = task.assignedToId
        task.assignedToId = new_assignee
        db.add(
            WorkflowHistory(
                instanceId=task.instanceId,
                stepId=task.stepId,
                action=Action.REASSIGNED,
                performedById=None,
                performedAt=now,
                comments=(
                    "Reassigned by scripts/observation_verifier_closure.py — the step's "
                    "assignee rule changed (verification returns to the observer, closure "
                    "moves to the Plant Head). No decision was made on anyone's behalf."
                ),
            )
        )
        _log(f"    ✓ {number}: {old} → {new_assignee}")
    await db.flush()
    _log(f"\n    ✓ {len(moves)} task(s) reassigned.")


async def main() -> None:
    apply = "--apply" in sys.argv
    strategy = "report"
    for arg in sys.argv[1:]:
        if arg.startswith("--in-flight="):
            strategy = arg.split("=", 1)[1].strip()
    if strategy not in ("report", "reassign"):
        raise SystemExit("--in-flight must be 'report' or 'reassign'")

    _log(f"Safety Observation — verifier/closure retarget ({'APPLY' if apply else 'dry run'})")

    async with AsyncSessionLocal() as db:
        definition = await _load_definition(db)
        editor_id = await _editor_id(db)
        if apply and editor_id is None:
            raise SystemExit(
                "No HSE_MANAGER user to attribute the definition version to, and "
                "WorkflowDefinitionVersion.editedById is NOT NULL. Seed one first."
            )
        _log(f"\ndefinition: {definition.name} ({definition.id})")

        verifier, closure = await reshape_definition(
            db, definition=definition, editor_id=editor_id, apply=apply
        )
        await handle_in_flight(
            db, definition=definition, verifier=verifier, closure=closure,
            strategy=strategy, apply=apply,
        )

        if apply:
            await db.commit()
            _log("\ncommitted.")
        else:
            await db.rollback()
            _log("\ndry run — nothing written. Re-run with --apply.")


if __name__ == "__main__":
    asyncio.run(main())
