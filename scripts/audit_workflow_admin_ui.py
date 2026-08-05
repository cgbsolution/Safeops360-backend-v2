"""Read-only audit: can every workflow definition actually be edited and
saved from Configuration → Workflows without breaking?

Checks each definition against the exact rules the save endpoint enforces
(workflow_definitions._validate_steps) plus the round-trip fidelity of the
admin API's step schema (schemas.workflow.StepInput / StepOut).

Run:  python -m scripts.audit_workflow_admin_ui
"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models.workflow import WorkflowDefinition, WorkflowInstance
from app.core.db import AsyncSessionLocal
from app.routers.workflow_definitions import _validate_steps
from app.schemas.workflow import StepInput, StepOut

# Columns that exist on WorkflowStep but are absent from the admin API's
# step schema. Anything set here is dropped the first time an editor saves.
ROUND_TRIP_BLIND_SPOTS = ["parallelStrategy", "slaBySeverity"]


async def main() -> None:
    async with AsyncSessionLocal() as db:
        definitions = (
            await db.execute(
                select(WorkflowDefinition)
                .options(selectinload(WorkflowDefinition.steps))
                .order_by(WorkflowDefinition.module, WorkflowDefinition.recordType)
            )
        ).scalars().all()

        schema_fields = set(StepInput.model_fields) | set(StepOut.model_fields)
        missing_from_schema = [c for c in ROUND_TRIP_BLIND_SPOTS if c not in schema_fields]

        print(f"{len(definitions)} workflow definitions\n")
        print(f"{'module/recordType':<34}{'steps':>6}  {'runs':>5}  save-validation")
        print("-" * 88)

        blocked: list[tuple[str, str]] = []
        at_risk: list[tuple[str, str]] = []

        for d in definitions:
            steps = sorted(d.steps, key=lambda s: s.sequence)
            payload = [
                {
                    "sequence": s.sequence,
                    "stepType": s.stepType.value if hasattr(s.stepType, "value") else s.stepType,
                    "name": s.name,
                }
                for s in steps
            ]
            err = _validate_steps(payload)
            runs = (
                await db.execute(
                    select(func.count())
                    .select_from(WorkflowInstance)
                    .where(WorkflowInstance.definitionId == d.id)
                )
            ).scalar_one()

            label = f"{d.module}/{d.recordType or '*'}"
            verdict = "OK" if err is None else f"BLOCKED — {err}"
            print(f"{label:<34}{len(steps):>6}  {runs:>5}  {verdict}")
            if err is not None:
                blocked.append((label, err))

            # Would saving this definition from the UI silently lose data?
            for col in missing_from_schema:
                carriers = [s.name for s in steps if getattr(s, col, None) not in (None, {}, "")]
                if carriers:
                    at_risk.append((label, f"{col} set on: {', '.join(carriers)}"))

        print("\n" + "=" * 88)
        print("1. Definitions that cannot be saved from the editor as they stand")
        if blocked:
            for label, err in blocked:
                print(f"   ✗ {label}: {err}")
        else:
            print("   none — every definition passes _validate_steps")

        print("\n2. Step columns the admin API cannot round-trip")
        if missing_from_schema:
            print(f"   ! absent from StepInput/StepOut: {', '.join(missing_from_schema)}")
            print("     update_definition() deletes and re-inserts every step, so these")
            print("     columns reset to NULL on the first save from the editor.")
            if at_risk:
                print("     Definitions currently carrying values that WOULD be lost:")
                for label, detail in at_risk:
                    print(f"       ✗ {label}: {detail}")
            else:
                print("     No definition currently sets either column — nothing is lost")
                print("     today, but any future use of them would be silently dropped.")
        else:
            print("   none")


if __name__ == "__main__":
    asyncio.run(main())
