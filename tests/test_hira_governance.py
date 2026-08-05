"""HIRA ALARP governance — offline unit tests for the Phase-2 fixes.

Covers the pure logic that guards approval and re-approval:
  • materiality now includes the ALARP cost band
  • a material edit withdraws a live approval → PENDING_REAPPROVAL, keyed on the
    ENTRY's approval state (independent of study status — the closed hole)
  • an override is voided when the risk basis moves
  • the unacceptableOverrideActive property (drives the approve gate)

The approve/override endpoints themselves are integration-level (DB) and are
exercised manually; here we lock down the decision logic they call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.models.hira import HiraEntry
from app.routers.hira import (
    REAPPROVAL_STATUS,
    _apply_reapproval,
    _classify_entry_change,
    _clear_unacceptable_override,
    _risk_fingerprint,
)


def _entry(**over):
    base = dict(
        status="APPROVED",
        initialLikelihoodScore=4, initialSeverityScore=4, initialRiskLevel="HIGH",
        residualLikelihoodScore=3, residualSeverityScore=3, residualRiskLevel="MODERATE",
        routine="ROUTINE",
        alarpFurtherControlsConsidered=None, alarpGrosslyDisproportionate=None, alarpCostBand="LOW",
        unacceptableOverrideById="user-x", unacceptableOverrideAt=datetime.now(timezone.utc),
        unacceptableOverrideJustification="prior", unacceptableOverrideExpiresAt=datetime.now(timezone.utc),
    )
    base.update(over)
    return SimpleNamespace(**base)


# ── materiality: cost band ───────────────────────────────────────────

def test_alarp_cost_band_change_is_material():
    e = _entry(alarpCostBand="VERY_HIGH")
    fp = _risk_fingerprint(e)  # risk unchanged → not the source of materiality
    before_scalars = {"routine": "ROUTINE", "alarpFurtherControlsConsidered": None,
                      "alarpGrosslyDisproportionate": None, "alarpCostBand": "LOW"}
    material, reasons = _classify_entry_change(e, fp, before_scalars, {"alarpCostBand": "VERY_HIGH"})
    assert material is True
    assert any("alarpCostBand" in r for r in reasons)


def test_pure_narrative_edit_is_not_material():
    e = _entry()
    fp = _risk_fingerprint(e)
    before_scalars = {"routine": "ROUTINE", "alarpFurtherControlsConsidered": None,
                      "alarpGrosslyDisproportionate": None, "alarpCostBand": "LOW"}
    material, reasons = _classify_entry_change(e, fp, before_scalars, {"alarpJustification": "reworded"})
    assert material is False
    assert reasons == []


# ── re-approval ──────────────────────────────────────────────────────

def test_material_edit_withdraws_approval_and_clears_override():
    e = _entry(status="APPROVED")
    moved = _apply_reapproval(e, is_material=True)
    assert moved is True
    assert e.status == REAPPROVAL_STATUS == "PENDING_REAPPROVAL"
    # Override voided — it covered the previous assessment.
    assert e.unacceptableOverrideById is None
    assert e.unacceptableOverrideExpiresAt is None


def test_non_material_edit_leaves_approval():
    e = _entry(status="APPROVED")
    assert _apply_reapproval(e, is_material=False) is False
    assert e.status == "APPROVED"
    assert e.unacceptableOverrideById == "user-x"  # untouched


def test_reapproval_independent_of_study_status():
    # An entry APPROVED under a still-DRAFT study is a real approval — a material
    # edit must withdraw it (the study-status hole is closed).
    e = _entry(status="ACTIVE")
    assert _apply_reapproval(e, is_material=True) is True
    assert e.status == "PENDING_REAPPROVAL"


def test_reapproval_noop_on_unapproved_entry():
    e = _entry(status="DRAFT")
    assert _apply_reapproval(e, is_material=True) is False
    assert e.status == "DRAFT"


def test_clear_override_blanks_all_fields():
    e = _entry()
    _clear_unacceptable_override(e)
    assert e.unacceptableOverrideById is None
    assert e.unacceptableOverrideAt is None
    assert e.unacceptableOverrideJustification is None
    assert e.unacceptableOverrideExpiresAt is None


# ── override-active property (drives the approve gate) ────────────────

def test_override_active_true_when_in_force():
    e = HiraEntry()
    e.unacceptableOverrideById = "plant-head-1"
    e.unacceptableOverrideExpiresAt = datetime.now(timezone.utc) + timedelta(days=30)
    assert e.unacceptableOverrideActive is True


def test_override_inactive_when_expired():
    e = HiraEntry()
    e.unacceptableOverrideById = "plant-head-1"
    e.unacceptableOverrideExpiresAt = datetime.now(timezone.utc) - timedelta(days=1)
    assert e.unacceptableOverrideActive is False


def test_override_inactive_when_absent():
    e = HiraEntry()
    e.unacceptableOverrideById = None
    e.unacceptableOverrideExpiresAt = None
    assert e.unacceptableOverrideActive is False
