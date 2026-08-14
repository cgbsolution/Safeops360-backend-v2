"""Department-segregated management-system audits — the parts that decide what
the auditor sees and what each of the two reports says.

The customer conducts ONE audit per department (HR / Admin / OHC) and assesses
each against both source sheets: the IMS one (ISO 9001 / 14001 / 45001) and the
EnMS one (ISO 50001). Four behaviours carry that, and each fails silently rather
than loudly if it drifts — which is why they are pinned here:

  1. **Three parameters, one verdict.** Conformance / Non-Conformance /
     Observation is a narrower FACE on the engine's grade + status, not a second
     state machine. If the mapping breaks, the audit still saves and still
     scores — it just scores the wrong number.
  2. **Risk grade stops being a submission gate** in that mode, because the
     customer's form has no risk column. If it does not, the audit is simply
     unsubmittable and no error explains why.
  3. **A report is scoped to its stream, entirely.** Every count, score, finding
     and clause on the IMS report must come from IMS checkpoints. A report that
     computed the audit-wide headline and printed an IMS-only register would be
     internally inconsistent in the one place that matters most.
  4. **A multi-standard line counts toward every standard it cites.** One IMS
     checkpoint is assessed against three ISO standards at once; aggregating on
     the joined display string invents a fourth standard and reports the three
     real ones as absent.

House no-DB style: everything here is a pure function over hand-built rows, in
the same shape as `test_audit_report_insights.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import page_grading as pg
from app.services.audit_compliance import (
    STREAMS,
    STREAM_META,
    _apply_page_grading,
    _build_report_snapshot,
    _clause_index,
    _finalizability,
    _standards_rollup,
    normalise_stream,
)


# ─── fixtures ───────────────────────────────────────────────────────────────


def _row(code, *, dept="DEPT_HR", stream="IMS", mode=pg.CONFORMANCE_TRISTATE,
         clauses=None, grade=None, status=None, score=None, allotted=None,
         risk=None, observation=None, value=None, assess="NOT_ASSESSED",
         state="OPEN", crit="major", seq=1, repl=None, pair=None):
    """A materialised checkpoint row, in the shape the service reads."""
    return SimpleNamespace(
        id=code, checkpointCode=code, checkpointQuestion=f"Requirement for {code}?",
        guidance="", requirementReference=" · ".join(
            f"{c['code']} {c['clause']}" for c in (clauses or [])
        ),
        standard=" · ".join(c["standard"] for c in (clauses or [])),
        standardClauses=clauses or [],
        categoryId=dept, categoryName={"DEPT_HR": "Human Resources",
                                       "DEPT_ADMIN": "Administration",
                                       "DEPT_OHC": "Occupational Health Centre"}[dept],
        categoryColor="#7C3AED", criticality=crit, responseType="page_grading",
        sequence=seq, orderIndex=seq,
        streamCode=stream, replicationKey=repl, pairKey=pair, conformanceMode=mode,
        requirementType="INTERNAL_REQUIREMENT",
        gradeAwarded=grade, complianceStatus=status,
        scoreObtained=score, scoreAllotted=allotted, riskGrade=risk,
        observation=observation, auditorNote=None,
        auditorResponse={"value": value, "text_observation": observation} if value else None,
        auditeeResponse=None, plantManagerReview=None, capa=None, capaId=None,
        assessmentStatus=assess, workflowState=state, currentRound=0,
        overallStatus="not_answered" if value is None else f"answered_{value}",
        requiresPhotoOnFail=False, autoTriggerCapaOnFail=False, capaSeverity=None,
        linkedSafeopsModule=None, routedToUserId=None, assignedOwnerId=None,
        assignedAuditorId=None, assignedById=None, assignedAt=None,
        isAdHoc=False, addedById=None, auditorEvidenceIds=[], auditeeEvidenceIds=[],
        finalizedAt=None, answeredAt=None, interactions=[],
    )


IMS_CLAUSES = [
    {"code": "QMS", "standard": "ISO 9001:2015", "clause": "9.2/10.2"},
    {"code": "EMS", "standard": "ISO 14001:2015", "clause": "9.2/10.2"},
    {"code": "OHSMS", "standard": "ISO 45001:2018", "clause": "9.2/10.2"},
]
ENMS_CLAUSES = [{"code": "EnMS", "standard": "ISO 50001:2018", "clause": "9.2"}]


def _audit(responses, *, status="submitted_pending_response"):
    return SimpleNamespace(
        id="a1", auditNumber="AUD-PI-2026-NW-0001", title="Q3 IMS + EnMS — North Works",
        plantId="p1", industryCode="PAGE_IMS", auditType="management_system_audit",
        leadAuditorUserId="u-lead", plantManagerUserId="u-pm", templateId=None,
        scopePresetUsed=None, selectedDisciplineIds=["DEPT_HR", "DEPT_ADMIN", "DEPT_OHC"],
        scheduledDate=None, submittedAt=None, closedAt=None, status=status,
        adHocCount=0, reopenCount=0, coAuditors=[], auditees=[], responses=responses,
        scopeAreas=[], scopeDepartments=[], scopeDescription="", actualStartAt=None,
        actualEndAt=None, scheduledStartTime="09:00", estimatedDurationHours=8,
        openingRemarks="", closingRemarks="", signOffs=None,
    )


# ── 1. Three parameters, one verdict ──────────────────────────────────────


@pytest.mark.parametrize(
    "verdict,grade,status,value,score",
    [
        ("CONFORMANCE", pg.GRADE_EFFECTIVE, pg.STATUS_COMPLIED, "pass", 3),
        ("NON_CONFORMANCE", pg.GRADE_MAJOR_IMPROVEMENT, pg.STATUS_NON_COMPLIANCE, "fail", 1),
        ("OBSERVATION", pg.GRADE_SOME_IMPROVEMENT, pg.STATUS_NEW_OBSERVATION, "partial", 2),
    ],
)
def test_a_tristate_verdict_writes_the_engines_grade_and_status(
    verdict, grade, status, value, score
):
    """The point of the narrowing: everything downstream — routing, auto-CAPA,
    the department rollup, both reports — keeps reading grade + status and needs
    no branch of its own."""
    resp = _row("PI-HR-IMS-001", clauses=IMS_CLAUSES)
    merged: dict = {}
    got = _apply_page_grading(resp, {"conformance": verdict}, merged)

    assert resp.gradeAwarded == grade
    assert resp.complianceStatus == status
    assert got == value
    assert merged["value"] == value
    assert resp.scoreAllotted == pg.FULL_SCORE
    assert resp.scoreObtained == score


def test_the_customers_own_labels_are_accepted():
    """The workbook is what anyone pastes from, so "Non-Conformance" has to land
    on NON_CONFORMANCE rather than silently becoming an unset field."""
    resp = _row("PI-HR-IMS-002", clauses=IMS_CLAUSES)
    _apply_page_grading(resp, {"conformance": "Non-Conformance"}, {})
    assert resp.complianceStatus == pg.STATUS_NON_COMPLIANCE


def test_an_unknown_conformance_token_is_refused_not_ignored():
    """A verdict that quietly does nothing is how an auditor concludes a
    department is clean when it was never answered."""
    resp = _row("PI-HR-IMS-003", clauses=IMS_CLAUSES)
    with pytest.raises(ValueError, match="Unknown conformance"):
        _apply_page_grading(resp, {"conformance": "Mostly fine"}, {})


def test_clearing_a_tristate_verdict_clears_the_grade_and_status():
    resp = _row("PI-HR-IMS-004", clauses=IMS_CLAUSES,
                grade=pg.GRADE_EFFECTIVE, status=pg.STATUS_COMPLIED, score=3, allotted=3)
    _apply_page_grading(resp, {"conformance": None}, {})
    assert resp.gradeAwarded is None
    assert resp.complianceStatus is None
    assert resp.scoreAllotted is None


def test_tristate_never_reaches_the_statuses_it_dropped():
    """The deliberate cost of matching the customer's form, asserted so it is a
    decision on the record rather than an accident: N/A, MAS (N/A) and the two
    repeat variants are unreachable, so every checkpoint stays in the score
    denominator and a repeat scores the same as a first finding."""
    reachable = {s for _v, (_g, s) in pg.TRISTATE_TO_GRADE_STATUS.items()}
    assert reachable == {pg.STATUS_COMPLIED, pg.STATUS_NON_COMPLIANCE,
                         pg.STATUS_NEW_OBSERVATION}
    assert not (reachable & pg.REPEAT_STATUSES)
    assert not (reachable & pg.NOT_APPLICABLE_STATUSES)


# ── 2. Risk grade: kept, but not a gate ───────────────────────────────────


def test_risk_grade_is_not_required_in_tristate_but_is_in_full():
    """The customer's IMS/EnMS form has no risk column. Gating submission on a
    field their auditors are never given would make the audit unsubmittable."""
    assert pg.requires_risk_grade(pg.GRADE_MAJOR_IMPROVEMENT, pg.CONFORMANCE_FULL)
    assert not pg.requires_risk_grade(pg.GRADE_MAJOR_IMPROVEMENT, pg.CONFORMANCE_TRISTATE)
    # An absent mode is the historic behaviour, not the new one.
    assert pg.requires_risk_grade(pg.GRADE_MAJOR_IMPROVEMENT, None)


def test_a_risk_grade_set_on_a_tristate_finding_survives_a_re_save():
    """`requires` and `carries` are different questions. Collapsing them would
    erase every risk grade an auditor deliberately set on a Non-Conformance."""
    resp = _row("PI-HR-IMS-005", clauses=IMS_CLAUSES, risk=pg.RISK_HIGH)
    _apply_page_grading(resp, {"conformance": "NON_CONFORMANCE"}, {})
    assert resp.riskGrade == pg.RISK_HIGH


def test_re_grading_to_conformance_clears_a_stale_risk_grade():
    """A checkpoint that is no longer a finding must not keep reporting High."""
    resp = _row("PI-HR-IMS-006", clauses=IMS_CLAUSES, risk=pg.RISK_HIGH)
    _apply_page_grading(resp, {"conformance": "CONFORMANCE"}, {})
    assert resp.riskGrade is None


# ── 3. A report is scoped to its stream, entirely ─────────────────────────


def _mixed_audit():
    """Two departments, both streams, with a different verdict on each side so a
    leak between them shows up as a wrong number rather than a wrong shape."""
    return _audit([
        # IMS: two conformances, one non-conformance -> 7 of 9 points.
        _row("PI-HR-IMS-001", stream="IMS", clauses=IMS_CLAUSES, seq=1,
             grade=pg.GRADE_EFFECTIVE, status=pg.STATUS_COMPLIED, score=3, allotted=3,
             value="pass", assess="PASS", state="PASSED", repl="IMS-001"),
        _row("PI-HR-IMS-002", stream="IMS", clauses=IMS_CLAUSES, seq=2,
             grade=pg.GRADE_EFFECTIVE, status=pg.STATUS_COMPLIED, score=3, allotted=3,
             value="pass", assess="PASS", state="PASSED", repl="IMS-002"),
        _row("PI-ADMIN-IMS-041", dept="DEPT_ADMIN", stream="IMS", clauses=IMS_CLAUSES, seq=3,
             grade=pg.GRADE_MAJOR_IMPROVEMENT, status=pg.STATUS_NON_COMPLIANCE,
             score=1, allotted=3, value="fail", assess="FAIL", state="RESOLVED",
             observation="Calibration plan not maintained.", repl="IMS-041"),
        # EnMS: one conformance -> 3 of 3 points.
        _row("PI-HR-ENMS-001", stream="ENMS", clauses=ENMS_CLAUSES, seq=4,
             grade=pg.GRADE_EFFECTIVE, status=pg.STATUS_COMPLIED, score=3, allotted=3,
             value="pass", assess="PASS", state="PASSED", repl="ENMS-001"),
    ])


def test_each_report_counts_only_its_own_stream():
    audit = _mixed_audit()
    ims = _build_report_snapshot(audit, "INTERIM", stream="IMS")
    enms = _build_report_snapshot(audit, "INTERIM", stream="ENMS")

    assert ims["checkpointsTotal"] == 3
    assert enms["checkpointsTotal"] == 1
    # The headline percentage is this report's points over this report's
    # allotment — 7/9 and 3/3, NOT 10/12 twice.
    assert (ims["scoreObtained"], ims["scoreAllotted"]) == (7, 9)
    assert (enms["scoreObtained"], enms["scoreAllotted"]) == (3, 3)
    assert enms["overallScorePct"] == 100.0
    assert ims["overallScorePct"] != enms["overallScorePct"]


def test_a_finding_appears_on_its_own_report_and_not_the_other():
    audit = _mixed_audit()
    ims = _build_report_snapshot(audit, "INTERIM", stream="IMS")
    enms = _build_report_snapshot(audit, "INTERIM", stream="ENMS")
    assert [f["checkpointCode"] for f in ims["findings"]] == ["PI-ADMIN-IMS-041"]
    assert enms["findings"] == []


def test_a_report_names_which_of_the_two_documents_it_is():
    """Null on a whole-audit report, which is how a reader and the PDF renderer
    tell a single-report audit from one half of a pair."""
    audit = _mixed_audit()
    ims = _build_report_snapshot(audit, "INTERIM", stream="IMS")
    assert ims["reportStream"] == "IMS"
    assert ims["reportStreamLabel"] == "IMS"
    assert "ISO 9001" in ims["reportStreamStandards"]

    enms = _build_report_snapshot(audit, "INTERIM", stream="ENMS")
    assert enms["reportStreamLabel"] == "EnMS"
    assert enms["reportStreamStandards"] == "ISO 50001:2018"

    whole = _build_report_snapshot(audit, "INTERIM")
    assert whole["reportStream"] is None
    assert whole["reportStreamLabel"] is None
    assert whole["checkpointsTotal"] == 4


def test_a_department_report_says_departments_not_disciplines():
    """`disciplineRag` keeps its key for API consumers, but the label printed on
    the cover of a document a certification body reads has to name what the rows
    actually are."""
    ims = _build_report_snapshot(_mixed_audit(), "INTERIM", stream="IMS")
    assert ims["scopeAxis"] == "DEPARTMENT"
    assert "department" in ims["disciplinesInScopeLabel"]
    assert "discipline" not in ims["disciplinesInScopeLabel"]


def test_the_departments_on_a_report_are_only_those_the_stream_reaches():
    """The EnMS report here covers HR alone, because Admin holds no EnMS row in
    this fixture. Listing Admin at 0% would report a department as failing when
    it was never in that report's scope."""
    enms = _build_report_snapshot(_mixed_audit(), "INTERIM", stream="ENMS")
    assert [d["categoryId"] for d in enms["disciplineRag"]] == ["DEPT_HR"]


def test_finalizability_is_per_stream():
    """The IMS report must not be held back because an EnMS finding is still
    with its auditee — the two are separate documents."""
    rows = _mixed_audit().responses
    rows.append(_row("PI-HR-ENMS-002", stream="ENMS", clauses=ENMS_CLAUSES, seq=5,
                     grade=pg.GRADE_MAJOR_IMPROVEMENT, status=pg.STATUS_NON_COMPLIANCE,
                     score=1, allotted=3, value="fail", assess="FAIL",
                     state="AWAITING_AUDITEE", observation="Still open."))
    audit = _audit(rows)

    assert _finalizability(audit, stream="IMS")["finalizable"] is True
    assert _finalizability(audit, stream="ENMS")["finalizable"] is False
    # And the whole-audit view still sees the blocker.
    assert _finalizability(audit)["blockerCount"] == 1


# ── 4. A multi-standard line counts toward every standard it cites ────────


def test_one_ims_checkpoint_counts_toward_all_three_iso_standards():
    """Aggregating on the joined display string would invent a fourth standard
    named "ISO 9001:2015 · ISO 14001:2015 · ISO 45001:2018" and report the three
    real ones as absent — on the report a certification body reads per standard.

    The totals therefore sum to MORE than the checkpoint count, which is
    correct: a line assessed against three standards is three standards' worth
    of evidence.
    """
    rows = [
        _row("PI-HR-IMS-001", clauses=IMS_CLAUSES, value="pass",
             score=3, allotted=3, assess="PASS"),
        _row("PI-HR-ENMS-001", stream="ENMS", clauses=ENMS_CLAUSES, value="pass",
             score=3, allotted=3, assess="PASS"),
    ]
    rollup = {r["standard"]: r for r in _standards_rollup(rows)}
    assert set(rollup) == {
        "ISO 9001:2015", "ISO 14001:2015", "ISO 45001:2018", "ISO 50001:2018",
    }
    for std in ("ISO 9001:2015", "ISO 14001:2015", "ISO 45001:2018"):
        assert rollup[std]["total"] == 1
        assert rollup[std]["pass"] == 1
        assert rollup[std]["scorePct"] == 100.0


def test_the_clause_index_files_a_row_under_every_clause_it_cites():
    """An assessor opens the index looking up "ISO 45001 §6.1.2". A row filed
    under all three standards joined into one string is a row they cannot find
    under any of them."""
    rows = [_row("PI-HR-IMS-001", clauses=IMS_CLAUSES, value="fail", assess="FAIL")]
    idx = {(e["standard"], e["clause"]) for e in _clause_index(rows)}
    assert idx == {
        ("ISO 9001:2015", "9.2/10.2"),
        ("ISO 14001:2015", "9.2/10.2"),
        ("ISO 45001:2018", "9.2/10.2"),
    }


def test_a_row_without_structured_clauses_still_indexes_on_its_free_text():
    """Every library except the department one carries only the display strings,
    and 2,500 existing rows have no `standardClauses` at all."""
    row = _row("LEGACY-001", clauses=[], value="pass", assess="PASS")
    row.standard, row.requirementReference = "SA8000:2014", "Clause 3.1"
    assert _standards_rollup([row])[0]["standard"] == "SA8000:2014"
    assert _clause_index([row])[0]["clause"] == "Clause 3.1"


# ── The stream vocabulary ─────────────────────────────────────────────────


def test_normalise_stream_accepts_any_casing_and_refuses_anything_else():
    assert normalise_stream("ims") == "IMS"
    assert normalise_stream(" EnMS ") == "ENMS"
    # None means "the whole audit" — a legal value, not an error.
    assert normalise_stream(None) is None
    assert normalise_stream("QMS") is None


def test_every_stream_has_a_report_title_and_names_its_standards():
    """Both appear on the cover of an issued document, so neither may be blank."""
    for code in STREAMS:
        meta = STREAM_META[code]
        assert meta["reportTitle"].strip()
        assert meta["standards"].strip()
        assert meta["label"].strip()
