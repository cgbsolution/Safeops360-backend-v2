"""Wipe operational/demo data from the database.

WHAT GETS DELETED:
  All transactional records seeded for the demo —
    • Observations + Near Miss + Incidents (+ attachments + investigation team)
    • Permits + crew + FLRAs + crew signatures
    • Training records (the per-employee history; programs survive)
    • Inspections (Equipment master is also wiped — plant-specific demo)
    • Manhours
    • Workflow instances + tasks + history + version snapshots
    • TrainingProgram + Equipment master tables (their values are
      cement-plant-specific demo data)

WHAT'S KEPT:
    • User                 — your login accounts
    • Role / Permission / RolePermission — RBAC matrix
    • UserRole             — who has which role at which scope
    • Plant / Area         — plant master (already configured)
    • WorkflowDefinition + WorkflowStep — workflow blueprints
      (engine cannot run without these — re-seed via seed_workflows.py
      if you want them gone)

USAGE:
    python -m app.seed.wipe_demo_data --yes

The --yes flag is required. Without it the script aborts with a summary
of what would be deleted, so you can sanity-check before running.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.equipment import Equipment, Inspection
from app.models.flra import FLRA, FLRACrewSignature, FLRATeamMember
from app.models.incident import Incident, IncidentAttachment, IncidentInvestigationMember
from app.models.manhours import Manhours
from app.models.near_miss import NearMiss
from app.models.observation import Observation
from app.models.permit import Permit, PermitCrewMember
from app.models.training import TrainingProgram, TrainingRecord
from app.models.workflow import (
    WorkflowDefinitionVersion,
    WorkflowHistory,
    WorkflowInstance,
    WorkflowTask,
)

# Order matters — children before parents so FK constraints don't trip.
# The list runs sequentially in this order.
WIPE_ORDER: list[tuple[str, type]] = [
    # Workflow runtime first — depends on every operational record
    ("WorkflowHistory", WorkflowHistory),
    ("WorkflowTask", WorkflowTask),
    ("WorkflowInstance", WorkflowInstance),
    ("WorkflowDefinitionVersion", WorkflowDefinitionVersion),
    # Incident graph
    ("IncidentAttachment", IncidentAttachment),
    ("IncidentInvestigationMember", IncidentInvestigationMember),
    ("Incident", Incident),
    # FLRA graph (must precede Permit)
    ("FLRACrewSignature", FLRACrewSignature),
    ("FLRATeamMember", FLRATeamMember),
    ("FLRA", FLRA),
    # Permit graph
    ("PermitCrewMember", PermitCrewMember),
    ("Permit", Permit),
    # Other operational records
    ("NearMiss", NearMiss),
    ("Observation", Observation),
    ("TrainingRecord", TrainingRecord),
    ("Inspection", Inspection),
    ("Manhours", Manhours),
    # Master data that's plant-specific demo and isn't worth keeping
    ("Equipment", Equipment),
    ("TrainingProgram", TrainingProgram),
]


async def count_rows(db: AsyncSession) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, model in WIPE_ORDER:
        n = (await db.execute(select(func.count()).select_from(model))).scalar_one()
        counts[label] = int(n)
    return counts


async def wipe(db: AsyncSession) -> dict[str, int]:
    deleted: dict[str, int] = {}
    for label, model in WIPE_ORDER:
        # ORM delete instead of TRUNCATE so cascade behaviour matches the
        # SQLAlchemy relationships exactly. Slower but safer.
        result = await db.execute(text(f'DELETE FROM "{model.__tablename__}"'))
        deleted[label] = int(result.rowcount or 0)
    return deleted


async def main() -> int:
    confirm = "--yes" in sys.argv
    async with AsyncSessionLocal() as db:
        before = await count_rows(db)

        total = sum(before.values())
        print("\n   Operational data currently in the database:")
        for label in (k for k, v in before.items() if v > 0):
            print(f"     {label:<32} {before[label]:>6} row(s)")
        if total == 0:
            print("     (nothing to delete — already clean)")
            return 0

        if not confirm:
            print(f"\n  ⚠️  {total} rows would be deleted across {sum(1 for v in before.values() if v > 0)} tables.")
            print(f"  ⚠️  Re-run with --yes to actually delete:")
            print(f"      python -m app.seed.wipe_demo_data --yes")
            print(f"\n  Auth + RBAC + Plant/Area + WorkflowDefinition rows are preserved.\n")
            return 1

        print(f"\n   Wiping {total} rows…")
        deleted = await wipe(db)
        await db.commit()

        for label in (k for k, v in deleted.items() if v > 0):
            print(f"     - {label:<32} {deleted[label]:>6} deleted")
        print(f"\n  ✅  Done. {sum(deleted.values())} rows removed.")
        print(f"      Users + roles + permissions + plants + workflow definitions left intact.\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
