"""Factory-profile governed edits — offline unit tests for the pure logic.

The approval workflow's DB side (two sequential sign-offs, the same-person
guard, the 409 on a second in-flight request) lives in the router and is
exercised against a real Postgres. What is tested here is the part that decides
*what* gets approved and *what value lands*: the diff builder and the apply
step. A diff that renders one thing and applies another would be an audit trail
that lies, so the round trip is the thing worth pinning down.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.services.factory_ext import (
    GOVERNED_STAGES,
    PROFILE_FIELD_LABELS,
    apply_change_request,
    build_profile_diff,
    profile_edit_is_governed,
)


class _Profile:
    """Stand-in for a FactoryProfile row — the diff logic only reads attributes."""

    def __init__(self, **kw):
        self.factoryName = "Meridian Apparel — Tirupur 1"
        self.city = "Tirupur"
        self.state = "Tamil Nadu"
        self.totalLandAreaSqm = 12000.0
        self.factoryLicenseNo = "TN/FAC/2009/1187"
        self.factoryLicenseValidUntil = datetime(2026, 3, 31, tzinfo=timezone.utc)
        self.applicableActs = ["Factories Act 1948"]
        self.registrationNos = [{"type": "GST", "number": "33ABCDE1234F1Z5"}]
        self.pollutionControlBoard = None
        self.__dict__.update(kw)


class _CR:
    def __init__(self, changes):
        self.changes = changes


# ── governance gate ──────────────────────────────────────────────────────────
def test_only_active_profiles_are_governed():
    assert profile_edit_is_governed("ACTIVE") is True
    for stage in ("INITIATED", "EXECUTION", "VALIDATION", "ARCHIVED", "", None):
        assert profile_edit_is_governed(stage) is False
    assert GOVERNED_STAGES == frozenset({"ACTIVE"})


# ── diff ─────────────────────────────────────────────────────────────────────
def test_unchanged_fields_produce_no_diff():
    p = _Profile()
    assert build_profile_diff(p, {"factoryName": p.factoryName, "city": p.city}) == []


def test_diff_reports_label_from_and_to():
    p = _Profile()
    diff = build_profile_diff(p, {"city": "Coimbatore"})
    assert len(diff) == 1
    assert diff[0]["field"] == "city"
    assert diff[0]["label"] == PROFILE_FIELD_LABELS["city"]
    assert diff[0]["from"] == "Tirupur"
    assert diff[0]["to"] == "Coimbatore"


def test_unknown_fields_are_ignored():
    """siteId and factoryCode are immutable — a payload naming them changes
    nothing and must not smuggle a field past the approvers as an empty diff."""
    p = _Profile()
    assert build_profile_diff(p, {"siteId": "someone-elses-plant", "factoryCode": "XX-1"}) == []


def test_dates_render_as_dates_and_list_values_are_summarised():
    p = _Profile()
    new_expiry = datetime(2027, 3, 31, tzinfo=timezone.utc)
    diff = {
        c["field"]: c
        for c in build_profile_diff(
            p,
            {
                "factoryLicenseValidUntil": new_expiry,
                "applicableActs": ["Factories Act 1948", "Contract Labour (R&A) Act 1970"],
            },
        )
    }
    assert diff["factoryLicenseValidUntil"]["from"] == "2026-03-31"
    assert diff["factoryLicenseValidUntil"]["to"] == "2027-03-31"
    assert diff["applicableActs"]["to"] == "Factories Act 1948, Contract Labour (R&A) Act 1970"


def test_equivalent_list_of_dicts_is_not_a_change():
    """The API takes registrationNos as models and stores them as dicts; the
    same list arriving in the other shape must not read as an edit."""
    p = _Profile()
    same = [{"type": "GST", "number": "33ABCDE1234F1Z5"}]
    assert build_profile_diff(p, {"registrationNos": same}) == []


# ── apply ────────────────────────────────────────────────────────────────────
def test_approved_change_applies_the_raw_value_not_the_rendered_one():
    p = _Profile()
    new_expiry = datetime(2027, 3, 31, tzinfo=timezone.utc)
    diff = build_profile_diff(
        p, {"city": "Coimbatore", "totalLandAreaSqm": 15500.0, "factoryLicenseValidUntil": new_expiry}
    )
    # A fresh profile object — approval happens long after the request was raised.
    target = _Profile()
    apply_change_request(target, _CR(diff))
    assert target.city == "Coimbatore"
    assert target.totalLandAreaSqm == 15500.0
    assert target.factoryLicenseValidUntil == new_expiry


def test_apply_can_clear_a_field():
    p = _Profile(pollutionControlBoard="KSPCB")
    diff = build_profile_diff(p, {"pollutionControlBoard": None})
    assert diff[0]["to"] is None
    target = _Profile(pollutionControlBoard="KSPCB")
    apply_change_request(target, _CR(diff))
    assert target.pollutionControlBoard is None


def test_apply_ignores_fields_outside_the_editable_set():
    """Belt and braces: even a hand-crafted change row naming an immutable field
    can't move it, so tampering with a stored request achieves nothing."""
    target = _Profile(siteId="plant-owned-by-this-factory")
    apply_change_request(target, _CR([{"field": "siteId", "label": "Site", "value": "hijacked"}]))
    assert target.siteId == "plant-owned-by-this-factory"
