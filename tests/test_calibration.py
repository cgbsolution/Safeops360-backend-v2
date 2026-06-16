"""Tests for the calibration service's pure logic (Commit 5).

The DB-bound parts of run_calibration() (aggregating AgentInvocation
rows + writing back to Agent / AgentPrompt) still need the deferred
DB harness — that work is exercised manually through the
/api/agents/calibration/run endpoint. What we CAN test here is the
calibration-score formula, which is the part most likely to drift if
someone tweaks the weights.

Run from the backend root:
    pytest tests/test_calibration.py -v
"""

from __future__ import annotations

import pytest

from app.services.agents.calibration import _compute_calibration_score


# ─── Score formula edge cases ──────────────────────────────────────────


def test_score_is_none_when_no_decisions() -> None:
    # The dashboard should distinguish "not yet calibrated" from
    # "very bad" — formula returns None when nothing has been decided.
    assert _compute_calibration_score(acc=0, mod=0, rej=0, exp=0) is None


def test_score_perfect_acceptance_is_one() -> None:
    assert _compute_calibration_score(acc=10, mod=0, rej=0, exp=0) == 1.0


def test_score_all_rejections_is_zero() -> None:
    assert _compute_calibration_score(acc=0, mod=0, rej=10, exp=0) == 0.0


def test_score_all_expired_counts_as_rejection() -> None:
    # EXPIRED (cron timed out the human's decision window) is treated
    # as a soft reject — same effect on the score.
    assert _compute_calibration_score(acc=0, mod=0, rej=0, exp=10) == 0.0


def test_modifications_count_as_half() -> None:
    # Half the decisions accepted, half modified → score = 0.75.
    # Rationale: a modification means the agent gave a useful starting
    # point but missed something. That's a partial win.
    assert _compute_calibration_score(acc=5, mod=5, rej=0, exp=0) == 0.75


def test_realistic_pilot_mix() -> None:
    # Realistic pilot data: 60% accepted, 25% modified, 10% rejected,
    # 5% expired. Expected score: (60 + 12.5) / 100 = 0.725.
    score = _compute_calibration_score(acc=60, mod=25, rej=10, exp=5)
    assert score == pytest.approx(0.725)


def test_score_is_rounded_to_four_dp() -> None:
    # 1 accept, 2 modified: (1 + 1) / 3 = 0.6666...
    # Stored to 4 dp so the dashboard renders a clean number and the
    # calibration column doesn't churn over float noise.
    assert _compute_calibration_score(acc=1, mod=2, rej=0, exp=0) == 0.6667


@pytest.mark.parametrize(
    "acc,mod,rej,exp,want",
    [
        # Mostly modified — agent useful but imprecise. Below the
        # L0→L1 promotion threshold of ~0.65.
        (0, 50, 0, 0, 0.5),
        # Mostly accepted but several rejections. Borderline.
        (40, 5, 15, 0, 0.7083),
        # Equal accept/reject. Score sits just below 0.5.
        (10, 0, 10, 0, 0.5),
    ],
)
def test_score_known_mixes(
    acc: int, mod: int, rej: int, exp: int, want: float
) -> None:
    score = _compute_calibration_score(acc=acc, mod=mod, rej=rej, exp=exp)
    assert score is not None
    assert score == pytest.approx(want)
