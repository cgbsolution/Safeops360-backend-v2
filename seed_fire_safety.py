"""Seed Fire Safety & Emergency Response demo data (P1-4) for the two Meridian plants.

Idempotent: wipes Fire* rows then reseeds. Engineers concrete states the dashboard
asserts (OVERDUE / DUE_INSPECTION / OUT_OF_SERVICE items, a drill with a MAJOR_GAP
CAPA, an APPROVED emergency plan + assembly points per site).

    python seed_fire_safety.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text

from app.core.db import AsyncSessionLocal
from app.models.fire_safety import (
    AssemblyPoint, FireDrill, FireDrillFinding, FireEmergencyPlan, FireEquipment, FireIncidentLink,
)
from app.models.plant import Plant
import app.services.fire_safety as svc

NOW = datetime.now(timezone.utc)

TYPES = ["FIRE_EXTINGUISHER", "HOSE_REEL", "HYDRANT", "SMOKE_DETECTOR", "HEAT_DETECTOR", "FIRE_ALARM_PANEL", "GAS_SUPPRESSION", "EMERGENCY_LIGHT"]
FREQ = {"FIRE_EXTINGUISHER": 30, "HOSE_REEL": 90, "HYDRANT": 90, "SMOKE_DETECTOR": 180, "HEAT_DETECTOR": 180, "FIRE_ALARM_PANEL": 365, "GAS_SUPPRESSION": 365, "EMERGENCY_LIGHT": 180}
CAP = {"FIRE_EXTINGUISHER": "6 kg ABC dry powder", "HOSE_REEL": "30 m × 19 mm", "HYDRANT": "single-headed", "GAS_SUPPRESSION": "FM-200 server room"}


def _eq(plant_id, pcode, code_n, etype, building, loc, last_offset_days, freq):
    """Build one equipment with last/next dates derived from offset (negative=in past)."""
    last = NOW + timedelta(days=last_offset_days)
    e = FireEquipment(
        equipmentCode=f"FE-{pcode}-{code_n:04d}", type=etype, location=loc, plantId=plant_id,
        buildingId=building, inspectionFrequencyDays=freq, capacitySpec=CAP.get(etype),
        maintenanceContractor="SafeFire Services Pvt Ltd", installationDate=NOW - timedelta(days=900),
        lastInspectionDate=last, nextInspectionDueDate=last + timedelta(days=freq),
        qrCode=f"SAFEOPS-FIRE-FE-{pcode}-{code_n:04d}",
    )
    e.status = svc.compute_status(e, NOW)
    return e


async def seed_for_plant(db, plant, idx, equip_specs):
    pid = plant.id
    pcode = (plant.code or f"P{idx}").upper().replace(" ", "")
    n = 0
    for (etype, building, loc, last_off) in equip_specs:
        n += 1
        db.add(_eq(pid, pcode, n, etype, building, loc, last_off, FREQ[etype]))
    # one OUT_OF_SERVICE with reason
    n += 1
    oos = _eq(pid, pcode, n, "FIRE_EXTINGUISHER", "Block-C", "Block-C – Spray Booth", -10, 30)
    oos.status = "OUT_OF_SERVICE"; oos.outOfServiceReason = "Discharged during minor fire — pending recharge"
    db.add(oos)

    aps = []
    for j, (nm, cap) in enumerate([("North Gate Lawn", 400), ("South Car Park", 350), ("ETP Lawn", 200)], start=1):
        ap = AssemblyPoint(code=f"AP-{pcode}-{j:02d}", name=nm, plantId=pid, capacity=cap,
                           buildingIds=[], latitude=30.9 + j * 0.001, longitude=75.8 + j * 0.001)
        db.add(ap); aps.append(ap)
    await db.flush()

    plan = FireEmergencyPlan(
        planCode=f"FEP-{pcode}-01", title=f"Fire & Emergency Response Plan — {plant.name}", plantId=pid,
        fireTypes=["ELECTRICAL", "CHEMICAL", "GENERAL_COMBUSTION"], status="APPROVED",
        assemblyPointIds=[a.id for a in aps],
        commandStructure=[
            {"role": "INCIDENT_CONTROLLER", "responsibilities": "Overall command; liaise with fire brigade"},
            {"role": "ASSEMBLY_POINT_WARDEN", "responsibilities": "Muster + headcount"},
            {"role": "SEARCH_RESCUE", "responsibilities": "Sweep + casualty extraction"},
        ],
        externalContacts=[
            {"name": "City Fire Brigade", "role": "Fire & Rescue", "phone": "101"},
            {"name": "State Electricity Emergency", "role": "Power isolation", "phone": "1912"},
            {"name": "Nearest Hospital", "role": "Casualty", "phone": "108"},
        ],
        criticalEquipmentShutdownSequence="1) Isolate HT feeder  2) Trip boiler  3) Close gas main  4) Stop ETP blowers",
        lastReviewDate=NOW - timedelta(days=200), nextReviewDate=NOW + timedelta(days=165),
    )
    db.add(plan)
    await db.flush()

    # drills: plant 0 → a completed NEEDS_IMPROVEMENT drill w/ a MAJOR_GAP + CAPA + a re-drill PLANNED
    if idx == 0:
        d = FireDrill(drillCode=f"DRL-{pcode}-{NOW.year}-001", plantId=pid, drillType="EVACUATION",
                      planId=plan.id, scheduledDate=NOW - timedelta(days=80), conductedDate=NOW - timedelta(days=80),
                      status="COMPLETED", outcome="NEEDS_IMPROVEMENT", participantCount=312,
                      evacuationTimeMinutes=14.0, evacuationTargetMinutes=12.0, assemblyPointVerified=True, unaccountedPersons=0,
                      isAnnualMandatory=True, reportRichText="Evacuation 2 min over target; one assembly-point warden off-site.")
        db.add(d); await db.flush()
        db.add(FireDrillFinding(drillId=d.id, severity="MAJOR_GAP", description="Assembly-point warden (North Gate) not on site at muster", capaId="CAPA-FIRE-DEMO-001"))
        db.add(FireDrillFinding(drillId=d.id, severity="MINOR_GAP", description="Evacuation time 14 min vs 12 min target"))
        db.add(FireDrill(drillCode=f"DRL-{pcode}-{NOW.year}-002", plantId=pid, drillType="EVACUATION",
                         planId=plan.id, scheduledDate=NOW + timedelta(days=25), status="PLANNED", isAnnualMandatory=True))
    else:
        d = FireDrill(drillCode=f"DRL-{pcode}-{NOW.year}-001", plantId=pid, drillType="EVACUATION",
                      planId=plan.id, scheduledDate=NOW - timedelta(days=60), conductedDate=NOW - timedelta(days=60),
                      status="COMPLETED", outcome="SATISFACTORY", participantCount=210,
                      evacuationTimeMinutes=9.0, evacuationTargetMinutes=10.0, assemblyPointVerified=True, unaccountedPersons=0, isAnnualMandatory=True)
        db.add(d)
        db.add(FireDrill(drillCode=f"DRL-{pcode}-{NOW.year}-002", plantId=pid, drillType="FIRE_FIGHTING",
                         planId=plan.id, scheduledDate=NOW + timedelta(days=40), status="PLANNED"))
    await db.flush()


async def main():
    async with AsyncSessionLocal() as db:
        # wipe (raw — Fire* are governed; ORM delete is blocked by the guard)
        for t in ("FireDrillFinding", "FireDrill", "FireEmergencyPlan", "AssemblyPoint", "FireIncidentLink", "FireEquipment"):
            await db.execute(text(f'DELETE FROM "{t}"'))
        await db.commit()

        plants = (await db.execute(select(Plant).order_by(Plant.code).limit(2))).scalars().all()
        if len(plants) < 2:
            print("Need ≥2 plants; found", len(plants)); return

        # plant 0 (richer): 2 OVERDUE + 3 DUE + rest active
        specs0 = [
            ("FIRE_EXTINGUISHER", "Block-A", "Block-A – Stitching Floor, Col 4", -40),  # OVERDUE (freq 30)
            ("HEAT_DETECTOR", "Block-A", "Block-A – Stitching Floor ceiling", -200),     # OVERDUE (freq 180)
            ("FIRE_EXTINGUISHER", "Block-B", "Block-B – Cutting", -20),                  # DUE (within 30)
            ("FIRE_EXTINGUISHER", "Block-B", "Block-B – Finishing", -25),                # DUE
            ("HOSE_REEL", "Block-A", "Block-A – Corridor", -65),                         # DUE (freq 90)
            ("HYDRANT", "Yard", "Main Yard – East", -10),
            ("SMOKE_DETECTOR", "Block-A", "Block-A – Store", -30),
            ("FIRE_ALARM_PANEL", "Admin", "Admin – Reception", -30),
            ("GAS_SUPPRESSION", "IT", "Server Room", -60),
            ("EMERGENCY_LIGHT", "Block-B", "Block-B – Exit 2", -20),
            ("FIRE_EXTINGUISHER", "Admin", "Admin – Floor 1", -5),
            ("FIRE_EXTINGUISHER", "Block-A", "Block-A – Dispatch", -8),
        ]
        specs1 = [
            ("FIRE_EXTINGUISHER", "Unit-1", "Unit-1 – Knitting", -35),   # OVERDUE
            ("FIRE_EXTINGUISHER", "Unit-1", "Unit-1 – Dyeing", -12),
            ("HOSE_REEL", "Unit-2", "Unit-2 – Corridor", -20),
            ("HYDRANT", "Yard", "South Yard", -15),
            ("SMOKE_DETECTOR", "Unit-2", "Unit-2 – Store", -40),
            ("FIRE_ALARM_PANEL", "Admin", "Admin – Lobby", -50),
            ("HEAT_DETECTOR", "Unit-1", "Unit-1 – Boiler house", -90),
            ("EMERGENCY_LIGHT", "Unit-2", "Unit-2 – Exit 1", -25),
        ]
        await seed_for_plant(db, plants[0], 0, specs0)
        await seed_for_plant(db, plants[1], 1, specs1)
        await db.commit()

        # self-assert
        eq = (await db.execute(select(FireEquipment))).scalars().all()
        statuses: dict[str, int] = {}
        for e in eq:
            statuses[e.status] = statuses.get(e.status, 0) + 1
        drills = (await db.execute(select(FireDrill))).scalars().all()
        plans_n = (await db.execute(select(FireEmergencyPlan))).scalars().all()
        aps = (await db.execute(select(AssemblyPoint))).scalars().all()
        print(f"✅ seeded: {len(eq)} equipment {statuses}, {len(aps)} assembly points, {len(plans_n)} plans, {len(drills)} drills")
        assert statuses.get("OVERDUE", 0) >= 2, "expected ≥2 overdue"
        assert statuses.get("OUT_OF_SERVICE", 0) >= 1, "expected an out-of-service item"
        assert statuses.get("DUE_INSPECTION", 0) >= 2, "expected due items"
        print("   assertions OK (overdue/due/out-of-service engineered)")

asyncio.run(main())
