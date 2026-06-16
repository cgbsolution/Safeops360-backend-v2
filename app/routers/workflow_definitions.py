"""Workflow definition admin router. Mirror of /api/workflow/definitions/*.

Gated on CONFIGURATION.WORKFLOWS — only ADMIN / SYSTEM_ADMIN / CORPORATE_HSE
hold this in the default matrix. The visual workflow editor in the React app
hits these endpoints to create, edit, version, restore, toggle, and test-run
workflow definitions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import require_permission
from app.models.user import User
from app.models.workflow import (
    StepType,
    WorkflowDefinition,
    WorkflowDefinitionVersion,
    WorkflowInstance,
    WorkflowStep,
)
from app.schemas.workflow import (
    DefinitionCreate,
    DefinitionOut,
    DefinitionUpdate,
)

router = APIRouter(
    prefix="/api/workflow/definitions",
    tags=["workflow-admin"],
    dependencies=[Depends(require_permission("CONFIGURATION.WORKFLOWS"))],
)

VALID_STEP_TYPES = {"MAKER", "CHECKER", "ASSIGNEE_TASK", "VERIFIER", "CLOSURE"}


def _validate_steps(steps: list[dict[str, Any]]) -> str | None:
    if not steps:
        return "Workflow must have at least one step."
    for i, s in enumerate(steps):
        if not (s.get("name") or "").strip():
            return f"Step {i + 1} is missing a name."
        if s.get("stepType") not in VALID_STEP_TYPES:
            return f"Step {i + 1} has an unknown type: {s.get('stepType')}."
    if sum(1 for s in steps if s["stepType"] == "MAKER") != 1:
        return "Workflow must have exactly one Maker step."
    if steps[0]["stepType"] != "MAKER":
        return "The first step must be the Maker."
    if sum(1 for s in steps if s["stepType"] == "CLOSURE") != 1:
        return "Workflow must have exactly one Closure step."
    if steps[-1]["stepType"] != "CLOSURE":
        return "The last step must be the Closure."
    middle = any(s["stepType"] in {"CHECKER", "ASSIGNEE_TASK"} for s in steps)
    if not middle:
        return "Workflow must have at least one Checker or Assignee step between Maker and Closure."
    return None


@router.get("")
async def list_definitions(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(WorkflowDefinition)
            .options(selectinload(WorkflowDefinition.steps))
            .order_by(WorkflowDefinition.module, WorkflowDefinition.recordType, WorkflowDefinition.name)
        )
    ).scalars().all()
    return {"definitions": [DefinitionOut.model_validate(d) for d in rows]}


@router.post("")
async def create_definition(
    payload: DefinitionCreate,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not payload.module or not payload.name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "module and name are required")
    definition = WorkflowDefinition(
        module=payload.module,
        recordType=payload.recordType,
        name=payload.name,
        description=payload.description,
        isActive=payload.isActive,
    )
    db.add(definition)
    await db.flush()
    # Skeleton: Maker → Checker(HSE) → Closure(HSE)
    db.add_all([
        WorkflowStep(definitionId=definition.id, sequence=1, stepType=StepType.MAKER, name="Submitted by Initiator"),
        WorkflowStep(
            definitionId=definition.id,
            sequence=2,
            stepType=StepType.CHECKER,
            name="Review",
            approverRole="HSE_MANAGER",
            slaHours=24,
        ),
        WorkflowStep(
            definitionId=definition.id,
            sequence=3,
            stepType=StepType.CLOSURE,
            name="Closure",
            approverRole="HSE_MANAGER",
        ),
    ])
    await db.flush()
    # Re-load with steps so the response matches DefinitionOut
    fresh = await db.get(
        WorkflowDefinition, definition.id, options=[selectinload(WorkflowDefinition.steps)]
    )
    return {"definition": DefinitionOut.model_validate(fresh)}


@router.get("/{definition_id}")
async def get_definition(
    definition_id: str, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    definition = await db.get(
        WorkflowDefinition, definition_id, options=[selectinload(WorkflowDefinition.steps)]
    )
    if definition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return {"definition": DefinitionOut.model_validate(definition)}


@router.put("/{definition_id}")
async def update_definition(
    definition_id: str,
    payload: DefinitionUpdate,
    user: User = Depends(require_permission("CONFIGURATION.WORKFLOWS")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    definition = await db.get(
        WorkflowDefinition, definition_id, options=[selectinload(WorkflowDefinition.steps)]
    )
    if definition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    if payload.steps is not None:
        ordered = sorted(
            [s.model_dump() for s in payload.steps], key=lambda s: s.get("sequence", 0)
        )
        for i, s in enumerate(ordered):
            s["sequence"] = i + 1
        err = _validate_steps(ordered)
        if err:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, err)

        # Snapshot the current definition before mutation — versioning audit trail
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
                for st in sorted(definition.steps, key=lambda x: x.sequence)
            ],
        }
        last_version = (
            await db.execute(
                select(func.max(WorkflowDefinitionVersion.version)).where(
                    WorkflowDefinitionVersion.definitionId == definition.id
                )
            )
        ).scalar_one() or 0
        db.add(
            WorkflowDefinitionVersion(
                definitionId=definition.id,
                version=last_version + 1,
                snapshot=json.dumps(snapshot),
                editedById=user.id,
                changeNote=payload.changeNote,
            )
        )

        # Replace steps
        for st in list(definition.steps):
            await db.delete(st)
        await db.flush()
        for s in ordered:
            db.add(
                WorkflowStep(
                    definitionId=definition.id,
                    sequence=s["sequence"],
                    stepType=StepType(s["stepType"]),
                    name=s["name"],
                    approverRole=s.get("approverRole"),
                    approverField=s.get("approverField"),
                    approverUserId=s.get("approverUserId"),
                    approverGroupRoles=s.get("approverGroupRoles"),
                    slaHours=s.get("slaHours"),
                    slaUnit=s.get("slaUnit"),
                    escalationRole=s.get("escalationRole"),
                    isOptional=s.get("isOptional", False),
                    conditionExpr=s.get("conditionExpr"),
                    notes=s.get("notes"),
                )
            )

    if payload.name is not None:
        definition.name = payload.name
    if payload.description is not None:
        definition.description = payload.description or None
    if payload.recordType is not None:
        definition.recordType = payload.recordType or None
    if payload.isActive is not None:
        definition.isActive = payload.isActive

    await db.flush()
    fresh = await db.get(
        WorkflowDefinition, definition.id, options=[selectinload(WorkflowDefinition.steps)]
    )
    return {"definition": DefinitionOut.model_validate(fresh)}


@router.delete("/{definition_id}")
async def delete_definition(
    definition_id: str, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    in_use = (
        await db.execute(
            select(func.count()).select_from(WorkflowInstance).where(WorkflowInstance.definitionId == definition_id)
        )
    ).scalar_one()
    if in_use > 0:
        # Soft-delete to preserve in-flight instances
        definition = await db.get(WorkflowDefinition, definition_id)
        if definition is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        definition.isActive = False
        await db.flush()
        return {"ok": True, "softDeleted": True, "instanceCount": int(in_use)}
    definition = await db.get(WorkflowDefinition, definition_id)
    if definition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    await db.delete(definition)
    await db.flush()
    return {"ok": True}


@router.patch("/{definition_id}/toggle")
async def toggle_definition(
    definition_id: str,
    body: dict[str, bool] = Body(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    definition = await db.get(WorkflowDefinition, definition_id)
    if definition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    definition.isActive = bool(body.get("isActive"))
    await db.flush()
    return {"definition": {"id": definition.id, "isActive": definition.isActive}}


@router.get("/{definition_id}/versions")
async def list_versions(
    definition_id: str, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(WorkflowDefinitionVersion)
            .where(WorkflowDefinitionVersion.definitionId == definition_id)
            .order_by(WorkflowDefinitionVersion.version.desc())
        )
    ).scalars().all()
    return {
        "versions": [
            {
                "id": v.id,
                "version": v.version,
                "editedById": v.editedById,
                "changeNote": v.changeNote,
                # Frontend reads `createdAt` from this list; the DB column is
                # actually `editedAt`. Map for back-compat with the existing UI.
                "createdAt": v.editedAt,
            }
            for v in rows
        ]
    }


@router.post("/{definition_id}/restore/{version_id}")
async def restore_version(
    definition_id: str,
    version_id: str,
    user: User = Depends(require_permission("CONFIGURATION.WORKFLOWS")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    version = await db.get(WorkflowDefinitionVersion, version_id)
    if version is None or version.definitionId != definition_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")
    try:
        snapshot = json.loads(version.snapshot)
    except json.JSONDecodeError as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Snapshot is corrupted") from e

    last_version = (
        await db.execute(
            select(func.max(WorkflowDefinitionVersion.version)).where(
                WorkflowDefinitionVersion.definitionId == definition_id
            )
        )
    ).scalar_one() or 0

    definition = await db.get(
        WorkflowDefinition, definition_id, options=[selectinload(WorkflowDefinition.steps)]
    )
    if definition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Definition not found")

    definition.name = snapshot["name"]
    definition.description = snapshot.get("description")
    definition.module = snapshot["module"]
    definition.recordType = snapshot.get("recordType")
    definition.isActive = snapshot.get("isActive", True)
    for st in list(definition.steps):
        await db.delete(st)
    await db.flush()
    for s in snapshot.get("steps", []):
        db.add(
            WorkflowStep(
                definitionId=definition.id,
                sequence=s["sequence"],
                stepType=StepType(s["stepType"]),
                name=s["name"],
                approverRole=s.get("approverRole"),
                approverField=s.get("approverField"),
                approverUserId=s.get("approverUserId"),
                approverGroupRoles=s.get("approverGroupRoles"),
                slaHours=s.get("slaHours"),
                slaUnit=s.get("slaUnit"),
                escalationRole=s.get("escalationRole"),
                isOptional=s.get("isOptional", False),
                conditionExpr=s.get("conditionExpr"),
                notes=s.get("notes"),
            )
        )
    db.add(
        WorkflowDefinitionVersion(
            definitionId=definition.id,
            version=last_version + 1,
            snapshot=version.snapshot,
            editedById=user.id,
            changeNote=f"Restored from v{version.version}",
        )
    )
    await db.flush()
    return {"ok": True, "restoredFromVersion": version.version, "newVersion": last_version + 1}


@router.post("/{definition_id}/test-run")
async def test_run(
    definition_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Dry-run a workflow against a sample record without creating real tasks.
    Returns the trace of which steps would fire vs skip."""
    definition = await db.get(
        WorkflowDefinition, definition_id, options=[selectinload(WorkflowDefinition.steps)]
    )
    if definition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Definition not found")

    record_data = body.get("recordData") or {}
    from app.services.workflow_engine import _evaluate_condition  # internal helper

    trace: list[dict[str, Any]] = []
    for step in sorted(definition.steps, key=lambda s: s.sequence):
        applies = _evaluate_condition(step.conditionExpr, record_data)
        trace.append(
            {
                "sequence": step.sequence,
                "stepType": step.stepType.value,
                "name": step.name,
                "status": "AUTO" if step.stepType == StepType.MAKER or step.stepType == StepType.CLOSURE else ("EXECUTED" if applies else "SKIPPED"),
                "reason": None if applies else "Step condition not met for sample record",
                "conditionExpr": step.conditionExpr,
                "approverRole": step.approverRole,
                "approverField": step.approverField,
                "slaHours": step.slaHours,
            }
        )
    return {"trace": trace, "errors": [], "evaluatedAt": datetime.now(timezone.utc).isoformat()}
