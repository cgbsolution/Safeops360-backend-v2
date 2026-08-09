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

    rag = []
    for d in DISCIPLINES:
        c = cat[d]
        a = c["passed"] + c["partial"] + c["failed"]
        rag.append({"categoryId": d, "categoryName": d,
                    "pct": round((c["passed"] + 0.5 * c["partial"]) / a * 100, 1) if a else None,
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
        "categoryScores": [{"category_id": r["categoryId"], "category_name": r["categoryName"],
                            "score_pct": r["pct"], **{k: r[k] for k in
                                                      ("passed", "partial", "failed", "na", "total")}}
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


def test_footer_does_not_overprint_itself():
    snap, register = _fixture()
    text = _text(_render(snap, register))
    assert "CONFIDENTIAL" in text and "Page 2 of" in text
    # The pre-existing zero-width-cell bug rendered these on top of each other.
    assert "hashPage" not in text and "Rage" not in text
