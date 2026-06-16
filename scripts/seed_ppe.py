"""Seed the PPE Management module catalog (PPE-01).

Master/config only, idempotent:
  Global PPE Type library (~41 types across every category) — upserted by
  code, so re-running is safe and additive.

Demo inventory/issuances were removed — the module starts with the type
library only and no plant items, so it comes up as a clean slate.

Run from the backend root:
    .venv/Scripts/python.exe scripts/seed_ppe.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.ppe import PpeType


def _std(s: str) -> list[dict]:
    return [{"standard": s, "clause": "Full standard", "requirement": ""}] if s else []


def _schedule(periodic_days: int | None, third_party: bool, annual: bool = False) -> list[dict]:
    sched = [{"inspection_type": "pre_use", "interval_days": None, "requires_competent_person": False,
              "requires_third_party": False, "regulatory_reference": ""}]
    if periodic_days:
        sched.append({
            "inspection_type": "annual" if annual else "periodic",
            "interval_days": periodic_days,
            "requires_competent_person": True,
            "requires_third_party": third_party,
            "regulatory_reference": "",
        })
    return sched


# (code, name, category, subcategory, life_yrs, tracks, personal, competency, fit_mo,
#  permit_types, standard, periodic_days, third_party)
CATALOG: list[tuple] = [
    ("HELMET-IS3521", "Industrial Safety Helmet", "head_protection", "safety_helmet", 5, True, False, None, None, ["confined_space", "work_at_height", "hot_work", "general_cold", "electrical"], "IS 3521", 180, False),
    ("HELMET-VENTILATED", "Vented Safety Helmet", "head_protection", "safety_helmet", 5, True, False, None, None, ["general_cold"], "EN 397", 180, False),
    ("HARNESS-FULLBODY-EN361", "Full-Body Safety Harness", "fall_protection", "full_body_harness", 10, True, True, "WORK-HEIGHT-L1", None, ["work_at_height"], "EN 361", 365, True),
    ("LANYARD-SHOCKABSORB", "Shock-Absorbing Lanyard", "fall_protection", "lanyard", 10, True, True, None, None, ["work_at_height"], "EN 355", 365, False),
    ("LANYARD-TWINTAIL", "Twin-Tail Lanyard", "fall_protection", "lanyard", 10, True, True, None, None, ["work_at_height"], "EN 355", 365, False),
    ("SRL-FALLARREST", "Self-Retracting Lifeline", "fall_protection", "srl", 10, True, False, None, None, ["work_at_height"], "EN 360", 365, True),
    ("RESCUE-TRIPOD", "Confined Space Rescue Tripod", "fall_protection", "rescue_tripod", 10, True, False, None, None, ["confined_space"], "EN 795", 365, True),
    ("SCBA-POSITIVEPRESSURE", "Self-Contained Breathing Apparatus (SCBA)", "respiratory_protection", "scba", 15, True, False, "CS-ENTRANT-L1", None, ["confined_space"], "EN 137", 365, True),
    ("AIRLINE-RESP", "Airline Breathing Apparatus", "respiratory_protection", "airline", 15, True, False, "CS-ENTRANT-L1", None, ["confined_space"], "EN 14593", 365, True),
    ("RESP-HALFMASK", "Half-Face Respirator", "respiratory_protection", "half_mask", 5, True, True, None, 12, [], "EN 140", 180, False),
    ("RESP-FULLFACE", "Full-Face Respirator", "respiratory_protection", "full_mask", 8, True, True, None, 12, ["confined_space"], "EN 136", 180, False),
    ("FFP3-MASK", "FFP3 Filtering Half Mask", "respiratory_protection", "disposable", 0, False, False, None, None, [], "EN 149", None, False),
    ("DUST-MASK-FFP2", "FFP2 Dust Mask", "respiratory_protection", "disposable", 0, False, False, None, None, [], "EN 149", None, False),
    ("GAS-DETECTOR-4GAS", "Portable 4-Gas Detector (O2, LEL, H2S, CO)", "gas_detection", "gas_monitor_4gas", 5, True, False, "GAS-TEST", None, ["confined_space"], "EN 60079", 365, False),
    ("GAS-DETECTOR-SINGLE-H2S", "Single-Gas H2S Detector", "gas_detection", "gas_monitor_single", 3, True, False, "GAS-TEST", None, ["confined_space"], "EN 60079", 365, False),
    ("GOGGLES-CHEM", "Chemical Splash Goggles", "eye_face_protection", "goggles", 3, True, False, None, None, [], "EN 166", None, False),
    ("SPECTACLES-SAFETY", "Safety Spectacles", "eye_face_protection", "spectacles", 3, True, False, None, None, ["general_cold"], "EN 166", None, False),
    ("VISOR-WELDING-AUTO", "Auto-Darkening Welding Visor", "eye_face_protection", "welding_visor", 5, True, True, None, None, ["hot_work"], "EN 175", None, False),
    ("FACESHIELD-GRINDING", "Grinding Face Shield", "eye_face_protection", "face_shield", 3, True, False, None, None, ["hot_work"], "EN 166", None, False),
    ("EARMUFF", "Ear Defenders (Muffs)", "hearing_protection", "earmuff", 5, True, True, None, None, [], "EN 352", None, False),
    ("EARPLUG-REUSABLE", "Reusable Ear Plugs", "hearing_protection", "earplug", 1, False, True, None, None, [], "EN 352", None, False),
    ("GLOVES-GENERAL", "General Handling Gloves", "hand_protection", "general", 1, False, True, None, None, ["general_cold"], "EN 388", None, False),
    ("GLOVES-CUT5", "Cut-Resistant Gloves (Level 5)", "hand_protection", "cut_resistant", 2, True, True, None, None, [], "EN 388", None, False),
    ("GLOVES-CHEM-NITRILE", "Chemical Nitrile Gloves", "hand_protection", "chemical", 2, True, True, None, None, [], "EN 374", None, False),
    ("GLOVES-HEAT", "Heat-Resistant Gloves", "hand_protection", "heat_resistant", 2, True, True, None, None, ["hot_work"], "EN 407", None, False),
    ("WELDING-GAUNTLET", "Welding Gauntlets", "hand_protection", "welding", 2, True, True, None, None, ["hot_work"], "EN 12477", None, False),
    ("GLOVES-ELEC-LV", "Electrical Insulating Gloves — Low Voltage", "electrical_protection", "elec_glove_lv", 3, True, True, None, None, ["electrical"], "IS 4770", 180, True),
    ("GLOVES-ELEC-HT", "Electrical Insulating Gloves — High Tension", "electrical_protection", "elec_glove_ht", 3, True, True, None, None, ["electrical_ht"], "IS 4770", 90, True),
    ("ARC-FLASH-SUIT", "Arc Flash Suit", "electrical_protection", "arc_flash", 5, True, True, None, None, ["electrical_ht"], "IEC 61482", 365, False),
    ("SHOES-SAFETY", "Safety Shoes (Steel Toe)", "foot_protection", "safety_shoes", 2, True, True, None, None, ["confined_space", "work_at_height", "hot_work", "general_cold"], "EN ISO 20345", None, False),
    ("GUMBOOTS-SAFETY", "Safety Gumboots", "foot_protection", "gumboots", 2, True, True, None, None, [], "EN ISO 20345", None, False),
    ("SHOES-ELEC", "Electrical Hazard Footwear", "foot_protection", "elec_footwear", 2, True, True, None, None, ["electrical"], "EN ISO 20345", None, False),
    ("COVERALL-COTTON", "Cotton Coverall", "body_protection", "coverall", 2, True, True, None, None, ["general_cold"], "", None, False),
    ("COVERALL-FR", "Flame-Resistant Coverall", "body_protection", "fr_coverall", 3, True, True, None, None, ["hot_work"], "EN ISO 11612", None, False),
    ("APRON-WELDING-LEATHER", "Leather Welding Apron", "body_protection", "welding_apron", 3, True, True, None, None, ["hot_work"], "EN ISO 11611", None, False),
    ("SUIT-CHEM-TYPE3", "Chemical Protective Suit (Type 3)", "chemical_protection", "chem_suit", 3, True, False, "CHEMICAL-HANDLING", None, [], "EN 14605", None, False),
    ("APRON-CHEM-PVC", "PVC Chemical Apron", "chemical_protection", "chem_apron", 2, True, True, None, None, [], "EN 14605", None, False),
    ("VEST-HIVIS", "High-Visibility Vest", "visibility_high", "hi_vis_vest", 2, True, True, None, None, ["excavation", "general_cold"], "EN ISO 20471", None, False),
    ("RAINCOAT-HIVIS", "Hi-Vis Rain Suit", "visibility_high", "hi_vis_rain", 2, True, True, None, None, [], "EN ISO 20471", None, False),
    ("LIFEJACKET", "Life Jacket / PFD", "body_protection", "life_jacket", 5, True, False, None, None, [], "EN ISO 12402", 365, False),
    ("KNEE-PADS", "Knee Protection Pads", "body_protection", "knee_pads", 2, True, True, None, None, [], "EN 14404", None, False),
]


def seed_catalog(s: Session) -> int:
    existing = set(s.execute(select(PpeType.code)).scalars().all())
    added = 0
    for (code, name, category, subcat, life, tracks, personal, comp, fit_mo,
         permits, standard, periodic, third_party) in CATALOG:
        if code in existing:
            continue
        s.add(PpeType(
            tenantId=None,
            code=code,
            name=name,
            description=f"{name} — {category.replace('_', ' ')}.",
            category=category,
            subcategory=subcat,
            applicableStandards=_std(standard),
            minimumSpecification=f"Conforms to {standard}." if standard else "",
            controlsHazards=[],
            enablesPermitTypes=permits,
            requiredForAreas=[],
            serviceLifeYears=life,
            inspectionSchedule=_schedule(periodic, third_party, annual=(periodic == 365)),
            requiresCompetencyToUse=comp,
            requiresFitTest=fit_mo is not None,
            fitTestValidityMonths=fit_mo,
            requiredTrainingPrograms=[],
            tracksIndividualItems=tracks,
            reorderPointPer100Workers=10,
            isPersonalIssue=personal,
            statutoryProvisionRequired=True,
            regulatoryReferences=[{"regulation": "Factories Act 1948", "section": "Section 35", "requirement": "Employer must provide and maintain PPE"}],
            isActive=True,
            isGlobal=True,
        ))
        added += 1
    s.flush()
    return added


def main() -> int:
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, future=True)
    with Session(engine) as s:
        added = seed_catalog(s)
        total = s.execute(select(PpeType)).scalars().all()
        s.commit()
        print(f"Catalog          : +{added} new types (total {len(total)})")
        print("Done.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
