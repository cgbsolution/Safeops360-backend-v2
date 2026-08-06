"""Seed the platform-default inspection frequency rules — Fire & Life Safety §5.1.

    python scripts/seed_fire_frequency_master.py

Idempotent: re-running updates the cadence and citation of an existing rule for
the same (region, assetType, assetSubtype, plantId) key rather than inserting a
duplicate, which the partial-unique index would reject anyway.

These are `plantId = NULL` rows — platform defaults for region IN. A tenant
tightens one by inserting a plant-scoped row; `services/fire_frequency.resolve`
picks the more specific rule with no code change. The same mechanism is how the
GCC remap lands: seed region='GCC' rows, pass region='GCC'.

**On the citations.** `regulatoryReference` records the clause a client's own
fire consultant mapped the cadence to, and the seeded values are SafeOps360's
starting position, not legal advice — NBC 2016 and IS 2190 are not reproduced
here and the applicable cadence varies by state fire-service rules and occupancy
class. They are seeded because a blank citation field never gets filled in, and a
wrong-but-visible one gets corrected on the first audit. Review before go-live.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.models.fire_safety import InspectionFrequencyMaster  # noqa: E402

REGION = "IN"

# (assetType, assetSubtype, frequency, regulatoryReference)
#
# Subtype rows exist only where the subtype genuinely changes the cadence — a CO2
# extinguisher needs the same monthly visual as a DCP one, so there is no subtype
# row for it, and adding one would be config noise that hides the rule that matters.
RULES: list[tuple[str, str | None, str, str]] = [
    # Detection & alarm
    ("PANEL",            None, "MONTHLY",   "IS 2189:2008 cl.9 — monthly panel functional check"),
    ("DETECTOR",         None, "QUARTERLY", "IS 2189:2008 cl.9 — quarterly detector sensitivity/functional test"),
    ("PA_SYSTEM",        None, "QUARTERLY", "IS 2189:2008 — voice evacuation / PA audibility check"),

    # Portable & fixed suppression
    ("EXTINGUISHER",     None, "MONTHLY",   "IS 2190:2010 cl.7.1 — monthly visual inspection"),
    ("HYDRANT",          None, "MONTHLY",   "NBC 2016 Part 4 — wet riser / hydrant monthly flow & valve check"),
    ("HOSE_REEL",        None, "MONTHLY",   "NBC 2016 Part 4 — hose reel run-out and pressure check"),
    ("SPRINKLER_HEAD",   None, "QUARTERLY", "NBC 2016 Part 4 — sprinkler installation quarterly inspection"),
    ("FIRE_PUMP",        None, "WEEKLY",    "NBC 2016 Part 4 — weekly pump run test (no-flow / churn)"),
    ("FIRE_WATER_TANK",  None, "MONTHLY",   "NBC 2016 Part 4 — static water storage level & condition check"),

    # Life-safety ancillaries
    ("EMERGENCY_LIGHT",  None, "MONTHLY",   "NBC 2016 Part 4 — monthly emergency luminaire function test"),
    ("SMOKE_CURTAIN",    None, "HALF_YEARLY", "Manufacturer schedule — smoke curtain deployment test"),

    # Catch-all so an unclassified asset is never on the silent 30-day fallback.
    ("OTHER",            None, "QUARTERLY", "Site fire safety plan — default cadence for unclassified assets"),
]

# Annual statutory tests that run ALONGSIDE the routine cadence above. Seeded as
# subtype-scoped rows because they apply to a specific asset class, not to the
# whole type: only stored-pressure extinguisher bodies take a hydrostatic test.
SUBTYPE_RULES: list[tuple[str, str, str, str]] = [
    ("EXTINGUISHER", "CO2",   "ANNUAL", "IS 2190:2010 cl.7.3 — CO2 cylinder annual weight check"),
    ("EXTINGUISHER", "WATER", "ANNUAL", "IS 2190:2010 cl.7.3 — annual discharge & refill"),
]


async def main() -> None:
    created = updated = 0
    async with AsyncSessionLocal() as db:
        for asset_type, subtype, frequency, ref in [
            *RULES,
            *[(t, s, f, r) for t, s, f, r in SUBTYPE_RULES],
        ]:
            stmt = (
                select(InspectionFrequencyMaster)
                .where(InspectionFrequencyMaster.region == REGION)
                .where(InspectionFrequencyMaster.assetType == asset_type)
                .where(InspectionFrequencyMaster.plantId.is_(None))
            )
            stmt = (
                stmt.where(InspectionFrequencyMaster.assetSubtype.is_(None))
                if subtype is None
                else stmt.where(InspectionFrequencyMaster.assetSubtype == subtype)
            )
            row = (await db.execute(stmt)).scalars().first()
            if row is None:
                db.add(
                    InspectionFrequencyMaster(
                        plantId=None, region=REGION, assetType=asset_type, assetSubtype=subtype,
                        frequency=frequency, regulatoryReference=ref, createdBy="SEED",
                    )
                )
                created += 1
                print(f"  + {asset_type}{'/' + subtype if subtype else '':<14} {frequency:<12} {ref}")
            else:
                if row.frequency != frequency or row.regulatoryReference != ref:
                    row.frequency, row.regulatoryReference = frequency, ref
                    row.isActive, row.isDeleted = True, False
                    updated += 1
                    print(f"  ~ {asset_type}{'/' + subtype if subtype else '':<14} → {frequency}")
        await db.commit()
    print(f"\n✅  Inspection frequency master seeded — {created} created, {updated} updated (region {REGION}).")
    print("    Review the regulatory citations with the site fire consultant before go-live.")


if __name__ == "__main__":
    asyncio.run(main())
