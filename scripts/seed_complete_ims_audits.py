"""Seed TWO complete QMS/EMS/OHS (PAGE_IMS) audits, own facility, end to end.

The MANAGEMENT_SYSTEMS category is the one Page conduct **by department**: HR,
Administration and the Occupational Health Centre are each assessed against
BOTH source sheets — the IMS one (ISO 9001 / 14001 / 45001) and the EnMS one
(ISO 50001) — for 206 checkpoints in total. See docs/cams/20.

`seed_complete_internal_audit.py` does this for the INTERNAL category. This is
its counterpart, and it has to cover four things that category does not have:

  * **tristate grading** — Conformance / Non-Conformance / Observation, the
    customer's own column header, rather than the seven-value status ladder
  * **two streams per audit** — IMS and EnMS, each issuing its OWN report, so
    every audit here ends with FOUR reports (interim + final, twice)
  * **three departments** — each with its own auditee owner, its own rollup and
    its own discipline sign-off
  * **PIL/MR/F04-R1 NC reports** — every non-conformity carries a numbered NC
    report with a Why-Why root cause analysis gating its Correction and
    Preventive Action, closed by two signatures

Two audits, deliberately different in their END STATE:

  1. **H1 FY26** — closed, and every NC report closed with it. What a healthy
     completed cycle looks like: 100% of NCs signed off by the M.R.
  2. **H2 FY26** — closed, with its NC reports spread across EVERY stage of the
     register, including one that failed verification and looped back. An audit
     closes when the fieldwork is accepted; its corrective actions outlive it,
     and a demo where every NC is tidily shut hides the screen's whole purpose.

Everything is populated: opening and closing meetings, frozen auditor
competence, an observation on every one of the 206 checkpoints, evidence,
iteration threads through all four auditee branches, per-department and
per-stream rollups, sign-offs, and the full NC → RCA → CAPA → verification →
closure chain.

**Evidence.** Real generated images uploaded to Supabase storage when Pillow and
SUPABASE_* are both available. When they are not, the seeder still runs and says
so — checkpoint evidence is then metadata-only. It never writes a photo record
with a fabricated storagePath, because a dead reference is worse than an honest
absence.

Idempotent: both audits are keyed by title and rebuilt on every run, along with
their meetings, findings, NC reports, RCAs and CAPAs.

Run from the backend root:
    python scripts/seed_complete_ims_audits.py
    python scripts/seed_complete_ims_audits.py --audit 1     # just the first
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Running a FILE puts scripts/ on sys.path, not the backend root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.models.assurance import EngagementCompetenceSnapshot  # noqa: E402
from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit  # noqa: E402
from app.models.capa import Capa, CapaAction, CapaRootCause  # noqa: E402
from app.models.competency_matrix import Competency, CompetencyRecord  # noqa: E402
from app.models.cams_completion import AuditFinding  # noqa: E402
from app.models.plant import Plant  # noqa: E402
from app.models.rca import RootCauseAnalysis  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import assurance, nc_rca_capa, signoff  # noqa: E402
from app.services import audit_compliance as svc  # noqa: E402
from app.services import page_grading as pg  # noqa: E402

INDUSTRY_CODE = "PAGE_IMS"
PLANT_CODE = "NW"
DEPARTMENTS = ["DEPT_HR", "DEPT_ADMIN", "DEPT_OHC"]
DEPT_LABEL = {
    "DEPT_HR": "Human Resources",
    "DEPT_ADMIN": "Administration",
    "DEPT_OHC": "Occupational Health Centre",
}
STREAMS = ["IMS", "ENMS"]

AUDITS = [
    {
        "key": "H1",
        "title": "QMS, EMS & OHS Integrated Audit — H1 FY26 (HR, Admin, OHC)",
        "days_ago": 145,
        # Every NC report driven to CLOSED.
        "nc_profile": "ALL_CLOSED",
    },
    {
        "key": "H2",
        "title": "QMS, EMS & OHS Integrated Audit — H2 FY26 (HR, Admin, OHC)",
        "days_ago": 38,
        # NC reports spread across every stage the register can show.
        "nc_profile": "MIXED",
    },
]

# Verdict mix per audit. H2 is the later cycle and shows fewer NCs — a
# programme that never improves is not a programme, and a reviewer comparing
# the two should be able to see the trend that makes the register worth reading.
MIX = {
    "H1": {"NA": 0.04, "NON_CONFORMANCE": 0.085, "OBSERVATION": 0.15},
    "H2": {"NA": 0.04, "NON_CONFORMANCE": 0.055, "OBSERVATION": 0.12},
}

EVIDENCE_URL_TTL_SEC = 10 * 365 * 86400


# ─────────────────────────────────────────────────────────────────────
# Deterministic verdict allocation
# ─────────────────────────────────────────────────────────────────────
def _roll(audit_key: str, code: str) -> float:
    """A stable 0..1 for one checkpoint in one audit.

    Hash-derived rather than `random`, so two runs of this seeder produce the
    same audit. A demo whose non-conformities move every time it is re-seeded
    cannot be walked through from a script, and a screenshot taken on Monday
    stops matching the data on Tuesday.
    """
    h = hashlib.sha256(f"{audit_key}:{code}".encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def verdict_for(audit_key: str, code: str, criticality: str) -> str:
    """CONFORMANCE | NON_CONFORMANCE | OBSERVATION | NA."""
    r = _roll(audit_key, code)
    m = MIX[audit_key]
    if r < m["NA"]:
        # N/A concentrates on the EnMS sheet in HR and OHC, which is where a
        # real audit finds lines that do not apply — an energy-baseline
        # question has no meaning in a first-aid room.
        return "NA"
    if r < m["NA"] + m["NON_CONFORMANCE"]:
        return pg.TRI_NON_CONFORMANCE
    if r < m["NA"] + m["NON_CONFORMANCE"] + m["OBSERVATION"]:
        return pg.TRI_OBSERVATION
    return pg.TRI_CONFORMANCE


def risk_for(criticality: str) -> str:
    return {"critical": "HIGH", "major": "MEDIUM"}.get(criticality, "LOW")


# ─────────────────────────────────────────────────────────────────────
# Observation copy — one for EVERY checkpoint, not a shared placeholder
# ─────────────────────────────────────────────────────────────────────
CONFORMANCE_NOTES = [
    "Verified against records for the sampled period. Documents current, "
    "approved and available at the point of use; responsible person able to "
    "explain the process without reference to the procedure.",
    "Sampled {n} records across the review period — all complete, authorised "
    "and retained per the retention schedule. No gaps found.",
    "Objective evidence sighted and cross-checked against the master register. "
    "Revision status matches the controlled copy; superseded versions withdrawn.",
    "Process demonstrated on site during the visit. Records traceable end to "
    "end and consistent with what the procedure requires.",
    "Reviewed with the process owner. Monitoring data available for the full "
    "period, trended, and reviewed at the departmental meeting.",
    "Training records, competence evidence and the authorisation list agree. "
    "The person performing the activity is on the approved list.",
]

OBSERVATION_NOTES = [
    "Compliant, but the record is maintained manually alongside the system "
    "entry. Duplication risks the two disagreeing — recommend retiring the "
    "manual copy once the system record is confirmed complete.",
    "Requirement met. Two of the {n} sampled entries were completed late "
    "(within tolerance). Recommend a reminder ahead of the due date rather "
    "than after it.",
    "Evidence available but held locally by the process owner rather than in "
    "the controlled location. Recommend filing to the master register so it "
    "survives a change of role holder.",
    "Procedure followed correctly; the written procedure has not been reviewed "
    "since the last revision cycle and still references a superseded form "
    "number. Recommend updating at the next scheduled review.",
    "Practice is sound and better than the documented method. Recommend "
    "updating the procedure to reflect what is actually done, so a new starter "
    "is taught the current practice.",
    "Complied. Awareness among the wider team is uneven — the process owner is "
    "confident, the deputy less so. Recommend a short refresher for the "
    "backup role holder.",
]

NA_NOTES = [
    "Not applicable to this department for the period under review — the "
    "activity is not performed here. Confirmed with the department head and "
    "excluded from the score.",
    "Not applicable: this requirement addresses an aspect this department does "
    "not own. Verified against the aspects/impacts register; excluded from the "
    "denominator.",
]

# Non-conformity scenarios. Each is a complete NC report: what was found, the
# Why-Why ladder the auditee filled, the systemic root cause it reached, the
# Correction, the Preventive Action and the auditor's verification.
#
# The ladders are written to REACH THE SYSTEM, which is what PIL/MR/F04-R1 row
# 16 asks for ("What failed in the system to allow this nonconformity to
# occur"). A ladder that stops at "the officer forgot" is the failure mode the
# R1 revision exists to remove, so none of these do.
NC_SCENARIOS = [
    {
        "finding": "Management review minutes for Q2 do not record the status of "
                   "actions from the previous review. Clause requires follow-up "
                   "actions from previous management reviews as an input.",
        "whys": [
            ("Why was the management review input on previous actions not effective?",
             "The Q2 minutes template has no section for previous-action status, so it was not captured."),
            ("Why does the template have no section for it?",
             "The template was created from the Q1 agenda, which was itself an ad-hoc document."),
            ("Why was an ad-hoc document used as the template?",
             "No controlled template for management review minutes exists in the document master list."),
            ("Why is there no controlled template?",
             "Management review records were treated as meeting notes rather than as quality records."),
            ("Why were they treated as meeting notes?",
             "The document control procedure does not name management review minutes as a controlled record "
             "type."),
        ],
        "root_cause": "The document control procedure (QSP-04) does not classify management "
                      "review minutes as a controlled record, so no template was ever "
                      "authored, reviewed or issued for them.",
        "correction": "Q2 minutes reissued as revision 01 with a previous-action status "
                      "section completed retrospectively from the Q1 action log, "
                      "circulated to all attendees for confirmation.",
        "preventive": "Controlled template PIL/MR/F09 created for management review "
                      "minutes, carrying all clause 9.3.2 inputs as fixed headings; added "
                      "to the document master list and QSP-04 amended to name management "
                      "review minutes as a controlled record.",
        "verification": "Re-checked at the Q3 management review. Minutes issued on "
                        "PIL/MR/F09 with the previous-action status section completed and "
                        "all 9.3.2 inputs present. Document master list shows F09 at "
                        "revision 00, issued and controlled.",
    },
    {
        "finding": "Three of the twelve sampled induction records for new joiners "
                   "have no evidence of EHS awareness training, though the "
                   "induction checklist lists it as mandatory.",
        "whys": [
            ("Why was the induction process for achieving competence not effective?",
             "EHS awareness was delivered but the attendance sheet was not returned to HR for filing."),
            ("Why was the attendance sheet not returned?",
             "The EHS trainer files their own copy and there is no handover step to HR."),
            ("Why is there no handover step?",
             "The induction checklist names HR as the owner but does not say who supplies each piece of "
             "evidence."),
            ("Why does the checklist not assign evidence ownership?",
             "It was written as a coverage list for the joiner, not as a records checklist for the process."),
            ("Why was it written that way?",
             "The competence procedure defines what training must be given but not what record proves it was "
             "given."),
            ("Why does the procedure not define the record?",
             "Records requirements were left to each function to decide rather than being specified "
             "centrally."),
        ],
        "root_cause": "The competence procedure specifies required training but does not "
                      "specify the retained documented information that evidences it, so "
                      "each function keeps its own record and none is authoritative.",
        "correction": "The three joiners re-verified: two had attended and their sheets "
                      "were recovered from the trainer's file and added to the personnel "
                      "records; the third had genuinely not attended and completed EHS "
                      "awareness on 14 March, record filed.",
        "preventive": "Competence procedure amended to name the retained record for each "
                      "training type and its custodian. Induction checklist reissued with "
                      "an evidence column and a sign-off by HR before the record is "
                      "closed. Monthly reconciliation of joiners against training records "
                      "added to the HR dashboard.",
        "verification": "Sampled all 9 joiners since the corrective action. Every record "
                        "carries the EHS attendance sheet, signed by both the trainer and "
                        "HR. Monthly reconciliation evidenced for two cycles with nil "
                        "exceptions.",
    },
    {
        "finding": "The significant energy uses (SEU) register has not been "
                   "reviewed since the previous certification cycle, though two "
                   "new compressors were commissioned during the period.",
        "whys": [
            ("Why was the process for maintaining the energy review not effective?",
             "The SEU register is reviewed annually and the compressors were commissioned mid-cycle."),
            ("Why did a mid-cycle change not trigger a review?",
             "There is no trigger linking new energy-consuming equipment to the SEU register."),
            ("Why is there no trigger?",
             "The management-of-change procedure covers process and product changes, "
             "not utilities."),
            ("Why does it not cover utilities?",
             "Utilities are managed by Engineering, who are not named as a stakeholder "
             "in the MOC procedure."),
            ("Why is Engineering not named?",
             "The MOC procedure was written for the QMS scope and was not re-scoped "
             "when EnMS was integrated."),
        ],
        "root_cause": "The management-of-change procedure was never re-scoped when ISO "
                      "50001 was brought into the integrated system, so an energy-"
                      "significant change has no route into the energy review.",
        "correction": "SEU register updated to include both compressors, with baseline "
                      "consumption established from the six months of available meter "
                      "data and energy performance indicators recalculated.",
        "preventive": "MOC procedure re-scoped to the integrated system, adding Engineering "
                      "as a mandatory reviewer and an explicit 'energy significance' "
                      "screening question. Any change answering yes routes to the energy "
                      "manager before approval.",
        "verification": "Re-checked against the two changes raised since. Both carry the "
                        "energy significance screening; the chiller replacement was routed "
                        "to the energy manager and the SEU register updated at "
                        "commissioning rather than at year end.",
    },
    {
        "finding": "Emergency preparedness drill for the effluent treatment plant "
                   "was not conducted in the period. The plan requires a "
                   "half-yearly drill; the last recorded drill was 14 months ago.",
        "whys": [
            ("Why was the process for achieving emergency preparedness not effective?",
             "The ETP drill was not included in the annual drill calendar for the year."),
            ("Why was it not in the calendar?",
             "The calendar is built from the previous year's calendar, and the ETP drill was missed in that "
             "one too."),
            ("Why was it missed the previous year?",
             "The calendar is compiled by hand rather than generated from the emergency "
             "plan's own schedule."),
            ("Why is it compiled by hand?",
             "The emergency plan lists required drills in narrative text, with no schedule table to work "
             "from."),
            ("Why does the plan have no schedule table?",
             "The plan was written to satisfy the clause rather than to be operated from."),
        ],
        "root_cause": "The emergency preparedness plan records drill requirements as "
                      "narrative prose with no schedule, so the annual calendar is "
                      "reconstructed from memory each year and an omission propagates "
                      "forward indefinitely.",
        "correction": "ETP chemical spill and overflow drill conducted on 22 February with "
                      "the ETP operators, EHS and the OHC first-aid team; drill report and "
                      "learnings issued and the two gaps identified closed.",
        "preventive": "Emergency plan revised to carry a drill schedule table naming every "
                      "required drill, its frequency and its owner. The annual calendar is "
                      "now generated from that table, and the table is a controlled "
                      "document reviewed at management review.",
        "verification": "Drill schedule table verified in the revised plan (rev 03). The "
                        "current-year calendar reconciles line for line against it, "
                        "including the ETP drill. Two drills conducted on schedule since.",
    },
    {
        "finding": "Health surveillance records for employees in the high-noise "
                   "area show four audiometry tests overdue by more than three "
                   "months against the OHC schedule.",
        "whys": [
            ("Why was the process for achieving scheduled health surveillance not effective?",
             "The four employees transferred into the high-noise area and were not added to the surveillance "
             "list."),
            ("Why were they not added on transfer?",
             "The OHC surveillance list is updated from the annual manpower list, not from transfer "
             "notifications."),
            ("Why is it not updated from transfers?",
             "Internal transfers are processed by HR and the OHC is not on the notification list."),
            ("Why is the OHC not notified?",
             "The transfer process was designed around payroll and reporting-line changes only."),
            ("Why was it designed that way?",
             "No risk assessment was done on the transfer process itself, so its OHS consequences were never "
             "identified."),
        ],
        "root_cause": "The internal transfer process has never been risk-assessed for its "
                      "occupational-health consequences, so a change of exposure category "
                      "carries no obligation to notify the OHC.",
        "correction": "All four employees underwent audiometry within two weeks; results "
                      "reviewed by the occupational health physician, no threshold shift "
                      "detected. Surveillance list reconciled against the full current "
                      "manpower list, identifying no further omissions.",
        "preventive": "Transfer process amended so any move into or out of a designated "
                      "exposure area raises an automatic OHC notification, with the "
                      "surveillance list updated before the transfer takes effect. "
                      "Quarterly reconciliation of the exposure register against the "
                      "surveillance list added to the OHC plan.",
        "verification": "Re-checked at the quarterly reconciliation. Six transfers into "
                        "designated areas since the action, all six notified to the OHC "
                        "and added to surveillance before the effective date. No overdue "
                        "tests on the current list.",
    },
    {
        "finding": "The legal and other requirements register was last updated "
                   "nine months ago and does not reflect the revised state "
                   "hazardous waste rules notified during the period.",
        "whys": [
            ("Why was the process for maintaining legal requirements not effective?",
             "The register is updated when the EHS officer becomes aware of a change; the notification was "
             "not seen."),
            ("Why was the notification not seen?",
             "There is no subscription to a regulatory update service; awareness depends on industry "
             "contacts."),
            ("Why is there no subscription?",
             "The need was raised previously but no budget owner was identified."),
            ("Why was no budget owner identified?",
             "Compliance monitoring is not a line item in any department's budget."),
            ("Why is it not a line item?",
             "The compliance obligation procedure assigns responsibility for the register but not the "
             "resources to maintain it."),
        ],
        "root_cause": "The compliance obligation procedure assigns accountability for the "
                      "legal register without assigning the resource to keep it current, "
                      "so maintenance depends on one person's informal network.",
        "correction": "Register updated against the revised hazardous waste rules; the four "
                      "affected obligations reassessed, and the two requiring a change to "
                      "the waste manifest process actioned. Compliance evaluation redone "
                      "for the period.",
        "preventive": "Annual subscription to a regulatory tracking service approved and "
                      "budgeted under EHS. Procedure amended to require a quarterly "
                      "documented review of the register against the service feed, "
                      "reported at management review.",
        "verification": "Subscription active and evidenced. Two quarterly reviews completed "
                        "and minuted, each identifying and dispositioning changes. Register "
                        "reconciles against the current regulatory position.",
    },
    {
        "finding": "Calibration status could not be demonstrated for two of the "
                   "eight sampled monitoring instruments in the ETP laboratory; "
                   "calibration labels were illegible and no certificate was "
                   "produced during the audit.",
        "whys": [
            ("Why was the process for achieving valid measurement results not effective?",
             "The calibration certificates for both instruments are held by the vendor and were never "
             "received."),
            ("Why were they never received?",
             "The purchase order for calibration does not require the certificate as a deliverable."),
            ("Why does the PO not require it?",
             "Calibration is raised as a service PO, and service POs have no deliverables field."),
            ("Why do service POs have no deliverables field?",
             "The procurement procedure distinguishes goods from services but defines acceptance criteria "
             "only for goods."),
            ("Why only for goods?",
             "The procedure was written before calibration was outsourced and has not been revised since."),
        ],
        "root_cause": "The procurement procedure defines acceptance criteria for goods only. "
                      "Calibration became an outsourced service afterwards and no "
                      "acceptance criterion — the certificate — was ever defined for it.",
        "correction": "Both instruments recalled and recalibrated by the vendor; "
                      "certificates obtained and filed, and labels replaced with the "
                      "durable type. Results produced by both instruments since the last "
                      "valid calibration reviewed — no out-of-tolerance impact on reported "
                      "effluent results.",
        "preventive": "Procurement procedure revised to define acceptance criteria for "
                      "outsourced services, with the calibration certificate named as a "
                      "mandatory deliverable and payment released only against it. "
                      "Calibration register extended to track certificate receipt "
                      "separately from calibration date.",
        "verification": "All instruments calibrated since the action have certificates on "
                        "file, received before payment release. Register shows certificate "
                        "receipt tracked. Labels legible on all sampled instruments.",
    },
    {
        "finding": "Contractor workers at the utility block were observed working "
                   "without the hot-work permit displayed at the work location, "
                   "though a valid permit had been issued that morning.",
        "whys": [
            ("Why was the process for controlling hot work not effective?",
             "The permit was issued and retained at the security desk rather than being carried to the work "
             "location."),
            ("Why was it retained at the desk?",
             "The permit is issued in a single copy and the issuing officer keeps it as the record."),
            ("Why is there only one copy?",
             "The permit form is a bound book with no duplicate leaf."),
            ("Why is a single-copy book in use?",
             "The form was specified for the internal maintenance team, who work adjacent to the issuing "
             "office."),
            ("Why was it not respecified for contractors?",
             "The contractor safety procedure adopts the existing permit system without assessing whether it "
             "fits contractor working."),
        ],
        "root_cause": "The contractor safety procedure adopted the in-house permit system "
                      "unchanged, without assessing its fitness for work carried out away "
                      "from the issuing office by people who are not employees.",
        "correction": "Work stopped and the permit retrieved and displayed before work "
                      "resumed. All open permits that day checked for display; one further "
                      "instance corrected. Toolbox talk delivered to both contractor crews "
                      "the same shift.",
        "preventive": "Permit book replaced with a two-part duplicate form — original "
                      "displayed at the work location, copy retained by the issuer. "
                      "Contractor safety procedure revised to require display and to make "
                      "it a verification point on the EHS officer's daily walkdown.",
        "verification": "Walkdown records for six weeks reviewed and the utility block "
                        "visited unannounced during the follow-up. All four active permits "
                        "displayed at the work location; duplicate copies traced to the "
                        "issuing register.",
    },
]

AUDITEE_REPLIES = [
    "Accepted. Evidence attached and the corrective action is under way with "
    "the target date agreed at the closing meeting.",
    "Accepted. The gap is confirmed on our side and the action owner has been "
    "named. Supporting records attached.",
    "Accepted with clarification — the record existed but was held outside the "
    "controlled location. It has been filed correctly and the evidence is "
    "attached.",
    "Accepted. We have completed the immediate correction and the systemic "
    "action is scheduled; evidence of both attached.",
]


# ─────────────────────────────────────────────────────────────────────
# Evidence — real when the environment allows it, honest when it does not
# ─────────────────────────────────────────────────────────────────────
class Evidence:
    """Generates and uploads checkpoint evidence, or degrades and says so.

    Evidence is not decoration here. `submit_audit` refuses any fail/partial row
    on a checkpoint flagged `requiresPhotoOnFail` — which is every `critical` and
    `major` line, 77 of the 206 — so without photographs the audit cannot leave
    the conduct stage at all. The seeder therefore checks up front and stops
    with instructions, rather than grading 206 checkpoints and then failing.

    `--no-evidence` proceeds anyway with photo records that carry no
    `storagePath`. That satisfies the submit gate and leaves
    `auditorEvidenceIds` empty, so the register and the report PDF show no
    photographs — half a record. It is offered because a demo database with no
    Supabase is still worth seeding, and refused by default because half a
    record is exactly what a demo must not look like.
    """

    def __init__(self, allow_metadata_only: bool = False) -> None:
        self.enabled = False
        self.metadata_only = False
        self.allow_metadata_only = allow_metadata_only
        self.reason = ""

        def _degrade(reason: str) -> None:
            self.reason = reason
            self.metadata_only = self.allow_metadata_only

        try:
            from PIL import Image, ImageDraw  # noqa: F401
        except ModuleNotFoundError:
            return _degrade("Pillow is not installed (python -m pip install Pillow)")
        try:
            from app.services.storage import is_storage_configured
        except Exception as e:  # noqa: BLE001
            return _degrade(f"storage module unavailable: {e}")
        if not is_storage_configured():
            return _degrade("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not configured")
        self.enabled = True

    def _frame(self, *, code: str, dept: str, stream: str, verdict: str,
               question: str, site: str, captured: str) -> bytes:
        from PIL import Image, ImageDraw

        tint = {"IMS": (76, 29, 149), "ENMS": (12, 74, 110)}.get(stream, (51, 65, 85))
        img = Image.new("RGB", (1024, 768), (248, 250, 252))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 1024, 132], fill=tint)
        d.text((32, 30), f"{code}   [{stream}]", fill=(255, 255, 255))
        d.text((32, 62), DEPT_LABEL.get(dept, dept), fill=(226, 232, 240))
        d.text((32, 92), f"{site}   ·   {captured}", fill=(203, 213, 225))
        d.text((32, 176), "AUDIT EVIDENCE — GENERATED PLACEHOLDER", fill=(15, 23, 42))
        d.text((32, 208), "Not a site photograph. Seeded demonstration data.",
               fill=(100, 116, 139))
        y = 268
        for i in range(0, len(question), 68):
            d.text((32, y), question[i:i + 68], fill=(30, 41, 59))
            y += 26
            if y > 560:
                break
        d.rectangle([32, 632, 992, 712], outline=tint, width=3)
        d.text((48, 664), f"Verdict recorded: {verdict}", fill=tint)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        return buf.getvalue()

    def preflight(self) -> None:
        """Push ONE real object through the whole pipeline before anything else.

        Generate, upload, sign, delete. It takes a second and it converts every
        storage misconfiguration — wrong key, missing bucket, renamed keyword —
        from "the audit silently would not seed" into a message naming the
        actual fault, before 206 checkpoints have been graded against it.
        """
        if not self.enabled:
            return
        from app.services.storage import (
            create_signed_download_url, delete_storage_object, upload_object,
        )

        path = svc.attachment_storage_path("preflight", "PREFLIGHT", "preflight.jpg")
        data = self._frame(
            code="PREFLIGHT", dept="DEPT_HR", stream="IMS", verdict="—",
            question="Storage preflight — written and removed by the seeder.",
            site="preflight", captured="",
        )
        upload_object(path, data, "image/jpeg")
        url = create_signed_download_url(path, expires_in_sec=60)
        if not url:
            raise RuntimeError(f"Storage signed no URL for {path}")
        try:
            delete_storage_object(path)
        except Exception:  # noqa: BLE001 — leaving one stray object is harmless
            pass

    def photos(self, *, audit_id: str, code: str, dept: str, stream: str,
               verdict: str, question: str, site: str, captured: datetime,
               n: int = 1) -> list[dict]:
        """Photo records for one checkpoint.

        In metadata-only mode these carry no `storagePath` on purpose: the
        submit gate only checks that `photos` is non-empty, and inventing a
        path would put a dead reference in `auditorEvidenceIds` that the
        report would then try to sign and fail on.
        """
        if self.metadata_only:
            return [{
                "fileName": f"{code}-{i:02d}.jpg",
                "mimeType": "image/jpeg",
                "caption": (
                    f"{code} — {DEPT_LABEL.get(dept, dept)} [{stream}] — evidence "
                    f"recorded on site (seeded without storage; no file attached)"
                ),
            } for i in range(1, n + 1)]
        if not self.enabled:
            return []
        from app.services.storage import create_signed_download_url, upload_object

        out = []
        for frame in range(1, n + 1):
            data = self._frame(
                code=code, dept=dept, stream=stream, verdict=verdict,
                question=question, site=site,
                captured=captured.strftime("%d %b %Y %H:%M"),
            )
            file_name = f"{code}-{frame:02d}.jpg"
            path = svc.attachment_storage_path(audit_id, code, file_name)
            # NOT wrapped in try/except. An upload that fails here guarantees
            # `submit_audit` will reject the audit for a missing evidence photo
            # 200 checkpoints later, and the first version of this seeder
            # swallowed the error into a one-line warning and carried on —
            # which is how a wrong keyword argument presented as "the audit
            # would not seed" instead of as "expires_in is not a parameter".
            upload_object(path, data, "image/jpeg")
            url = create_signed_download_url(path, expires_in_sec=EVIDENCE_URL_TTL_SEC)
            out.append({
                "url": url if isinstance(url, str) else (url or {}).get("signedUrl", ""),
                "storagePath": path,
                "fileName": file_name,
                "mimeType": "image/jpeg",
                "caption": f"{code} — {DEPT_LABEL.get(dept, dept)} [{stream}] — generated placeholder",
            })
        return out


# ─────────────────────────────────────────────────────────────────────
# Connection resilience
# ─────────────────────────────────────────────────────────────────────
# `pool_pre_ping` (already on in app/core/db.py) validates a connection at
# CHECKOUT. It cannot help when the connection dies mid-statement, which is what
# a pooled Supabase link over a flaky WAN does — `ConnectionDoesNotExistError:
# connection was closed in the middle of operation`, several thousand rows into
# a seed run.
#
# So each unit of work runs in its own short session and is retried on a
# transport failure with a fresh one. Retrying is safe because every unit here
# is idempotent-by-construction: it re-reads its rows and re-applies the same
# state, so a retry after a half-applied transaction converges on the same
# result rather than doubling anything.
_TRANSIENT = (
    "connection was closed",
    "connection does not exist",
    "connection is closed",
    "server closed the connection",
    "connection reset",
    "cannot perform operation",
    "ssl connection has been closed",
    "terminating connection",
)


def _is_transient(exc: BaseException) -> bool:
    text = f"{exc}".lower()
    cause = f"{getattr(exc, '__cause__', '')}".lower()
    return any(t in text or t in cause for t in _TRANSIENT)


async def in_session(fn, *, label: str = "", attempts: int = 5):
    """Run `fn(db)` in a fresh session and commit. Retry on a dropped link.

    On failure the whole engine pool is disposed, not just the session: the
    pool can be holding several connections killed by the same network event,
    and handing the retry another dead one from the same pool just fails again.
    """
    from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

    from app.core.db import engine

    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            async with AsyncSessionLocal() as db:
                out = await fn(db)
                await db.commit()
                return out
        except (DBAPIError, OperationalError, InterfaceError, OSError) as e:
            if attempt == attempts or not _is_transient(e):
                raise
            print(f"     ~ link dropped{label}; disposing pool, retry "
                  f"{attempt}/{attempts - 1} in {delay:.0f}s")
            try:
                await engine.dispose()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


# ─────────────────────────────────────────────────────────────────────
# Schema preflight
# ─────────────────────────────────────────────────────────────────────
# Columns this seeder needs that `scripts/add_nc_report_rca_capa.py` adds.
# SQLAlchemy SELECTs every mapped column, so a single missing one makes any
# query touching AuditFinding fail — including the seeder's own teardown, and
# including the product's Findings page. Checking here turns a 90-line asyncpg
# traceback into one sentence naming the script to run.
REQUIRED_AUDITFINDING_COLUMNS = (
    "ncrNumber", "rcaId", "rcaStatus", "orgRepresentativeId",
    "verificationDetails", "auditorSignedById", "auditorSignedAt",
    "mrSignedById", "mrSignedAt",
)


async def preflight_schema() -> None:
    from sqlalchemy import text as sa_text

    async with AsyncSessionLocal() as db:
        present = {
            r[0] for r in (await db.execute(sa_text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'AuditFinding'"
            ))).all()
        }
    if not present:
        raise SystemExit(
            "\nThe AuditFinding table does not exist in this database.\n"
            "Run:  python scripts/add_cams_completion.py\n"
        )
    missing = [c for c in REQUIRED_AUDITFINDING_COLUMNS if c not in present]
    if missing:
        raise SystemExit(
            f"\nThe PIL NC-report columns are not applied to this database.\n"
            f"Missing on AuditFinding: {', '.join(missing)}\n\n"
            f"Run the migration first:\n"
            f"    python scripts/add_nc_report_rca_capa.py\n\n"
            f"It is additive and re-runnable. Until it runs, every query that\n"
            f"touches AuditFinding fails — this seeder, and the product's\n"
            f"Findings page and unified register with it.\n"
        )


# ─────────────────────────────────────────────────────────────────────
# Auditor competence, frozen at assignment
# ─────────────────────────────────────────────────────────────────────
# An integrated audit is conducted against four standards, so the competence
# the report claims for its auditors has to cover all four.
AUDITOR_COMPETENCIES = (
    "ISO-9001-INTERNAL-AUDITOR", "ISO-14001-INTERNAL-AUDITOR",
    "ISO-45001-INTERNAL-AUDITOR", "ISO-50001-INTERNAL-AUDITOR",
)


async def freeze_competence(db, *, audit_id: str, plant_id: str,
                            user_ids: list[str], captured_by: str) -> int:
    """Back the report's competence section with real Skill Matrix records.

    `assurance.capture_competence_snapshot` no-ops unless the audit TYPE
    declares required competencies, and no `CamsAuditType` row exists for
    `management_system_audit`. Rather than mutate global audit-type
    configuration from a seeder, the records and the engagement snapshot are
    written here — the snapshot then agrees with what the Skill Matrix says,
    which is the whole point of freezing it.

    Returns 0 and does nothing if none of the auditor competencies are seeded,
    rather than inventing them: a competence claim with no competency behind it
    is worse on a certification report than an empty section.
    """
    comps = (await db.execute(
        select(Competency).where(Competency.code.in_(AUDITOR_COMPETENCIES))
    )).scalars().all()
    if not comps:
        return 0

    now = datetime.now(timezone.utc)
    valid_from = now - timedelta(days=420)
    valid_until = now + timedelta(days=580)
    written = 0
    for uid in user_ids:
        for comp in comps:
            rec = (await db.execute(
                select(CompetencyRecord).where(
                    CompetencyRecord.personUserId == uid,
                    CompetencyRecord.competencyId == comp.id,
                )
            )).scalars().first()
            if rec is None:
                rec = CompetencyRecord(
                    plantId=plant_id, personUserId=uid, competencyId=comp.id,
                    createdByUserId=captured_by,
                )
                db.add(rec)
            rec.state = "validated"
            rec.currentValidatedAt = valid_from
            rec.currentValidatedByUserId = captured_by
            rec.currentValidationMethod = "external_certificate"
            rec.externalCertificateReference = f"{comp.code}/{uid[-6:].upper()}"
            rec.externalCertificateAuthority = "Accredited certification body"
            rec.validFrom = valid_from
            rec.validUntil = valid_until
            rec.nextRevalidationDue = valid_until
            rec.updatedByUserId = captured_by
            await db.flush()

            db.add(EngagementCompetenceSnapshot(
                engagementKind="AUDIT", engagementId=audit_id, userId=uid,
                competencyId=comp.id, competencyCode=comp.code,
                competencyName=comp.name, state=rec.state,
                validUntil=rec.validUntil,
                externalCertificateReference=rec.externalCertificateReference,
                held=True, waivedGap=False, capturedByUserId=captured_by,
            ))
            written += 1
    await db.flush()
    return written


# ─────────────────────────────────────────────────────────────────────
# Cast
# ─────────────────────────────────────────────────────────────────────
async def resolve_cast(db, plant_id: str) -> dict:
    """Distinct, independent people for lead, co-auditor, plant manager and one
    auditee per department — chosen the way `resolve_cast` in the internal-audit
    seeder does, by consulting the independence guard BEFORE picking rather than
    discovering a refusal afterwards."""
    from app.services import audit_assignment as assignment
    from app.services import independence

    slots = (await assignment.assignable_users(db, plant_id=plant_id))["assignable"]

    auditees: list[dict] = []
    for u in slots["auditee"]:
        auditees.append(u)
        if len(auditees) == len(DEPARTMENTS):
            break
    if len(auditees) < len(DEPARTMENTS):
        raise SystemExit(
            f"Need {len(DEPARTMENTS)} distinct assignable auditees at this plant; "
            f"found {len(auditees)}. Seed more users first."
        )
    taken = {u["id"] for u in auditees}
    pm = next((u for u in slots["plantManager"] if u["id"] not in taken), None)
    if pm is None:
        raise SystemExit("No plant manager available outside the auditee seats.")
    taken.add(pm["id"])

    scope = independence.EngagementScope(
        kind="AUDIT", id=None, siteId=plant_id, disciplineCodes=[],
        areaIds=[], departments=[], leadAuditorId=None, teamAuditorIds=[],
        auditeeUserIds=sorted(taken),
    )
    pool = [u for u in slots["leadAuditor"] if u["id"] not in taken]
    verdicts = await independence.check_many(
        db, user_ids=[u["id"] for u in pool], scope=scope, assigning_as="AUDITOR",
    )
    clean = [u for u in pool if not verdicts[u["id"]].blocking]
    if not clean:
        raise SystemExit(
            "Every eligible auditor at this site has a blocking independence "
            "conflict against this scope."
        )
    return {
        "lead": clean[0],
        "co": clean[1] if len(clean) > 1 else None,
        "pm": pm,
        "auditees": auditees,
    }


# ─────────────────────────────────────────────────────────────────────
# Teardown
# ─────────────────────────────────────────────────────────────────────
async def wipe_previous(db, title: str) -> None:
    """Remove a previous run of THIS audit, and everything hanging off it."""
    audits = (await db.execute(
        select(ComplianceAudit).where(ComplianceAudit.title == title)
        .execution_options(include_deleted=True)
    )).scalars().all()
    if not audits:
        return
    for a in audits:
        findings = (await db.execute(
            select(AuditFinding).where(AuditFinding.auditId == a.id)
            .execution_options(include_deleted=True)
        )).scalars().all()
        for f in findings:
            if f.rcaId:
                rca = await db.get(RootCauseAnalysis, f.rcaId)
                if rca is not None:
                    await db.delete(rca)
            if f.capaId:
                capa = await db.get(Capa, f.capaId)
                if capa is not None:
                    for child in (await db.execute(
                        select(CapaAction).where(CapaAction.capaId == capa.id)
                    )).scalars().all():
                        await db.delete(child)
                    for rc in (await db.execute(
                        select(CapaRootCause).where(CapaRootCause.capaId == capa.id)
                    )).scalars().all():
                        await db.delete(rc)
                    await db.delete(capa)
            await db.delete(f)
        # CAPAs auto-spawned at submit reference the checkpoint, not the finding.
        for capa in (await db.execute(
            select(Capa).where(Capa.sourceReferenceUrl.like(f"%{a.id}%"))
            .execution_options(include_deleted=True)
        )).scalars().all():
            for child in (await db.execute(
                select(CapaAction).where(CapaAction.capaId == capa.id)
            )).scalars().all():
                await db.delete(child)
            await db.delete(capa)
        await db.delete(a)
    await db.flush()
    print(f"  ·  removed {len(audits)} previous copy/copies of this audit")


# ─────────────────────────────────────────────────────────────────────
# Phases
# ─────────────────────────────────────────────────────────────────────
async def phase_schedule(spec: dict, ev: Evidence) -> dict:
    async with AsyncSessionLocal() as db:
        plant = (await db.execute(
            select(Plant).where(Plant.code == PLANT_CODE)
        )).scalars().first()
        if plant is None:
            plant = (await db.execute(
                select(Plant).order_by(Plant.createdAt).limit(1)
            )).scalars().first()
        if plant is None:
            raise SystemExit("No plant in this database — seed a plant first.")

        await wipe_previous(db, spec["title"])
        cast = await resolve_cast(db, plant.id)
        lead = await db.get(User, cast["lead"]["id"])
        co = await db.get(User, cast["co"]["id"]) if cast["co"] else None
        pm = await db.get(User, cast["pm"]["id"])
        auditees = [await db.get(User, u["id"]) for u in cast["auditees"]]
        auditee_by_dept = dict(zip(DEPARTMENTS, auditees))

        now = datetime.now(timezone.utc)
        scheduled = now - timedelta(days=spec["days_ago"])

        def audit_data(co_user: User | None) -> dict:
            return {
                "plantId": plant.id,
                "title": spec["title"],
                "industryCode": INDUSTRY_CODE,
                "auditType": "management_system_audit",
                # Empty = the full library: all three departments, both streams.
                "selectedDisciplineIds": [],
                "scheduledDate": scheduled,
                "scheduledStartTime": "09:00",
                "estimatedDurationHours": 16,
                "leadAuditorUserId": lead.id,
                "coAuditors": ([{"userId": co_user.id, "disciplineIds": ["DEPT_OHC"]}]
                               if co_user else []),
                "plantManagerUserId": pm.id,
                "auditees": [{"userId": u.id, "responsibleCategories": [d]}
                             for d, u in auditee_by_dept.items()],
                "scopeDepartments": [DEPT_LABEL[d] for d in DEPARTMENTS],
                "scopeAreas": ["Admin Block", "HR Office", "Occupational Health Centre",
                               "Sewage Treatment Plant", "Effluent Treatment Plant",
                               "Utility Block", "Canteen & Welfare"],
                "scopeDescription": (
                    "Integrated management-system audit against ISO 9001:2015, "
                    "ISO 14001:2015, ISO 45001:2018 (IMS stream) and ISO 50001:2018 "
                    "(EnMS stream). Human Resources, Administration and the "
                    "Occupational Health Centre each assessed against both source "
                    "sheets — 206 checkpoints. Two reports are issued, one per stream."
                ),
                "openingRemarks": (
                    "Opening meeting held with the Management Representative and all "
                    "three department heads. Scope, audit criteria (both source "
                    "sheets), sampling basis, the conformance vocabulary "
                    "(Conformance / Non-Conformance / Observation) and the closing "
                    "meeting time were confirmed before fieldwork began."
                ),
            }

        try:
            audit = await svc.create_audit(db, user=lead, data=audit_data(co))
        except ValueError as e:
            if co is None or "independence" not in str(e).lower():
                raise
            print(f"  !  co-auditor refused by the independence guard; single-auditor.\n     {e}")
            co = None
            audit = await svc.create_audit(db, user=lead, data=audit_data(None))
        await db.flush()
        print(f"  1. scheduled            {audit.auditNumber} "
              f"({audit.totalCheckpoints} checkpoints across {len(DEPARTMENTS)} departments)")

        attendees = (
            [{"userId": lead.id, "role": "Lead auditor"}]
            + ([{"userId": co.id, "role": "Auditor — OHC"}] if co else [])
            + [{"userId": pm.id, "role": "Management Representative"}]
            + [{"userId": u.id, "role": f"Auditee — {DEPT_LABEL[d]}"}
               for d, u in auditee_by_dept.items()]
        )
        await assurance.upsert_meeting(
            db, engagement_kind="AUDIT", engagement_id=audit.id,
            meeting_type="OPENING", user=lead,
            payload={
                "heldAt": scheduled.replace(hour=9, minute=0).isoformat(),
                "attendees": attendees,
                "scopeConfirmed": True,
                "notes": (
                    "Both source sheets tabled. Confirmed that each department is "
                    "assessed against the IMS and the EnMS sheet, that two reports "
                    "will be issued, and that non-conformities will be raised on "
                    "PIL/MR/F04-R1 with a Why-Why root cause analysis required "
                    "before corrective action is planned."
                ),
            },
        )
        print(f"  2. opening meeting      {len(attendees)} attendees, scope confirmed")

        team = [lead.id] + ([co.id] if co else [])
        n_comp = await freeze_competence(
            db, audit_id=audit.id, plant_id=plant.id, user_ids=team, captured_by=lead.id,
        )
        if n_comp:
            print(f"  3. competence frozen    {n_comp} snapshot(s) for {len(team)} auditor(s)")
        else:
            print(f"  3. competence frozen    skipped — none of {len(AUDITOR_COMPETENCIES)} "
                  f"auditor competencies are seeded in the Skill Matrix")

        rows = (await db.execute(
            select(AuditCheckpointResponse)
            .where(AuditCheckpointResponse.auditId == audit.id)
            .order_by(AuditCheckpointResponse.sequence)
        )).scalars().all()
        checkpoints = [
            {"code": r.checkpointCode, "dept": r.categoryId, "stream": r.streamCode or "IMS",
             "question": r.checkpointQuestion, "criticality": r.criticality}
            for r in rows
        ]
        ctx = {
            "auditId": audit.id, "auditNumber": audit.auditNumber,
            "plantName": plant.name, "plantId": plant.id,
            "leadId": lead.id, "pmId": pm.id,
            "auditeeIds": [u.id for u in auditees],
            "auditeeByDept": {d: u.id for d, u in auditee_by_dept.items()},
            "attendees": attendees, "scheduled": scheduled, "now": now,
            "checkpoints": checkpoints, "key": spec["key"],
        }
        await db.commit()
        return ctx


async def phase_conduct(ctx: dict, ev: Evidence) -> dict:
    """Grade all 206 with the tristate vocabulary, submit, issue both interims.

    Evidence is generated and uploaded FIRST, with no database session open.
    Uploading inside the session was the real cause of the dropped connections:
    ~250 blocking HTTP round trips interleaved with the writes left the pooled
    Postgres connection idle for minutes at a stretch, and Supabase's pooler
    reaps it. Separating the two means the database is only ever touched in
    short, retryable bursts.
    """
    tally = {"CONFORMANCE": 0, "NON_CONFORMANCE": 0, "OBSERVATION": 0, "NA": 0}
    nc_seen = 0
    plan: list[dict] = []

    for cp in ctx["checkpoints"]:
        verdict = verdict_for(ctx["key"], cp["code"], cp["criticality"])
        tally[verdict] += 1
        idx = int(hashlib.sha256(cp["code"].encode()).hexdigest()[:4], 16)

        if verdict == "NA":
            plan.append({"checkpointCode": cp["code"], "gradeAwarded": pg.GRADE_NA,
                         "auditFindings": NA_NOTES[idx % len(NA_NOTES)]})
            continue

        if verdict == pg.TRI_NON_CONFORMANCE:
            note = NC_SCENARIOS[nc_seen % len(NC_SCENARIOS)]["finding"]
            nc_seen += 1
        elif verdict == pg.TRI_OBSERVATION:
            note = OBSERVATION_NOTES[idx % len(OBSERVATION_NOTES)].format(n=(idx % 8) + 5)
        else:
            note = CONFORMANCE_NOTES[idx % len(CONFORMANCE_NOTES)].format(n=(idx % 8) + 5)

        photos = ev.photos(
            audit_id=ctx["auditId"], code=cp["code"], dept=cp["dept"],
            stream=cp["stream"], verdict=pg.TRISTATE_LABEL.get(verdict, verdict),
            question=cp["question"], site=ctx["plantName"], captured=ctx["scheduled"],
            n=2 if verdict == pg.TRI_NON_CONFORMANCE else 1,
        )
        payload = {
            "checkpointCode": cp["code"],
            "conformance": verdict,
            "auditFindings": note,
            "riskGrade": risk_for(cp["criticality"]),
        }
        if photos:
            payload["photos"] = photos
        plan.append(payload)

    n_photos = sum(len(p.get("photos") or []) for p in plan)
    print(f"  4a. evidence prepared   {n_photos} image(s) for {len(plan)} checkpoint(s)")

    # Write in retryable bursts. `save_response` MERGES, so replaying a chunk
    # after a dropped connection re-applies the same verdict rather than
    # duplicating anything.
    CHUNK = 25

    async def _save(db, *, batch: list[dict]):
        lead = await db.get(User, ctx["leadId"])
        for payload in batch:
            await svc.save_response(db, user=lead, audit_id=ctx["auditId"], payload=payload)

    for offset in range(0, len(plan), CHUNK):
        batch = plan[offset:offset + CHUNK]
        await in_session(
            lambda db, b=batch: _save(db, batch=b),
            label=f" (grading {offset + 1}-{offset + len(batch)}/{len(plan)})",
        )
    print("  4. graded               " + ", ".join(
        f"{pg.TRISTATE_LABEL.get(k, k)}={v}" for k, v in tally.items() if v))

    async def _submit(db):
        lead = await db.get(User, ctx["leadId"])
        return await svc.submit_audit(db, user=lead, audit_id=ctx["auditId"])

    res = await in_session(_submit, label=" (submit)")
    print(f"  5. submitted            {res['capasSpawned']} CAPA auto-spawned, "
          f"score {res['score']['score_obtained']}/{res['score']['score_allotted']} "
          f"= {res['score']['overall_score_pct']}%")

    interims = []
    for stream in STREAMS:
        async def _report(db, stream=stream):
            lead = await db.get(User, ctx["leadId"])
            return await svc.generate_report(
                db, user=lead, audit_id=ctx["auditId"], report_type="INTERIM", stream=stream,
            )
        r = await in_session(_report, label=f" (interim {stream})")
        interims.append(r["reportCode"])
    print(f"  6. interim reports      {', '.join(interims)}")

    async def _routed(db):
        rows = (await db.execute(
            select(AuditCheckpointResponse).where(
                AuditCheckpointResponse.auditId == ctx["auditId"],
                AuditCheckpointResponse.workflowState == "AWAITING_AUDITEE",
            ).order_by(AuditCheckpointResponse.sequence)
        )).scalars().all()
        return [
            {"id": r.id, "code": r.checkpointCode, "dept": r.categoryId,
             "ownerId": r.assignedOwnerId or r.routedToUserId}
            for r in rows
        ]

    findings = await in_session(_routed, label=" (routed findings)")
    return {"findings": findings, "interims": interims}


async def phase_resolve(ctx: dict, findings: list[dict], ev: Evidence) -> None:
    """Walk every routed finding through the iteration state machine.

    All four branches are exercised — accept, request-more-info (round 2),
    escalate to the Management Representative, and raise-CAPA — because a demo
    that only ever shows the happy path teaches the screen wrong.
    """
    tally = {"ACCEPT": 0, "MORE_INFO": 0, "ESCALATE": 0, "CAPA": 0}

    # ONE RETRIED SESSION PER FINDING. `transition_checkpoint` runs several
    # statements per action and up to four actions per finding; fifty of those
    # over one connection is what the flaky link kept killing. Retrying is safe
    # because each finding re-reads its own row and re-applies the same
    # transitions.
    async def _resolve_one(db, *, i: int, f: dict):
        lead = await db.get(User, ctx["leadId"])
        pm = await db.get(User, ctx["pmId"])
        owner = (await db.get(User, f["ownerId"])) if f["ownerId"] else lead
        owner = owner or lead
        reply = AUDITEE_REPLIES[i % len(AUDITEE_REPLIES)]
        branch = ["ACCEPT", "ACCEPT", "MORE_INFO", "ACCEPT", "ESCALATE",
                  "ACCEPT", "CAPA", "ACCEPT"][i % 8]

        try:
            await svc.transition_checkpoint(
                db, user=owner, audit_id=ctx["auditId"], checkpoint_id=f["id"],
                action="AUDITEE_RESPOND",
                payload={"comment": reply, "actionTaken": reply},
            )
            if branch == "MORE_INFO":
                await svc.transition_checkpoint(
                    db, user=lead, audit_id=ctx["auditId"], checkpoint_id=f["id"],
                    action="REQUEST_MORE_INFO",
                    payload={"comment": "Please attach the controlled copy of the "
                                        "record, not the working copy."},
                )
                await svc.transition_checkpoint(
                    db, user=owner, audit_id=ctx["auditId"], checkpoint_id=f["id"],
                    action="AUDITEE_RESPOND",
                    payload={"comment": "Controlled copy attached, revision confirmed "
                                        "against the master list.",
                             "actionTaken": "Controlled copy retrieved and attached."},
                )
                await svc.transition_checkpoint(
                    db, user=lead, audit_id=ctx["auditId"], checkpoint_id=f["id"],
                    action="ACCEPT", payload={"comment": "Accepted on the second round."},
                )
            elif branch == "ESCALATE":
                # ESCALATE parks the row with the plant manager; only
                # PM_ACCEPT resolves it from there. Using plain ACCEPT would
                # be rejected by the state machine.
                await svc.transition_checkpoint(
                    db, user=lead, audit_id=ctx["auditId"], checkpoint_id=f["id"],
                    action="ESCALATE",
                    payload={"comment": "Response does not address the systemic cause; "
                                        "referred to the Management Representative."},
                )
                await svc.transition_checkpoint(
                    db, user=pm, audit_id=ctx["auditId"], checkpoint_id=f["id"],
                    action="PM_ACCEPT",
                    payload={"comment": "Reviewed with both parties. Action plan agreed "
                                        "and resourced; finding accepted."},
                )
            elif branch == "CAPA":
                # RAISE_CAPA is the auditor-side spawn; it lands the row on
                # ACCEPTED_WITH_CAPA. The NC-report trigger later ADOPTS this
                # CAPA rather than raising a second one against the same
                # non-conformity.
                await svc.transition_checkpoint(
                    db, user=lead, audit_id=ctx["auditId"], checkpoint_id=f["id"],
                    action="RAISE_CAPA",
                    payload={"comment": "Accepted; systemic action tracked as a CAPA."},
                )
            else:
                await svc.transition_checkpoint(
                    db, user=lead, audit_id=ctx["auditId"], checkpoint_id=f["id"],
                    action="ACCEPT",
                    payload={"comment": "Evidence sighted and accepted."},
                )
            tally[branch] += 1
        except ValueError as e:
            await db.rollback()
            print(f"     ! {f['code']}: {e}")

    for i, f in enumerate(findings):
        await in_session(
            lambda db, i=i, f=f: _resolve_one(db, i=i, f=f),
            label=f" (finding {i + 1}/{len(findings)})",
        )

    print("  7. findings resolved    " + ", ".join(f"{k}={v}" for k, v in tally.items() if v))


async def phase_nc_reports(ctx: dict, profile: str) -> dict:
    """PIL/MR/F04-R1 — raise, analyse, action, verify and close every NC.

    Each NC is ONE retried unit of work in its own session (see `in_session`).
    Driving twenty of them through seven stages inside a single session is
    thousands of statements over one connection, and on a flaky link that
    connection will not survive them.
    """
    async def _trigger(db):
        lead = await db.get(User, ctx["leadId"])
        audit = await db.get(ComplianceAudit, ctx["auditId"])
        return await nc_rca_capa.trigger_for_audit(db, audit, actor_id=lead.id)

    result = await in_session(_trigger, label=" (raising NC reports)")
    print(f"  8. NC reports raised    {result['created']} "
          f"(of {result['nonConformities']} non-conformities; "
          f"{result['findingsMaterialised']} finding rows materialised)")
    if result["failures"]:
        for fl in result["failures"]:
            print(f"     ! {fl['findingCode']}: {fl['reason']}")

    async def _list_ncs(db):
        rows = (await db.execute(
            select(AuditFinding.id).where(
                AuditFinding.auditId == ctx["auditId"],
                AuditFinding.rcaId.isnot(None),
                AuditFinding.isDeleted.is_(False),
            ).order_by(AuditFinding.ncrNumber)
        )).scalars().all()
        return list(rows)

    finding_ids = await in_session(_list_ncs, label=" (listing NCs)")

    async def _drive(db, *, i: int, finding_id: str):
        lead = await db.get(User, ctx["leadId"])
        pm = await db.get(User, ctx["pmId"])
        finding = await db.get(AuditFinding, finding_id)
        if finding is None:
            return
        sc = NC_SCENARIOS[i % len(NC_SCENARIOS)]
        owner = (await db.get(User, finding.ownerId)) if finding.ownerId else lead
        owner = owner or lead
        rca = await db.get(RootCauseAnalysis, finding.rcaId)
        capa = await db.get(Capa, finding.capaId) if finding.capaId else None
        if rca is None or capa is None:
            return

        # How far this NC is driven. H1 closes everything; H2 stops each NC
        # at a different stage so the register shows its whole vocabulary.
        # 8 rungs, not 7. Rung 7 is "verified, awaiting the M.R." — a real and
        # common resting state that a 7-rung ladder made unreachable, because
        # its last rung ran verification and the M.R. signature together.
        #   1 RCA_PENDING   2 RCA_IN_REVIEW   3 ACTIONS_PENDING
        #   4 actions proposed   5 actions in progress   6 AWAITING_VERIFICATION
        #   7 AWAITING_MR_SIGNOFF   8 CLOSED
        depth = 8 if profile == "ALL_CLOSED" else (i % 8) + 1
        # One NC fails its first re-check and loops back before finally closing,
        # so the register has a worked example of the INEFFECTIVE path. Pinned
        # to the first fully-closed NC rather than a fixed index: the previous
        # gate named i == 2, which lands on rung 3 and never reaches
        # verification at all, so the branch was dead code.
        loops_back = profile == "MIXED" and depth == 8 and i % 16 == 7

        # ── 1. the auditee fills the Why-Why ladder ──────────────
        if depth >= 1:
            payload = dict(rca.analysisPayload or {})
            payload["problemStatement"] = finding.description or sc["finding"]
            payload["whys"] = [{"question": q, "answer": a} for q, a in sc["whys"]]
            payload["rootCause"] = sc["root_cause"]
            rca.analysisPayload = payload
            rca.narrative = (
                f"Why-Why analysis to {len(sc['whys'])} levels against "
                f"{nc_rca_capa.PIL_FORM_NO}. The chain reaches a documented-system "
                f"failure rather than an individual lapse."
            )
            rca.status = "IN_ANALYSIS"
            rca.updatedBy = owner.id
            await db.flush()
            await nc_rca_capa.sync_finding_rca_status(db, rca)

        # ── 2. submitted for review ──────────────────────────────
        if depth >= 2:
            problems = nc_rca_capa.validate_why_payload(rca.analysisPayload)
            if problems:
                print(f"     ! NCR {finding.ncrNumber} ladder rejected: {problems}")
            else:
                rca.status = "PEER_REVIEW"
                await db.flush()
                await nc_rca_capa.sync_finding_rca_status(db, rca)

        # ── 3. approved — this releases the CAPA ─────────────────
        if depth >= 3:
            rca.status = "APPROVED"
            rca.approverId = lead.id
            rca.approvedAt = datetime.now(timezone.utc)
            await db.flush()
            await nc_rca_capa.release_capa_from_rca(db, rca, actor_id=lead.id)
            await nc_rca_capa.sync_finding_rca_status(db, rca)

        # ── 4. Correction + Preventive Action planned ────────────
        if depth >= 4:
            due = datetime.now(timezone.utc) + timedelta(days=21)
            db.add(CapaAction(
                capaId=capa.id, actionType=nc_rca_capa.ACTION_TYPE_FOR_CORRECTION,
                description=sc["correction"],
                rationale=f"Correction — {nc_rca_capa.PIL_CORRECTION_PROMPT}.",
                ownerUserId=owner.id, dueDate=due, status="PROPOSED", sortOrder=0,
            ))
            db.add(CapaAction(
                capaId=capa.id, actionType=nc_rca_capa.ACTION_TYPE_FOR_PREVENTIVE,
                description=sc["preventive"],
                rationale=f"Preventive Action — {nc_rca_capa.PIL_PREVENTIVE_PROMPT}.",
                ownerUserId=owner.id, dueDate=due + timedelta(days=14),
                status="PROPOSED", sortOrder=0,
            ))
            await db.flush()

        actions = (await db.execute(
            select(CapaAction).where(CapaAction.capaId == capa.id)
        )).scalars().all()

        # ── 5. actions worked ────────────────────────────────────
        if depth == 5:
            for a in actions:
                a.status = "IN_PROGRESS"
                a.startedAt = datetime.now(timezone.utc) - timedelta(days=4)
            capa.state = "ACTIONS_IN_PROGRESS"
            await db.flush()

        # ── 6. actions completed, awaiting verification ──────────
        if depth >= 6:
            for a in actions:
                a.status = "COMPLETED"
                a.startedAt = datetime.now(timezone.utc) - timedelta(days=12)
                a.completedAt = datetime.now(timezone.utc) - timedelta(days=3)
                a.evidenceOfCompletion = (
                    "Revised document issued and circulated; records for the "
                    "following cycle sampled and found compliant."
                )
                a.approverUserId = pm.id
                a.approvedAt = datetime.now(timezone.utc) - timedelta(days=2)
            capa.state = "PENDING_VERIFICATION"
            await db.flush()

        # ── 7. the auditor verifies effectiveness ────────────────
        if depth >= 7:
            # The loop-back first, where this NC is the worked example: a
            # re-check that finds the nonconformity still there sends the CAPA
            # back to its owner rather than closing anything. The actions are
            # then redone and re-verified, so the NC ends closed WITH a failed
            # round in its history — which is what the INEFFECTIVE path
            # actually looks like on a real register.
            if loops_back:
                await nc_rca_capa.verify_nc(
                    db, finding,
                    verification_details=(
                        "Re-checked on site. The revised procedure is issued but the "
                        "records for the following cycle still show the original "
                        "practice — the change has not reached the people doing the "
                        "work. Not effective."
                    ),
                    result="INEFFECTIVE", actor_id=lead.id,
                )
                for a in actions:
                    a.status = "COMPLETED"
                    a.completedAt = datetime.now(timezone.utc) - timedelta(days=1)
                    a.evidenceOfCompletion = (
                        "Re-worked after the failed verification: the change was "
                        "briefed to every affected role holder and the following "
                        "cycle's records re-sampled."
                    )
                capa.state = "PENDING_VERIFICATION"
                await db.flush()

            await nc_rca_capa.verify_nc(
                db, finding, verification_details=sc["verification"],
                result="EFFECTIVE", actor_id=lead.id,
            )

        # ── 8. the M.R. signs, and the NC closes ─────────────────
        if depth >= 8:
            await nc_rca_capa.mr_sign_off(db, finding, actor_id=pm.id)

    for i, fid in enumerate(finding_ids):
        await in_session(
            lambda db, i=i, fid=fid: _drive(db, i=i, finding_id=fid),
            label=f" (NCR {i + 1}/{len(finding_ids)})",
        )

    # Recompute the register so what we print is what the screen will show.
    async def _register(db):
        audit = await db.get(ComplianceAudit, ctx["auditId"])
        return await nc_rca_capa.nc_register(db, audit)

    reg = await in_session(_register, label=" (NC register)")
    live = {k: v for k, v in reg["byStage"].items() if v}
    print(f"  9. NC lifecycle         {reg['closed']}/{reg['total']} closed — "
          + ", ".join(f"{k.replace('_',' ').title()}={v}" for k, v in live.items()))
    return reg


async def phase_close(ctx: dict) -> list[str]:
    async with AsyncSessionLocal() as db:
        lead = await db.get(User, ctx["leadId"])
        pm = await db.get(User, ctx["pmId"])
        auditees = [await db.get(User, uid) for uid in ctx["auditeeIds"]]
        now = ctx["now"]

        await assurance.upsert_meeting(
            db, engagement_kind="AUDIT", engagement_id=ctx["auditId"],
            meeting_type="CLOSING", user=lead,
            payload={
                "heldAt": (now - timedelta(days=2)).isoformat(),
                "attendees": ctx["attendees"],
                "findingsSummaryPresented": (
                    "Every non-conformity was presented with its evidence, clause "
                    "reference and department. NC reports were issued on "
                    "PIL/MR/F04-R1 and the Why-Why analysis requirement explained "
                    "to each owner. Observations were tabled separately."
                ),
                "auditeeAcknowledged": True,
                "auditeeAcknowledgedByUserId": auditees[0].id,
                "notes": (
                    "Both stream results were presented separately — the IMS score "
                    "against ISO 9001/14001/45001 and the EnMS score against "
                    "ISO 50001. No dissent recorded."
                ),
            },
        )
        print(" 10. closing meeting      findings presented and acknowledged")

        a = await db.get(ComplianceAudit, ctx["auditId"])
        await signoff.record_signoff(
            db, audit=a, user=lead, role="LEAD_AUDITOR", signature_kind="TYPED",
            typed_name=lead.name,
            statement="I confirm this integrated audit was conducted against both "
                      "source sheets per the approved programme.",
        )
        await signoff.record_signoff(
            db, audit=a, user=auditees[0], role="AUDITEE_OWNER", signature_kind="TYPED",
            typed_name=auditees[0].name,
            statement="Findings received and corrective actions agreed.",
        )
        await signoff.record_signoff(
            db, audit=a, user=pm, role="PLANT_MANAGER", signature_kind="TYPED",
            typed_name=pm.name,
            statement="Audit result noted. Resourcing for the agreed corrective "
                      "and preventive actions approved.",
        )
        auditor_by_dept = dict((await db.execute(
            select(AuditCheckpointResponse.categoryId,
                   AuditCheckpointResponse.assignedAuditorId)
            .where(AuditCheckpointResponse.auditId == ctx["auditId"]).distinct()
        )).all())
        for dept, auditor_id in auditor_by_dept.items():
            signer = (await db.get(User, auditor_id)) if auditor_id else lead
            await signoff.record_signoff(
                db, audit=a, user=signer or lead, role="DISCIPLINE_AUDITOR",
                discipline_code=dept, signature_kind="TYPED",
                typed_name=(signer or lead).name,
                statement=f"I conducted the {DEPT_LABEL.get(dept, dept)} department "
                          f"against both source sheets and confirm the findings recorded.",
            )
        status = await signoff.signoff_status(db, a)
        print(f" 11. sign-offs            {len(status['signOffs'])} recorded "
              f"({status.get('disciplinesSigned','?')}/{status.get('disciplinesTotal','?')} departments)")

        await svc.close_audit(
            db, user=lead, audit_id=ctx["auditId"],
            closing_remarks=(
                "All findings responded to, evidenced and accepted. Non-conformities "
                "carry PIL/MR/F04-R1 NC reports with approved Why-Why analyses; their "
                "corrective and preventive actions are tracked to closure on the NC "
                "register outside this audit. Audit closed."
            ),
        )
        finals = []
        for stream in STREAMS:
            r = await svc.generate_report(
                db, user=lead, audit_id=ctx["auditId"], report_type="FINAL", stream=stream,
            )
            finals.append(r["reportCode"])
        print(f" 12. closed + FINAL       {', '.join(finals)}")
        await db.commit()
        return finals


async def seed_one(spec: dict, ev: Evidence) -> dict:
    print(f"\n{'=' * 74}\n{spec['title']}\n{'=' * 74}")
    ctx = await phase_schedule(spec, ev)
    try:
        conducted = await phase_conduct(ctx, ev)
    except Exception:
        # Print the real traceback. Losing it is what turned a wrong keyword
        # argument into "the data did not seed" — the audit row was committed
        # by phase_schedule and everything after it silently rolled back.
        import traceback
        traceback.print_exc()
        raise SystemExit(
            f"\nConduct phase failed for {ctx['auditNumber']}.\n"
            f"The audit row exists and is still 'scheduled'. Fix the cause above\n"
            f"and re-run — the seeder rebuilds this audit from scratch.\n"
        )
    await phase_resolve(ctx, conducted["findings"], ev)
    reg = await phase_nc_reports(ctx, spec["nc_profile"])
    finals = await phase_close(ctx)
    return {
        "auditNumber": ctx["auditNumber"], "title": spec["title"],
        "interims": conducted["interims"], "finals": finals, "register": reg,
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audit", type=int, choices=[1, 2],
                    help="seed only the first or second audit")
    ap.add_argument("--no-evidence", action="store_true",
                    help="proceed without real evidence files (photo metadata only)")
    args = ap.parse_args()
    specs = AUDITS if args.audit is None else [AUDITS[args.audit - 1]]

    # Silence outbound email for the duration of the seed.
    #
    # `transition_checkpoint` sends a handoff notification on nearly every
    # action, awaited INSIDE the caller's transaction. Seeding fires ~150 of
    # them, and against a live SMTP host that is slow or unreachable each one
    # blocks for its timeout while the Postgres transaction sits open — which is
    # how the pooler came to close the connection mid-operation. It is also
    # simply wrong for a seeder to email real auditees about invented findings.
    #
    # Patched on the notifications module rather than on the caller, because
    # `_notify` imports `send_email` at call time, so this reaches every path.
    from app.services import notifications as _notifications

    async def _no_email(*_a, **_kw) -> bool:
        return False

    _notifications.send_email = _no_email  # type: ignore[assignment]
    print("Email: suppressed for this run (seeded findings must not notify real people).")

    # Schema before storage: a missing column is cheaper to discover than a
    # round trip to Supabase, and it is the prerequisite that actually blocks.
    await preflight_schema()

    ev = Evidence(allow_metadata_only=args.no_evidence)
    if ev.enabled:
        try:
            ev.preflight()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            raise SystemExit(
                f"\nStorage preflight failed: {e}\n\n"
                f"Stopping here rather than grading 206 checkpoints against a\n"
                f"storage layer that cannot accept them — `submit_audit` refuses\n"
                f"any Non-Conformance or Observation on a critical/major\n"
                f"checkpoint with no photograph, so the audit could not be\n"
                f"submitted anyway.\n\n"
                f"Fix the storage configuration, or run with --no-evidence to\n"
                f"seed without evidence files.\n"
            ) from e
        print("Evidence: real images, uploaded to Supabase storage (preflight OK).")
    elif ev.metadata_only:
        print(f"Evidence: METADATA ONLY — {ev.reason}.\n"
              f"          Checkpoints carry observations and photo records, but no\n"
              f"          files. The register and the report PDF will show no\n"
              f"          photographs. Re-run without --no-evidence once Pillow and\n"
              f"          SUPABASE_* are available to get the complete record.")
    else:
        raise SystemExit(
            f"\nEvidence is unavailable: {ev.reason}.\n\n"
            f"This is a hard stop rather than a warning because `submit_audit`\n"
            f"refuses any Non-Conformance or Observation on a critical/major\n"
            f"checkpoint that has no photograph — 77 of the 206 IMS checkpoints\n"
            f"are flagged that way, so the audit could not be submitted and the\n"
            f"seeder would fail after grading everything.\n\n"
            f"Either:\n"
            f"  python -m pip install Pillow      # and set SUPABASE_URL +\n"
            f"                                    # SUPABASE_SERVICE_ROLE_KEY\n"
            f"  python scripts/seed_complete_ims_audits.py\n\n"
            f"or accept an incomplete record:\n"
            f"  python scripts/seed_complete_ims_audits.py --no-evidence\n"
        )

    out = []
    for spec in specs:
        out.append(await seed_one(spec, ev))

    print(f"\n{'=' * 74}\nDONE\n{'=' * 74}")
    for r in out:
        reg = r["register"]
        print(f"\n{r['auditNumber']}  {r['title']}")
        print(f"  reports   interim {', '.join(r['interims'])}"
              f"  ·  final {', '.join(r['finals'])}")
        print(f"  NC report {reg['total']} non-conformities, {reg['closed']} closed, "
              f"{reg['overdue']} overdue")
        for stage, n in reg["byStage"].items():
            if n:
                print(f"              {stage.replace('_', ' ').title():<24} {n}")


if __name__ == "__main__":
    asyncio.run(main())
