"""WP-49 / F-22 - per-audit-type scoring rules, and the rule rendered.

`MINIMUM_PASS_SCORE = 80.0` was a module constant applied to every audit of
every type while `AuditTemplate.scoring` sat unused. Worse, the gate was
INVISIBLE: an audit could read "99.5% overall" and be a FAIL because of 8
critical failures, with nothing on screen saying why.

The two properties pinned hardest:
  * the critical gate is independent of the percentage, and
  * every verdict carries a sentence explaining which condition failed.
"""

from __future__ import annotations

from app.services.scoring_rules import (
    DEFAULT_CRITICAL_GATE,
    DEFAULT_MINIMUM_PASS_SCORE,
    DEFAULT_MIN_ASSESSED_PCT_FOR_GRADE,
    ScoringRules,
    evaluate,
    grade_visibility,
    rules_from,
)


def test_absent_config_preserves_the_historic_behaviour():
    """Existing audit types must score exactly as before until someone
    deliberately configures them."""
    r = rules_from(None)
    assert r.minimumPassScore == DEFAULT_MINIMUM_PASS_SCORE == 80.0
    assert r.criticalGateThreshold == DEFAULT_CRITICAL_GATE == 0


def test_partial_config_falls_back_per_field():
    """A type that sets only a pass mark must not silently lose the gate."""
    r = rules_from({"minimumPassScore": 95})
    assert r.minimumPassScore == 95.0
    assert r.criticalGateThreshold == DEFAULT_CRITICAL_GATE


def test_malformed_config_does_not_pass_everything():
    """Garbage in the JSON column must fall back to the strict default, not
    disable the gate — a config typo should never turn every audit green."""
    r = rules_from({"minimumPassScore": "not-a-number"})
    assert r.minimumPassScore == DEFAULT_MINIMUM_PASS_SCORE


def test_pass_mark_is_clamped_to_a_sane_range():
    assert rules_from({"minimumPassScore": 500}).minimumPassScore == 100.0
    assert rules_from({"minimumPassScore": -20}).minimumPassScore == 0.0


def test_unknown_na_handling_falls_back_to_exclude():
    assert rules_from({"naHandling": "NONSENSE"}).naHandling == "EXCLUDE"


# ── the gate ─────────────────────────────────────────────────────────


def test_a_critical_failure_fails_a_near_perfect_audit():
    """The headline case from the diagnosis."""
    v = evaluate(overall_pct=99.5, critical_failures=8, rules=ScoringRules())
    assert v["passed"] is False
    assert v["band"] == "CRITICAL_NC"
    assert v["scoreMet"] is True   # the percentage was fine
    assert v["gateMet"] is False   # the gate was not


def test_the_explanation_names_the_condition_that_failed():
    """A number without its rule is not a result."""
    v = evaluate(overall_pct=99.5, critical_failures=8, rules=ScoringRules())
    e = v["explanation"]
    assert "99.5%" in e and "8 critical failure(s)" in e and "threshold 0" in e


def test_a_clean_audit_above_the_mark_passes():
    v = evaluate(overall_pct=85.0, critical_failures=0, rules=ScoringRules())
    assert v["passed"] is True and v["band"] == "CONFORMING"


def test_below_the_mark_fails_even_with_no_criticals():
    v = evaluate(overall_pct=60.0, critical_failures=0, rules=ScoringRules())
    assert v["passed"] is False and v["gateMet"] is True
    assert "below the 80% pass mark" in v["explanation"]


def test_a_configured_gate_tolerance_is_honoured():
    """Some regimes tolerate a capped number of criticals; the engine must not
    hard-code zero."""
    r = rules_from({"minimumPassScore": 95, "criticalGateThreshold": 2})
    assert evaluate(overall_pct=96.0, critical_failures=2, rules=r)["passed"] is True
    assert evaluate(overall_pct=96.0, critical_failures=3, rules=r)["passed"] is False


def test_nothing_assessable_is_not_assessed_not_zero():
    """A 0% over an empty set reads as total failure and is meaningless."""
    v = evaluate(overall_pct=None, critical_failures=0, rules=ScoringRules())
    assert v["band"] == "NOT_ASSESSED" and v["passed"] is False
    assert "no score can be computed" in v["explanation"]


def test_near_miss_is_minor_not_major():
    v = evaluate(overall_pct=72.0, critical_failures=0, rules=ScoringRules())
    assert v["band"] == "MINOR_NC"
    assert evaluate(overall_pct=40.0, critical_failures=0, rules=ScoringRules())["band"] == "MAJOR_NC"


def test_every_verdict_carries_the_rules_it_applied():
    """The report renders the rule alongside the number, so it has to travel
    with the verdict rather than being re-derived."""
    v = evaluate(overall_pct=90.0, critical_failures=0, rules=rules_from({"minimumPassScore": 88}))
    assert v["rules"]["minimumPassScore"] == 88.0


# ── Grade suppression (coverage floor) ───────────────────────────────
#
# The interim report headlined "100.0% (CONFORMING)" on 1 of 82 checkpoints.
# That is the 78.9%-over-0-of-82 defect wearing a disclaimer nobody reading the
# cover will see: the caveat was in the body, the grade was on the cover.


def test_below_floor_suppresses_the_grade():
    g = grade_visibility(assessed=1, applicable=82)
    assert g["showGrade"] is False
    assert g["assessedPct"] == 1.2
    assert g["label"] == "1 of 82 assessed"


def test_at_the_floor_the_grade_renders():
    """Exactly 20% must pass — the threshold is inclusive, so a boundary audit
    is not silently ungraded."""
    g = grade_visibility(assessed=20, applicable=100)
    assert g["showGrade"] is True
    assert g["assessedPct"] == 20.0


def test_just_below_and_just_above_the_floor():
    assert grade_visibility(assessed=19, applicable=100)["showGrade"] is False
    assert grade_visibility(assessed=21, applicable=100)["showGrade"] is True


def test_full_coverage_renders():
    assert grade_visibility(assessed=82, applicable=82)["showGrade"] is True


def test_zero_applicable_never_grades():
    """All-N/A audit: nothing to grade, and no division by zero."""
    g = grade_visibility(assessed=0, applicable=0)
    assert g["showGrade"] is False and g["assessedPct"] == 0.0


def test_floor_is_per_audit_type_configurable():
    """A 12-checkpoint inspection and a 1500-checkpoint social audit do not
    share an "enough to say something" point."""
    lenient = ScoringRules(minimumAssessedPctForGrade=5.0)
    assert grade_visibility(assessed=1, applicable=82, rules=lenient)["showGrade"] is False
    assert grade_visibility(assessed=5, applicable=82, rules=lenient)["showGrade"] is True
    strict = ScoringRules(minimumAssessedPctForGrade=90.0)
    assert grade_visibility(assessed=82, applicable=100, rules=strict)["showGrade"] is False


def test_floor_round_trips_through_rules_from():
    assert rules_from({"minimumAssessedPctForGrade": 35}).minimumAssessedPctForGrade == 35.0


def test_malformed_floor_falls_back_to_default():
    r = rules_from({"minimumAssessedPctForGrade": "not-a-number"})
    assert r.minimumAssessedPctForGrade == DEFAULT_MIN_ASSESSED_PCT_FOR_GRADE


def test_floor_is_clamped_to_0_100():
    assert rules_from({"minimumAssessedPctForGrade": 500}).minimumAssessedPctForGrade == 100.0
    assert rules_from({"minimumAssessedPctForGrade": -5}).minimumAssessedPctForGrade == 0.0


def test_suppression_does_not_change_the_computed_score():
    """Visibility, not scoring: the band is still computed and still stored."""
    v = evaluate(overall_pct=100.0, critical_failures=0, rules=ScoringRules())
    assert v["band"] == "CONFORMING" and v["passed"] is True
    assert grade_visibility(assessed=1, applicable=82)["showGrade"] is False
