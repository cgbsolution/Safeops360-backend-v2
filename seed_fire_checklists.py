"""Seed the Page Industries fire checklists (PIL/EHS/CL 025-028) into CAMS.

    python seed_fire_checklists.py

Idempotent by design, and upsert-only — it never deletes. Re-running after
editing `services/fire_checklist_templates.py` updates the wording in place and
leaves every filled-in checklist intact. That matters more here than in most
seeders: these are controlled documents against which real inspections are
already recorded, and a wipe-and-reseed would orphan every answer on the
platform the first time someone fixed a typo in an item.

HOW ANSWERS SURVIVE A RE-SEED
-----------------------------
`CamsResponse.answers` is keyed by question id, and question ids are generated.
So the seeder must not recreate questions it has already created. It matches an
existing question by its stable item key — `fas_d_03`, `fe_06` — stored in
`CamsTemplateQuestion.standardClauseRef`.

That column is "the clause this question comes from", and for a controlled client
document the clause reference genuinely IS the sheet's row identity: PIL/EHS/CL/
025-R1 (A) row 3 is `fas_d_03` in every revision that keeps the row. Reusing it
beats adding an `itemKey` column that only one module would ever populate.

WHAT ELSE THIS SEEDS
--------------------
Demo assets for each checklist family, because a template with nothing to run
against cannot be opened, let alone verified: a zone-addressed FAS panel and a
loop-addressed one (the two monthly variants), a beam detector, a hydrant system,
and extinguishers carrying real Register of Fire Extinguishers data including the
HP-test and refill certificates the register projects its due-date columns from.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.db import AsyncSessionLocal
from app.models.cams import (
    CamsAuditType, CamsTemplate, CamsTemplateQuestion, CamsTemplateSection,
)
from app.models.fire_safety import FireAssetCertificate, FireEquipment
from app.models.plant import Plant
from app.models.user import User
from app.services import fire_certificates as certsvc
from app.services import fire_safety as firesvc
from app.services.fire_checklist_templates import (
    ALL_TEMPLATES, BEAM_DETECTOR, FIRE_ALARM_PANEL, FIRE_EXTINGUISHER, HYDRANT_SYSTEM, TemplateDef,
)

NOW = datetime.now(timezone.utc)

AUDIT_TYPE_CODE = "FIRE-CHECKLIST"


# ═══════════════════════════════════════════════════════════════════════════
# Audit type
# ═══════════════════════════════════════════════════════════════════════════
async def ensure_audit_type(db) -> CamsAuditType:
    at = (
        await db.execute(select(CamsAuditType).where(CamsAuditType.typeCode == AUDIT_TYPE_CODE))
    ).scalars().first()
    if at is None:
        at = CamsAuditType(typeCode=AUDIT_TYPE_CODE, name="Fire & Life Safety Checklist",
                           engagementType="INSPECTION")
        db.add(at)
    at.requiresAssetRef = True          # a fire checklist without an asset is meaningless
    at.requiresAuditorCompetency = []
    at.competenceEnforcement = "WARN"
    # PASS_FAIL, not the platform's default PERCENT_CONFORMANCE. A statutory
    # equipment check does not have a pass mark: "82 % of the fire alarm works"
    # is not a compliance statement anyone can act on, and a percentage invites
    # exactly that reading. One NO fails the sheet.
    at.scoringRules = {"minimumPassScore": 100.0, "naHandling": "EXCLUDE"}
    at.standardRefs = sorted({t.documentNo for t in ALL_TEMPLATES})
    at.isActive = True
    await db.flush()
    return at


# ═══════════════════════════════════════════════════════════════════════════
# Templates
# ═══════════════════════════════════════════════════════════════════════════
def _document_meta(t: TemplateDef, audit_type_id: str) -> dict:
    return {
        "documentNo": t.documentNo,
        "supersedesNo": t.supersedesNo,
        "revision": t.revision,
        "effectiveDate": t.effectiveDate,
        "reviewDate": t.reviewDate,
        "department": t.department,
        "pageLabel": t.pageLabel,
        "frequency": t.frequency,
        "assetType": t.assetType,
        "layout": t.layout,
        "siteVariant": t.siteVariant,
        "sourceSheet": t.sourceSheet,
        "signOffRoles": list(t.signOffRoles),
        "footnotes": list(t.footnotes),
        "sectionNotes": {s.title: s.note for s in t.sections if s.note},
        "auditTypeId": audit_type_id,
    }


async def upsert_template(db, t: TemplateDef, owner_id: str, audit_type_id: str) -> tuple[CamsTemplate, str]:
    tpl = (
        await db.execute(
            select(CamsTemplate)
            .options(selectinload(CamsTemplate.sections).selectinload(CamsTemplateSection.questions))
            .where(CamsTemplate.templateCode == t.code)
        )
    ).scalars().first()

    action = "updated"
    # Held separately rather than read back off `tpl.sections`. Assigning to that
    # relationship on a freshly-added instance makes SQLAlchemy lazy-load the
    # collection it is about to replace, which under asyncio is a MissingGreenlet
    # rather than a query — the classic async-ORM trap.
    loaded: list[CamsTemplateSection] = []
    if tpl is None:
        action = "created"
        tpl = CamsTemplate(templateCode=t.code, name=t.name, ownerId=owner_id, version=1)
        db.add(tpl)
        await db.flush()
    else:
        loaded = list(tpl.sections)

    tpl.name = t.name
    tpl.description = f"{t.documentNo} ({t.revision}) — {t.sourceSheet}"
    tpl.applicableEngagementTypes = ["INSPECTION"]
    tpl.standardRefs = [t.documentNo, t.supersedesNo]
    tpl.documentMeta = _document_meta(t, audit_type_id)
    tpl.scoringConfig = {"mode": "PASS_FAIL"}
    tpl.isGlobal = True
    tpl.isDeleted = False
    # A controlled document arrives already approved by the client's own document
    # control — it is not a draft the platform is authoring. Engagements refuse to
    # start against a non-APPROVED template, so anything else would make every
    # seeded checklist unopenable.
    tpl.status = "APPROVED"
    tpl.approvedBy = owner_id
    tpl.approvedAt = tpl.approvedAt or NOW

    existing_sections = {s.title: s for s in loaded}
    # Item key -> question, across ALL sections. Global rather than per-section so
    # that moving an item to a different heading in a later revision reuses the
    # same question row and keeps its recorded answers.
    existing_questions: dict[str, CamsTemplateQuestion] = {
        q.standardClauseRef: q
        for s in loaded for q in s.questions if q.standardClauseRef
    }

    for s_idx, sec_def in enumerate(t.sections):
        sec = existing_sections.get(sec_def.title)
        if sec is None:
            sec = CamsTemplateSection(templateId=tpl.id, title=sec_def.title, orderIndex=s_idx)
            db.add(sec)
            await db.flush()
        sec.orderIndex = s_idx

        for q_idx, item in enumerate(sec_def.items):
            q = existing_questions.get(item.key)
            if q is None:
                q = CamsTemplateQuestion(sectionId=sec.id, standardClauseRef=item.key)
                db.add(q)
            else:
                q.sectionId = sec.id
            q.orderIndex = q_idx
            q.text = item.text
            q.questionType = item.type
            q.isMandatory = item.mandatory
            q.guidance = item.guidance
            q.ncTriggersFinding = item.triggers_finding
            q.evidenceRequiredOnNc = False
            q.weight = None
            # `options` is CamsTemplateQuestion's generic per-question config slot.
            # The per-item NC severity goes here rather than into a new column that
            # only this module would populate; services/fire_capa.py reads it back.
            # `noFindingReason` is carried too so the screen can explain why a
            # pass/fail check does not raise, instead of looking like an oversight.
            cfg = {"ncSeverity": item.nc_severity}
            if item.no_finding_reason:
                cfg["noFindingReason"] = item.no_finding_reason
            q.options = cfg
    await db.flush()
    return tpl, action


# ═══════════════════════════════════════════════════════════════════════════
# Demo assets
# ═══════════════════════════════════════════════════════════════════════════
# (code suffix, type, subtype, location, capacity) — the subtype on a panel is
# what selects its monthly variant: a ZONE panel gets the Unit-21 A sheet with a
# Zone Number field, a LOOP panel gets Unit-21 B with a Loop Number field.
SYSTEM_ASSETS = [
    ("FAS-A", FIRE_ALARM_PANEL, "ZONE", "Unit-21 A — Panel Room", None),
    ("FAS-B", FIRE_ALARM_PANEL, "LOOP", "Unit-21 B — Panel Room", None),
    ("BEAM-01", BEAM_DETECTOR, "BEAM", "Weaving Department — Roof Beam Line 1", None),
    ("FHS-01", HYDRANT_SYSTEM, None, "Fire Pump House — Main Yard", "Hydrant & Sprinkler System"),
]

# (allotted tag, subtype, capacity, year, make, location, hp_offset_days, refill_offset_days)
# Offsets are relative to today and deliberately spread across the badge ladder so
# the register's OVERDUE / DUE_SOON / OK / NOT_RECORDED states are all visible
# without hand-editing rows after a seed.
EXTINGUISHERS = [
    ("36773", "CO2", "2KG", 2021, "SAFETECH", "Admin — Reception", -20, 340),
    ("36774", "ABC", "6KG", 2022, "SAFETECH", "Block-A — Stitching Floor, Col 4", 900, -12),
    ("36775", "DCP", "9KG", 2020, "MINIMAX", "Block-A — Dispatch", 18, 25),
    ("36776", "CO2", "4.5KG", 2023, "SAFEX", "Block-B — Cutting", 1500, 600),
    ("36777", "FOAM", "9L", 2019, "MINIMAX", "Fire Pump House — Main Yard", None, None),
    ("36778", "ABC", "6KG", 2024, "SAFETECH", "Unit-21 A — Panel Room", 1200, 400),
]


async def seed_assets(db, plant: Plant) -> dict[str, int]:
    pcode = (plant.code or "P1").upper().replace(" ", "")
    counts = {"systems": 0, "extinguishers": 0, "certificates": 0}

    async def upsert_equipment(code: str, **fields) -> FireEquipment:
        e = (
            await db.execute(select(FireEquipment).where(FireEquipment.equipmentCode == code))
        ).scalars().first()
        if e is None:
            e = FireEquipment(equipmentCode=code, plantId=plant.id, **fields)
            db.add(e)
        else:
            for k, v in fields.items():
                setattr(e, k, v)
        await db.flush()
        return e

    for suffix, etype, subtype, location, capacity in SYSTEM_ASSETS:
        freq = 30 if etype == BEAM_DETECTOR else 30
        last = NOW - timedelta(days=3)
        e = await upsert_equipment(
            f"FIRE-{pcode}-{suffix}", type=etype, assetSubtype=subtype, location=location,
            capacitySpec=capacity, inspectionFrequencyDays=freq,
            installationDate=NOW - timedelta(days=1200),
            lastInspectionDate=last, nextInspectionDueDate=last + timedelta(days=freq),
            isActive=True, isDeleted=False,
        )
        e.status = firesvc.compute_status(e, NOW)
        counts["systems"] += 1

    for tag, subtype, capacity, year, make, location, hp_off, refill_off in EXTINGUISHERS:
        last = NOW - timedelta(days=10)
        e = await upsert_equipment(
            f"FIRE-{pcode}-FE-{tag}", type=FIRE_EXTINGUISHER, assetSubtype=subtype,
            location=location, capacitySpec=capacity, make=make,
            serialNo=f"MFR-{tag}", allottedSerialNo=tag, yearOfManufacture=year,
            # Cylinder life: the client's own sheet shows a ten-year life
            # (manufactured 2021 -> expires 2031).
            expiryDate=datetime(year + 10, 4, 27, tzinfo=timezone.utc),
            weightKg=float(capacity.rstrip("KGL") or 0) if capacity[0].isdigit() else None,
            registerRemarks=None, inspectionFrequencyDays=30,
            installationDate=datetime(year, 4, 27, tzinfo=timezone.utc),
            lastInspectionDate=last, nextInspectionDueDate=last + timedelta(days=30),
            isActive=True, isDeleted=False,
        )
        e.status = firesvc.compute_status(e, NOW)
        counts["extinguishers"] += 1

        # HP test and refill are certificates, not columns — see services/fire_register.py.
        for cert_type, offset, life_days in (
            ("HYDROSTATIC_TEST", hp_off, 5 * 365),
            ("REFILL", refill_off, 365),
        ):
            if offset is None:
                continue  # NOT_RECORDED — a real and important register state
            due = NOW + timedelta(days=offset)
            issued = due - timedelta(days=life_days)
            existing = (
                await db.execute(
                    select(FireAssetCertificate)
                    .where(FireAssetCertificate.assetId == e.id)
                    .where(FireAssetCertificate.certificateType == cert_type)
                )
            ).scalars().first()
            if existing is None:
                existing = FireAssetCertificate(assetId=e.id, plantId=plant.id, certificateType=cert_type)
                db.add(existing)
                counts["certificates"] += 1
            existing.issueDate = issued
            existing.expiryDate = due
            existing.certificateNo = f"{cert_type[:2]}-{tag}-{issued.year}"
            existing.issuingAuthority = "SafeFire Services Pvt Ltd"
            existing.escalationTierDays = []
            existing.documentIds = []
            existing.isDeleted = False
            existing.status = certsvc.status_for(due, certsvc.DEFAULT_TIERS, NOW)
    await db.flush()
    return counts


# ═══════════════════════════════════════════════════════════════════════════
async def main() -> None:
    async with AsyncSessionLocal() as db:
        owner = (await db.execute(select(User).order_by(User.createdAt).limit(1))).scalars().first()
        if owner is None:
            print("No users found — run the platform seed first.")
            return
        plant = (await db.execute(select(Plant).order_by(Plant.code).limit(1))).scalars().first()
        if plant is None:
            print("No plants found — run the platform seed first.")
            return

        audit_type = await ensure_audit_type(db)

        created = updated = 0
        for t in ALL_TEMPLATES:
            _tpl, action = await upsert_template(db, t, owner.id, audit_type.id)
            created += action == "created"
            updated += action == "updated"
        await db.commit()

        counts = await seed_assets(db, plant)
        await db.commit()

        # ── self-assert: a seeder that reports success without checking is a
        # seeder that reports success when the DDL was never applied ──────────
        tpls = (
            await db.execute(
                select(CamsTemplate)
                .options(selectinload(CamsTemplate.sections).selectinload(CamsTemplateSection.questions))
                .where(CamsTemplate.templateCode.in_([t.code for t in ALL_TEMPLATES]))
            )
        ).scalars().all()
        by_code = {t.templateCode: t for t in tpls}

        print(f"Templates: {created} created, {updated} updated  (audit type {audit_type.typeCode})")
        problems: list[str] = []
        for t in ALL_TEMPLATES:
            got = by_code.get(t.code)
            if got is None:
                problems.append(f"{t.code}: not persisted")
                continue
            n_q = sum(len(s.questions) for s in got.sections)
            keys = {q.standardClauseRef for s in got.sections for q in s.questions}
            flag = "  " if (n_q == len(t.items) and got.status == "APPROVED") else "!!"
            print(f"  {flag} {t.code:26} {t.frequency:9} {got.documentMeta.get('layout',''):13} "
                  f"items={n_q:3}/{len(t.items):<3} sections={len(got.sections)}  {t.documentNo}")
            if n_q != len(t.items):
                problems.append(f"{t.code}: {n_q} questions, expected {len(t.items)}")
            if keys != {i.key for i in t.items}:
                problems.append(f"{t.code}: item keys drifted from the source definition")
            if got.status != "APPROVED":
                problems.append(f"{t.code}: status {got.status}, checklists cannot be raised against it")

        print(f"Assets on {plant.code}: {counts['systems']} systems, "
              f"{counts['extinguishers']} extinguishers, {counts['certificates']} new certificates")

        if problems:
            print("\nFAILED:")
            for p in problems:
                print("  -", p)
            raise SystemExit(1)
        print("\nOK — 11 controlled checklists seeded and openable.")


if __name__ == "__main__":
    asyncio.run(main())
