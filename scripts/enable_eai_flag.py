"""One-off: enable the EAI Environmental Register feature flag for a plant.

Run from the backend root:
    .venv/Scripts/python.exe scripts/enable_eai_flag.py [PLANT_ID]

Uses the SYNC (session-pooler :5432) connection via psycopg2 to avoid
Windows asyncio/asyncpg event-loop friction. Prints before/after state so
the change is auditable and trivially reversible (set the flag back to
False to disable again).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.eai import EaiFeatureFlag
from app.models.plant import Plant

# Plant shown on the EAI page (LMS — Lumshnong Integrated Unit).
DEFAULT_PLANT_ID = "cmpwi9h820009Bnbk3nxzcdjf"


def main() -> int:
    plant_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PLANT_ID
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, future=True)

    with Session(engine) as s:
        plant = s.get(Plant, plant_id)
        if plant is None:
            # Fall back to a name match in case the screenshot ID was off.
            plant = (
                s.execute(select(Plant).where(Plant.name.ilike("%Lumshnong%")))
                .scalars()
                .first()
            )
        if plant is None:
            print(f"!! No plant found for id={plant_id!r} or name ~ 'Lumshnong'.")
            print("   Existing plants:")
            for p in s.execute(select(Plant)).scalars().all():
                print(f"     {p.id}  {p.code}  {p.name}")
            return 1

        print(f"Plant : {plant.id}  {plant.code}  {plant.name}")

        flag = (
            s.execute(select(EaiFeatureFlag).where(EaiFeatureFlag.plantId == plant.id))
            .scalar_one_or_none()
        )
        print(
            "Before: "
            + (
                "no flag row (disabled by default)"
                if flag is None
                else f"eaiRegisterEnabled={flag.eaiRegisterEnabled}"
            )
        )

        if flag is None:
            flag = EaiFeatureFlag(
                plantId=plant.id,
                eaiRegisterEnabled=True,
                enabledAt=datetime.now(timezone.utc),
            )
            s.add(flag)
        else:
            flag.eaiRegisterEnabled = True
            if flag.enabledAt is None:
                flag.enabledAt = datetime.now(timezone.utc)

        s.commit()
        s.refresh(flag)
        print(
            f"After : eaiRegisterEnabled={flag.eaiRegisterEnabled}  "
            f"enabledAt={flag.enabledAt}  (row id={flag.id})"
        )
        print("Done. Reload the EAI page for this plant.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
