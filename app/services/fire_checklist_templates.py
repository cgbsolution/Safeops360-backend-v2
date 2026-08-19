"""Controlled-document definitions for the Page Industries fire checklists.

Every entry here is a verbatim transcription of a client XLSX sheet — the item
wording, the document number, the revision, the effective/review dates and the
sheet's own section headings. Nothing is paraphrased: an auditor holding the
paper original must be able to read this file (or the PDF it drives) line for
line against their copy. The source spellings are kept as printed ("Wheather",
"corrossion", "Deparment") because a controlled document is reproduced, not
corrected.

WHY THIS IS DATA AND NOT A NEW SET OF TABLES
--------------------------------------------
The platform already has a checklist engine — `CamsTemplate` → `CamsTemplateSection`
→ `CamsTemplateQuestion`, run by `CamsEngagement` + `CamsResponse`, with scoring,
findings and the CAPA link. `models/fire_safety.py` says so explicitly: fire
inspections are CAMS engagements, "one engine, no parallel checklist store". So
these eleven sheets are *seed data for that engine*, not eleven new tables. The
next EHS checklist the client hands over is a new `TemplateDef` in this file and
a re-run of `seed_fire_checklists.py`.

ONE ENGAGEMENT PER INSPECTION OCCURRENCE
----------------------------------------
The paper sheets are period *grids*: 7 daily alarm checks x 31 date columns, 21
extinguisher checks x 12 month columns, 3 alarm checks x 4 quarter columns. It is
tempting to model the sheet as the record — one row per item, one column per
date. That would need a response store keyed (item, date), which CAMS does not
have and which would fork the engine.

It is also the wrong reading of the document. A month's daily grid is not one
inspection with 31 answers per item; it is **31 daily inspections printed on one
page to save paper**. So the record is the occurrence — one `CamsEngagement` per
(template, asset, period) — and the grid is a *pivot for the screen and the PDF*.
`periodLabel` carries the occurrence identity:

    DAILY      -> "2026-08-19"   one engagement per calendar day
    MONTHLY    -> "2026-08"
    QUARTERLY  -> "2026-Q3"
    ANNUAL     -> "2026"

`layout` below tells the UI and the PDF which way to pivot a set of those.

EVERY "NO" RAISES A CAPA
------------------------
`triggers_finding` defaults to **True**. A failed statutory check that leads
nowhere is a failed check nobody acts on, so each "No" creates a CAMS finding and
opens a CAPA against it.

An earlier revision of this file defaulted the flag to False, reasoning that a
daily grid at seven checks a day would flood the register with 217 chances a
month to raise "power indicator lamp off". The arithmetic was right and the
conclusion was wrong: the fix for "this raises the same CAPA thirty times" is not
to raise none, it is to stop raising duplicates. `services/fire_capa.py` keeps
exactly one open CAPA per (asset, item) — the first "No" opens it, every later
"No" on the same item for the same asset increments an occurrence count on the
same finding. A lamp dead for three weeks is one CAPA reading "observed 21
times", which is more actionable than twenty-one tickets, not less.

`nc_severity` says what a failure IS. Most routine checks are MINOR_NC: scuffed
paint is not a major non-conformance, and grading everything major makes the
field meaningless. The handful that genuinely are — no water in the hydrant tank,
design pressure not held, an extinguisher past its refill date — carry MAJOR_NC,
which is what makes the platform treat the CAPA as mandatory rather than
advisory.

`triggers_finding=False` stays available for the rows that are readings rather
than judgements (a battery voltage, a detector serial number) and for the
free-text closing fields, where there is no "No" to fail.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── Asset kinds these checklists attach to ───────────────────────────────────
# Deliberately the same vocabulary as `FireEquipment.type`, so a checklist's
# `assetType` matches the register without a translation table.
FIRE_ALARM_PANEL = "FIRE_ALARM_PANEL"
BEAM_DETECTOR = "BEAM_DETECTOR"
HYDRANT_SYSTEM = "FIRE_HYDRANT_SYSTEM"
FIRE_EXTINGUISHER = "FIRE_EXTINGUISHER"

# ── Layouts — how a set of period engagements is pivoted for screen/print ────
LAYOUT_DAY_GRID = "DAY_GRID"          # items x 1..31 date columns, one month per page
LAYOUT_MONTH_GRID = "MONTH_GRID"      # items x Jan..Dec columns, one year per page
LAYOUT_QUARTER_GRID = "QUARTER_GRID"  # items x Q1..Q4 columns, one year per page
LAYOUT_FORM = "FORM"                  # a single period's sectioned form


@dataclass(frozen=True)
class Item:
    """One row of a source sheet.

    `key` is a stable slug and is the *identity* of the question across reseeds.
    Question ids are generated and change if a template is rebuilt, so answers
    stored against them would orphan; the seeder matches on (templateCode, key)
    and reuses the existing question row instead of recreating it.
    """

    key: str
    text: str
    type: str = "YES_NO_NA"          # YES_NO_NA | NUMERIC | TEXT
    guidance: str | None = None
    mandatory: bool = True
    # A "No" here raises a finding and a CAPA. On by default — see the module
    # docstring for why, and for how duplicates are prevented.
    triggers_finding: bool = True
    # What a failure of this specific check IS. MINOR_NC unless the check is one
    # where a "No" means the system would not work in a fire.
    nc_severity: str = "MINOR_NC"
    # Why this item does NOT raise a finding, when it is a Yes/No/NA check.
    # Required for any such exemption and asserted by the test suite: a pass/fail
    # check quietly excluded from CAPA is the failure mode this whole feature
    # exists to fix, so an exemption has to be argued in the data, not assumed.
    # Readings (NUMERIC/TEXT) need no reason — there is no "No" to fail.
    no_finding_reason: str | None = None

    def __post_init__(self) -> None:
        # Only a Yes/No/NA item can be non-conforming: `_coerce` returns no
        # conformance for a NUMERIC or TEXT answer, so a reading could never raise
        # a finding however the flag were set. Forcing it False here keeps the
        # data honest rather than leaving a flag that reads as enabled and is inert
        # — and means the invariant lives in the type instead of in a hand-kept
        # list of keys that drifts the first time an item is added.
        if self.type != "YES_NO_NA":
            object.__setattr__(self, "triggers_finding", False)
            object.__setattr__(self, "nc_severity", "MINOR_NC")


@dataclass(frozen=True)
class Section:
    """A heading on the source sheet. `title` is the sheet's own wording."""

    title: str
    items: list[Item]
    # Rendered under the heading on screen and in the PDF — carries the free-text
    # blocks the sheets print between tables ("20 % of ___ detectors = ___").
    note: str | None = None


@dataclass(frozen=True)
class TemplateDef:
    code: str                 # CamsTemplate.templateCode — stable, referenced by the UI
    name: str
    documentNo: str
    supersedesNo: str
    revision: str
    effectiveDate: str        # ISO — transcribed from the sheet's dd.mm.yyyy
    reviewDate: str
    frequency: str            # DAILY | MONTHLY | QUARTERLY | ANNUAL
    assetType: str
    layout: str
    sections: list[Section]
    siteVariant: str | None = None   # only where two units' sheets genuinely differ
    sourceSheet: str = ""            # workbook sheet name, for traceability
    pageLabel: str = "1 of 1"
    department: str = "EHS"
    signOffRoles: tuple[str, str, str] = (
        "Prepared by: Person In-charge",
        "Reviewed by: Intermediatory Head",
        "Approved by: HOD",
    )
    # Free-text lines the sheet prints below the item table (revision notes,
    # operating rules, per-column signature captions). Reproduced on the PDF;
    # not answerable.
    footnotes: list[str] = field(default_factory=list)

    @property
    def items(self) -> list[Item]:
        return [i for s in self.sections for i in s.items]


# ═══════════════════════════════════════════════════════════════════════════
# PIL/EHS/CL/025 — Fire Alarm System   (6 sheets)
# ═══════════════════════════════════════════════════════════════════════════
_FAS_EFF, _FAS_REV = "2025-04-01", "2028-03-31"
_FAS_SUP = "PIL/EHS/CL/002-R0"

FAS_DAILY = TemplateDef(
    code="PIL-FAS-DAILY",
    name="Daily Fire Alarm System Inspection Checklist",
    documentNo="PIL/EHS/CL/025-R1 (A)",
    supersedesNo=_FAS_SUP, revision="R1",
    effectiveDate=_FAS_EFF, reviewDate=_FAS_REV,
    frequency="DAILY", assetType=FIRE_ALARM_PANEL, layout=LAYOUT_DAY_GRID,
    sourceSheet="Daily",
    sections=[
        Section("Daily Attention", [
            Item("fas_d_01", "Wheather the panel indicates normal operation", nc_severity="MAJOR_NC"),
            Item("fas_d_02", "FAS faults are updated in the register"),
            Item("fas_d_03", "Fire alarm control unit is powered up.", nc_severity="MAJOR_NC"),
            Item("fas_d_04", "Power indicator lamp is available."),
            Item("fas_d_05", "Fire alarm zone lamps are working properly.", nc_severity="MAJOR_NC"),
            Item("fas_d_06", "Other indicators are working properly."),
            Item("fas_d_07", "Fire hooter in fire panel is working properly.", nc_severity="MAJOR_NC"),
        ]),
    ],
    # The sheet carries an Electrician and a Safety Officer signature row per date
    # column, distinct from the page-foot Prepared/Reviewed/Approved block.
    footnotes=["Electrician Signature", "Safety Officer signature"],
)


# The two units' monthly sheets are identical except for the triggered-device
# addressing (Unit-21 A is a ZONE panel, Unit-21 B is a LOOP panel) and the hooter
# location list. That is exactly what `siteVariant` is for — two templates, one
# document number, selected by the panel's own `assetSubtype`.
def _fas_monthly(variant: str, addressing: str, hooters: list[str], batteries: list[str]) -> TemplateDef:
    return TemplateDef(
        code=f"PIL-FAS-MONTHLY-{variant}",
        name=(
            "Automatic Fire Detection & Alarm System - Inspecting, Testing & Maintenance "
            f"({variant.replace('_', '-')})"
        ),
        documentNo="PIL/EHS/CL/025-R1 (B)",
        supersedesNo=_FAS_SUP, revision="R1",
        effectiveDate=_FAS_EFF, reviewDate=_FAS_REV,
        frequency="MONTHLY", assetType=FIRE_ALARM_PANEL, layout=LAYOUT_FORM,
        siteVariant=variant,
        sourceSheet=f"Monthly {variant.replace('_', '-')}",
        sections=[
            Section("Monthly Attention", [
                Item("fas_m_01", "Trigger device or end of line switch on one zone circuit has been operated "
                                 "to test the ability of the control & indicating equipment to receive a singal "
                                 "& to sound the alarm & operate a signal warning devices.", nc_severity="MAJOR_NC"),
                Item("fas_m_02", "Visual examination of the battery & connection has been made."),
                Item("fas_m_03", "Action has been taken to correct the defects including low electrolyte level."),
                Item("fas_m_04", "Manual call point is in working condition.", nc_severity="MAJOR_NC"),
            ]),
            Section("Battery Details", [
                Item(f"fas_m_batt_{b.lower()}_vdc", f"Battery ({b}) = ______ VDC", type="NUMERIC", mandatory=False)
                for b in batteries
            ]),
            Section("1. Triggered Device", [
                # `addressing` is the ONLY structural difference between the two
                # unit sheets: Zone Number on 21 A, Loop Number on 21 B.
                Item("fas_m_trig_addr", f"{addressing} :", type="TEXT", triggers_finding=False),
                Item("fas_m_trig_detector", "Smoke Detector Num / Resistor", type="TEXT", triggers_finding=False),
                Item("fas_m_trig_detector_signal", "Is FAS Panel receiving signal & sounding alarm", nc_severity="MAJOR_NC"),
                Item("fas_m_trig_mcp", "MCP Num", type="TEXT", triggers_finding=False),
                Item("fas_m_trig_mcp_signal", "Is FAS Panel receiving signal & sounding alarm (MCP)", nc_severity="MAJOR_NC"),
                Item("fas_m_trig_open_serial", "Serial num of Detector removed to check open fault", type="TEXT", triggers_finding=False),
                Item("fas_m_trig_open_fault", "Open fault shown in the FAS Panel ( YES/NO )"),
            ]),
            Section("2. Visual Examination Of Battery", [
                Item("fas_m_batt_terminals", "Are the terminals connected properly ?"),
                Item("fas_m_batt_damage", "Batteries are not damaged and free from cracks"),
                Item("fas_m_batt_wires", "Are wires free from damage ?"),
                Item("fas_m_batt_leak", "Are Batteries free from leakage ?"),
                Item("fas_m_batt_corrosion", "Are batteries free from corrossion ?"),
                Item("fas_m_batt_voltage", "What is the measured voltage ?", type="NUMERIC", triggers_finding=False),
            ]),
            Section("Hooter Location - Audible to all areas (YES / NO)", [
                Item(f"fas_m_hooter_{i:02d}", loc)
                for i, loc in enumerate(hooters, start=1)
            ]),
            Section("Additional Hooters", [
                Item("fas_m_hooter_addl_required", "Is additional hooters required ?", triggers_finding=False,
                     no_finding_reason=(
                         'Inverted polarity: "No" means no extra hooters are needed, which is '
                         'the healthy answer here. A CAPA raised on "No" would be backwards.'
                     )),
                Item("fas_m_hooter_addl_location", "If required, for which location ?", type="TEXT", mandatory=False, triggers_finding=False),
                Item("fas_m_hooter_addl_count", "How many hooters are required ?", type="NUMERIC", mandatory=False, triggers_finding=False),
            ]),
        ],
    )


FAS_MONTHLY_21A = _fas_monthly(
    "UNIT_21_A", "Zone Number",
    ["Near Security room", "Near Dyeing Emergency exit", "Near Chemical Room",
     "Weaving Deparment", "Warping Department", "Near STP/ETP"],
    ["B1", "B2"],
)

FAS_MONTHLY_21B = _fas_monthly(
    "UNIT_21_B", "Loop Number",
    ["Near Security room", "Dyeing  Machine area", "Boiler Room", "ETP Area", "Canteen"],
    ["B1", "B2"],
)

FAS_QUARTERLY = TemplateDef(
    code="PIL-FAS-QUARTERLY",
    name="Quarterly Fire Alarm System Inspection Checklist",
    documentNo="PIL/EHS/CL/025-R1 (C)",
    supersedesNo=_FAS_SUP, revision="R1",
    effectiveDate=_FAS_EFF, reviewDate=_FAS_REV,
    frequency="QUARTERLY", assetType=FIRE_ALARM_PANEL, layout=LAYOUT_QUARTER_GRID,
    siteVariant="UNIT_21_A", sourceSheet="Quarterly-21A",
    sections=[
        Section("Quarterly Attention", [
            Item("fas_q_01", "Test battery and their connections to ensure that they are in  good serviceable condition."),
            Item("fas_q_02", "All detectors are cleaned in the quarter"),
            Item("fas_q_03", "Carry out visual inspection to ensure that structural or occupany change have not "
                             "affected the operation of Manual call point, Smoke Detector, Heat Detector "
                             "and/or Beam Detector."),
        ]),
        # The sheet's battery table is Description x (Make, Volt, Voltage at
        # inspection, Condition). Flattened to one item per cell so every cell is
        # separately answerable and the PDF rebuilds the table from the same data
        # rather than from a second storage shape.
        Section("Battery Readings", [
            it
            for slug, label in (
                ("b1", "Battery 01 (Main Panel)"), ("b2", "Battery 02 (Main Panel)"),
                ("b3", "Battery 01 (Repeater Panel)"), ("b4", "Battery 02 (Repeater Panel)"),
            )
            for it in (
                Item(f"fas_q_{slug}_make", f"{label} - Make", type="TEXT", mandatory=False),
                Item(f"fas_q_{slug}_rated", f"{label} - Volt (rated)", type="NUMERIC", mandatory=False),
                Item(f"fas_q_{slug}_measured", f"{label} - Voltage at the time of inspection",
                     type="NUMERIC", mandatory=False),
                Item(f"fas_q_{slug}_condition", f"{label} - Battery Condition", type="TEXT", mandatory=False),
            )
        ]),
        Section("Battery Endurance Test", [
            Item("fas_q_cutoff_at", "Raw power cut off at (time / date)", type="TEXT", mandatory=False, triggers_finding=False),
            Item("fas_q_supported_till", "After raw power cutoff, fully charged battery supported the system "
                                         "till (time / date)", type="TEXT", mandatory=False, triggers_finding=False),
            Item("fas_q_alarm_minutes", "Minutes battery sustained on sounding the alarm",
                 type="NUMERIC", mandatory=False, triggers_finding=False),
        ]),
        Section("Sound from hooters is 80dB(A) at all locations (Yes/No)", [
            Item("fas_q_db_warping", "Warping"),
            Item("fas_q_db_weaving", "Weaving"),
            Item("fas_q_db_dyeing", "Dyeing"),
            Item("fas_q_db_hrd", "HRD"),
            Item("fas_q_db_etp", "ETP"),
        ]),
        Section("Additional Hooters", [
            Item("fas_q_hooter_addl_required", "Is additional hooters required ?", triggers_finding=False,
                     no_finding_reason=(
                         'Inverted polarity: "No" means no extra hooters are needed, which is '
                         'the healthy answer here. A CAPA raised on "No" would be backwards.'
                     )),
            Item("fas_q_hooter_addl_location", "If required, for which location ?", type="TEXT", mandatory=False, triggers_finding=False),
            Item("fas_q_hooter_addl_count", "How many hooters are required ?", type="NUMERIC", mandatory=False, triggers_finding=False),
        ]),
        Section("Closing", [
            Item("fas_q_deviations", "Description of Deviations (if any):", type="TEXT", mandatory=False, triggers_finding=False),
            Item("fas_q_inspected_by", "Inspected By (Name)", type="TEXT", mandatory=False, triggers_finding=False),
        ]),
    ],
)

FAS_ANNUAL = TemplateDef(
    code="PIL-FAS-ANNUAL",
    name="Annually Fire Alarm System Inspection Checklist",
    documentNo="PIL/EHS/CL/025-R1 (D)",
    supersedesNo=_FAS_SUP, revision="R1",
    effectiveDate=_FAS_EFF, reviewDate=_FAS_REV,
    frequency="ANNUAL", assetType=FIRE_ALARM_PANEL, layout=LAYOUT_FORM,
    sourceSheet="Annually",
    sections=[
        Section("Annual Attention", [
            Item("fas_a_01", "Test the operation of atleast 20 percent of the detectors in the entire installation.",
                 guidance="Selection should be done in such a way that all the detectors in an installation "
                          "must be checked once in every 5 years"),
            Item("fas_a_02", "Observe for insulation damage in power cable and FAS cable."),
            Item("fas_a_03", "Ensure that joints are properly connected using jointers in power cable and FAS cable."),
            Item("fas_a_04", "Maintenance records for UPS is available."),
            Item("fas_a_05", "Trip and check MCB.", guidance="In LT Panel and near FAS Panel"),
            Item("fas_a_06", "Check the termination of cables in FAS panel."),
        ]),
        Section(
            "Detector Sample",
            [
                Item("fas_a_total_detectors", "Total detectors in the installation",
                     type="NUMERIC", mandatory=False, triggers_finding=False),
                Item("fas_a_sample_size", "20 % - No's of detectors to be checked annually",
                     type="NUMERIC", mandatory=False, triggers_finding=False),
            ],
            note="20 % of ______ Detectors = ______ No's of detectors to be checked annually",
        ),
        Section("Triggered device details - Zone / Loop", [
            it
            for n in range(1, 9)
            for it in (
                Item(f"fas_a_z{n}_count", f"Zone {n}/Loop {n} - No. of detectors checked",
                     type="NUMERIC", mandatory=False),
                Item(f"fas_a_z{n}_serials", f"Zone {n}/Loop {n} - Serial No. of checked detectors",
                     type="TEXT", mandatory=False),
                Item(f"fas_a_z{n}_remarks", f"Zone {n}/Loop {n} - Remarks", type="TEXT", mandatory=False),
            )
        ]),
        Section("Closing", [
            Item("fas_a_deviations", "Description of Deviations (if any):", type="TEXT", mandatory=False, triggers_finding=False),
            Item("fas_a_inspected_by", "Inspected By (Name)", type="TEXT", mandatory=False, triggers_finding=False),
        ]),
    ],
)

FAS_BEAM_DAILY = TemplateDef(
    code="PIL-FAS-BEAM-DAILY",
    name="Beam Detector - Inspecting, Testing, Maintenance (Daily)",
    documentNo="PIL/EHS/CL/025-R1 (E)",
    supersedesNo=_FAS_SUP, revision="R1",
    effectiveDate=_FAS_EFF, reviewDate=_FAS_REV,
    frequency="DAILY", assetType=BEAM_DETECTOR, layout=LAYOUT_DAY_GRID,
    sourceSheet="Beam Detector",
    sections=[
        Section("Daily Attention", [
            Item("beam_d_01", "Detector securly fastened to the beam.", nc_severity="MAJOR_NC"),
            Item("beam_d_02", "Free from physical damage, dust and dirt accumulation."),
            Item("beam_d_03", "Power Cables connected properly.", nc_severity="MAJOR_NC"),
            Item("beam_d_04", "If any obstructions indication light working properly."),
            Item("beam_d_05", "Reflectiors are in deserved place and free from damages."),
        ]),
    ],
    footnotes=["ELECTRICIAN SIGNATURE", "SAFETY OFFICER SIGNATURE"],
)


# ═══════════════════════════════════════════════════════════════════════════
# PIL/EHSD/CL/026 — Fire Hydrant & Sprinkler System   (4 sheets)
# ═══════════════════════════════════════════════════════════════════════════
_FHS_SUP = "PIL/EHSD/CL/003-R0"
_FHS_EFF = "2024-09-04"

FHS_DAILY = TemplateDef(
    code="PIL-FHS-DAILY",
    name="Daily Fire Hydrant System Maintenance Checklist",
    documentNo="PIL/EHSD/CL/026-R2",
    supersedesNo=_FHS_SUP, revision="R2",
    effectiveDate=_FHS_EFF, reviewDate="2026-07-28",
    frequency="DAILY", assetType=HYDRANT_SYSTEM, layout=LAYOUT_DAY_GRID,
    sourceSheet="Daily",
    sections=[
        Section("Descriptions", [
            Item("fhs_d_01", "No leakages found in System."),
            Item("fhs_d_02", "Design pressure is maintained in the system", nc_severity="MAJOR_NC"),
            Item("fhs_d_03", "Discharge valve is open .", nc_severity="MAJOR_NC"),
            Item("fhs_d_04", "Pumps are set in auto mode", nc_severity="MAJOR_NC"),
            Item("fhs_d_05", "Power indicator lamp on the panel is in working condition"),
            Item("fhs_d_06", "Pumps are tested in auto mode by reducing the pressure using test line"),
            Item("fhs_d_07", "Water level in FHS tank (Minimum 95%)", nc_severity="MAJOR_NC"),
            Item("fhs_d_08", "Check the pump glands, packing's, etc., and replace the damaged gland for packing "
                             "whenever found damaged or worn out .",
                 guidance="Leakage from glands at specified OEM rate  is allowable"),
        ]),
    ],
    footnotes=[
        "CHECKED BY:- [SAFETY OFFICER]",
        "Revision details: Fire hydrant tank water level checking added in the check point",
    ],
)

FHS_MONTHLY = TemplateDef(
    code="PIL-FHS-MONTHLY",
    name="Monthly Fire Hydrant System Maintenance Checklist",
    documentNo="PIL/EHSD/CL/026-R2",
    supersedesNo=_FHS_SUP, revision="R2",
    effectiveDate=_FHS_EFF, reviewDate="2026-07-28",
    frequency="MONTHLY", assetType=HYDRANT_SYSTEM, layout=LAYOUT_FORM,
    sourceSheet="Monthly",
    sections=[
        Section("Valves:", [
            Item("fhs_m_v1", "The valves spindle should be checked to identify signs of excessive wear "
                             "including leakage in the gland."),
            Item("fhs_m_v2", "Open the valve slightly to see that water is flowing freely and there is no "
                             "obstruction in the outlet."),
            Item("fhs_m_v3", "All cut off (isolating) valves are operated and oiled."),
            Item("fhs_m_v4", "Lip washer, lugs and blank caps are in good condition."),
            Item("fhs_m_v5", "Spindle and lugs move freely."),
        ]),
        Section("Hydrant Box:", [
            Item("fhs_m_h1", "All hydrant boxes are equipped with hoses and nozzles.", nc_severity="MAJOR_NC"),
            Item("fhs_m_h2", "Nozzles are in good condition"),
            Item("fhs_m_h3", "Hose boxes are in good condition"),
            Item("fhs_m_h4", "Hydrant points and hose boxes are easily accessible and free from obstruction"),
            Item("fhs_m_h5", "The hose box key is present, available, and readily accessible"),
        ]),
        Section("Pump Room:", [
            Item("fhs_m_p1", "Check water level in FHS sump/OHT", nc_severity="MAJOR_NC"),
            Item("fhs_m_p2", "Coupling and shaft is guarded"),
            Item("fhs_m_p3", "Check alignment of pump motors, nuts, bolts, couplings etc."),
            Item("fhs_m_p4", "Quantity of fuel in day tank is at desired level"),
            Item("fhs_m_p5", "Water is available in priming tank"),
        ]),
        Section("Others:", [
            Item("fhs_m_o1", "Open hydrant or valve pit to ensure that it is clean and not filled with any dirt "
                             "or leaking water.",
                 guidance="If the pit is full of water, it should be emptied and cleaned."),
            Item("fhs_m_o2", "Pressure gauges are in good working condition"),
            Item("fhs_m_o3", "The paint work of the hydrants, pit covers, indicator plates, etc, are in good "
                             "condition & free of corrosion."),
            Item("fhs_m_o4", "Fire hydrant supports are in good condition."),
        ]),
        Section("Water Monitor:", [
            Item("fhs_m_w1", "Water monitor rotates 360 degree"),
        ]),
    ],
    footnotes=["Revision details: Hose box key availability is added in the checkpoints"],
)

FHS_QUARTERLY = TemplateDef(
    code="PIL-FHS-QUARTERLY",
    name="Quarterly Fire Hydrant System Maintenance Checklist",
    documentNo="PIL/EHSD/CL/026-R1",
    supersedesNo=_FHS_SUP, revision="R1",
    effectiveDate=_FHS_EFF, reviewDate="2027-09-03",
    frequency="QUARTERLY", assetType=HYDRANT_SYSTEM, layout=LAYOUT_QUARTER_GRID,
    sourceSheet="Quarterly",
    sections=[
        Section("Quarterly attention", [
            Item("fhs_q_01", "Hydrant mains are tested at system design pressure (Farthest point)", nc_severity="MAJOR_NC"),
            Item("fhs_q_02", "All first aid hose reels and RRL are in good condition "
                             "(Check 25% of hose reels each quarter)"),
        ]),
        Section("Closing", [
            Item("fhs_q_inspected_by", "Inspected By", type="TEXT", mandatory=False, triggers_finding=False),
            Item("fhs_q_observation", "Any Observation if.?", type="TEXT", mandatory=False, triggers_finding=False),
        ]),
    ],
)

FHS_YEARLY = TemplateDef(
    code="PIL-FHS-YEARLY",
    name="Yearly Fire Hydrant System Maintenance Checklist",
    documentNo="PIL/EHSD/CL/026-R1 (D)",
    supersedesNo=_FHS_SUP, revision="R1",
    effectiveDate=_FHS_EFF, reviewDate="2027-09-03",
    frequency="ANNUAL", assetType=HYDRANT_SYSTEM, layout=LAYOUT_FORM,
    sourceSheet="Yearly",
    sections=[
        Section("Yearly Attention", [
            Item("fhs_y_01", "Check the insulation resistance of pump motor circuit"),
        ]),
        Section("Closing", [
            Item("fhs_y_inspected_by", "Inspected By", type="TEXT", mandatory=False, triggers_finding=False),
            Item("fhs_y_observation", "Any Observation (If):", type="TEXT", mandatory=False, triggers_finding=False),
        ]),
    ],
    footnotes=[
        "Note: Follow these simple rules whenever hydrants are operated:",
        "1. Open the hydrant valves slowly, specially, if the hose is connected directly to a branch.",
        "2. Close the valve slowly to prevent water hammer and a possible main burst.",
        "3. After use, ensure that the hydrant valve is properly closed and there is no leakage.",
        "4. The stand pipe or hose should not be disconnected from a hydrant in which no water is available "
        "or from which the flow has suddenly stopped.",
    ],
)


# ═══════════════════════════════════════════════════════════════════════════
# PIL/EHSD/CL/027 — Fire Extinguisher Inspection Checklist
# ═══════════════════════════════════════════════════════════════════════════
FE_INSPECTION = TemplateDef(
    code="PIL-FE-INSPECTION",
    name="Fire Extinguisher Inspection Checklist",
    documentNo="PIL/EHSD/CL/027-R1",
    supersedesNo="PIL/EHSD/CL/004-R0", revision="R1",
    effectiveDate="2024-09-05", reviewDate="2027-09-04",
    # Monthly cadence rendered as a 12-month year grid — the sheet is one page
    # per extinguisher per year with a column per month.
    frequency="MONTHLY", assetType=FIRE_EXTINGUISHER, layout=LAYOUT_MONTH_GRID,
    sourceSheet="FE Checklist",
    sections=[
        Section("Checks to be done", [
            Item("fe_01", "Exterior body (No damages /cracks/corrosion)"),
            Item("fe_02", "Paint in good condition"),
            Item("fe_03", "Identification Number Marked and Readable"),
            Item("fe_04", "Cap (No cracks, vent holes free, polish)"),
            Item("fe_05", "Discharge hose/horn/nozzle in good condition"),
            # The one check on this sheet that is a compliance breach rather than
            # a housekeeping defect: an over-due extinguisher is not a working
            # extinguisher. The single item seeded to raise a CAMS finding.
            Item("fe_06", "Fire Extinguisher refilled within due date", triggers_finding=True, nc_severity="MAJOR_NC"),
            Item("fe_07", "Fire Extinguisher clear of blockages"),
            Item("fe_08", "Fire Extinguisher is easily accessible"),
            Item("fe_09", "Safety pin / clip properly fixed"),
            Item("fe_10", "Carrying handle in good condition"),
            Item("fe_11", "Inspection tag in place."),
            Item("fe_12", "Plunger & piercer free movement."),
            Item("fe_13", "Squeeze lever / Knob is in good condition"),
            Item("fe_14", "Pressure guage in good condition & needle in green zone", nc_severity="MAJOR_NC"),
            Item("fe_15", "Fire Extinguisher Trolley / Stand is in good condition"),
            Item("fe_16", "Visibility markings are available"),
            Item("fe_17", "Fire extinguishers Signage boards are displayed"),
            Item("fe_18", "Manufacturer name is visible"),
            Item("fe_19", "Year of Manufacturing and Serial number is visible"),
            Item("fe_20", "ISI mark available on fire extinguisher is available"),
            Item("fe_21", "Operation instructions sticker is available on body"),
        ]),
    ],
    footnotes=[
        'Write "Yes" if satisfactory; "No" if unsatisfactory; "NA" if not applicable '
        "(write comments on back side of this page)",
    ],
)


ALL_TEMPLATES: list[TemplateDef] = [
    FAS_DAILY, FAS_MONTHLY_21A, FAS_MONTHLY_21B, FAS_QUARTERLY, FAS_ANNUAL, FAS_BEAM_DAILY,
    FHS_DAILY, FHS_MONTHLY, FHS_QUARTERLY, FHS_YEARLY,
    FE_INSPECTION,
]

BY_CODE: dict[str, TemplateDef] = {t.code: t for t in ALL_TEMPLATES}


def for_asset_type(asset_type: str) -> list[TemplateDef]:
    return [t for t in ALL_TEMPLATES if t.assetType == asset_type]


# ── Register of Fire Extinguishers — PIL/EHSD/CL/028-R1 ──────────────────────
# Not a checklist: the register IS the FireEquipment asset master, so this is
# document metadata only, used by the register screen header and its PDF export.
FE_REGISTER_DOC = {
    "documentNo": "PIL/EHSD/CL/028-R1",
    "supersedesNo": "PIL/EHSD/CL/092-R0",
    "revision": "R1",
    "effectiveDate": "2024-09-05",
    "reviewDate": "2027-09-04",
    "title": "REGISTER OF FIRE EXTINGUISHERS",
    "department": "EHS",
    # Column order transcribed from the sheet, so the screen and the PDF present
    # the register in the order the client's own auditors read it.
    "columns": [
        ("slNo", "Sl. No"), ("serialNo", "Manufacturer Serial No."), ("type", "Type"),
        ("capacity", "Capacity"), ("yearOfManufacture", "Year Manufacture"), ("expiryDate", "Expiry Date"),
        ("make", "Make"), ("allottedSerialNo", "Alloted Serial No."), ("location", "Location"),
        ("hpTestedOn", "HP tested on"), ("hpTestDueDate", "HP Test due date"),
        ("dateOfDischarge", "Date of Discharge"), ("refilledOn", "Refilled on"),
        ("dueForRefilling", "Due for refilling"), ("weightKg", "Weight in Kgs"), ("remarks", "Remarks"),
    ],
}

__all__ = [
    "Item", "Section", "TemplateDef", "ALL_TEMPLATES", "BY_CODE", "for_asset_type",
    "FE_REGISTER_DOC", "LAYOUT_DAY_GRID", "LAYOUT_MONTH_GRID", "LAYOUT_QUARTER_GRID", "LAYOUT_FORM",
    "FIRE_ALARM_PANEL", "BEAM_DETECTOR", "HYDRANT_SYSTEM", "FIRE_EXTINGUISHER",
]
