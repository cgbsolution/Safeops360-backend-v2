"""Risk-based audit frequency recommendation — scoring tests.

Design: [docs/cams/08-audit-programme.md](../../docs/cams/08-audit-programme.md) §5.

Two behaviours matter more than the arithmetic itself and are asserted hardest:

  * **An unavailable input is never scored as zero.** A missing feed
    redistributes its weight across the available signals and is reported by
    name. Defaulting it to zero is the F-48 mistake — a broken dependency that
    silently reads as "nothing wrong".
  * **The engine recommends; it never applies.** Frequency changes only through
    `accept_recommendation`, which needs a user id.
"""

from __future__ import annotations

from app.services.programme.recommend import (
    BAND_HOLD,
    BAND_INCREASE,
    REDUCTION_VETO_INPUTS,
    SATURATION,
    WEIGHTS,
    InputSignal,
    Recommendation,
    band_for,
    frequency_for,
    reduction_vetoed,
    score_signals,
)


def sig(key: str, raw: float | None, available: bool = True) -> InputSignal:
    return InputSignal(key, raw, available, key)


def all_signals(**over) -> list[InputSignal]:
    base = {k: 0.0 for k in WEIGHTS}
    base.update(over)
    return [sig(k, v) for k, v in base.items()]


# ── weights ──────────────────────────────────────────────────────────


def test_weights_sum_to_100():
    """The weighting IS the policy — it must be legible as percentages."""
    assert sum(WEIGHTS.values()) == 100


def test_every_weight_has_a_saturation_point():
    """Without saturation a site with 40 open NCs swamps every other signal."""
    assert set(WEIGHTS) == set(SATURATION)


# ── scoring ──────────────────────────────────────────────────────────


def test_clean_scope_unit_scores_zero():
    score, scale = score_signals(all_signals())
    assert score == 0.0 and scale == 1.0


def test_fully_saturated_scores_100():
    score, _ = score_signals([sig(k, SATURATION[k]) for k in WEIGHTS])
    assert score == 100.0


def test_score_is_capped_at_100_when_inputs_exceed_saturation():
    score, _ = score_signals([sig(k, SATURATION[k] * 10) for k in WEIGHTS])
    assert score == 100.0


def test_partial_signal_contributes_proportionally():
    # Half-saturated open NCs only: 30 weight × 0.5 = 15.
    score, _ = score_signals(all_signals(openCriticalMajorNCs=SATURATION["openCriticalMajorNCs"] / 2))
    assert score == 15.0


def test_negative_input_cannot_reduce_the_score():
    score, _ = score_signals(all_signals(overdueCapas=-99))
    assert score == 0.0


# ── unavailable inputs — the F-48 lesson ─────────────────────────────


def test_unavailable_input_is_not_scored_as_zero():
    """With the incident signal unavailable, a fully-saturated remainder must
    still reach 100 — otherwise a missing feed silently caps every site's score
    and looks like good news."""
    signals = [sig(k, SATURATION[k]) for k in WEIGHTS if k != "incidentSignal"]
    signals.append(sig("incidentSignal", None, available=False))
    score, scale = score_signals(signals)
    assert score == 100.0
    assert scale > 1.0  # weight was redistributed, not dropped


def test_unavailable_inputs_are_reported_by_name():
    signals = all_signals()
    signals = [s for s in signals if s.key != "incidentSignal"]
    signals.append(sig("incidentSignal", None, available=False))
    r = Recommendation("u1", 2, 2, 0.0, "REDUCE", signals, 1.0)
    assert "incidentSignal" in r.unavailable
    assert "unavailable" in r.narrative()


def test_all_inputs_unavailable_scores_zero_without_dividing_by_zero():
    signals = [sig(k, None, available=False) for k in WEIGHTS]
    score, scale = score_signals(signals)
    assert score == 0.0 and scale == 1.0


def test_narrative_distinguishes_no_signals_from_missing_signals():
    clean = Recommendation("u1", 1, 1, 0.0, "REDUCE", all_signals(), 1.0).narrative()
    assert "no adverse signals" in clean
    assert "unavailable" not in clean


# ── bands ────────────────────────────────────────────────────────────


def test_band_thresholds():
    assert band_for(BAND_INCREASE) == "INCREASE"
    assert band_for(BAND_INCREASE - 0.1) == "HOLD"
    assert band_for(BAND_HOLD) == "HOLD"
    assert band_for(BAND_HOLD - 0.1) == "REDUCE"


# ── the reduction veto ───────────────────────────────────────────────
#
# Caught by a failing test during the build: repeat findings at FULL saturation
# score 25, which is below BAND_HOLD (40), so the plain threshold recommended
# auditing that scope unit LESS often. That is the opposite of what clause 9.2.2
# asks for. The veto exists because of this case.


def test_repeat_chains_alone_score_below_the_hold_threshold():
    """The arithmetic that made the veto necessary — documented, not hidden."""
    score, _ = score_signals(all_signals(repeatFindingChains=SATURATION["repeatFindingChains"]))
    assert score == float(WEIGHTS["repeatFindingChains"])
    assert score < BAND_HOLD


def test_open_repeat_findings_veto_a_reduction():
    """Never recommend auditing something less often while its prior findings
    keep recurring."""
    signals = all_signals(repeatFindingChains=SATURATION["repeatFindingChains"])
    vetoes = reduction_vetoed(signals)
    assert "repeatFindingChains" in vetoes
    score, _ = score_signals(signals)
    assert band_for(score, vetoes=vetoes) == "HOLD"


def test_open_critical_ncs_veto_a_reduction():
    signals = all_signals(openCriticalMajorNCs=1)
    assert band_for(score_signals(signals)[0], vetoes=reduction_vetoed(signals)) == "HOLD"


def test_a_genuinely_clean_scope_unit_may_still_be_reduced():
    """The veto must not make REDUCE unreachable — a clean, frequently-audited
    scope unit is exactly what should free up auditor days."""
    signals = all_signals()
    assert reduction_vetoed(signals) == []
    assert band_for(score_signals(signals)[0], vetoes=[]) == "REDUCE"


def test_veto_never_forces_an_increase():
    """The veto floors the band at HOLD. How much worse than 'hold' the
    situation is remains the score's job."""
    signals = all_signals(repeatFindingChains=1)
    assert band_for(score_signals(signals)[0], vetoes=reduction_vetoed(signals)) != "INCREASE"


def test_unavailable_veto_signal_does_not_veto():
    """An unmeasurable signal cannot be evidence of a problem."""
    signals = [s for s in all_signals() if s.key != "repeatFindingChains"]
    signals.append(sig("repeatFindingChains", None, available=False))
    assert reduction_vetoed(signals) == []


def test_narrative_explains_a_vetoed_hold():
    """A HOLD sitting under a low score looks inconsistent unless the reason is
    stated, so the narrative says why."""
    signals = all_signals(repeatFindingChains=2)
    score, scale = score_signals(signals)
    vetoes = reduction_vetoed(signals)
    r = Recommendation("u1", 2, 2, score, band_for(score, vetoes=vetoes), signals, scale, vetoes)
    assert "held rather than reduced" in r.narrative()


def test_vetoes_are_exposed_in_the_payload():
    signals = all_signals(openCriticalMajorNCs=3)
    vetoes = reduction_vetoed(signals)
    score, scale = score_signals(signals)
    d = Recommendation("u1", 2, 2, score, band_for(score, vetoes=vetoes), signals, scale, vetoes).as_dict()
    assert d["reductionVetoedBy"] == ["openCriticalMajorNCs"]


# ── frequency translation ────────────────────────────────────────────


def test_increase_adds_one_audit():
    assert frequency_for(2, "INCREASE", 4) == 3


def test_reduce_removes_one_audit():
    assert frequency_for(3, "REDUCE", 4) == 2


def test_hold_keeps_the_current_frequency():
    assert frequency_for(2, "HOLD", 4) == 2


def test_frequency_never_drops_below_one():
    """A scope unit that is in scope gets audited. Recommending zero would let
    the programme quietly drop coverage."""
    assert frequency_for(1, "REDUCE", 4) == 1
    assert frequency_for(None, "REDUCE", 4) == 1


def test_frequency_never_exceeds_the_number_of_periods():
    """No point recommending more audits than there are periods to hold them."""
    assert frequency_for(4, "INCREASE", 4) == 4
    assert frequency_for(12, "INCREASE", 4) == 4


def test_missing_current_frequency_is_treated_as_one():
    assert frequency_for(None, "INCREASE", 4) == 2
    assert frequency_for(None, "HOLD", 4) == 1


# ── payload shape ────────────────────────────────────────────────────


def test_recommendation_payload_carries_its_inputs_not_just_its_output():
    """Persisting the arithmetic is what makes the recommendation defensible —
    a reviewer can disagree with a number rather than with a black box."""
    signals = all_signals(openCriticalMajorNCs=4)
    score, scale = score_signals(signals)
    d = Recommendation("u1", 2, 3, score, band_for(score), signals, scale).as_dict()

    assert d["score"] == score
    assert len(d["inputs"]) == len(WEIGHTS)
    for row in d["inputs"]:
        assert {"input", "label", "rawValue", "available", "weight", "contribution"} <= set(row)
    assert d["narrative"]


def test_contribution_reflects_the_redistribution_scale():
    signals = [sig(k, SATURATION[k]) for k in WEIGHTS if k != "statutoryCriticality"]
    signals.append(sig("statutoryCriticality", None, available=False))
    score, scale = score_signals(signals)
    d = Recommendation("u1", 1, 2, score, band_for(score), signals, scale).as_dict()
    nc = next(r for r in d["inputs"] if r["input"] == "openCriticalMajorNCs")
    # 30 of 90 available weight, rescaled to 100 → 33.3
    assert nc["weight"] > WEIGHTS["openCriticalMajorNCs"]
