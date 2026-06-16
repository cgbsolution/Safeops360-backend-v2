"""Seed workflow definitions. Idempotent.

Mirrors prisma/seed-workflows.ts. Run as:
  python -m app.seed.seed_workflows
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.workflow import StepType, WorkflowDefinition, WorkflowStep

# (module, recordType, name, description, steps[])
DEFINITIONS: list[dict[str, Any]] = [
    {
        "module": "OBSERVATION",
        "recordType": None,
        "name": "Safety Observation — default",
        "description": "Maker → Section Head approves → Action Owner executes → Safety Officer verifies → HSE Manager closes",
        "steps": [
            {"sequence": 1, "stepType": "MAKER", "name": "Submitted by Observer"},
            {"sequence": 2, "stepType": "CHECKER", "name": "Section Head Review", "approverRole": "DEPARTMENT_HEAD", "slaHours": 24},
            {"sequence": 3, "stepType": "ASSIGNEE_TASK", "name": "Action Owner Executes", "approverField": "ACTION_OWNER", "slaHours": 168, "escalationRole": "HSE_MANAGER"},
            {"sequence": 4, "stepType": "VERIFIER", "name": "Safety Officer Verifies", "approverRole": "SAFETY_OFFICER", "slaHours": 48},
            {"sequence": 5, "stepType": "CLOSURE", "name": "HSE Manager Closes", "approverRole": "HSE_MANAGER"},
        ],
    },
    {
        "module": "NEAR_MISS",
        "recordType": None,
        "name": "Near Miss — default",
        "description": "Maker → HSE Manager review → Action Owner executes CAPA → Safety Officer verifies → Closure",
        "steps": [
            {"sequence": 1, "stepType": "MAKER", "name": "Reported"},
            {"sequence": 2, "stepType": "CHECKER", "name": "HSE Review (CAPA)", "approverRole": "HSE_MANAGER", "slaHours": 24},
            {"sequence": 3, "stepType": "ASSIGNEE_TASK", "name": "CAPA Execution", "approverRole": "HSE_MANAGER", "slaHours": 336},
            {"sequence": 4, "stepType": "VERIFIER", "name": "Safety Officer Verifies", "approverRole": "SAFETY_OFFICER", "slaHours": 72},
            {"sequence": 5, "stepType": "CLOSURE", "name": "Closure", "approverRole": "HSE_MANAGER"},
        ],
    },
    {
        "module": "PTW",
        "recordType": "GENERAL_COLD",
        "name": "PTW — Cold Work",
        "description": "Issuer → Safety Officer → Receiver acknowledges + FLRA → Closure",
        "steps": [
            {"sequence": 1, "stepType": "MAKER", "name": "Submitted"},
            {"sequence": 2, "stepType": "CHECKER", "name": "Issuer Approval", "approverField": "ISSUER", "slaHours": 4},
            {"sequence": 3, "stepType": "CHECKER", "name": "Safety Officer Approval", "approverRole": "SAFETY_OFFICER", "slaHours": 4},
            {"sequence": 4, "stepType": "ASSIGNEE_TASK", "name": "Receiver Acknowledges + FLRA", "approverField": "RECEIVER", "slaHours": 8},
            {"sequence": 5, "stepType": "CLOSURE", "name": "Closure", "approverRole": "HSE_MANAGER"},
        ],
    },
    {
        "module": "PTW",
        "recordType": "HOT_WORK",
        "name": "PTW — Hot Work (high-risk)",
        "description": "Issuer → Safety Officer → Plant Head → Receiver + FLRA → Closure",
        "steps": [
            {"sequence": 1, "stepType": "MAKER", "name": "Submitted"},
            {"sequence": 2, "stepType": "CHECKER", "name": "Issuer Approval", "approverField": "ISSUER", "slaHours": 4},
            {"sequence": 3, "stepType": "CHECKER", "name": "Safety Officer Approval", "approverRole": "SAFETY_OFFICER", "slaHours": 4},
            {"sequence": 4, "stepType": "CHECKER", "name": "Plant Head Approval", "approverRole": "PLANT_HEAD", "slaHours": 4},
            {"sequence": 5, "stepType": "ASSIGNEE_TASK", "name": "Receiver Acknowledges + FLRA", "approverField": "RECEIVER", "slaHours": 4},
            {"sequence": 6, "stepType": "CLOSURE", "name": "Closure", "approverRole": "HSE_MANAGER"},
        ],
    },
    {
        "module": "PTW",
        "recordType": "CONFINED_SPACE",
        "name": "PTW — Confined Space",
        "description": "Issuer → Safety Officer → Plant Head → Receiver + FLRA + Gas Test → Closure",
        "steps": [
            {"sequence": 1, "stepType": "MAKER", "name": "Submitted"},
            {"sequence": 2, "stepType": "CHECKER", "name": "Issuer Approval", "approverField": "ISSUER", "slaHours": 4},
            {"sequence": 3, "stepType": "CHECKER", "name": "Safety Officer Approval", "approverRole": "SAFETY_OFFICER", "slaHours": 4},
            {"sequence": 4, "stepType": "CHECKER", "name": "Plant Head Approval", "approverRole": "PLANT_HEAD", "slaHours": 4},
            {"sequence": 5, "stepType": "ASSIGNEE_TASK", "name": "Receiver Acknowledges + FLRA + Gas Test", "approverField": "RECEIVER", "slaHours": 4},
            {"sequence": 6, "stepType": "CLOSURE", "name": "Closure", "approverRole": "HSE_MANAGER"},
        ],
    },
    {
        "module": "PTW",
        "recordType": "WORK_AT_HEIGHT",
        "name": "PTW — Work at Height",
        "description": "Issuer → Safety Officer → Plant Head → Receiver + FLRA → Closure",
        "steps": [
            {"sequence": 1, "stepType": "MAKER", "name": "Submitted"},
            {"sequence": 2, "stepType": "CHECKER", "name": "Issuer Approval", "approverField": "ISSUER", "slaHours": 4},
            {"sequence": 3, "stepType": "CHECKER", "name": "Safety Officer Approval", "approverRole": "SAFETY_OFFICER", "slaHours": 4},
            {"sequence": 4, "stepType": "CHECKER", "name": "Plant Head Approval", "approverRole": "PLANT_HEAD", "slaHours": 4},
            {"sequence": 5, "stepType": "ASSIGNEE_TASK", "name": "Receiver Acknowledges + FLRA", "approverField": "RECEIVER", "slaHours": 4},
            {"sequence": 6, "stepType": "CLOSURE", "name": "Closure", "approverRole": "HSE_MANAGER"},
        ],
    },
    {
        "module": "PTW",
        "recordType": "EXCAVATION",
        "name": "PTW — Excavation",
        "description": "Issuer → Safety Officer → Plant Head → Receiver + FLRA → Closure",
        "steps": [
            {"sequence": 1, "stepType": "MAKER", "name": "Submitted"},
            {"sequence": 2, "stepType": "CHECKER", "name": "Issuer Approval", "approverField": "ISSUER", "slaHours": 4},
            {"sequence": 3, "stepType": "CHECKER", "name": "Safety Officer Approval", "approverRole": "SAFETY_OFFICER", "slaHours": 4},
            {"sequence": 4, "stepType": "CHECKER", "name": "Plant Head Approval", "approverRole": "PLANT_HEAD", "slaHours": 4},
            {"sequence": 5, "stepType": "ASSIGNEE_TASK", "name": "Receiver Acknowledges + FLRA", "approverField": "RECEIVER", "slaHours": 4},
            {"sequence": 6, "stepType": "CLOSURE", "name": "Closure", "approverRole": "HSE_MANAGER"},
        ],
    },
    {
        "module": "PTW",
        "recordType": "ELECTRICAL_LOTO",
        "name": "PTW — Electrical / LOTO",
        "description": "Issuer → Safety Officer → Plant Head → Receiver + FLRA → Closure",
        "steps": [
            {"sequence": 1, "stepType": "MAKER", "name": "Submitted"},
            {"sequence": 2, "stepType": "CHECKER", "name": "Issuer Approval", "approverField": "ISSUER", "slaHours": 4},
            {"sequence": 3, "stepType": "CHECKER", "name": "Safety Officer Approval", "approverRole": "SAFETY_OFFICER", "slaHours": 4},
            {"sequence": 4, "stepType": "CHECKER", "name": "Plant Head Approval", "approverRole": "PLANT_HEAD", "slaHours": 4},
            {"sequence": 5, "stepType": "ASSIGNEE_TASK", "name": "Receiver Acknowledges + FLRA", "approverField": "RECEIVER", "slaHours": 4},
            {"sequence": 6, "stepType": "CLOSURE", "name": "Closure", "approverRole": "HSE_MANAGER"},
        ],
    },
    {
        "module": "INCIDENT",
        "recordType": None,
        "name": "Incident Investigation",
        "description": "8-step investigation flow: Reported → HSE classify → Lead investigates → RCA → CAPA → Verify → Plant Head → Closed",
        "steps": [
            {"sequence": 1, "stepType": "MAKER", "name": "Reported"},
            {"sequence": 2, "stepType": "CHECKER", "name": "HSE Classification", "approverRole": "HSE_MANAGER", "slaHours": 24},
            {"sequence": 3, "stepType": "ASSIGNEE_TASK", "name": "Investigation Lead", "approverRole": "HSE_MANAGER", "slaHours": 168},
            {"sequence": 4, "stepType": "CHECKER", "name": "RCA Review", "approverRole": "HSE_MANAGER", "slaHours": 72},
            {"sequence": 5, "stepType": "ASSIGNEE_TASK", "name": "CAPA Execution", "approverRole": "HSE_MANAGER", "slaHours": 720},
            {"sequence": 6, "stepType": "VERIFIER", "name": "Safety Officer Verifies", "approverRole": "SAFETY_OFFICER", "slaHours": 72},
            {"sequence": 7, "stepType": "CHECKER", "name": "Plant Head Sign-off", "approverRole": "PLANT_HEAD", "slaHours": 72},
            {"sequence": 8, "stepType": "CLOSURE", "name": "Closure", "approverRole": "HSE_MANAGER"},
        ],
    },
    {
        "module": "TRAINING",
        "recordType": None,
        "name": "Training Record",
        "description": "Scheduled → Trainer conducts → Assessment → Certificate → Closed",
        "steps": [
            {"sequence": 1, "stepType": "MAKER", "name": "Scheduled"},
            {"sequence": 2, "stepType": "ASSIGNEE_TASK", "name": "Conduct Training", "approverField": "TRAINER", "slaHours": 168},
            {"sequence": 3, "stepType": "CHECKER", "name": "Assessment Review", "approverRole": "HSE_MANAGER", "slaHours": 24},
            {"sequence": 4, "stepType": "ASSIGNEE_TASK", "name": "Certificate Issuance", "approverRole": "HSE_MANAGER", "slaHours": 24},
            {"sequence": 5, "stepType": "CLOSURE", "name": "Closure", "approverRole": "HSE_MANAGER"},
        ],
    },
    {
        "module": "INSPECTION",
        "recordType": None,
        "name": "Inspection",
        "description": "Scheduled → Inspector conducts → HSE verifies → Closed",
        "steps": [
            {"sequence": 1, "stepType": "MAKER", "name": "Scheduled"},
            {"sequence": 2, "stepType": "ASSIGNEE_TASK", "name": "Conduct Inspection", "approverField": "ASSIGNED_INSPECTOR", "slaHours": 168},
            {"sequence": 3, "stepType": "VERIFIER", "name": "HSE Review", "approverRole": "HSE_MANAGER", "slaHours": 48},
            {"sequence": 4, "stepType": "CLOSURE", "name": "Closure", "approverRole": "HSE_MANAGER"},
        ],
    },
    {
        "module": "MANHOURS",
        "recordType": None,
        "name": "Manhours Submission",
        "description": "Submitted → Plant Head approves → Closed",
        "steps": [
            {"sequence": 1, "stepType": "MAKER", "name": "Submitted"},
            {"sequence": 2, "stepType": "CHECKER", "name": "Plant Head Approval", "approverRole": "PLANT_HEAD", "slaHours": 168},
            {"sequence": 3, "stepType": "CLOSURE", "name": "Closure", "approverRole": "HSE_MANAGER"},
        ],
    },
]


async def upsert_definition(db: AsyncSession, d: dict[str, Any]) -> str:
    stmt = select(WorkflowDefinition).where(
        WorkflowDefinition.module == d["module"],
        (WorkflowDefinition.recordType == d["recordType"]) if d["recordType"] is not None else WorkflowDefinition.recordType.is_(None),
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        existing.name = d["name"]
        existing.description = d["description"]
        existing.isActive = True
        # Drop old steps
        for s in list(existing.steps):
            await db.delete(s)
        await db.flush()
        definition_id = existing.id
    else:
        new = WorkflowDefinition(
            module=d["module"],
            recordType=d["recordType"],
            name=d["name"],
            description=d["description"],
            isActive=True,
        )
        db.add(new)
        await db.flush()
        definition_id = new.id

    for s in d["steps"]:
        db.add(
            WorkflowStep(
                definitionId=definition_id,
                sequence=s["sequence"],
                stepType=StepType(s["stepType"]),
                name=s["name"],
                approverRole=s.get("approverRole"),
                approverField=s.get("approverField"),
                approverUserId=s.get("approverUserId"),
                approverGroupRoles=s.get("approverGroupRoles"),
                slaHours=s.get("slaHours"),
                escalationRole=s.get("escalationRole"),
            )
        )
    return definition_id


async def main() -> None:
    print("🔁  Seeding workflow definitions (idempotent)…")
    async with AsyncSessionLocal() as db:
        # Reload existing rows with their steps so we can delete them safely
        existing_defs = (
            await db.execute(
                select(WorkflowDefinition).options(
                    __import__("sqlalchemy.orm", fromlist=["selectinload"]).selectinload(WorkflowDefinition.steps)
                )
            )
        ).scalars().all()
        # Build a lookup so upsert can find them
        for d in DEFINITIONS:
            await upsert_definition(db, d)
            print(f"   + {d['module']}/{d['recordType'] or '*'} — {len(d['steps'])} steps")
        await db.commit()
    print("✅  Workflow definitions seeded.")


if __name__ == "__main__":
    asyncio.run(main())
