"""Audit report PDF — offline render tests (house no-DB style).

The redesign added a charted Section 1 on top of a register that is the audit
trail's record of truth. The risk it introduces is not that the chart looks
wrong — it is that the re-layout quietly drops a checkpoint, an observation or
the integrity digest. These tests render a 120-checkpoint report and assert that
nothing below Section 1 was lost.

Text-content assertions need a PDF text extractor, which is not a declared
dependency (nothing in the running product reads a PDF back). Those are skipped
where it is absent; the render-and-structure tests always run.
"""

from __future__ import annotations

import re

import pytest

from app.services.insights.rules_audit_report import compute_report_insights
from app.services.report_pdf import render_audit_report_pdf

DISCIPLINES = ("HR", "EHS", "PRODUCTION")
PREFIX = {"HR": "PI-HR", "EHS": "PI-EHS", "PRODUCTION": "PI-PR"}
USER_NAMES = {"u_anjali": "Anjali Verma", "u_lead": "S. Krishnan"}
FULL_HASH = "a1b2c3d4" * 8

OBSERVATION = ("Requirement only partially met. Evidence exists for the current month but the "
               "review cycle has slipped twice and no corrective action was recorded.")


def _fixture():
    """A 120-checkpoint Page Industries audit: 2 critical fails, a repeat NC on
    PI-PR-002, and the verbatim-observation group on the -005 checkpoints."""
    findings, register, cat = [], [], {d: dict(passed=0, partial=0, failed=0, na=0, total=0)
                                       for d in DISCIPLINES}
    for d in DISCIPLINES:
        for i in range(1, 41):
            code = f"{PREFIX[d]}-{i:03d}"
            cat[d]["total"] += 1
            if code in ("PI-EHS-011", "PI-PR-002"):
                status, sev = "FAIL", "critical"
            elif i % 7 == 5:
                status, sev = "PARTIAL", "major"
            elif i % 23 == 0:
                status, sev = "NA", "minor"
            else:
                status, sev = "PASS", "minor"

            repeat = code == "PI-PR-002"
            obs = OBSERVATION if i == 5 else f"Verified for {code} during the walkthrough."
            row = {
                "checkpointCode": code, "discipline": d, "question": f"{d} requirement {i}?",
                "severity": sev, "assessmentStatus": status,
                "workflowState": "RESOLVED" if status != "PASS" else "PASSED",
                "standard": "Factories Act 1948", "requirementReference": f"S.{20 + i % 40}",
                "observation": obs, "isAdHoc": False, "ownerId": "u_anjali",
                "capaNumber": f"CAPA-{i:04d}" if sev == "critical" else None,
                "auditorEvidenceIds": ["e1"], "auditeeEvidenceIds": [],
                "interactions": [
                    {"round": 1, "timestamp": "2026-06-14T10:00:00", "action": "ASSESS",
                     "actorId": "u_lead", "actorRole": "AUDITOR",
                     "resultingState": "ROUTED_TO_OWNER", "comment": None},
                    {"round": 1, "timestamp": "2026-06-16T14:30:00",
                     "action": "AUDITEE_RESPONSE", "actorId": "u_anjali",
                     "actorRole": "AUDITEE", "resultingState": "AUDITEE_RESPONDED",
                     "comment": "Evidence attached."},
                ],
            }
            register.append(row)
            if status == "PASS":
                cat[d]["passed"] += 1
                continue
            if status == "NA":
                cat[d]["na"] += 1
                continue
            cat[d]["partial" if status == "PARTIAL" else "failed"] += 1
            findings.append({
                **{k: row[k] for k in ("checkpointCode", "discipline", "severity",
                                       "assessmentStatus", "workflowState", "ownerId",
                                       "question", "observation", "standard",
                                       "requirementReference", "capaNumber", "isAdHoc")},
                "round": 1, "capaStatus": "IN_PROGRESS" if row["capaNumber"] else None,
                "requirementType": "STATUTORY_REGULATORY" if i % 3 == 0 else "INTERNAL_REQUIREMENT",
                "gradeAwarded": "UNSATISFACTORY", "scoreAllotted": 3, "scoreObtained": 0,
                "complianceStatus": "REPEATED_NON_COMPLIANCE" if repeat else "NON_COMPLIANCE",
                "riskGrade": "HIGH", "isRepeat": repeat,
            })

    # Points, the way the product scores: 3 per assessed checkpoint, N/A allotted
    # nothing, a repeat penalised at -1. The fixture must carry real allotments —
    # without them every bar renders "n/a" and a chart regression walks straight
    # through the suite.
    rag = []
    for d in DISCIPLINES:
        c = cat[d]
        a = c["passed"] + c["partial"] + c["failed"]
        allotted = a * 3
        obtained = c["passed"] * 3 + c["partial"] * 2 + c["failed"] * 0
        if d == "PRODUCTION":
            obtained -= 1  # PI-PR-002 is a repeat: -1 rather than 0
        rag.append({"categoryId": d, "categoryName": d,
                    "score_obtained": obtained, "score_allotted": allotted,
                    "pct": round(obtained / allotted * 100, 1) if allotted else None,
                    **c})

    snap = {
        "reportType": "FINAL", "auditCode": "AUD-PI-2026-NW-0021",
        "title": "Internal Audit - North Garment Unit",
        "plantName": "Page Industries - North Garment Unit",
        "auditType": "internal_compliance_audit", "plannedDate": "2026-06-12",
        "closedAt": "2026-06-30T11:20:00", "overallScorePct": 88.0,
        "overallResult": "CRITICAL_NC", "auditPassed": False,
        "checkpointsTotal": 120, "checkpointsAssessed": 120,
        "passCount": sum(c["passed"] for c in cat.values()),
        "failCount": sum(c["failed"] for c in cat.values()),
        "partialCount": sum(c["partial"] for c in cat.values()),
        "naCount": sum(c["na"] for c in cat.values()),
        "criticalFailures": 2, "majorFailures": len(findings) - 2, "minorFailures": 0,
        "openIterationsCount": 0, "criticalOpenCount": 0, "notAssessedCount": 0, "adHocCount": 0,
        "disciplineRag": rag,
        "scoreObtained": sum(r["score_obtained"] for r in rag),
        "scoreAllotted": sum(r["score_allotted"] for r in rag),
        "categoryScores": [{"category_id": r["categoryId"], "category_name": r["categoryName"],
                            "score_pct": r["pct"],
                            **{k: r[k] for k in ("passed", "partial", "failed", "na", "total",
                                                 "score_obtained", "score_allotted")}}
                           for r in rag],
        "capaSummary": {"total": 2, "open": 2, "overdue": 1},
        "findings": findings, "openIterations": [],
        "grade": {"showGrade": True, "assessed": 117, "applicable": 117, "assessedPct": 100.0,
                  "threshold": 20, "label": "Graded"},
        "gate": {"band": "CRITICAL_NC", "passed": False,
                 "explanation": "A critical non-conformance fails the audit regardless of the "
                                "overall percentage.", "rules": {}},
        "generatedAt": "2026-07-02T09:00:00", "snapshotHash": FULL_HASH[:16],
        "userNames": USER_NAMES, "revision": 1,
    }
    snap["insights"] = compute_report_insights(snap)
    return snap, register


def _render(snap, register):
    return render_audit_report_pdf(
        {"id": "rpt_x", "reportType": "FINAL", "reportCode": "RPT-AUD-PI-2026-NW-0021-F01",
         "snapshot": snap, "snapshotHashFull": FULL_HASH,
         "signOffs": [{"role": "Lead Auditor", "name": "S. Krishnan", "signedAt": "2026-07-01"}]},
        generated_by_name="S. Krishnan", register=register, user_names=USER_NAMES)


def _text(pdf_bytes):
    pdfium = pytest.importorskip("pypdfium2", reason="no PDF text extractor installed")
    import io
    doc = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
    return "".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))


# ─── renders at all ─────────────────────────────────────────────────────────

def test_renders_a_valid_pdf():
    snap, register = _fixture()
    out = _render(snap, register)
    assert out.startswith(b"%PDF-") and out.rstrip().endswith(b"%%EOF")
    assert len(out) > 10_000


def test_renders_when_the_snapshot_predates_the_insight_layer():
    # An immutable snapshot cannot be backfilled, so an older report simply has
    # no Section 1. It must still render rather than raising.
    snap, register = _fixture()
    del snap["insights"]
    assert _render(snap, register).startswith(b"%PDF-")


def test_renders_with_no_findings_at_all():
    snap, register = _fixture()
    snap["findings"] = []
    snap["criticalFailures"] = snap["majorFailures"] = 0
    snap["insights"] = compute_report_insights(snap)
    assert _render(snap, register).startswith(b"%PDF-")


def test_renders_when_nothing_was_assessed():
    snap, register = _fixture()
    snap["overallScorePct"] = None
    snap["grade"]["showGrade"] = False
    snap["insights"] = compute_report_insights(snap)
    assert _render(snap, register).startswith(b"%PDF-")


# ─── the record of truth survives the re-layout ─────────────────────────────

def test_every_checkpoint_still_appears_in_the_register():
    snap, register = _fixture()
    text = _text(_render(snap, register))
    missing = [r["checkpointCode"] for r in register if r["checkpointCode"] not in text]
    assert not missing, f"{len(missing)} checkpoint(s) dropped: {missing[:5]}"
    assert len(register) == 120


def test_observation_text_is_reproduced_verbatim():
    snap, register = _fixture()
    flat = re.sub(r"\s+", " ", _text(_render(snap, register)))
    assert re.sub(r"\s+", " ", OBSERVATION) in flat


def test_integrity_digest_and_signoff_are_untouched():
    snap, register = _fixture()
    text = _text(_render(snap, register)).replace("\n", "")
    assert FULL_HASH in text
    assert "Record Integrity" in text
    assert "Sign-Off" in text and "S. Krishnan" in text


def test_iteration_history_still_prints():
    snap, register = _fixture()
    text = _text(_render(snap, register))
    assert "Auditee Response" in text and "Evidence attached." in text


# ─── the insight layer reaches the page ─────────────────────────────────────

def test_insight_summary_is_section_one():
    snap, register = _fixture()
    assert "1. Insight Summary" in _text(_render(snap, register))


def test_critical_banner_and_repeat_callout_render():
    snap, register = _fixture()
    text = _text(_render(snap, register))
    assert "the audit fails regardless of score" in text
    assert "repeat non-conformance" in text and "PI-PR-002" in text


def test_findings_register_is_grouped_by_severity():
    snap, register = _fixture()
    # The extractor collapses runs of spaces, so compare on normalised text.
    text = re.sub(r"[ \t]+", " ", _text(_render(snap, register)))
    assert "CRITICAL - 2 finding(s)" in text
    assert re.search(r"MAJOR - \d+ finding\(s\)", text)


def test_category_figures_are_charted_without_losing_the_table():
    snap, register = _fixture()
    text = _text(_render(snap, register))
    assert "UNDERLYING FIGURES" in text
    # Every discipline's raw counts survive alongside the bars.
    for d in DISCIPLINES:
        assert d in text


def test_every_category_percentage_on_the_page_is_the_same_number():
    """The bar and the table beneath it must not disagree.

    This is the defect verbatim: Section 6 printed Production at 85.0% on the
    bar and 88.3% in its own table three centimetres below, because the two
    read different keys computed by different formulas. Both now read points,
    so each discipline's percentage may appear only once as a value.
    """
    snap, register = _fixture()
    text = re.sub(r"[ \t]+", " ", _text(_render(snap, register)))

    points = {c["score_pct"] for c in snap["categoryScores"]}
    for c in snap["categoryScores"]:
        assert f"{c['score_pct']}%" in text, (
            f"{c['category_name']}: charted percentage {c['score_pct']}% missing")

    # Any pass-ratio value that is not ALSO some category's points score must be
    # absent. Set-based because two categories can legitimately collide on a
    # value — in this fixture EHS scores 92.3% on points, which is exactly HR's
    # pass-ratio, and a per-row check would read that coincidence as a failure.
    for c in snap["categoryScores"]:
        assessed = c["passed"] + c["partial"] + c["failed"]
        ratio = round((c["passed"] + 0.5 * c["partial"]) / assessed * 100, 1)
        if ratio not in points:
            assert f"{ratio}%" not in text, (
                f"{c['category_name']}: superseded pass-ratio {ratio}% still rendered")


def test_score_arithmetic_is_printed_so_a_reader_can_check_it():
    snap, register = _fixture()
    text = re.sub(r"[ \t]+", " ", _text(_render(snap, register)))
    # Headline: "311 of 357 points" under the dial.
    assert f"{snap['scoreObtained']} of {snap['scoreAllotted']} points" in text
    # Per category: "106/120 pts" on the bar and "106/120" in the Points column.
    for c in snap["categoryScores"]:
        assert f"{c['score_obtained']}/{c['score_allotted']}" in text
    # And the one sentence that says what the percentage means.
    assert "points earned / points available" in text
    assert "repeat finding -1" in text


def test_partial_count_is_no_longer_hidden_from_the_summary_line():
    # "32P 4F / 40" omitted Partial entirely, so the counts never summed to the
    # total even though partials earn points toward the percentage beside them.
    snap, register = _fixture()
    text = re.sub(r"[ \t]+", " ", _text(_render(snap, register)))
    c = next(c for c in snap["categoryScores"] if c["category_name"] == "PRODUCTION")
    expected = (f"{c['passed']}P {c['partial']}Ptl {c['failed']}F"
                + (f" {c['na']}NA" if c["na"] else "") + f" / {c['total']}")
    assert expected in text
    # …and the counts now actually add up to the total, which is the point.
    assert c["passed"] + c["partial"] + c["failed"] + c["na"] == c["total"]


# ─── sign-off prints the record, or says what is missing ────────────────────

_SIGNS = [
    {"role": "LEAD_AUDITOR", "userId": "u_lead", "name": "S. Krishnan",
     "designation": "HSE Manager", "disciplineCode": None, "signatureKind": "TYPED",
     "typedName": "S. Krishnan", "statement": "Conducted per the approved programme.",
     "signedAt": "2026-07-01T10:00:00+00:00"},
    {"role": "AUDITEE_OWNER", "userId": "u_anjali", "name": "Anjali Verma",
     "designation": "Admin", "disciplineCode": None, "signatureKind": "DRAWN",
     "typedName": None, "statement": None, "signedAt": "2026-07-01T10:05:00+00:00"},
    {"role": "DISCIPLINE_AUDITOR", "userId": "u_lead", "name": "S. Krishnan",
     "designation": "HSE Manager", "disciplineCode": "EHS", "signatureKind": "TYPED",
     "typedName": "S. Krishnan", "statement": None, "signedAt": "2026-07-01T10:07:00+00:00"},
]


def _render_signed(signs, summary):
    snap, register = _fixture()
    snap["signOffSummary"] = summary
    return _text(render_audit_report_pdf(
        {"id": "rpt_x", "reportType": "FINAL", "reportCode": "RPT-X-F01",
         "snapshot": snap, "snapshotHashFull": FULL_HASH, "signOffs": signs},
        generated_by_name="S. Krishnan", register=register, user_names=USER_NAMES))


def test_signoff_prints_name_role_and_when_it_was_signed():
    # The old renderer read `name`/`signedAt` off a payload that only ever
    # carried role + userId, so every line printed "LEAD_AUDITOR: -  -".
    text = _render_signed(_SIGNS, {"recorded": 3, "awaitingRoles": [],
                                   "missingRequiredRoles": []})
    assert "S. Krishnan" in text and "Anjali Verma" in text
    assert "HSE Manager" in text
    assert "01 Jul 2026" in text                       # a real signature time
    assert "Drawn signature on file" in text           # and how it was signed
    assert "Conducted per the approved programme." in text
    assert "DISCIPLINE_AUDITOR: -" not in text
    assert "All sign-offs required for closure were recorded." in text


def test_signoff_renders_a_variable_length_list_with_many_discipline_auditors():
    """Three disciplines, three separate auditor signatures, plus the required
    pair — the shape F01 actually had and F02 collapsed to two blank lines."""
    signs = _SIGNS[:2] + [
        {"role": "DISCIPLINE_AUDITOR", "userId": f"u{i}", "name": f"Auditor {d}",
         "designation": "Auditor", "disciplineCode": d, "signatureKind": "TYPED",
         "typedName": f"Auditor {d}", "statement": f"I conducted the {d} discipline.",
         "signedAt": f"2026-07-01T11:0{i}:00+00:00"}
        for i, d in enumerate(DISCIPLINES)
    ]
    text = _render_signed(signs, {"recorded": 5, "missingRequiredRoles": [],
                                  "unsignedDisciplines": [], "disciplinesSigned": 3,
                                  "disciplinesTotal": 3, "statement": "All recorded."})
    for d in DISCIPLINES:
        assert f"Auditor {d}" in text
        assert f"I conducted the {d} discipline." in text
        assert f"Discipline Auditor - {d}" in text
    assert text.count("Signed 01 Jul 2026") == 5   # every signer has a real time


def test_signoff_names_the_roles_that_have_not_signed():
    text = _render_signed(_SIGNS[:1], {"recorded": 1, "missingRequiredRoles": ["AUDITEE_OWNER"],
                                       "unsignedDisciplines": ["Production"],
                                       "disciplinesSigned": 2, "disciplinesTotal": 3,
                                       "statement": "Awaiting."})
    assert "Outstanding required sign-off: Auditee Owner." in text
    assert "Discipline sign-off outstanding (2 of 3 signed): Production." in text


def test_no_signoff_states_the_absence_rather_than_printing_blanks():
    text = _render_signed([], {"recorded": 0, "missingRequiredRoles": ["LEAD_AUDITOR", "AUDITEE_OWNER"],
                               "unsignedDisciplines": [], "disciplinesSigned": 0,
                               "disciplinesTotal": 0, "statement": "Awaiting."})
    assert "No sign-off has been recorded for this audit." in text
    assert "Outstanding required sign-off: Lead Auditor, Auditee Owner." in text


# ─── the contract that made this possible is gone ───────────────────────────

def test_report_generation_cannot_be_handed_sign_offs_by_a_caller():
    """The root cause, locked shut.

    `generate_report` used to accept `sign_offs` and freeze it verbatim into an
    immutable compliance document, so whatever the HTTP contract could carry
    (role + userId — no name, no timestamp) became the report's record of who
    signed. Widening that contract would move the same trust boundary outward;
    removing it is what stops a client asserting a signature at all.
    """
    import inspect
    from app.routers.audit_compliance import GenerateReportBody
    from app.services.audit_compliance import generate_report

    assert "sign_offs" not in inspect.signature(generate_report).parameters
    assert "signOffs" not in GenerateReportBody.model_fields
    # A client still posting the old field must not 422 — it is ignored.
    assert GenerateReportBody(**{"reportType": "FINAL", "signOffs": [
        {"role": "LEAD_AUDITOR", "userId": "u1"}]}).reportType == "FINAL"


def test_no_caller_smuggles_sign_offs_past_the_generator():
    """Seeders and scripts must exercise the same path as production.

    `seed_complete_internal_audit.py` used to pass the live signer list straight
    to the service, bypassing the contract the product uses — which is exactly
    why the seeded report looked correct while every real one did not.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    offenders = [
        f"{p.relative_to(root)}:{n}"
        for p in list((root / "scripts").rglob("*.py")) + list((root / "app").rglob("*.py"))
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "generate_report(" in line and "sign_offs" in line
    ]
    assert not offenders, f"sign_offs passed to generate_report at: {offenders}"


def test_footer_does_not_overprint_itself():
    snap, register = _fixture()
    text = _text(_render(snap, register))
    assert "CONFIDENTIAL" in text and "Page 2 of" in text
    # The pre-existing zero-width-cell bug rendered these on top of each other.
    assert "hashPage" not in text and "Rage" not in text
