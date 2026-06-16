"""Reproduce the GET /api/hira/entries/{id} 500 to find the failing field.

Loads the entry exactly like the router's get_entry, runs the same
HiraEntryOut.model_validate(...).model_dump() + denorm loop, and prints
the full traceback if it raises.

    .venv/Scripts/python.exe scripts/debug_hira_entry.py <entryId>
"""

from __future__ import annotations

import sys
import traceback

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.models.hira import HiraEntry, HiraEntryHazard
from app.schemas.hira import HiraEntryOut

ENTRY_ID = sys.argv[1] if len(sys.argv) > 1 else "cmpwjajto0005ja27eellq3t0"


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        stmt = (
            select(HiraEntry)
            .where(HiraEntry.id == ENTRY_ID)
            .options(
                selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
                selectinload(HiraEntry.existingControls),
                selectinload(HiraEntry.recommendedControls),
                selectinload(HiraEntry.regulationRefs),
                selectinload(HiraEntry.study),
            )
        )
        entry = s.execute(stmt).scalar_one_or_none()
        if entry is None:
            print(f"!! entry {ENTRY_ID} not found")
            # show a few real entry ids to retry with
            for row in s.execute(select(HiraEntry.id, HiraEntry.sequenceNumber).limit(5)):
                print("   ", row)
            return 1

        print(f"Loaded entry {entry.id} seq={entry.sequenceNumber}")
        print(f"  hazards={len(entry.hazards)} existing={len(entry.existingControls)} "
              f"recommended={len(entry.recommendedControls)} regs={len(entry.regulationRefs)}")

        try:
            out = HiraEntryOut.model_validate(entry).model_dump()
            for i, hz in enumerate(out["hazards"]):
                src_hz = entry.hazards[i]
                if src_hz.hazard is not None:
                    hz["hazardCode"] = src_hz.hazard.code
                    hz["hazardCategory"] = src_hz.hazard.category
                    hz["hazardName"] = src_hz.hazard.name
            HiraEntryOut(**out)
            print("OK — validated with no error (500 is elsewhere)")
            return 0
        except Exception:  # noqa: BLE001
            print("\n!! REPRODUCED the failure:\n")
            traceback.print_exc()
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
