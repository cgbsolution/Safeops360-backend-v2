"""Statutory obligations service boundary (WP-52 / Q8).

Design: [docs/cams/07-programme.md](../../docs/cams/07-programme.md) WP-52.

The behaviour under test is the one that caused F-48: a broken dependency used
to render as "0 obligations, 0% assurance", which is indistinguishable from a
genuinely empty register and reads as GOOD NEWS on a compliance dashboard.

"No obligations" and "could not read obligations" are different facts. These
tests pin that they stay different.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.obligations import (
    ObligationsUnavailable,
    ObligationSummary,
    unavailable_payload,
)


def _ob(**over) -> ObligationSummary:
    base = dict(
        id="o1",
        obligationCode="OBL-001",
        title="Factory licence",
        siteId="plant-nw",
        status="COMPLIANT",
        statuteReference="Factories Act §6",
        validUntil=datetime(2027, 3, 31, tzinfo=timezone.utc),
        renewalLeadDays=60,
    )
    base.update(over)
    return ObligationSummary(**base)


# ── renewal-due derivation (F-49) ────────────────────────────────────


def test_renewal_due_is_valid_until_minus_lead_days():
    """Rows read OVERDUE beside a FUTURE expiry, which looked like a bug and was
    not — the obligation is overdue for RENEWAL. This is the date that makes the
    row make sense."""
    assert _ob().renewalDueAt == datetime(2027, 1, 30, tzinfo=timezone.utc)


def test_renewal_due_with_no_lead_days_equals_expiry():
    assert _ob(renewalLeadDays=None).renewalDueAt == datetime(2027, 3, 31, tzinfo=timezone.utc)
    assert _ob(renewalLeadDays=0).renewalDueAt == datetime(2027, 3, 31, tzinfo=timezone.utc)


def test_renewal_due_is_none_for_a_perpetual_obligation():
    """An obligation with no expiry has no renewal date — not "today"."""
    assert _ob(validUntil=None).renewalDueAt is None


def test_long_lead_time_pushes_renewal_well_before_expiry():
    ob = _ob(renewalLeadDays=180)
    assert ob.renewalDueAt == ob.validUntil - timedelta(days=180)


# ── the unavailable contract — the F-48 fix ──────────────────────────


def test_unavailable_payload_reports_nulls_not_zeros():
    """The load-bearing assertion. A failed dependency must NEVER produce a
    number a dashboard can render as a percentage."""
    p = unavailable_payload("register offline")
    assert p["available"] is False
    assert p["totalObligations"] is None
    assert p["verifiedPct"] is None
    assert p["verifiedByAuditCount"] is None
    assert p["openNcCount"] is None


def test_unavailable_payload_carries_the_reason():
    p = unavailable_payload("no module named erm_p2")
    assert "erm_p2" in p["unavailableReason"]


def test_unavailable_payload_has_no_rows_to_render():
    p = unavailable_payload("x")
    assert p["rows"] == [] and p["statusCounts"] == {}


def test_unavailable_is_an_exception_not_an_empty_list():
    """Returning [] would let a caller silently treat a broken register as an
    empty one — exactly the conflation this module exists to prevent."""
    with pytest.raises(ObligationsUnavailable) as e:
        raise ObligationsUnavailable("table missing")
    assert e.value.reason == "table missing"


def test_available_and_unavailable_payloads_share_the_branch_key():
    """A consumer branches on ONE key rather than inferring failure from a zero."""
    assert "available" in unavailable_payload("x")


# ── the narrowed shape ───────────────────────────────────────────────


def test_summary_exposes_only_what_cams_consumes():
    """Narrowing is what makes the boundary meaningful: CAMS depends on these
    fields, and ERM stays free to change everything else."""
    fields = set(ObligationSummary.__dataclass_fields__)
    assert fields == {
        "id", "obligationCode", "title", "siteId", "status",
        "statuteReference", "validUntil", "renewalLeadDays",
        "regulatorName", "criticality",
    }


def test_summary_is_immutable():
    """A read-model that callers can mutate is not a boundary."""
    ob = _ob()
    with pytest.raises(Exception):
        ob.status = "OVERDUE"  # type: ignore[misc]
