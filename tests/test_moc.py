"""MOC (Management of Change) Gensuite-parity rebuild — offline unit tests.

Pure-function coverage for the new lifecycle gate helpers, in the house no-DB
style (SimpleNamespace fixtures). DB-touching paths (training gate, emergency
retro-approval, reviewer suggestion, attachments) are exercised by driving the
live flow, not here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.routers.moc import (
    _band_from_score,
    _emergency_pending_retro,
    _pssr_gate_ok,
    _residual_risk_string,
)


# ─── Risk banding (Combined Risk Register convention) ───────────────────────

def test_band_from_score_thresholds():
    assert [_band_from_score(s) for s in (1, 4, 5, 9, 10, 15, 16, 25)] == [
        "low", "low", "moderate", "moderate", "high", "high", "critical", "critical"
    ]
    assert _band_from_score(None) is None


def test_residual_risk_string_from_band_and_score():
    # explicit band wins, with MEDIUM mapped to the MOC "moderate" chip key
    assert _residual_risk_string({"band": "MEDIUM"}) == "moderate"
    assert _residual_risk_string({"band": "CRITICAL"}) == "critical"
    # falls back to score when no band present
    assert _residual_risk_string({"score": 12}) == "high"
    assert _residual_risk_string({"likelihood": 2, "severity": 2, "score": 4}) == "low"
    assert _residual_risk_string(None) is None


# ─── PSSR closure gate ──────────────────────────────────────────────────────

def test_pssr_gate_not_required_passes():
    cr = SimpleNamespace(pssrRequired=False, pssrChecklist=None)
    ok, _ = _pssr_gate_ok(cr)
    assert ok is True


def test_pssr_gate_required_but_incomplete_blocks():
    cr = SimpleNamespace(pssrRequired=True, pssrChecklist=None)
    ok, msg = _pssr_gate_ok(cr)
    assert ok is False and "pre-startup safety review" in msg.lower()

    cr2 = SimpleNamespace(pssrRequired=True, pssrChecklist={"outcome": "go", "completedAt": None})
    assert _pssr_gate_ok(cr2)[0] is False


def test_pssr_gate_required_and_go_passes():
    cr = SimpleNamespace(
        pssrRequired=True, pssrChecklist={"outcome": "go", "completedAt": "2026-07-01T00:00:00Z"}
    )
    assert _pssr_gate_ok(cr)[0] is True


def test_pssr_gate_no_go_blocks_even_when_completed():
    cr = SimpleNamespace(
        pssrRequired=True, pssrChecklist={"outcome": "no_go", "completedAt": "2026-07-01T00:00:00Z"}
    )
    ok, msg = _pssr_gate_ok(cr)
    assert ok is False and "no-go" in msg.lower()


# ─── Emergency pending-retro flag ───────────────────────────────────────────

def _cr(urgency="standard", due=None, status="submitted"):
    return SimpleNamespace(urgency=urgency, emergencyRetroApprovalDueAt=due, status=status)


def test_emergency_pending_retro_true_when_open_emergency_with_due():
    due = datetime.now(timezone.utc) + timedelta(hours=72)
    assert _emergency_pending_retro(_cr("emergency", due, "implementation_in_progress")) is True


def test_emergency_pending_retro_false_when_standard_or_cleared_or_closed():
    due = datetime.now(timezone.utc) + timedelta(hours=72)
    assert _emergency_pending_retro(_cr("standard", due, "implementation_in_progress")) is False
    assert _emergency_pending_retro(_cr("emergency", None, "implementation_in_progress")) is False
    assert _emergency_pending_retro(_cr("emergency", due, "closed_successful")) is False
