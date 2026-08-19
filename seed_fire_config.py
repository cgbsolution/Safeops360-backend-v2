"""Seed the fire module's configuration: branded registers and inspection cadences.

    python seed_fire_config.py

Two gaps this closes, both of which showed on screen as something vaguer than a
bug and so had gone unnoticed:

1. **"No rule configured, platform fallback."** `InspectionFrequencyMaster`
   shipped with the Fire & Life Safety build and was never populated, so
   `fire_frequency.resolve()` fell through to `PLATFORM_FALLBACK_DAYS = 30` for
   every asset type and reported `source="FALLBACK", resolved=False`. Every due
   date on the register was therefore a hardcoded 30 days with no traceable rule
   behind it — which is exactly the answer a factory inspector's "why is this the
   due date?" must not get. The table did not need building; it needed rows.

   Cadences below are the NBC 2016 / IS 2190 practice the client's own sheets
   already follow, and each row carries the citation in `regulatoryReference` so
   the answer is on the record rather than in someone's memory.

2. **Branded registers were a screen, not a config.** "Register of Fire
   Extinguishers" is a controlled document (PIL/EHSD/CL/028-R1) with its own
   number, revision, column order and print layout — and so is a Register of Fire
   Alarm Panels, if a client asks. Making each one a screen is how the module
   previously ended up with two add/edit paths onto one table.
   `FireRegisterViewConfig` makes each a row: filter, columns, branding, PDF key.

Idempotent, upsert-only. Safe to re-run.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.fire_safety import FireRegisterViewConfig, InspectionFrequencyMaster
from app.services.fire_checklist_templates import (
    BEAM_DETECTOR, FIRE_ALARM_PANEL, FIRE_EXTINGUISHER, HYDRANT_SYSTEM,
)

NOW = datetime.now(timezone.utc)
REGION = "IN"


def _d(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Inspection cadences
# ═══════════════════════════════════════════════════════════════════════════
# (assetType, subtype, frequency, regulatoryReference)
#
# Region-level rows (plantId = NULL), which is the tier every plant inherits
# unless it declares something tighter. Deliberately the *statutory* cadence and
# not the client's most frequent sheet: an extinguisher carries a monthly visual
# inspection AND a five-yearly hydrostatic test, and it is the monthly one that
# governs the register's due-date badge, because that is the one that goes overdue.
FREQUENCIES: list[tuple[str, str | None, str, str]] = [
    (FIRE_EXTINGUISHER, None, "MONTHLY",
     "IS 2190:2010 cl.6 — monthly visual inspection of every extinguisher"),
    (FIRE_EXTINGUISHER, "CO2", "MONTHLY", "IS 2190:2010 cl.6"),
    (FIRE_EXTINGUISHER, "ABC", "MONTHLY", "IS 2190:2010 cl.6"),
    (FIRE_EXTINGUISHER, "DCP", "MONTHLY", "IS 2190:2010 cl.6"),
    (FIRE_EXTINGUISHER, "FOAM", "MONTHLY", "IS 2190:2010 cl.6"),

    (FIRE_ALARM_PANEL, None, "MONTHLY",
     "NBC 2016 Part 4 cl.3.4.9 / IS 2189 — monthly panel test; daily visual round per plant SOP"),
    (FIRE_ALARM_PANEL, "ZONE", "MONTHLY", "IS 2189:2008 cl.9 — zone-addressed panel"),
    (FIRE_ALARM_PANEL, "LOOP", "MONTHLY", "IS 2189:2008 cl.9 — loop-addressed panel"),
    (BEAM_DETECTOR, None, "MONTHLY", "IS 2189:2008 cl.9 — beam detector alignment and lens check"),

    (HYDRANT_SYSTEM, None, "MONTHLY",
     "NBC 2016 Part 4 cl.6.3 / IS 3844 — monthly valve, box and pump-room inspection"),

    ("HOSE_REEL", None, "QUARTERLY", "IS 884:1985 — first-aid hose reel, quarterly"),
    ("SMOKE_DETECTOR", None, "HALF_YEARLY", "IS 2189:2008 cl.9 — detector cleaning and test"),
    ("HEAT_DETECTOR", None, "HALF_YEARLY", "IS 2189:2008 cl.9"),
    ("GAS_SUPPRESSION", None, "ANNUAL", "IS 15493:2004 — annual clean-agent system service"),
    ("EMERGENCY_LIGHT", None, "HALF_YEARLY", "NBC 2016 Part 4 cl.4.6 — emergency lighting duration test"),
    ("HYDRANT", None, "MONTHLY", "NBC 2016 Part 4 cl.6.3"),
    ("FIRE_PUMP", None, "MONTHLY", "NBC 2016 Part 4 cl.6.4 — pump run test"),
    ("FIRE_WATER_TANK", None, "MONTHLY", "NBC 2016 Part 4 cl.6.2 — level and cleanliness"),
]


async def seed_frequencies(db) -> dict[str, int]:
    created = updated = 0
    for asset_type, subtype, frequency, ref in FREQUENCIES:
        row = (
            await db.execute(
                select(InspectionFrequencyMaster)
                .where(InspectionFrequencyMaster.plantId.is_(None))
                .where(InspectionFrequencyMaster.region == REGION)
                .where(InspectionFrequencyMaster.assetType == asset_type)
                .where(
                    InspectionFrequencyMaster.assetSubtype.is_(None)
                    if subtype is None
                    else InspectionFrequencyMaster.assetSubtype == subtype
                )
            )
        ).scalars().first()
        if row is None:
            row = InspectionFrequencyMaster(
                plantId=None, region=REGION, assetType=asset_type, assetSubtype=subtype,
            )
            db.add(row)
            created += 1
        else:
            updated += 1
        row.frequency = frequency
        row.customIntervalDays = None
        row.regulatoryReference = ref
        row.leadTimeDays = 7
        row.isActive = True
        row.isDeleted = False
    await db.flush()
    return {"created": created, "updated": updated}


# ═══════════════════════════════════════════════════════════════════════════
# 2. Branded registers
# ═══════════════════════════════════════════════════════════════════════════
# The extinguisher register is the client's actual controlled document, so its
# number, revision, dates and column order are transcribed. The alarm-panel and
# hydrant registers are NOT client documents — no client has issued one — so they
# ship as internal registers with a clearly internal document number rather than
# a fabricated PIL/EHSD/CL number. Inventing a client document number for a sheet
# the client has never seen would put a false controlled-document reference in
# front of an auditor.
REGISTERS: list[dict] = [
    {
        "assetType": FIRE_EXTINGUISHER,
        "brandName": "Register of Fire Extinguishers",
        "routeSlug": "extinguisher-register",
        "documentNo": "PIL/EHSD/CL/028-R1",
        "supersedesNo": "PIL/EHSD/CL/092-R0",
        "revision": "R1",
        "effectiveDate": "2024-09-05",
        "reviewDate": "2027-09-04",
        "pdfTemplateKey": "FE_REGISTER",
        "columns": [
            ("slNo", "Sl. No"), ("serialNo", "Manufacturer Serial No."), ("type", "Type"),
            ("capacity", "Capacity"), ("yearOfManufacture", "Year Manufacture"),
            ("expiryDate", "Expiry Date"), ("make", "Make"),
            ("allottedSerialNo", "Alloted Serial No."), ("location", "Location"),
            ("hpTestedOn", "HP tested on"), ("hpTestDueDate", "HP Test due date"),
            ("dateOfDischarge", "Date of Discharge"), ("refilledOn", "Refilled on"),
            ("dueForRefilling", "Due for refilling"), ("weightKg", "Weight in Kgs"),
            ("remarks", "Remarks"),
        ],
    },
    {
        "assetType": FIRE_ALARM_PANEL,
        "brandName": "Register of Fire Alarm Panels",
        "routeSlug": "alarm-panel-register",
        "documentNo": "SO360/FIRE/REG/FAS-01",
        "supersedesNo": None,
        "revision": "R1",
        "effectiveDate": "2026-01-01",
        "reviewDate": "2029-01-01",
        "pdfTemplateKey": "GENERIC_REGISTER",
        "columns": [
            ("slNo", "Sl. No"), ("equipmentCode", "Panel Code"), ("assetSubtype", "Addressing"),
            ("make", "Make"), ("model", "Model"), ("serialNo", "Serial No."),
            ("location", "Location"), ("zoneCount", "Zones / Loops"),
            ("lastInspectionDate", "Last inspected"), ("nextInspectionDueDate", "Next due"),
            ("status", "Status"), ("remarks", "Remarks"),
        ],
    },
    {
        "assetType": HYDRANT_SYSTEM,
        "brandName": "Register of Fire Hydrant & Sprinkler Systems",
        "routeSlug": "hydrant-system-register",
        "documentNo": "SO360/FIRE/REG/FHS-01",
        "supersedesNo": None,
        "revision": "R1",
        "effectiveDate": "2026-01-01",
        "reviewDate": "2029-01-01",
        "pdfTemplateKey": "GENERIC_REGISTER",
        "columns": [
            ("slNo", "Sl. No"), ("equipmentCode", "System Code"), ("make", "Make"),
            ("capacity", "Rated capacity"), ("location", "Pump house / location"),
            ("lastInspectionDate", "Last inspected"), ("nextInspectionDueDate", "Next due"),
            ("status", "Status"), ("remarks", "Remarks"),
        ],
    },
]


async def seed_registers(db) -> dict[str, int]:
    created = updated = 0
    for r in REGISTERS:
        row = (
            await db.execute(
                select(FireRegisterViewConfig)
                .where(FireRegisterViewConfig.tenantId.is_(None))
                .where(FireRegisterViewConfig.assetType == r["assetType"])
            )
        ).scalars().first()
        if row is None:
            row = FireRegisterViewConfig(tenantId=None, assetType=r["assetType"])
            db.add(row)
            created += 1
        else:
            updated += 1
        row.brandName = r["brandName"]
        row.routeSlug = r["routeSlug"]
        row.documentNo = r["documentNo"]
        row.supersedesNo = r["supersedesNo"]
        row.revision = r["revision"]
        row.effectiveDate = _d(r["effectiveDate"])
        row.reviewDate = _d(r["reviewDate"])
        row.department = "EHS"
        row.columns = [{"key": k, "label": lab} for k, lab in r["columns"]]
        row.pdfTemplateKey = r["pdfTemplateKey"]
        row.isActive = True
    await db.flush()
    return {"created": created, "updated": updated}


# ═══════════════════════════════════════════════════════════════════════════
async def main() -> None:
    async with AsyncSessionLocal() as db:
        freq = await seed_frequencies(db)
        regs = await seed_registers(db)
        await db.commit()

        # ── self-assert: the whole point was to stop the resolver falling back ──
        from app.services import fire_frequency as freqsvc

        problems: list[str] = []
        print(f"Frequency rules: {freq['created']} created, {freq['updated']} updated")
        for asset_type in (FIRE_EXTINGUISHER, FIRE_ALARM_PANEL, BEAM_DETECTOR, HYDRANT_SYSTEM):
            res = await freqsvc.resolve(db, asset_type=asset_type, region=REGION)
            ok = res.source != "FALLBACK" and res.resolved
            print(f"  {'  ' if ok else '!!'} {asset_type:22} {res.days:>4}d  "
                  f"{res.frequency:12} source={res.source}")
            if not ok:
                problems.append(f"{asset_type} still resolves to the platform fallback")

        print(f"\nBranded registers: {regs['created']} created, {regs['updated']} updated")
        rows = (
            await db.execute(
                select(FireRegisterViewConfig).where(FireRegisterViewConfig.isActive.is_(True))
            )
        ).scalars().all()
        for row in sorted(rows, key=lambda r: r.assetType):
            print(f"     /{row.routeSlug:26} {row.documentNo:24} "
                  f"{len(row.columns)} cols  {row.brandName}")
        slugs = [r.routeSlug for r in rows]
        if len(set(slugs)) != len(slugs):
            problems.append("two registers claim the same route slug")

        if problems:
            print("\nFAILED:")
            for p in problems:
                print("  -", p)
            raise SystemExit(1)
        print("\nOK — cadences resolve from config, branded registers are data.")


if __name__ == "__main__":
    asyncio.run(main())
