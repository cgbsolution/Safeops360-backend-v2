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
    PIL_MIN_WHY_LEVELS,
    _capa_severity,
    _clause_text,
    _days_until,
    _stage,
    assert_actions_unlocked,
    mr_sign_off,
    seed_why_payload,
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
        status="IN_REMEDIATION", verificationDetails=None, dueDate=None,
        closedAt=None, closedById=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _action(status="PROPOSED", action_type=ACTION_TYPE_FOR_CORRECTION):
    return SimpleNamespace(id="a1", status=status, actionType=action_type)


def test_an_untriggered_nonconformity_reports_as_such():
    assert _stage(_finding(rcaId=None, rcaStatus=None), None, []) == "NOT_TRIGGERED"


def test_stage_tracks_the_rca_then_the_actions_then_the_signatures():
    assert _stage(_finding(rcaStatus="DRAFT"), _capa(), []) == "RCA_PENDING"
    assert _stage(_finding(rcaStatus="PEER_REVIEW"), _capa(), []) == "RCA_IN_REVIEW"
    assert _stage(_finding(), _capa(state="ACTIONS_PLANNED"), []) == "ACTIONS_PENDING"
    assert _stage(_finding(), _capa(), [_action("IN_PROGRESS")]) == "ACTIONS_IN_PROGRESS"
    assert _stage(_finding(), _capa(), [_action("COMPLETED")]) == "AWAITING_VERIFICATION"
    assert _stage(
        _finding(auditorSignedAt=datetime.now(timezone.utc)), _capa(), [_action("COMPLETED")]
    ) == "AWAITING_MR_SIGNOFF"
    assert _stage(
        _finding(auditorSignedAt=datetime.now(timezone.utc), mrSignedAt=datetime.now(timezone.utc)),
        _capa(), [_action("COMPLETED")],
    ) == "CLOSED"


def test_an_approved_rca_whose_capa_never_unlocked_does_not_read_as_actionable():
    """If the release failed, the auditee cannot add an action. A register
    saying ACTIONS_PENDING would be telling them to do something the API
    refuses."""
    assert _stage(_finding(rcaStatus="APPROVED"), _capa(state="UNDER_RCA"), []) == "RCA_IN_REVIEW"


def test_a_cancelled_action_does_not_hold_an_nc_open():
    actions = [_action("COMPLETED"), _action("CANCELLED")]
    assert _stage(_finding(), _capa(), actions) == "AWAITING_VERIFICATION"


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
