"""Chemical threshold engine — offline unit tests for the pure logic.

Covers the two things that decide whether an MOC is raised at the right moment:
unit normalisation and the BELOW/APPROACHING/BREACHED band boundaries. The
DB-level behaviour (ledger aggregation, edge-triggering against
ChemicalThresholdState, the auto-MOC itself) is verified against a real Postgres
by `verify_chemical_constraints.py`, because the guarantees there are database
constraints and triggers that an in-memory fake cannot exercise honestly.
"""

from __future__ import annotations

import pytest

from app.services.chemical_threshold import _canonical, derive_status


# ── unit normalisation ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "qty,unit,expected",
    [
        (1, "KG", (1.0, "KG")),
        (1, "kg", (1.0, "KG")),        # case-insensitive
        (1, " KG ", (1.0, "KG")),      # whitespace tolerant
        (1000, "G", (1.0, "KG")),
        (2, "T", (2000.0, "KG")),
        (2, "TONNE", (2000.0, "KG")),
        (1, "L", (1.0, "L")),
        (500, "ML", (0.5, "L")),
        (1, "M3", (1000.0, "L")),
    ],
)
def test_canonical_conversions(qty, unit, expected):
    got = _canonical(qty, unit)
    assert got is not None
    assert got[1] == expected[1]
    assert got[0] == pytest.approx(expected[0])


def test_unknown_unit_returns_none_rather_than_guessing():
    """A threshold engine that quietly treats an unrecognised unit as kilograms
    is worse than one that says it cannot tell."""
    assert _canonical(1, "DRUM") is None
    assert _canonical(1, "") is None
    assert _canonical(1, "PALLET") is None


def test_mass_and_volume_are_different_families():
    """1 L is not 1 KG without a density, and this module holds no densities —
    SDS values are evidence here, not parsed data. The families must not mix."""
    mass = _canonical(1, "KG")
    volume = _canonical(1, "L")
    assert mass[1] != volume[1]


# ── band boundaries ───────────────────────────────────────────────────────────
def test_at_threshold_is_a_breach_not_a_near_miss():
    """MSIHC thresholds read "quantities equal to or exceeding". Using `>` here
    would let a site sit exactly on the limit with no obligation raised."""
    assert derive_status(10_000, 10_000, 0.8) == "BREACHED"
    assert derive_status(9_999.99, 10_000, 0.8) == "APPROACHING"


def test_approaching_band():
    assert derive_status(8_000, 10_000, 0.8) == "APPROACHING"   # exactly at ratio
    assert derive_status(7_999, 10_000, 0.8) == "BELOW"
    assert derive_status(9_500, 10_000, 0.8) == "APPROACHING"


def test_below_band():
    assert derive_status(0, 10_000, 0.8) == "BELOW"
    assert derive_status(1, 10_000, 0.8) == "BELOW"


def test_custom_approach_ratio_is_honoured():
    """The ratio is per-rule config: a Schedule 3 off-site plan needs more lead
    time than a Schedule 2 on-site one."""
    assert derive_status(5_000, 10_000, 0.5) == "APPROACHING"
    assert derive_status(5_000, 10_000, 0.9) == "BELOW"


def test_zero_or_negative_threshold_never_breaches():
    """Defensive: a CHECK constraint forbids these, but a NULL-ish value slipping
    through must not mark every site in the estate as breached."""
    assert derive_status(500, 0, 0.8) == "BELOW"
    assert derive_status(500, -1, 0.8) == "BELOW"
