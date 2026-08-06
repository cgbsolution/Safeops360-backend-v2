"""Fire & Life Safety — offline unit tests for the pure decision cores.

Every rule the build spec makes load-bearing was factored into a function that
only reads attributes, so SimpleNamespace stand-ins cover them with no DB — the
house test style (cf. test_hira_alarp).

What is deliberately NOT covered here: the CRITICAL-defect CAPA constraint and
the defect closure gate, both of which need a real session. The CAPA constraint's
final enforcement is a Postgres DEFERRABLE constraint trigger, and a unit test
that stubbed the database would be testing the stub. Those need the integration
pass against a live schema — see the handover note.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.fire_certificates import (
    DEFAULT_TIERS,
    days_remaining,
    due_tier,
    status_for,
    tiers_for,
)
from app.services.fire_defects import normalise_severity
from app.services.fire_frequency import (
    PLATFORM_FALLBACK_DAYS,
    _specificity,
    interval_days,
)
from app.services.fire_safety import compute_status

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


# ── Frequency resolution precedence (§5.1) ───────────────────────────────────
def _rule(**over):
    base = dict(plantId=None, assetSubtype=None, frequency="MONTHLY", customIntervalDays=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_specificity_orders_plant_over_region_and_subtype_over_type():
    plant_subtype = _rule(plantId="P1", assetSubtype="CO2")
    plant_type = _rule(plantId="P1")
    region_subtype = _rule(assetSubtype="CO2")
    region_type = _rule()
    ranks = [_specificity(r, "P1") for r in (plant_subtype, plant_type, region_subtype, region_type)]
    assert ranks == [4, 3, 2, 1]
    # Strictly descending — the precedence ladder is the whole contract.
    assert ranks == sorted(ranks, reverse=True)


def test_plant_rule_for_a_different_plant_does_not_outrank_the_regional_default():
    """A site-scoped rule must not leak across sites. Ranking it as a plant match
    when the plant differs would apply one factory's cadence to another's."""
    other_plant = _rule(plantId="P2")
    assert _specificity(other_plant, "P1") == 1


def test_interval_days_maps_each_frequency_enum():
    assert interval_days(_rule(frequency="WEEKLY")) == 7
    assert interval_days(_rule(frequency="MONTHLY")) == 30
    assert interval_days(_rule(frequency="QUARTERLY")) == 90
    assert interval_days(_rule(frequency="HALF_YEARLY")) == 182
    assert interval_days(_rule(frequency="ANNUAL")) == 365


def test_custom_frequency_reads_its_interval_and_falls_back_when_absent():
    assert interval_days(_rule(frequency="CUSTOM", customIntervalDays=45)) == 45
    # The DB CHECK makes this unreachable; if it is reached, monthly is the safe read.
    assert interval_days(_rule(frequency="CUSTOM")) == PLATFORM_FALLBACK_DAYS


def test_unknown_frequency_never_silently_becomes_annual():
    """An unrecognised enum must degrade toward MORE inspection, not less."""
    assert interval_days(_rule(frequency="FORTNIGHTLY")) == PLATFORM_FALLBACK_DAYS


# ── Asset status precedence (§5.2) ───────────────────────────────────────────
def _asset(**over):
    base = dict(
        statusOverride=None,
        nextInspectionDueDate=NOW + timedelta(days=200),
        status="ACTIVE",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_manual_override_beats_everything():
    a = _asset(statusOverride="DECOMMISSIONED", nextInspectionDueDate=NOW - timedelta(days=400))
    assert compute_status(a, NOW, has_open_critical_defect=True) == "DECOMMISSIONED"


def test_open_critical_defect_beats_a_healthy_schedule():
    """The rule that matters most: an asset inspected yesterday and found
    discharged is NON_COMPLIANT, not ACTIVE because its next date is far away."""
    a = _asset(nextInspectionDueDate=NOW + timedelta(days=89))
    assert compute_status(a, NOW) == "ACTIVE"
    assert compute_status(a, NOW, has_open_critical_defect=True) == "NON_COMPLIANT"


def test_never_inspected_is_due_not_active():
    assert compute_status(_asset(nextInspectionDueDate=None), NOW) == "DUE_INSPECTION"


def test_overdue_due_soon_and_active_boundaries():
    assert compute_status(_asset(nextInspectionDueDate=NOW - timedelta(days=1)), NOW) == "OVERDUE"
    assert compute_status(_asset(nextInspectionDueDate=NOW + timedelta(days=29)), NOW) == "DUE_INSPECTION"
    assert compute_status(_asset(nextInspectionDueDate=NOW + timedelta(days=31)), NOW) == "ACTIVE"


def test_naive_due_dates_are_treated_as_utc_not_crashed_on():
    """Rows written by the P1-4 DDL land as naive timestamps; comparing one to an
    aware `now` raises TypeError, which would take the whole nightly job down."""
    naive = _asset(nextInspectionDueDate=datetime(2026, 1, 1, 12, 0))
    assert compute_status(naive, NOW) == "OVERDUE"


def test_amc_lapse_is_not_a_status_input():
    """Spec §4.4 — informational only. Asserted by absence: an asset with no AMC
    concept at all still computes ACTIVE, so nothing can quietly start gating on it."""
    assert compute_status(_asset(), NOW) == "ACTIVE"


# ── Expiry tiers (§4.4, §5.6) ────────────────────────────────────────────────
def test_tiers_normalise_to_descending_unique_ints():
    assert tiers_for([30, 90, 30, 7, 60]) == [90, 60, 30, 7]
    assert tiers_for([]) == DEFAULT_TIERS
    assert tiers_for(None) == DEFAULT_TIERS
    # Junk and non-positive values are dropped rather than crashing the sweep.
    assert tiers_for([0, -5, 30]) == [30]


def test_each_tier_fires_exactly_once():
    """Idempotency is the whole point: a document 45 days out is inside both the
    90 and 60 tiers every night until expiry. Firing on 'inside a tier' would
    send the same reminder 30 nights running."""
    expiry = NOW + timedelta(days=45)
    tiers = [90, 60, 30, 7]
    first = due_tier(expiry, tiers, None, NOW)
    assert first == 60  # tightest tier entered, not the widest
    assert due_tier(expiry, tiers, first, NOW) is None  # same night, already sent
    # A week later it is still inside 60 and must stay quiet.
    assert due_tier(NOW + timedelta(days=38), tiers, 60, NOW) is None
    # Crossing into 30 fires once more.
    assert due_tier(NOW + timedelta(days=29), tiers, 60, NOW) == 30


def test_a_late_created_contract_gets_one_notice_not_three():
    """Created 20 days before expiry: 90 and 60 were never appropriate."""
    assert due_tier(NOW + timedelta(days=20), [90, 60, 30, 7], None, NOW) == 30


def test_expired_documents_still_fire_the_tightest_tier():
    assert due_tier(NOW - timedelta(days=5), [90, 60, 30, 7], 30, NOW) == 7


def test_no_expiry_date_is_perpetual_not_expired():
    """A certificate with no expiry is open-ended. Reading it as expired would
    flood the register with false alarms on every legacy row."""
    assert due_tier(None, DEFAULT_TIERS, None, NOW) is None
    assert status_for(None, DEFAULT_TIERS, NOW) == "VALID"
    assert days_remaining(None, NOW) is None


def test_status_boundaries():
    assert status_for(NOW - timedelta(days=1), DEFAULT_TIERS, NOW) == "EXPIRED"
    assert status_for(NOW + timedelta(days=89), DEFAULT_TIERS, NOW) == "EXPIRING_SOON"
    assert status_for(NOW + timedelta(days=200), DEFAULT_TIERS, NOW) == "VALID"


def test_unsorted_config_cannot_under_escalate():
    """`due_tier` returns the tightest entered tier, so a config row someone typed
    in ascending order must not change the answer."""
    expiry = NOW + timedelta(days=45)
    assert due_tier(expiry, tiers_for([7, 30, 60, 90]), None, NOW) == 60


# ── Defect severity mapping (§5.4) ───────────────────────────────────────────
def test_severity_accepts_both_vocabularies():
    assert normalise_severity("CRITICAL") == "CRITICAL_NC"
    assert normalise_severity("critical") == "CRITICAL_NC"
    assert normalise_severity("CRITICAL_NC") == "CRITICAL_NC"
    assert normalise_severity("MINOR") == "MINOR_NC"
    assert normalise_severity("MAJOR_NC") == "MAJOR_NC"


def test_unclassified_severity_defaults_to_major_not_minor():
    """An unclassified fire defect is more likely under-triaged than over-triaged.
    Defaulting to MINOR_NC would let a garbled severity skip the CAPA rule."""
    assert normalise_severity(None) == "MAJOR_NC"
    assert normalise_severity("") == "MAJOR_NC"
    assert normalise_severity("urgent-ish") == "MAJOR_NC"
