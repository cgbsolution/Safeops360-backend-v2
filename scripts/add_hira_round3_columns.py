"""HIRA Round 3 — additive DDL for the outstanding-gap build.

Adds (all nullable or defaulted, so existing rows stay valid and every
statement is safe to re-run):

  1. HiraEntryHazard.regulationRef / .regulationSection
     Hazard-row-grain regulatory citation. Distinct from the entry-level
     HiraEntryRegulationRef list — auditors expect the citation against the
     hazard, not just the activity.

  2. HiraEntryRecommendedControl.evidenceAttached / .documentReference
     Mirrors HiraEntryControl exactly (same types, ungated) so a delivered
     recommendation can carry its proof-of-implementation reference.

  3. HiraHazard.requiresPermit / .permitTypes
     Library-level "this hazard needs a permit" indicator that drives the
     Create-PTW prompt on an entry's hazard row.

  4. Permit.hiraEntryId / .hiraEntryHazardId
     First FK from a permit back to the HIRA row that prompted it. Both
     nullable — permits raised outside HIRA carry NULL.

Run:  python scripts/add_hira_round3_columns.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text  # noqa: E402

from app.core.db import engine  # noqa: E402

STATEMENTS: list[tuple[str, str]] = [
    (
        "HiraEntryHazard.regulationRef",
        'ALTER TABLE "HiraEntryHazard" ADD COLUMN IF NOT EXISTS "regulationRef" VARCHAR(200)',
    ),
    (
        "HiraEntryHazard.regulationSection",
        'ALTER TABLE "HiraEntryHazard" ADD COLUMN IF NOT EXISTS "regulationSection" VARCHAR(120)',
    ),
    (
        "HiraEntryRecommendedControl.evidenceAttached",
        'ALTER TABLE "HiraEntryRecommendedControl" '
        'ADD COLUMN IF NOT EXISTS "evidenceAttached" BOOLEAN NOT NULL DEFAULT false',
    ),
    (
        "HiraEntryRecommendedControl.documentReference",
        'ALTER TABLE "HiraEntryRecommendedControl" '
        'ADD COLUMN IF NOT EXISTS "documentReference" VARCHAR(500)',
    ),
    (
        "HiraHazard.requiresPermit",
        'ALTER TABLE "HiraHazard" ADD COLUMN IF NOT EXISTS "requiresPermit" BOOLEAN NOT NULL DEFAULT false',
    ),
    (
        "HiraHazard.permitTypes",
        'ALTER TABLE "HiraHazard" ADD COLUMN IF NOT EXISTS "permitTypes" JSONB',
    ),
    (
        "Permit.hiraEntryId",
        'ALTER TABLE "Permit" ADD COLUMN IF NOT EXISTS "hiraEntryId" TEXT',
    ),
    (
        "Permit.hiraEntryHazardId",
        'ALTER TABLE "Permit" ADD COLUMN IF NOT EXISTS "hiraEntryHazardId" TEXT',
    ),
    # FKs added separately so a re-run doesn't fail on an existing constraint.
    (
        "Permit.hiraEntryId FK",
        """DO $$ BEGIN
             ALTER TABLE "Permit" ADD CONSTRAINT "Permit_hiraEntryId_fkey"
               FOREIGN KEY ("hiraEntryId") REFERENCES "HiraEntry"(id) ON DELETE SET NULL;
           EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    ),
    (
        "Permit.hiraEntryHazardId FK",
        """DO $$ BEGIN
             ALTER TABLE "Permit" ADD CONSTRAINT "Permit_hiraEntryHazardId_fkey"
               FOREIGN KEY ("hiraEntryHazardId") REFERENCES "HiraEntryHazard"(id) ON DELETE SET NULL;
           EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    ),
    (
        "Permit.hiraEntryId index",
        'CREATE INDEX IF NOT EXISTS "ix_Permit_hiraEntryId" ON "Permit" ("hiraEntryId")',
    ),
]

VERIFY = """
select table_name, column_name, data_type, is_nullable, column_default
from information_schema.columns
where (table_name = 'HiraEntryHazard' and column_name in ('regulationRef','regulationSection'))
   or (table_name = 'HiraEntryRecommendedControl' and column_name in ('evidenceAttached','documentReference'))
   or (table_name = 'HiraHazard' and column_name in ('requiresPermit','permitTypes'))
   or (table_name = 'Permit' and column_name in ('hiraEntryId','hiraEntryHazardId'))
order by table_name, column_name
"""


async def main() -> None:
    async with engine.begin() as conn:
        for label, sql in STATEMENTS:
            await conn.execute(text(sql))
            print(f"  ok  {label}")

    # Verify in a FRESH connection — proving the DDL is visible outside the
    # transaction that wrote it, not just inside it.
    async with engine.connect() as conn:
        print("\nVerification (fresh connection):")
        rows = (await conn.execute(text(VERIFY))).fetchall()
        for r in rows:
            print("  ", tuple(r))
        print(f"\n{len(rows)} of 8 expected columns present.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
