"""PIL/MR/F04-R1 — Internal Audit NC Report: RCA + CAPA per non-conformity.

The form is the specification. Revision R1 states its own reason for existing —
"Preventive action is replaced with Root Cause Analysis in NC Report format" —
so the rules these tests pin are the form's rules, not ours:

  * the ladder has a FLOOR of five Whys and no ceiling (the worked example on
    the form runs six)
  * a root cause naming the system is mandatory
  * no Correction or Preventive Action may be planned until the RCA is approved
  * two signatures close an NC: the auditor's, then the M.R.'s, in that order
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.nc_rca_capa import (
    ACTION_TYPE_FOR_CORRECTION,
    ACTION_TYPE_FOR_PREVENTIVE,
    NC_STAGE_ACTION,
    NC_STAGE_HOLDER,
    NC_STAGES,
    PIL_MIN_WHY_LEVELS,
    _capa_severity,
    _clause_text,
    _days_until,
    _stage,
    assert_actions_unlocked,
    assert_auditee_may_edit,
    viewer_rights,
    issue_nc_report,
    mr_sign_off,
    save_auditee_action,
    seed_why_payload,
    update_auditor_section,
    validate_why_payload,
    verify_nc,
)
from app.services.rca import generate_rca_summary, is_empty_rca_data


def _ladder(n: int) -> list[dict[str, str]]:
    return [{"question": f"Why {i}?", "answer": f"Because {i}"} for i in range(1, n + 1)]


def _payload(**over):
    base = {
        "problemStatement": "Objectives for the HR department were not updated for FY26.",
        "whys": _ladder(PIL_MIN_WHY_LEVELS),
        "rootCause": "The management review procedure sets no frequency for objective revision.",
        "pilNcReport": {"formNo": "PIL/MR/F04-R1", "minLevels": PIL_MIN_WHY_LEVELS},
    }
    base.update(over)
    return base


# ── the ladder: a floor of five, not a form with five rows ───────────


def test_a_complete_ladder_validates():
    assert validate_why_payload(_payload()) == []


def test_four_whys_is_not_enough():
    problems = validate_why_payload(_payload(whys=_ladder(4)))
    assert any(str(PIL_MIN_WHY_LEVELS) in p for p in problems)


def test_the_ladder_has_no_ceiling():
    """The worked example printed on the form runs SIX levels. A form that
    stopped at five would teach auditees to pad to five and stop, which is the
    behaviour revision R1 was written to remove."""
    assert validate_why_payload(_payload(whys=_ladder(6))) == []
    assert validate_why_payload(_payload(whys=_ladder(9))) == []


def test_unanswered_whys_do_not_count_toward_the_floor():
    """Five rows on screen is not five levels of analysis."""
    whys = _ladder(2) + [{"question": "Why 3?", "answer": "   "}]
    problems = validate_why_payload(_payload(whys=whys))
    assert any(str(PIL_MIN_WHY_LEVELS) in p for p in problems)


def test_a_gap_in_the_chain_is_rejected():
    """Level 4 answering level 2 is not a chain. It reads as complete on a
    count, which is exactly why counting alone is not the check."""
    whys = _ladder(5)
    whys[2]["answer"] = ""
    problems = validate_why_payload(_payload(whys=whys))
    assert any("chain is broken" in p for p in problems)


def test_root_cause_is_mandatory():
    problems = validate_why_payload(_payload(rootCause=""))
    assert any("root cause" in p.lower() for p in problems)


def test_the_nonconformity_must_be_stated():
    problems = validate_why_payload(_payload(problemStatement="  "))
    assert problems


def test_every_problem_is_reported_at_once():
    """An auditee who left three things blank is told all three, not sent round
    the loop three times."""
    problems = validate_why_payload(
        {"problemStatement": "", "whys": _ladder(1), "rootCause": ""}
    )
    assert len(problems) >= 3


def test_an_empty_or_absent_payload_is_not_silently_valid():
    assert validate_why_payload(None)
    assert validate_why_payload({})
    assert validate_why_payload("five whys, honest")


# ── the seeded form ──────────────────────────────────────────────────


def test_the_seeded_ladder_is_blank_but_shaped():
    p = seed_why_payload(
        nonconformity="Objectives not updated.", requirement="Objectives are reviewed annually",
        clause="ISO 9001:2015 6.2", department="Human Resources", stream="IMS",
    )
    assert len(p["whys"]) == PIL_MIN_WHY_LEVELS
    assert p["rootCause"] == ""
    # Blank means blank: a seeded form must not validate.
    assert validate_why_payload(p)


def test_the_suggested_first_why_comes_from_the_requirement_not_the_observation():
    """The form's own example starts from the failed requirement. Starting an
    auditee at "why did the observation happen" reliably produces a first Why
    about the symptom, and the ladder never reaches the system."""
    p = seed_why_payload(
        nonconformity="No evidence of review seen.",
        requirement="Process for achieving the set objectives is effective",
        clause=None, department="HR", stream="IMS",
    )
    suggested = p["pilNcReport"]["suggestedFirstWhy"]
    assert suggested.startswith("Why")
    assert "objectives" in suggested.lower()


def test_the_suggestion_stays_out_of_the_ladder_itself():
    """`is_empty_rca_data` counts a why-row as filled if it carries a QUESTION.
    Seeding the ladder would make a blank form report as having analysis in it,
    to that helper and to anything later built on it."""
    p = seed_why_payload(
        nonconformity="x", requirement="Objectives are reviewed", clause=None,
        department=None, stream=None,
    )
    assert all(w["question"] == "" and w["answer"] == "" for w in p["whys"])


def test_the_form_context_travels_with_the_payload():
    p = seed_why_payload(
        nonconformity="x", requirement="y", clause="ISO 50001 9.3",
        department="Administration", stream="ENMS",
    )
    assert p["pilNcReport"]["formNo"] == "PIL/MR/F04-R1"
    assert p["pilNcReport"]["clause"] == "ISO 50001 9.3"
    assert p["pilNcReport"]["stream"] == "ENMS"


def test_the_payload_is_the_platform_five_why_contract():
    """The ladder reuses `services.rca`'s FIVE_WHY shape verbatim, so the
    existing summary and emptiness helpers work on it unchanged. A bespoke shape
    here would fork every downstream reader of an RCA."""
    assert is_empty_rca_data("FIVE_WHY", seed_why_payload(
        nonconformity="", requirement="Objectives are set", clause=None,
        department=None, stream=None,
    ))
    summary = generate_rca_summary("FIVE_WHY", _payload())
    assert summary and "Root cause:" in summary


# ── the gate ─────────────────────────────────────────────────────────


def _capa(**over):
    # Default is a RELEASED CAPA — the normal working state. Tests about the
    # gate pass state="UNDER_RCA" explicitly, so the lock is always visible in
    # the test that asserts it rather than hidden in this helper.
    base = dict(
        id="capa1", state="ACTIONS_PLANNED", rcaRecordId="rca1", capaNumber="CAPA-AUD-2026-NW-001",
        verificationResult=None, verificationEvidence=None, verificationCompletedAt=None,
        verificationCompletedByUserId=None, stateChangedAt=None, stateChangedByUserId=None,
        closedAt=None, closedByUserId=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_actions_are_locked_while_the_root_cause_is_under_analysis():
    with pytest.raises(ValueError, match="approved"):
        assert_actions_unlocked(_capa(state="UNDER_RCA"))


def test_the_gate_is_narrow_enough_not_to_touch_other_modules():
    """UNDER_RCA alone must not lock actions — other modules park CAPAs there
    for reasons of their own. It is UNDER_RCA *bound to a governed RCA record*
    that means "this is an NC report"."""
    assert_actions_unlocked(_capa(state="UNDER_RCA", rcaRecordId=None)) is None


def test_the_gate_opens_once_the_rca_is_approved():
    assert_actions_unlocked(_capa(state="ACTIONS_PLANNED")) is None


# ── the derived register stage ───────────────────────────────────────


def _finding(**over):
    base = dict(
        id="f1", rcaId="rca1", rcaStatus="APPROVED", capaId="capa1", ncrNumber="01",
        auditorSignedAt=None, auditorSignedById=None, mrSignedAt=None, mrSignedById=None,
        status="IN_REMEDIATION", verificationDetails=None, dueDate=date(2026, 9, 30),
        closedAt=None, closedById=None,
        # Custody: issued to the auditee AND returned, unless a test says otherwise.
        issuedAt=datetime(2026, 8, 1, tzinfo=timezone.utc), issuedById="auditor-1",
        auditeeSubmittedAt=datetime(2026, 8, 10, tzinfo=timezone.utc),
        auditeeSubmittedById="auditee-1",
        requirementText="Objectives are reviewed annually.",
        observedNonconformity="FY26 objectives not updated.",
        gradeText="Major Improvement Needed", clauseNo="ISO 9001:2015 6.2",
        evidenceNote="Register sighted.", orgRepresentativeId="pm-1",
        ownerId="auditee-1",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _action(status="PROPOSED", action_type=ACTION_TYPE_FOR_CORRECTION):
    return SimpleNamespace(id="a1", status=status, actionType=action_type)


def test_a_nonconformity_with_no_report_is_not_raised():
    assert _stage(_finding(rcaId=None, rcaStatus=None), None, []) == "NOT_RAISED"


def test_a_raised_report_starts_with_the_auditor_not_the_auditee():
    """PIL/MR/F04-R1 begins in the auditor's hands — the yellow half is theirs,
    and the auditee sees nothing until it is issued. The first model started
    every NC at the auditee, which is the paper form read backwards."""
    f = _finding(issuedAt=None, auditeeSubmittedAt=None)
    assert _stage(f, _capa(), []) == "WITH_AUDITOR_DRAFT"


def test_issuing_moves_custody_to_the_auditee():
    f = _finding(issuedAt=datetime.now(timezone.utc), auditeeSubmittedAt=None)
    assert _stage(f, _capa(), []) == "WITH_AUDITEE"


def test_returning_moves_custody_back_to_the_auditor():
    assert _stage(_finding(), _capa(), [_action("COMPLETED")]) == "WITH_AUDITOR_VERIFY"


def test_the_auditor_signature_hands_it_to_the_mr():
    f = _finding(auditorSignedAt=datetime.now(timezone.utc))
    assert _stage(f, _capa(), [_action("COMPLETED")]) == "WITH_MR"


def test_the_mr_signature_closes_it():
    f = _finding(auditorSignedAt=datetime.now(timezone.utc),
                 mrSignedAt=datetime.now(timezone.utc))
    assert _stage(f, _capa(), [_action("COMPLETED")]) == "CLOSED"


def test_every_stage_names_a_holder_and_an_action():
    """The register renders both from these tables; a stage missing from either
    shows a blank 'who has it' or a blank 'what next' column."""
    for stage in NC_STAGES:
        assert stage in NC_STAGE_ACTION
        assert stage in NC_STAGE_HOLDER
    assert NC_STAGE_HOLDER["WITH_AUDITEE"] == "AUDITEE"
    assert NC_STAGE_HOLDER["WITH_AUDITOR_VERIFY"] == "AUDITOR"
    assert NC_STAGE_HOLDER["CLOSED"] is None


# ── the custody gates ───────────────────────────────────────


def test_the_auditee_cannot_write_before_the_report_is_issued():
    with pytest.raises(ValueError, match="not been issued"):
        assert_auditee_may_edit(_finding(issuedAt=None, auditeeSubmittedAt=None))


def test_the_auditee_cannot_write_after_returning_it():
    """Editing the analysis after the form has gone back would change what the
    auditor is verifying against."""
    with pytest.raises(ValueError, match="returned to the"):
        assert_auditee_may_edit(_finding())


def test_the_auditee_may_write_while_they_hold_it():
    f = _finding(issuedAt=datetime.now(timezone.utc), auditeeSubmittedAt=None)
    assert assert_auditee_may_edit(f) is None


def test_the_auditor_section_locks_once_issued():
    """Changing the stated requirement under an auditee who is answering it
    rewrites the question. Correcting an issued NC means recalling it."""
    with pytest.raises(ValueError, match="already with the auditee"):
        asyncio.run(update_auditor_section(
            _FakeDb(), _finding(), data={"requirementText": "changed"}, actor_id="a"))


def test_the_auditor_section_is_writable_before_issue():
    f = _finding(issuedAt=None, auditeeSubmittedAt=None)
    asyncio.run(update_auditor_section(
        _FakeDb(), f, data={"requirementText": "Objectives are reviewed each April."},
        actor_id="auditor-1"))
    assert f.requirementText == "Objectives are reviewed each April."


def test_an_incomplete_auditor_section_cannot_be_issued():
    """The auditee would otherwise discover the gap only after opening it."""
    f = _finding(issuedAt=None, auditeeSubmittedAt=None, requirementText="",
                 observedNonconformity="", gradeText="")
    with pytest.raises(ValueError, match="Requirements"):
        asyncio.run(issue_nc_report(_FakeDb(), f, actor_id="auditor-1"))


def test_issuing_stamps_custody():
    f = _finding(issuedAt=None, auditeeSubmittedAt=None, status="OPEN")
    out = asyncio.run(issue_nc_report(_FakeDb(), f, actor_id="auditor-1"))
    assert out["stage"] == "WITH_AUDITEE"
    assert f.issuedAt is not None and f.issuedById == "auditor-1"


def test_a_report_cannot_be_issued_twice():
    with pytest.raises(ValueError, match="already been issued"):
        asyncio.run(issue_nc_report(_FakeDb(), _finding(), actor_id="auditor-1"))


# ── closure: two signatures, in order ────────────────────────────────


class _FakeDb:
    """Enough of AsyncSession for the closure functions: `get`, `execute`, `flush`."""

    def __init__(self, obj=None, rows=()):
        self._obj = obj
        self._rows = list(rows)
        self.flushed = 0

    async def get(self, _model, _id):
        return self._obj

    async def execute(self, _stmt):
        rows = self._rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def flush(self):
        self.flushed += 1


def test_verification_records_the_auditors_signature_and_details():
    finding, capa = _finding(), _capa(state="ACTIONS_PLANNED")
    out = asyncio.run(verify_nc(
        _FakeDb(capa, rows=[]), finding,
        verification_details="Re-checked the FY26 objective register on 12 Aug; updated and signed.",
        result="EFFECTIVE", actor_id="auditor-1",
    ))
    assert out["result"] == "EFFECTIVE"
    assert capa.state == "VERIFIED"
    assert finding.auditorSignedById == "auditor-1"
    assert finding.verificationDetails.startswith("Re-checked")


def test_effectiveness_cannot_be_verified_while_actions_are_open():
    with pytest.raises(ValueError, match="still open"):
        asyncio.run(verify_nc(
            _FakeDb(_capa(state="ACTIONS_PLANNED"), rows=[_action("IN_PROGRESS")]),
            _finding(), verification_details="Looks fine to me.",
            result="EFFECTIVE", actor_id="auditor-1",
        ))


def test_nothing_can_be_verified_while_the_auditee_still_holds_the_form():
    """Verification is the box at the FOOT of the form, under the auditee's
    half. It cannot be reached over their heads."""
    with pytest.raises(ValueError, match="still with the auditee"):
        asyncio.run(verify_nc(
            _FakeDb(_capa(state="ACTIONS_PLANNED"), rows=[]),
            _finding(auditeeSubmittedAt=None),
            verification_details="Closing this without waiting for them.",
            result="EFFECTIVE", actor_id="auditor-1",
        ))


def test_nothing_can_be_verified_before_the_rca_is_approved():
    with pytest.raises(ValueError, match="not approved"):
        asyncio.run(verify_nc(
            _FakeDb(_capa(state="UNDER_RCA"), rows=[]), _finding(rcaStatus="DRAFT"),
            verification_details="Signing this off early.",
            result="EFFECTIVE", actor_id="auditor-1",
        ))


def test_an_ineffective_recheck_reopens_rather_than_closes():
    """A re-check that found the nonconformity still there is not a closure.
    The NC keeps its NCR number — a second NCR for the same finding would make
    the closure rate look better than it is."""
    finding, capa = _finding(), _capa(state="ACTIONS_PLANNED")
    out = asyncio.run(verify_nc(
        _FakeDb(capa, rows=[]), finding,
        verification_details="Objective register still shows FY25 targets.",
        result="INEFFECTIVE", actor_id="auditor-1",
    ))
    assert out["reopened"] is True
    assert capa.state == "ACTIONS_PLANNED"
    assert finding.status == "IN_REMEDIATION"
    assert finding.ncrNumber == "01"
    # Custody returns to the AUDITEE, but the report stays issued — this is the
    # same form coming round again, not a new one.
    assert finding.auditeeSubmittedAt is None
    assert finding.issuedAt is not None
    assert _stage(finding, capa, []) == "WITH_AUDITEE"
    # The signature belonged to a verification that failed; keeping it would
    # present a reopened NC as auditor-signed.
    assert finding.auditorSignedAt is None


def test_the_mr_cannot_sign_before_the_auditor():
    with pytest.raises(ValueError, match="verification of effective closure"):
        asyncio.run(mr_sign_off(_FakeDb(_capa()), _finding(), actor_id="mr-1"))


def test_the_mr_signature_closes_the_nc_and_its_capa():
    finding = _finding(auditorSignedAt=datetime.now(timezone.utc), auditorSignedById="auditor-1")
    capa = _capa(state="VERIFIED")
    out = asyncio.run(mr_sign_off(_FakeDb(capa), finding, actor_id="mr-1"))
    assert out["status"] == "CLOSED"
    assert finding.mrSignedById == "mr-1"
    assert finding.status == "CLOSED" and finding.closedAt is not None
    assert capa.state == "CLOSED"


def test_an_nc_cannot_be_closed_twice():
    finding = _finding(
        auditorSignedAt=datetime.now(timezone.utc), mrSignedAt=datetime.now(timezone.utc)
    )
    with pytest.raises(ValueError, match="already closed"):
        asyncio.run(mr_sign_off(_FakeDb(_capa()), finding, actor_id="mr-1"))


def test_an_unknown_verification_result_is_refused():
    with pytest.raises(ValueError, match="Unknown verification result"):
        asyncio.run(verify_nc(
            _FakeDb(_capa(state="ACTIONS_PLANNED")), _finding(),
            verification_details="x" * 20, result="PROBABLY_FINE", actor_id="a",
        ))


# ── the form's smaller contracts ─────────────────────────────────────


def test_correction_and_preventive_action_are_distinct_action_types():
    """The form has both boxes and they are answered separately; collapsing
    them would make "what did we do now" and "what stops it recurring"
    indistinguishable in the register."""
    assert ACTION_TYPE_FOR_CORRECTION != ACTION_TYPE_FOR_PREVENTIVE


def test_a_multi_standard_ims_line_reports_every_clause_it_cites():
    """An IMS checkpoint is assessed against up to three ISO standards at once.
    The form's "Clause No" box has to name all of them."""
    response = SimpleNamespace(
        standardClauses=[
            {"standard": "ISO 9001:2015", "clause": "6.2"},
            {"standard": "ISO 14001:2015", "clause": "6.2"},
            {"standard": "ISO 45001:2018", "clause": "6.2"},
        ],
        requirementReference="6.2",
    )
    text = _clause_text(response)
    assert "9001" in text and "14001" in text and "45001" in text


def test_clause_falls_back_to_free_text_for_libraries_with_no_structured_clauses():
    response = SimpleNamespace(standardClauses=[], requirementReference="Cl. 8.5.1")
    assert _clause_text(response) == "Cl. 8.5.1"


def test_severity_gradient_carries_into_the_capa():
    assert _capa_severity("CRITICAL_NC") == "CRITICAL"
    assert _capa_severity("MAJOR_NC") == "HIGH"
    assert _capa_severity("MINOR_NC") == "MODERATE"


def test_a_backdated_due_date_still_yields_a_workable_closure_target():
    """A CAPA created already-overdue reports an SLA breach on the day it is
    raised, which teaches people to ignore the breach count."""
    assert _days_until(date.today() - timedelta(days=400)) >= 7
    assert _days_until(None) > 0


# ── the two halves belong to two PEOPLE, not just two stages ─────────


def _audit(**over):
    base = dict(
        id="aud1", auditNumber="AUD-PI-2026-NW-0043", plantId="p1", title="t",
        leadAuditorUserId="auditor-1",
        coAuditors=[{"userId": "co-auditor-1"}],
        auditees=[{"userId": "auditee-hr", "responsibleCategories": ["DEPT_HR"]},
                  {"userId": "auditee-admin", "responsibleCategories": ["DEPT_ADMIN"]}],
        plantManagerUserId="mr-1", actualEndAt=None, scheduledDate=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _response(**over):
    base = dict(
        id="r1", categoryId="DEPT_HR", categoryName="Human Resources", streamCode="IMS",
        checkpointCode="PI-HR-IMS-008", checkpointQuestion="HIRA", observation="",
        standardClauses=[], requirementReference="OHSMS 6.1.2", gradeAwarded=None,
        complianceStatus=None, auditorNote=None, auditorEvidenceIds=[],
        assignedOwnerId="auditee-hr", routedToUserId=None, assignedAuditorId=None,
        sequence=1,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _issued(**over):
    """A report currently with the auditee."""
    return _finding(auditeeSubmittedAt=None, ownerId="auditee-hr", **over)


def test_the_auditor_cannot_write_the_auditees_analysis():
    """The single rule the form's colour key exists to enforce. `UPDATE` is held
    by the auditee roles AND every auditor role, so a permission-only gate let
    the lead auditor author the response they would later verify."""
    r = viewer_rights(_audit(), _issued(), _response(), "auditor-1")
    assert r["isAuditor"] is True
    assert r["canEditAuditeeHalf"] is False
    assert "ISO 19011" in r["auditeeLockReason"]


def test_a_co_auditor_is_locked_out_too():
    r = viewer_rights(_audit(), _issued(), _response(), "co-auditor-1")
    assert r["canEditAuditeeHalf"] is False


def test_the_named_auditee_can_write_it():
    r = viewer_rights(_audit(), _issued(), _response(), "auditee-hr")
    assert r["isAuditee"] is True
    assert r["canEditAuditeeHalf"] is True
    assert r["auditeeLockReason"] is None


def test_an_auditee_of_another_department_cannot():
    """A Human Resources auditee is not the auditee of an Administration NC."""
    r = viewer_rights(_audit(), _issued(), _response(), "auditee-admin")
    assert r["canEditAuditeeHalf"] is False


def test_a_bystander_cannot():
    r = viewer_rights(_audit(), _issued(), _response(), "someone-else")
    assert r["canEditAuditeeHalf"] is False


def test_someone_listed_as_both_is_treated_as_the_auditor():
    """Fails safe: it withholds the auditee's section rather than handing an
    auditor the analysis they will later verify."""
    a = _audit(auditees=[{"userId": "auditor-1", "responsibleCategories": ["DEPT_HR"]}])
    r = viewer_rights(a, _issued(), _response(), "auditor-1")
    assert r["isAuditor"] is True and r["isAuditee"] is False
    assert r["canEditAuditeeHalf"] is False


def test_the_auditee_is_never_offered_the_yellow_half():
    drafting = _finding(issuedAt=None, auditeeSubmittedAt=None, ownerId="auditee-hr")
    r = viewer_rights(_audit(), drafting, _response(), "auditee-hr")
    assert r["canEditAuditorHalf"] is False


def test_the_auditor_owns_the_yellow_half_before_issue():
    drafting = _finding(issuedAt=None, auditeeSubmittedAt=None)
    r = viewer_rights(_audit(), drafting, _response(), "auditor-1")
    assert r["canEditAuditorHalf"] is True
    assert r["auditorLockReason"] is None


def test_the_auditee_is_never_offered_the_verification_block():
    """The auditee's own analysis is what is being verified. They cannot be the
    verifier, whatever stage the form is at or what a client prop says."""
    returned = _finding(ownerId="auditee-hr")          # with the auditor to verify
    r = viewer_rights(_audit(), returned, _response(), "auditee-hr")
    assert r["canVerify"] is False
    assert r["canSign"] is False
    assert "auditor" in r["closureWaitingReason"]


def test_only_the_auditor_verifies():
    returned = _finding(ownerId="auditee-hr")
    assert viewer_rights(_audit(), returned, _response(), "auditor-1")["canVerify"] is True
    assert viewer_rights(_audit(), returned, _response(), "mr-1")["canVerify"] is False


def test_only_the_mr_signs_and_only_after_the_auditor():
    signed = _finding(ownerId="auditee-hr", auditorSignedAt=datetime.now(timezone.utc))
    assert viewer_rights(_audit(), signed, _response(), "mr-1")["canSign"] is True
    # The auditor already signed; the second signature is a different person.
    assert viewer_rights(_audit(), signed, _response(), "auditor-1")["canSign"] is False
    assert viewer_rights(_audit(), signed, _response(), "auditee-hr")["canSign"] is False
    # And nobody may sign before the auditor has verified.
    unverified = _finding(ownerId="auditee-hr")
    assert viewer_rights(_audit(), unverified, _response(), "mr-1")["canSign"] is False


# ── the audit trail has to read as possible ──────────────────────────


def test_a_completion_dated_today_keeps_the_time_it_was_recorded():
    """A date-only "Completed on" stamped midnight, so an action finished at
    12:06 sorted ten hours BEFORE the CAPA that contains it was created. An
    audit trail that reads as impossible is worse than no audit trail."""
    from app.services.nc_rca_capa import _now

    now = _now()
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    capa = _capa(state="ACTIONS_PLANNED")
    action = SimpleNamespace(
        id="a1", capaId="capa1", actionType=ACTION_TYPE_FOR_CORRECTION,
        description="", ownerUserId="", dueDate=None, sortOrder=0,
        status="PROPOSED", completedAt=None, evidenceOfCompletion=None,
    )
    db = _FakeDb(capa)

    async def _get(model, _id):
        return action if _id == "a1" else capa
    db.get = _get

    asyncio.run(save_auditee_action(
        db, _finding(issuedAt=datetime.now(timezone.utc), auditeeSubmittedAt=None,
                     ownerId="auditee-hr"),
        action_type=ACTION_TYPE_FOR_CORRECTION, description="Re-ran the HIRA.",
        owner_id="auditee-hr", due_date=date(2026, 9, 30),
        completed_on=today_midnight, action_id="a1", actor_id="auditee-hr",
    ))
    assert action.status == "COMPLETED"
    assert action.completedAt.hour != 0 or action.completedAt.minute != 0, (
        "a completion recorded today must keep the time it was recorded, "
        "not collapse to midnight"
    )
    assert action.completedAt.date() == now.date()
