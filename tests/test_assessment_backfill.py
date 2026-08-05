"""WP-02 - the assessmentStatus precedence rule (F-29).

Design: [docs/cams/04-target.md](../../docs/cams/04-target.md) §9.

F-29 is the module's worst defect: the v2 migration added `assessmentStatus`
without backfilling it, so 242 rows across 6 audits read `NOT_ASSESSED` while
their answer sat in `auditorResponse->>'value'`. Four read paths then disagreed,
and `AUD-GT-2026-NW-0003` shipped a report saying **78.9% over 0-of-82 assessed
with 82 open items on a CLOSED audit**.

These tests pin the derivation rule so a future migration cannot reintroduce it.
The load-bearing property is the LAST one: a row whose verdict cannot be derived
stays NOT_ASSESSED rather than being guessed.
"""

from __future__ import annotations

from scripts.backfill_assessment_status import resolve


# -- JSON value is the primary source ---------------------------------


def test_pass_and_yes_both_resolve_to_pass():
    """Both spellings are accepted by `_norm_value`, so both must backfill."""
    assert resolve("pass", None) == "PASS"
    assert resolve("yes", None) == "PASS"


def test_fail_and_no_both_resolve_to_fail():
    assert resolve("fail", None) == "FAIL"
    assert resolve("no", None) == "FAIL"


def test_partial_and_na():
    assert resolve("partial", None) == "PARTIAL"
    assert resolve("na", None) == "NA"


# -- legacy overallStatus is the fallback ------------------------------


def test_legacy_overall_status_used_when_json_is_absent():
    """Rows predating the JSON response shape still carry a legacy verdict."""
    assert resolve(None, "answered_pass") == "PASS"
    assert resolve(None, "answered_partial") == "PARTIAL"
    assert resolve(None, "answered_fail") == "FAIL"
    assert resolve(None, "answered_na") == "NA"


def test_json_wins_over_legacy_when_both_present():
    """Precedence matters: the JSON value is what `_compute_score` reads, so it
    is the one the column must agree with."""
    assert resolve("fail", "answered_pass") == "FAIL"


# -- the honest-failure property --------------------------------------


def test_unanswered_row_is_not_guessed():
    assert resolve(None, "not_answered") is None
    assert resolve(None, None) is None


def test_workflow_only_statuses_are_not_verdicts():
    """`pending_auditee` and `response_accepted` describe where a finding is in
    its iteration thread, NOT what the auditor decided. Deriving a verdict from
    them would invent an assessment nobody made."""
    assert resolve(None, "pending_auditee") is None
    assert resolve(None, "response_accepted") is None


def test_unrecognised_value_is_left_alone():
    """A row we cannot resolve stays NOT_ASSESSED. That is at least honest —
    guessing is how F-29 happened in the first place."""
    assert resolve("maybe", None) is None
    assert resolve("", "") is None


def test_every_resolved_value_is_a_valid_assessment_status():
    from app.services.audit_compliance import _ASSESS_STATUS

    valid = set(_ASSESS_STATUS.values())
    for v in ("pass", "yes", "partial", "fail", "no", "na"):
        assert resolve(v, None) in valid
