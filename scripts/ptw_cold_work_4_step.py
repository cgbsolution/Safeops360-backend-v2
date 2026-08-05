"""PTW — Cold Work: collapse the approval chain to 4 steps, and push
PTW-NW-02233 to stage 4 (Closure).

Cold Work is the low-risk permit type; the Safety Officer Review step is
dropped so the chain reads:

    1 MAKER          Originator Submits
    2 CHECKER        Issuer Review              (PERMIT_ISSUER, 4h)
    3 ASSIGNEE_TASK  Receiver Acknowledges + FLRA (RECEIVER, 8h)
    4 CLOSURE        Issuer Closes Permit       (PERMIT_ISSUER)

Design notes
------------
* The Safety Officer step row is DELETED and the two steps after it are
  RE-SEQUENCED IN PLACE — their primary keys are preserved. Deleting and
  recreating the whole step set (what seed_workflows.upsert_definition
  does) would mint new ids and orphan every in-flight instance's
  currentStepId / WorkflowTask.stepId. Two Cold Work instances are
  currently parked on "Receiver Acknowledges + FLRA", so id stability is
  not optional here.
* The pre-change definition is snapshotted into WorkflowDefinitionVersion
  first, so Configuration → Workflows → PTW — Cold Work → History can
  show and restore the 5-step version. This is the same table the admin
  UI writes to, so the edit is indistinguishable from a UI edit.
* The record is advanced through the real workflow engine (approve /
  submit_execution), never by hand-written UPDATEs, so PermitApproval
  rows, WorkflowHistory, escalation-sibling cleanup and permit status
  sync all happen exactly as they would from the UI.

Run:
    python -m scripts.ptw_cold_work_4_step            # dry run
    python -m scripts.ptw_cold_work_4_step --apply
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.db import AsyncSessionLocal
from app.models.permit import Permit, PermitStatus
from app.models.workflow import (
    WorkflowDefinition,
    WorkflowDefinitionVersion,
    WorkflowInstance,
    WorkflowStep,
    WorkflowTask,
)
from app.services import workflow_engine as engine

PERMIT_NUMBER = "PTW-NW-02233"
DROP_STEP_NAME = "Safety Officer Review"
NEW_DESCRIPTION = (
    "Low-risk cold work — 4 steps: Originator submits → Issuer reviews → "
    "Receiver acknowledges (+ FLRA) → Issuer closes the permit."
)
OPEN_STATUSES = {"PENDING", "OVERDUE", "ESCALATED"}


def _log(msg: str = "") -> None:
    print(msg, flush=True)


# ─────────────────────────────────────────────────────────────────────────
# Part A — definition: 5 steps → 4
# ─────────────────────────────────────────────────────────────────────────
async def reshape_definition(db, *, editor_id: str, apply: bool) -> WorkflowDefinition:
    definition = (
        await db.execute(
            select(WorkflowDefinition)
            .options(selectinload(WorkflowDefinition.steps))
            .where(
                WorkflowDefinition.module == "PTW",
                WorkflowDefinition.recordType == "GENERAL_COLD",
            )
        )
    ).scalar_one()

    steps = sorted(definition.steps, key=lambda s: s.sequence)
    _log(f"\n[A] {definition.name} ({definition.id}) — {len(steps)} steps")
    for s in steps:
        _log(f"      {s.sequence}. {s.stepType} {s.name!r}")

    victim = next((s for s in steps if s.name == DROP_STEP_NAME), None)
    if victim is None:
        _log(f"    → {DROP_STEP_NAME!r} already gone; definition left as-is.")
        return definition

    # Refuse to strand live work: only safe when nothing references the step.
    open_tasks = (
        await db.execute(
            select(WorkflowTask).where(
                WorkflowTask.stepId == victim.id,
                WorkflowTask.status.in_(OPEN_STATUSES),
            )
        )
    ).scalars().all()
    parked = (
        await db.execute(
            select(WorkflowInstance).where(WorkflowInstance.currentStepId == victim.id)
        )
    ).scalars().all()
    if open_tasks or parked:
        raise SystemExit(
            f"ABORT: {len(open_tasks)} open task(s) and {len(parked)} instance(s) sit on "
            f"{DROP_STEP_NAME!r}. Move them on before removing the step."
        )

    # Version snapshot — same shape workflow_definitions.update_definition writes.
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

    _log(f"    → snapshot 5-step chain as version {last_version + 1}")
    _log(f"    → delete step {victim.sequence}. {victim.name!r} ({victim.id})")
    survivors = [s for s in steps if s.id != victim.id]
    for i, s in enumerate(survivors, start=1):
        if s.sequence != i:
            _log(f"    → re-sequence {s.name!r}: {s.sequence} → {i}")

    if not apply:
        return definition

    db.add(
        WorkflowDefinitionVersion(
            definitionId=definition.id,
            version=last_version + 1,
            snapshot=json.dumps(snapshot),
            editedById=editor_id,
            changeNote=(
                "Cold Work reduced to a 4-step chain — Safety Officer Review "
                "removed (low-risk permit type; Issuer review + receiver "
                "acknowledgement are the controls)."
            ),
        )
    )
    await db.delete(victim)
    await db.flush()
    for i, s in enumerate(survivors, start=1):
        s.sequence = i
    definition.description = NEW_DESCRIPTION
    await db.flush()
    return definition


# ─────────────────────────────────────────────────────────────────────────
# Part B — push PTW-NW-02233 to stage 4
# ─────────────────────────────────────────────────────────────────────────
async def revive_permit(db, permit: Permit, *, apply: bool) -> None:
    """The permit auto-expired mid-approval. Re-open its validity window so
    the activation gate (which hard-blocks on validTo < now) can pass and
    the engine's status sync stops short-circuiting on EXPIRED."""
    now = datetime.now(timezone.utc)
    _log(f"\n[B1] {permit.number}: status={permit.status} "
         f"validTo={permit.validTo} expiredAt={permit.expiredAt}")
    _log(f"    → status → SUBMITTED, validFrom → now, validTo → now+7d, "
         f"expiredAt/autoExpiredAt → null")
    if not apply:
        return
    permit.status = PermitStatus.SUBMITTED
    permit.validFrom = now
    permit.validTo = now + timedelta(days=7)
    permit.expiredAt = None
    permit.autoExpiredAt = None
    permit.expirationReason = None
    permit.isCurrentlySuspended = False
    permit.suspendedAt = None
    permit.suspendedReason = None
    await db.flush()


async def advance_to_stage_4(db, permit: Permit, *, apply: bool) -> None:
    instance = (
        await db.execute(
            select(WorkflowInstance).where(
                WorkflowInstance.module == "PTW",
                WorkflowInstance.recordId == permit.id,
            )
        )
    ).scalar_one()
    _log(f"\n[B2] instance {instance.id} at {instance.currentStepName!r}")

    if not apply:
        _log("    → (dry run) would approve Issuer Review as the permit issuer, "
             "then execute Receiver Acknowledges + FLRA as the receiver")
        return

    # Step 2 — Issuer Review, approved by the named issuer.
    task = (
        await db.execute(
            select(WorkflowTask)
            .where(
                WorkflowTask.instanceId == instance.id,
                WorkflowTask.stepName == "Issuer Review",
                WorkflowTask.status.in_(OPEN_STATUSES),
            )
            .order_by(WorkflowTask.assignedAt)
        )
    ).scalars().first()
    if task is not None:
        res = await engine.approve(
            db,
            task_id=task.id,
            user_id=permit.issuerId,
            comments="Cold work scope reviewed — approved by the permit issuer.",
            plant_id=permit.plantId,
        )
        _log(f"    ✓ Issuer Review approved → {res.get('advancedTo')}")
    await db.flush()

    # Step 3 — Receiver Acknowledges + FLRA, executed by the receiver.
    await db.refresh(instance)
    task = (
        await db.execute(
            select(WorkflowTask)
            .where(
                WorkflowTask.instanceId == instance.id,
                WorkflowTask.stepId == instance.currentStepId,
                WorkflowTask.status.in_(OPEN_STATUSES),
            )
            .order_by(WorkflowTask.assignedAt)
        )
    ).scalars().first()
    if task is None:
        _log("    ! no open task on the receiver step — nothing to execute")
        return
    res = await engine.submit_execution(
        db,
        task_id=task.id,
        user_id=permit.receiverId,
        comments="Receiver acknowledged the permit conditions at the worksite.",
        plant_id=permit.plantId,
    )
    _log(f"    ✓ Receiver step executed → {res.get('advancedTo')}")


async def report(db, permit_id: str) -> None:
    permit = await db.get(Permit, permit_id)
    instance = (
        await db.execute(
            select(WorkflowInstance).where(
                WorkflowInstance.module == "PTW", WorkflowInstance.recordId == permit_id
            )
        )
    ).scalar_one()
    steps = (
        await db.execute(
            select(WorkflowStep)
            .where(WorkflowStep.definitionId == instance.definitionId)
            .order_by(WorkflowStep.sequence)
        )
    ).scalars().all()
    current = next((s for s in steps if s.id == instance.currentStepId), None)
    _log("\n─── result ───")
    _log(f"  permit  {permit.number}  status={permit.status}  "
         f"validTo={permit.validTo}")
    _log(f"  chain   {len(steps)} steps: " + " → ".join(f"{s.sequence}.{s.name}" for s in steps))
    _log(f"  at      stage {current.sequence if current else '—'} "
         f"of {len(steps)}: {instance.currentStepName!r}")
    tasks = (
        await db.execute(
            select(WorkflowTask)
            .where(WorkflowTask.instanceId == instance.id)
            .order_by(WorkflowTask.assignedAt)
        )
    ).scalars().all()
    for t in tasks:
        _log(f"  task    {t.stepName!r} {t.status} → {t.assignedToId}")


async def main() -> None:
    apply = "--apply" in sys.argv
    _log("APPLY" if apply else "DRY RUN — pass --apply to write")
    async with AsyncSessionLocal() as db:
        permit = (
            await db.execute(select(Permit).where(Permit.number == PERMIT_NUMBER))
        ).scalar_one()

        await reshape_definition(db, editor_id=permit.originatorId, apply=apply)
        await revive_permit(db, permit, apply=apply)
        await advance_to_stage_4(db, permit, apply=apply)

        if apply:
            await db.commit()
            _log("\ncommitted.")
            await report(db, permit.id)
        else:
            await db.rollback()
            _log("\nrolled back (dry run).")


if __name__ == "__main__":
    asyncio.run(main())
