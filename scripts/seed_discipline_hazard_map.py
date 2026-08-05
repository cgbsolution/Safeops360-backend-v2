"""Seed DisciplineHazardMap - the join that makes the cross-module signal work.

docs/cams/08 §5.1, option 2.

**Why this table exists.** The risk-based frequency recommendation's best input
is "incidents and near-misses raise the audit frequency for the discipline they
occurred in". That is the differentiator versus Gensuite/Enablon, which schedule
off a static risk rating. But `Incident` carries a plant, an area and its own
taxonomy, while `AuditCheckpointResponse.categoryId` is the audit-discipline
taxonomy - and **nothing joined them**. This is that mapping, hand-authored,
small, tenant-overridable and visible in admin, because the map IS the rationale
and has to be inspectable rather than buried in code.

**An honest limitation, stated rather than papered over.** `IncidentType` is
mostly an OUTCOME-SEVERITY taxonomy - FIRST_AID, MTC, RWC, LTI, FATALITY describe
how badly someone was hurt, not what kind of hazard was involved. Only four of
its ten values carry any discipline signal:

    FIRE - ENVIRONMENTAL - PROCESS_SAFETY - PROPERTY_DAMAGE

So this seed maps those four and leaves the severity values unmapped. The
recommendation engine reports the incident signal as partially informed rather
than pretending an LTI tells it which discipline to audit. Getting a richer
signal needs a hazard-category field on Incident - a platform change, not a
CAMS one, and out of scope here.

Idempotent - re-running updates weights rather than duplicating rows.

    .venv/Scripts/python.exe scripts/seed_discipline_hazard_map.py            # dry run
    .venv/Scripts/python.exe scripts/seed_discipline_hazard_map.py --commit

WARNING: The backend .env points at PRODUCTION.
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.programme import DisciplineHazardMap

# (disciplineCode, hazardCategory, weight)
#
# Weight is 0.0–1.0: a partial association contributes proportionally rather
# than forcing a binary decision on an ambiguous pairing. A fire incident is
# strong evidence for auditing Fire & Life Safety (1.0) and weaker but real
# evidence for Electrical Safety (0.5), because electrical faults are a common
# ignition source.
MAPPINGS: list[tuple[str, str, float]] = [
    # -- GARMENTS_TEXTILE library -------------------------------------
    ("FIRE-LIFE-SAFETY", "FIRE", 1.0),
    ("FIRE-LIFE-SAFETY", "PROPERTY_DAMAGE", 0.5),
    ("ELECTRICAL-SAFETY", "FIRE", 0.5),
    ("ELECTRICAL-SAFETY", "PROPERTY_DAMAGE", 0.4),
    ("CHEMICAL-HAZMAT", "ENVIRONMENTAL", 0.8),
    ("CHEMICAL-HAZMAT", "PROCESS_SAFETY", 0.7),
    ("ENVIRONMENTAL-COMPLIANCE", "ENVIRONMENTAL", 1.0),
    ("MACHINE-SAFETY", "PROCESS_SAFETY", 0.8),
    ("MACHINE-SAFETY", "PROPERTY_DAMAGE", 0.4),
    # HIPO near-misses are the strongest leading indicator the platform holds,
    # and the incident-reporting discipline is exactly what they test.
    ("INCIDENT-NEAR-MISS", "HIPO_NEAR_MISS", 1.0),
    ("PPE-COMPLIANCE", "PROCESS_SAFETY", 0.3),
    ("HOUSEKEEPING-ERGONOMICS", "PROPERTY_DAMAGE", 0.3),
    # -- Manufacturing library ----------------------------------------
    ("FIRE-ELEC", "FIRE", 1.0),
    ("FIRE-ELEC", "PROPERTY_DAMAGE", 0.5),
    ("MACHINE-GUARDING", "PROCESS_SAFETY", 0.8),
    ("ENV-COMPLIANCE-M", "ENVIRONMENTAL", 1.0),
    ("WORK-AT-HEIGHT", "PROCESS_SAFETY", 0.6),
]

# Recorded so the admin screen can explain the gap rather than implying the
# unmapped values were an oversight.
UNMAPPED_BY_DESIGN = ["FIRST_AID", "MTC", "RWC", "LTI", "FATALITY"]


def main(commit: bool) -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    added = updated = 0
    with Session(engine) as s:
        for disc, hazard, weight in MAPPINGS:
            row = s.execute(
                select(DisciplineHazardMap).where(
                    DisciplineHazardMap.plantId.is_(None),
                    DisciplineHazardMap.disciplineCode == disc,
                    DisciplineHazardMap.hazardCategory == hazard,
                    DisciplineHazardMap.sourceModule == "INCIDENT",
                )
            ).scalars().first()
            if row is not None:
                if row.weight != weight or not row.isActive:
                    print(f"  ~ {disc:<28} {hazard:<18} {row.weight} -> {weight}")
                    updated += 1
                    if commit:
                        row.weight = weight
                        row.isActive = True
                continue
            print(f"  + {disc:<28} {hazard:<18} {weight}")
            added += 1
            if commit:
                s.add(
                    DisciplineHazardMap(
                        plantId=None,  # estate-wide default; tenants may override per site
                        disciplineCode=disc,
                        hazardCategory=hazard,
                        sourceModule="INCIDENT",
                        weight=weight,
                        createdBy="seed_discipline_hazard_map",
                    )
                )
        if commit:
            s.commit()

    print(f"\n  {added} new - {updated} updated - {len(MAPPINGS)} total mappings")
    print(f"\n  Unmapped by design (outcome-severity, not hazard): {', '.join(UNMAPPED_BY_DESIGN)}")
    print("  The recommendation engine treats these as carrying no discipline signal.")
    print("\nCOMMITTED." if commit else "\nDRY RUN - nothing written. Re-run with --commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--commit" in sys.argv))
