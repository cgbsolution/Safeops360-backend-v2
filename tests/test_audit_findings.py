"""WP-19 - Finding first-class on the audit side.

Design: [docs/cams/04-target.md](../../docs/cams/04-target.md) §1 decision 1.

An audit finding used to be an implicit property of a checkpoint, which is why
the unified register showed blank Due on ~40% of rows *structurally*, repeat
chains could not span the two engines, and 375 `observation` records were
dropped at the CamsFinding boundary.

The severity gradient below IS the policy: a critical NC with a 90-day due date
is not a control.
"""

from __future__ import annotations

from datetime import date

from app.services.audit_findings import (
    DEFAULT_DUE_DAYS,
    SEVERITY_FROM_CRITICALITY,
    due_date_for,
    severity_for,
)


# ── severity mapping ─────────────────────────────────────────────────


def test_every_checkpoint_criticality_maps_to_a_severity():
    for c in ("critical", "major", "minor", "observation"):
        assert severity_for(c) in SEVERITY_FROM_CRITICALITY.values()


def test_observation_survives_as_a_first_class_outcome():
    """375 rows carried criticality='observation' and were dropped at the
    CamsFinding boundary because that enum has no such value. An observation is
    a real finding that simply is not a non-conformity."""
    assert severity_for("observation") == "OBSERVATION"


def test_mapping_is_case_insensitive():
    assert severity_for("CRITICAL") == "CRITICAL_NC"
    assert severity_for("Major") == "MAJOR_NC"


def test_unknown_criticality_fails_upward_to_major():
    """Never downward. A finding that quietly became an observation would
    understate a real problem — the same fail-safe direction as the regime
    severity mapper."""
    assert severity_for("banana") == "MAJOR_NC"
    assert severity_for(None) == "MAJOR_NC"
    assert severity_for("") == "MAJOR_NC"


# ── due dates: the column that was structurally blank ────────────────


def test_due_date_gradient_is_steeper_for_worse_findings():
    """The gradient is the policy. A critical NC due in 90 days is not a control."""
    assert DEFAULT_DUE_DAYS["CRITICAL_NC"] < DEFAULT_DUE_DAYS["MAJOR_NC"]
    assert DEFAULT_DUE_DAYS["MAJOR_NC"] < DEFAULT_DUE_DAYS["MINOR_NC"]
    assert DEFAULT_DUE_DAYS["MINOR_NC"] < DEFAULT_DUE_DAYS["OBSERVATION"]


def test_critical_findings_are_due_within_a_week():
    assert DEFAULT_DUE_DAYS["CRITICAL_NC"] <= 7


def test_due_date_is_computed_from_the_raised_date():
    raised = date(2026, 7, 1)
    assert due_date_for("CRITICAL_NC", raised_on=raised) == date(2026, 7, 8)
    assert due_date_for("MAJOR_NC", raised_on=raised) == date(2026, 7, 31)


def test_every_severity_yields_a_due_date():
    """The absence of this value blanked ~40% of the unified register."""
    for sev in SEVERITY_FROM_CRITICALITY.values():
        assert due_date_for(sev, raised_on=date(2026, 1, 1)) is not None


def test_unknown_severity_still_gets_a_due_date():
    """A finding with no due date is invisible to every overdue query, so an
    unrecognised severity must fall back rather than return None."""
    d = due_date_for("SOMETHING_ELSE", raised_on=date(2026, 1, 1))
    assert d == date(2026, 1, 31)


# ── Placeholder audit titles (report-fix item 6) ─────────────────────
#
# WP-01 soft-deleted the junk-titled audits already in the tenant, but a one-off
# cleanse cannot stop the next one: AUD-GT-2026-NW-0016 was created afterwards
# and titled "Audit" — which the cleanse's `^(test|demo)` pattern never matched.
# The title prints on the report cover, so it is now rejected at save time.

import pytest  # noqa: E402

from app.services.audit_compliance import validate_audit_title  # noqa: E402


@pytest.mark.parametrize(
    "bad",
    ["Audit", "audit", "  Audit  ", "Test", "Test Audit", "Demo 123", "test 2",
     "Untitled", "New Audit", "abc", "123", "Aud"],
)
def test_placeholder_titles_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_audit_title(bad)


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_titles_are_rejected(blank):
    with pytest.raises(ValueError):
        validate_audit_title(blank)


@pytest.mark.parametrize(
    "good",
    ["Q3 Integrated SA8000 + ISO 45001 Audit - Meridian North Works",
     "Q1 Integrated Audit - North Works",
     "Fire Safety Audit 2026",
     "Attestation of Contractor Safety"],  # starts with "A", not "Audit"
)
def test_real_titles_are_accepted(good):
    assert validate_audit_title(good) == good.strip()


def test_the_rejection_message_names_the_offending_title():
    """A bare "invalid title" teaches nobody what to type instead."""
    with pytest.raises(ValueError, match="placeholder"):
        validate_audit_title("Audit")
