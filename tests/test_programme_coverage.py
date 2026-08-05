"""Annual Audit Programme — coverage classification and lifecycle guards.

Design: [docs/cams/08-audit-programme.md](../../docs/cams/08-audit-programme.md).

The pure cores are tested directly; the async accessors wrap them. Two invariants
matter more than the rest and are asserted hardest:

  * `partial` and `sampled` are FIRST-CLASS states — never merged into green.
    "We sampled 8 of 40 and passed" is a different assurance claim from "we
    verified all 40", and a matrix that erases the difference is lying.
  * No slot leaves PLANNED without an engagement or an amendment.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.services.programme.coverage import (
    ALL_STATES,
    aggregate_states,
    classify,
    period_bounds,
    period_index_for,
)
from app.services.programme.lifecycle import (
    SLOT_REQUIRES_AMENDMENT,
    approval_blockers,
    cycle_transition_allowed,
    slot_needs_amendment,
    slot_transition_allowed,
)

TH = 80.0


# ── classify ─────────────────────────────────────────────────────────


def test_full_coverage_at_threshold():
    assert classify(assessed=8, total=10, threshold_pct=TH, sampled=False) == "COVERED_FULL"


def test_full_coverage_above_threshold():
    assert classify(assessed=10, total=10, threshold_pct=TH, sampled=False) == "COVERED_FULL"


def test_partial_below_threshold():
    """The brief's example: Fire Safety touched at 3 of 14 is PARTIAL — not green,
    not blank. Its own state."""
    s = classify(assessed=3, total=14, threshold_pct=TH, sampled=False)
    assert s == "PARTIAL"


def test_sampled_coverage_is_never_merged_into_full():
    full = classify(assessed=10, total=10, threshold_pct=TH, sampled=False)
    sampled = classify(assessed=10, total=10, threshold_pct=TH, sampled=True)
    assert full == "COVERED_FULL"
    assert sampled == "COVERED_SAMPLED"
    assert full != sampled


def test_sampled_but_below_threshold_is_still_partial():
    """Sampling does not rescue insufficient coverage."""
    assert classify(assessed=2, total=14, threshold_pct=TH, sampled=True) == "PARTIAL"


def test_nothing_assessed_in_an_open_window_is_uncovered():
    assert classify(
        assessed=0, total=14, threshold_pct=TH, sampled=False, window_closed=False
    ) == "UNCOVERED"


def test_nothing_assessed_in_a_closed_window_is_overdue():
    assert classify(
        assessed=0, total=14, threshold_pct=TH, sampled=False, window_closed=True
    ) == "OVERDUE"


def test_zero_total_does_not_divide_by_zero():
    assert classify(assessed=0, total=0, threshold_pct=TH, sampled=False) == "UNCOVERED"


def test_waiver_wins_over_every_other_state():
    """A waiver is a governed decision and outranks the arithmetic — including an
    overdue window, which would otherwise report a gap someone already accepted."""
    for closed in (True, False):
        for assessed in (0, 5, 10):
            assert classify(
                assessed=assessed, total=10, threshold_pct=TH, sampled=False,
                waived=True, window_closed=closed,
            ) == "WAIVED"


def test_threshold_is_configurable_per_programme():
    """A 1,500-checkpoint engagement and a 30-question inspection should not face
    the same bar."""
    assert classify(assessed=5, total=10, threshold_pct=50.0, sampled=False) == "COVERED_FULL"
    assert classify(assessed=5, total=10, threshold_pct=90.0, sampled=False) == "PARTIAL"


def test_float_boundary_is_not_lost_to_rounding():
    """1/3 at a 33.33 threshold must count as covered, not fall to PARTIAL on an
    epsilon."""
    assert classify(assessed=1, total=3, threshold_pct=100 / 3, sampled=False) == "COVERED_FULL"


# ── aggregate_states ─────────────────────────────────────────────────


def test_aggregate_counts_both_covered_states_as_covered():
    agg = aggregate_states(["COVERED_FULL", "COVERED_SAMPLED", "PARTIAL", "UNCOVERED"])
    assert agg["covered"] == 2
    assert agg["coveragePct"] == 50.0
    assert agg["sampledOnly"] == 1


def test_waived_is_excluded_from_the_coverage_denominator():
    """A waived unit is neither a success nor a gap; counting it either way
    misstates the programme."""
    agg = aggregate_states(["COVERED_FULL", "WAIVED"])
    assert agg["considered"] == 1
    assert agg["coveragePct"] == 100.0
    assert agg["waived"] == 1


def test_gaps_count_partial_uncovered_and_overdue():
    agg = aggregate_states(["PARTIAL", "UNCOVERED", "OVERDUE", "COVERED_FULL"])
    assert agg["gaps"] == 3
    assert agg["overdue"] == 1


def test_all_waived_yields_null_coverage_not_zero():
    """Null, not 0.0 — a programme entirely waived has no coverage percentage, and
    printing 0% would read as total failure."""
    agg = aggregate_states(["WAIVED", "WAIVED"])
    assert agg["coveragePct"] is None


def test_empty_input_is_null_coverage():
    assert aggregate_states([])["coveragePct"] is None


def test_aggregate_reports_every_state_key():
    agg = aggregate_states(["COVERED_FULL"])
    for s in ALL_STATES:
        assert s in agg["counts"]


# ── period_bounds ────────────────────────────────────────────────────


def test_financial_year_splits_into_four_contiguous_quarters():
    b = period_bounds(date(2026, 4, 1), date(2027, 3, 31), 4)
    assert len(b) == 4
    assert b[0][0] == date(2026, 4, 1)
    assert b[-1][1] == date(2027, 3, 31)
    for (_, e), (s2, _) in zip(b, b[1:]):
        assert (s2 - e).days == 1  # contiguous, no gap and no overlap


def test_last_period_always_ends_exactly_on_period_end():
    """Day-count arithmetic must never leave the cycle short or long."""
    for n in (1, 2, 3, 4, 6, 12):
        b = period_bounds(date(2026, 1, 1), date(2026, 12, 31), n)
        assert b[-1][1] == date(2026, 12, 31)
        assert b[0][0] == date(2026, 1, 1)


def test_three_year_certification_cycle_splits_evenly():
    b = period_bounds(date(2026, 1, 1), date(2028, 12, 31), 3)
    assert len(b) == 3 and b[-1][1] == date(2028, 12, 31)


def test_zero_periods_is_coerced_to_one():
    assert len(period_bounds(date(2026, 1, 1), date(2026, 12, 31), 0)) == 1


def test_period_index_for_locates_a_date():
    b = period_bounds(date(2026, 4, 1), date(2027, 3, 31), 4)
    assert period_index_for(b, date(2026, 4, 2)) == 0
    assert period_index_for(b, date(2027, 3, 30)) == 3
    assert period_index_for(b, date(2025, 1, 1)) is None
    assert period_index_for(b, None) is None


# ── state machines ───────────────────────────────────────────────────


def test_cycle_cannot_skip_review():
    assert cycle_transition_allowed("DRAFT", "UNDER_REVIEW") is True
    assert cycle_transition_allowed("DRAFT", "APPROVED") is False
    assert cycle_transition_allowed("DRAFT", "ACTIVE") is False


def test_review_can_bounce_back_to_draft():
    assert cycle_transition_allowed("UNDER_REVIEW", "DRAFT") is True


def test_closed_cycle_is_terminal():
    for target in ("DRAFT", "UNDER_REVIEW", "APPROVED", "ACTIVE"):
        assert cycle_transition_allowed("CLOSED", target) is False


def test_slot_cannot_jump_from_planned_to_completed():
    """It has to become SCHEDULED first, which is where the engagement pointer is
    attached — otherwise a slot could read COMPLETED with nothing behind it."""
    assert slot_transition_allowed("PLANNED", "COMPLETED") is False
    assert slot_transition_allowed("PLANNED", "SCHEDULED") is True


def test_deferred_slot_can_be_replanned():
    assert slot_transition_allowed("DEFERRED", "PLANNED") is True


def test_terminal_slot_states_are_terminal():
    for st in ("COMPLETED", "CANCELLED", "WAIVED"):
        assert slot_transition_allowed(st, "PLANNED") is False


def test_non_execution_transitions_all_require_an_amendment():
    """The invariant: a slot cannot silently stop existing."""
    for st in ("DEFERRED", "CANCELLED", "WAIVED"):
        assert slot_needs_amendment(st) is True
        assert st in SLOT_REQUIRES_AMENDMENT


def test_materialising_transitions_do_not_require_an_amendment():
    for st in ("SCHEDULED", "IN_PROGRESS", "COMPLETED"):
        assert slot_needs_amendment(st) is False


# ── approval guard ───────────────────────────────────────────────────


def _unit(**over):
    base = dict(
        id="u1",
        dimensionKey="FS",
        dimensionLabel="Fire Safety",
        requiredPerCycle=2,
        waiverReason=None,
        waivedByUserId=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_clean_cycle_has_no_approval_blockers():
    assert approval_blockers(
        objectives="Verify OH&S conformity across the estate.",
        scope_units=[_unit()],
        slots_per_unit={"u1": 2},
        approver_id="u-approver",
        owner_id="u-owner",
    ) == []


def test_missing_objectives_blocks_approval():
    b = approval_blockers(
        objectives="   ",
        scope_units=[_unit()],
        slots_per_unit={"u1": 2},
        approver_id="a",
        owner_id="o",
    )
    assert any("objectives" in x for x in b)


def test_owner_cannot_approve_their_own_cycle():
    """Four-eyes, reusing the same segregation primitive as ERM Internal Controls."""
    b = approval_blockers(
        objectives="x" * 20,
        scope_units=[_unit()],
        slots_per_unit={"u1": 2},
        approver_id="u-same",
        owner_id="u-same",
    )
    assert any("cannot approve their own" in x for x in b)


def test_scope_unit_without_frequency_or_waiver_blocks():
    b = approval_blockers(
        objectives="x" * 20,
        scope_units=[_unit(requiredPerCycle=None)],
        slots_per_unit={},
        approver_id="a",
        owner_id="o",
    )
    assert any("required frequency" in x for x in b)


def test_documented_waiver_is_an_acceptable_alternative_to_a_frequency():
    assert approval_blockers(
        objectives="x" * 20,
        scope_units=[
            _unit(requiredPerCycle=None, waiverReason="Site mothballed", waivedByUserId="u-approver")
        ],
        slots_per_unit={},
        approver_id="a",
        owner_id="o",
    ) == []


def test_waiver_without_an_approver_blocks():
    b = approval_blockers(
        objectives="x" * 20,
        scope_units=[_unit(requiredPerCycle=None, waiverReason="because")],
        slots_per_unit={},
        approver_id="a",
        owner_id="o",
    )
    assert any("named approver" in x for x in b)


def test_frequency_without_a_planned_slot_blocks():
    """A frequency nobody scheduled against is a plan on paper only."""
    b = approval_blockers(
        objectives="x" * 20,
        scope_units=[_unit()],
        slots_per_unit={},
        approver_id="a",
        owner_id="o",
    )
    assert any("no planned slot" in x for x in b)


def test_empty_cycle_blocks():
    b = approval_blockers(
        objectives="x" * 20, scope_units=[], slots_per_unit={}, approver_id="a", owner_id="o"
    )
    assert any("at least one scope unit" in x for x in b)


def test_all_blockers_are_reported_together_not_just_the_first():
    """A user fixing an approval should see every problem in one pass."""
    b = approval_blockers(
        objectives="",
        scope_units=[_unit(requiredPerCycle=None)],
        slots_per_unit={},
        approver_id=None,
        owner_id="o",
    )
    assert len(b) >= 3
