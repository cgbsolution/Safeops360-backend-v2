"""Checklist completion rate — the shared read model.

Two properties carry this whole build, and both fail silently if wrong:

  * "cannot be computed" is NULL, never 0 and never 100. A site with no fire
    assets has neither passed nor failed its fire compliance; a fabricated
    figure on a fire register is dangerous in both directions — 0% sends
    someone to fix a problem that does not exist, 100% hides one that does.

  * the denominator is what is OWED, not what exists. A checklist nobody ever
    opened has no engagement row, so counting rows would score a site that did
    nothing as 100% complete.

Offline — the period maths and the Completion arithmetic need no database.
"""

from __future__ import annotations

from datetime import date

from app.services import compliance_read_model as crm
from app.services import fire_checklists as svc


# ── "cannot be computed" is not zero ─────────────────────────────────────────
def test_nothing_owed_is_null_not_zero():
    c = crm.Completion(owed=0, completed=0)
    assert c.rate is None
    d = c.as_dict()
    assert d["rate"] is None
    assert d["computable"] is False
    # The two values that must never appear for "no data".
    assert d["rate"] != 0
    assert d["rate"] != 100


def test_nothing_owed_stays_null_even_with_stray_completions():
    # Defensive: a completed run outside the owed set must not conjure a rate.
    c = crm.Completion(owed=0, completed=3)
    assert c.rate is None


def test_owed_but_none_done_is_a_real_zero():
    # This one IS 0% — the distinction the whole convention exists for.
    c = crm.Completion(owed=10, completed=0)
    assert c.rate == 0.0
    assert c.as_dict()["computable"] is True


def test_all_done_is_a_hundred():
    assert crm.Completion(owed=8, completed=8).rate == 100.0


def test_rate_is_completed_over_owed_not_over_runs_that_exist():
    # 4 owed, 1 signed off, 1 started-not-finished → 25%, not 50%.
    c = crm.Completion(owed=4, completed=1, inProgress=1)
    assert c.rate == 25.0
    assert c.missing == 2


def test_missing_never_goes_negative():
    c = crm.Completion(owed=2, completed=3, inProgress=1)
    assert c.missing == 0


def test_empty_result_serialises_as_no_data():
    r = crm.ComplianceResult(window={"start": "2026-01-01", "end": "2026-03-31"}, modules=["FIRE"])
    d = r.as_dict()
    assert d["overall"]["rate"] is None
    assert d["overall"]["computable"] is False
    assert d["byAsset"] == {} and d["byPlant"] == {}


# ── the denominator: periods owed ────────────────────────────────────────────
def test_periods_in_range_covers_every_overlapping_period():
    assert svc.periods_in_range("MONTHLY", date(2026, 1, 15), date(2026, 3, 2)) == [
        "2026-01", "2026-02", "2026-03",
    ]
    assert svc.periods_in_range("QUARTERLY", date(2026, 1, 1), date(2026, 12, 31)) == [
        "2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4",
    ]
    assert svc.periods_in_range("ANNUAL", date(2025, 6, 1), date(2026, 6, 1)) == ["2025", "2026"]


def test_periods_in_range_is_oldest_first_and_deduplicated():
    got = svc.periods_in_range("DAILY", date(2026, 3, 1), date(2026, 3, 5))
    assert got == ["2026-03-01", "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]
    assert len(got) == len(set(got))


def test_an_inverted_range_owes_nothing():
    assert svc.periods_in_range("MONTHLY", date(2026, 5, 1), date(2026, 1, 1)) == []


def test_a_single_day_still_owes_its_period():
    assert svc.periods_in_range("MONTHLY", date(2026, 5, 9), date(2026, 5, 9)) == ["2026-05"]


# ── the window ───────────────────────────────────────────────────────────────
def test_default_window_ends_yesterday_not_today():
    # The current period is still open. Counting an unfinished month against a
    # site reports every register as failing on the 1st.
    start, end = crm.default_window(today=date(2026, 8, 28), months=3)
    assert end == date(2026, 8, 27)
    assert start < end


def test_window_length_follows_the_months_argument():
    s1, e1 = crm.default_window(today=date(2026, 8, 28), months=1)
    s3, _ = crm.default_window(today=date(2026, 8, 28), months=3)
    assert s3 < s1 < e1


# ── completed means signed off ───────────────────────────────────────────────
def test_only_signed_off_statuses_count_as_complete():
    # A sheet in fieldwork is started, not done. Counting it would let a site
    # score 100% having approved nothing.
    assert "REPORT_ISSUED" in crm.COMPLETED_STATUSES
    assert "CLOSED" in crm.COMPLETED_STATUSES
    for not_done in ("PLANNED", "SCHEDULED", "IN_PROGRESS", "FIELDWORK_COMPLETE",
                     "FINDINGS_REVIEW", "CANCELLED"):
        assert not_done not in crm.COMPLETED_STATUSES


def test_both_modules_are_covered_by_default():
    assert set(crm.DEFAULT_MODULES) == {"FIRE", "CHEMICAL"}


def test_variant_rule_matches_the_scan_and_reminder_rule():
    # An inapplicable sheet must never be counted as owed — it would drag a
    # site's rate down for a document that does not apply to its panel.
    assert crm._variant_ok("UNIT_21_A_ZONE", "ZONE") is True
    assert crm._variant_ok("UNIT_21_A_ZONE", "LOOP") is False
    assert crm._variant_ok("UNIT_21_B_LOOP", "LOOP") is True
    assert crm._variant_ok("UNIT_21_B_LOOP", "ZONE") is False
    # No variant, or no subtype recorded → applies.
    assert crm._variant_ok("", "ZONE") is True
    assert crm._variant_ok("UNIT_21_A_ZONE", "") is True
