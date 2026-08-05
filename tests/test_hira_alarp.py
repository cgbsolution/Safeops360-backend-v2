"""HIRA ALARP tolerability banding — offline unit tests.

The ALARP logic in app.routers.hira is factored into pure helpers that only
touch attributes on the entry, so SimpleNamespace stand-ins cover them with no
DB — the house test style.

Policy under test (seeded default):
  CRITICAL         -> UNACCEPTABLE
  HIGH / MODERATE  -> TOLERABLE (accept only if ALARP demonstrated)
  LOW              -> BROADLY_ACCEPTABLE
Enforcement is warn-only: an UNACCEPTABLE residual is marked not-acceptable,
never hard-blocked.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.routers.hira import (
    DEFAULT_ALARP_BANDS,
    _alarp_demonstrated,
    _alarp_region,
    _evaluate_alarp,
)


def _entry(**overrides):
    """A residual-bearing entry stub with the ALARP fields cleared."""
    base = dict(
        residualRiskLevel="MODERATE",
        residualAlarpRegion=None,
        alarpStatus=None,
        alarpFurtherControlsConsidered=None,
        alarpGrosslyDisproportionate=None,
        alarpJustification=None,
        alarpDemonstratedById=None,
        alarpDemonstratedAt=None,
        residualAcceptable=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── _alarp_region ────────────────────────────────────────────────────

def test_default_region_mapping():
    assert _alarp_region("CRITICAL", None) == "UNACCEPTABLE"
    assert _alarp_region("HIGH", None) == "TOLERABLE"
    assert _alarp_region("MODERATE", None) == "TOLERABLE"
    assert _alarp_region("LOW", None) == "BROADLY_ACCEPTABLE"


def test_region_none_when_no_level():
    assert _alarp_region(None, None) is None
    assert _alarp_region("", DEFAULT_ALARP_BANDS) is None


def test_per_matrix_override_wins():
    # A stricter matrix that pushes HIGH into the unacceptable region.
    bands = {**DEFAULT_ALARP_BANDS, "HIGH": "UNACCEPTABLE"}
    assert _alarp_region("HIGH", bands) == "UNACCEPTABLE"
    # Unmapped level falls back to the default map.
    assert _alarp_region("LOW", {"CRITICAL": "UNACCEPTABLE"}) == "BROADLY_ACCEPTABLE"


# ── _alarp_demonstrated ──────────────────────────────────────────────

def test_demonstration_requires_all_three_parts():
    # Fully complete → demonstrated.
    e = _entry(
        alarpFurtherControlsConsidered=True,
        alarpGrosslyDisproportionate=True,
        alarpJustification="LEV grossly disproportionate to the marginal reduction.",
    )
    assert _alarp_demonstrated(e) is True

    # Missing justification → not demonstrated.
    assert _alarp_demonstrated(_entry(
        alarpFurtherControlsConsidered=True,
        alarpGrosslyDisproportionate=True,
        alarpJustification="   ",
    )) is False

    # Verdict says further reduction IS practicable → not demonstrated.
    assert _alarp_demonstrated(_entry(
        alarpFurtherControlsConsidered=True,
        alarpGrosslyDisproportionate=False,
        alarpJustification="Should implement guarding.",
    )) is False

    # No decision recorded on whether further controls were considered.
    assert _alarp_demonstrated(_entry(
        alarpFurtherControlsConsidered=None,
        alarpGrosslyDisproportionate=True,
        alarpJustification="x",
    )) is False


# ── _evaluate_alarp ──────────────────────────────────────────────────

def test_broadly_acceptable_is_accepted_without_demonstration():
    e = _entry(residualRiskLevel="LOW")
    _evaluate_alarp(e, "BROADLY_ACCEPTABLE", None, "u1")
    assert e.residualAlarpRegion == "BROADLY_ACCEPTABLE"
    assert e.alarpStatus == "NOT_REQUIRED"
    assert e.residualAcceptable is True


def test_tolerable_without_demonstration_is_required_and_not_acceptable():
    e = _entry(residualRiskLevel="MODERATE")
    _evaluate_alarp(e, "TOLERABLE", None, "u1")
    assert e.alarpStatus == "REQUIRED"
    assert e.residualAcceptable is False
    assert e.alarpDemonstratedById is None


def test_tolerable_with_demonstration_is_accepted_and_signed_off():
    e = _entry(
        residualRiskLevel="MODERATE",
        alarpFurtherControlsConsidered=True,
        alarpGrosslyDisproportionate=True,
        alarpJustification="Cost grossly disproportionate to benefit.",
    )
    _evaluate_alarp(e, "TOLERABLE", None, "user-42")
    assert e.alarpStatus == "DEMONSTRATED"
    assert e.residualAcceptable is True
    assert e.alarpDemonstratedById == "user-42"
    assert e.alarpDemonstratedAt is not None


def test_stricter_routine_threshold_still_blocks_a_demonstrated_tolerable():
    # Emergency activities are capped at LOW: a demonstrated MODERATE residual
    # is ALARP but still not acceptable under the legacy per-routine threshold.
    e = _entry(
        residualRiskLevel="MODERATE",
        alarpFurtherControlsConsidered=True,
        alarpGrosslyDisproportionate=True,
        alarpJustification="ALARP.",
    )
    _evaluate_alarp(e, "TOLERABLE", "LOW", "u1")
    assert e.alarpStatus == "DEMONSTRATED"  # ALARP is demonstrated…
    assert e.residualAcceptable is False     # …but the threshold gate still bites.


def test_unacceptable_is_never_acceptable_but_not_hard_blocked():
    e = _entry(residualRiskLevel="CRITICAL")
    _evaluate_alarp(e, "UNACCEPTABLE", None, "u1")
    assert e.residualAlarpRegion == "UNACCEPTABLE"
    assert e.alarpStatus == "NOT_REQUIRED"
    assert e.residualAcceptable is False


def test_walking_back_a_demonstration_clears_signoff():
    e = _entry(
        residualRiskLevel="MODERATE",
        alarpFurtherControlsConsidered=True,
        alarpGrosslyDisproportionate=True,
        alarpJustification="ALARP.",
    )
    _evaluate_alarp(e, "TOLERABLE", None, "u1")
    assert e.alarpDemonstratedAt is not None

    # Reviewer flips the verdict: further reduction is practicable after all.
    e.alarpGrosslyDisproportionate = False
    _evaluate_alarp(e, "TOLERABLE", None, "u1")
    assert e.alarpStatus == "REQUIRED"
    assert e.residualAcceptable is False
    assert e.alarpDemonstratedById is None
    assert e.alarpDemonstratedAt is None


def test_no_region_clears_status():
    e = _entry(residualRiskLevel=None)
    _evaluate_alarp(e, None, None, "u1")
    assert e.residualAlarpRegion is None
    assert e.alarpStatus is None
