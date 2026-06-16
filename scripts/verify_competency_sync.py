"""Verify the training->competency receiver against seeded data.

Reads the competency-record state distribution for a plant, runs
sync_plant_from_training, and prints the before/after so we can see training
evidence flowing into the Skill Matrix.

    .venv/Scripts/python.exe scripts/verify_competency_sync.py [PLANT_ID]
"""

from __future__ import annotations

import asyncio
import sys

# asyncpg wants the selector loop on Windows.
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import func, select  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.models.competency_matrix import Competency, CompetencyRecord  # noqa: E402
from app.services.competency_state import sync_plant_from_training  # noqa: E402

PLANT_ID = sys.argv[1] if len(sys.argv) > 1 else "cmpwi9h8200098nbk3nxzcdjf"


async def state_counts(db, plant_id) -> dict[str, int]:
    rows = (
        await db.execute(
            select(CompetencyRecord.state, func.count())
            .where(CompetencyRecord.plantId == plant_id)
            .group_by(CompetencyRecord.state)
        )
    ).all()
    return {s: n for s, n in rows}


async def main() -> int:
    async with AsyncSessionLocal() as db:
        # How many competencies tracked in this plant are actually training-fed?
        recs = (
            await db.execute(
                select(CompetencyRecord.competencyId)
                .where(CompetencyRecord.plantId == PLANT_ID)
                .distinct()
            )
        ).scalars().all()
        comps = (
            await db.execute(select(Competency).where(Competency.id.in_(recs)))
        ).scalars().all() if recs else []
        training_fed = [c for c in comps if (c.relatedTrainingProgramIds or [])]
        print(f"Plant {PLANT_ID}")
        print(f"  competencies in matrix: {len(comps)}  training-fed (have related programs): {len(training_fed)}")

        before = await state_counts(db, PLANT_ID)
        print(f"\nBEFORE: {before}")

        stats = await sync_plant_from_training(db, plant_id=PLANT_ID, actor_user_id="verify-script")
        print(f"\nsync_plant_from_training -> {stats}")

        after = await state_counts(db, PLANT_ID)
        print(f"AFTER : {after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
