"""Incident Intelligence (Slice 1) — offline unit tests.

Pure-function coverage for Features 5 (severity scoring + escalation) and the
shared trend matcher, in the house no-DB style (SimpleNamespace fixtures).
DB-touching orchestration (apply_severity_scoring, similar_incidents) is
exercised by driving the live flow, not here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import incident_ai, incident_severity as sev
from app.services import incident_similarity as sim
from app.services.erm import band_for_score


# ─── Feature 5 — likelihood / consequence / band ────────────────────────────

def test_likelihood_from_recurrence_bands():
    assert [sev._likelihood_from_recurrence(n) for n in (0, 1, 2, 3, 4, 9)] == [1, 2, 3, 4, 5, 5]


def test_consequence_fallback_from_label():
    assert sev._consequence_fallback("LOW") == 1
    assert sev._consequence_fallback("MEDIUM") == 2
    assert sev._consequence_fallback("HIGH") == 4
    assert sev._consequence_fallback("CRITICAL") == 5
    assert sev._consequence_fallback(None) == 2  # safe default


def test_band_floor_never_downgrades_human_label():
    # numeric scoring may escalate a label but never mechanically downgrade it
    assert sev._higher_band("HIGH", "LOW") == "HIGH"
    assert sev._higher_band("LOW", "CRITICAL") == "CRITICAL"
    assert sev._higher_band("MEDIUM", "MEDIUM") == "MEDIUM"


def test_score_uses_reused_erm_bands():
    # score = L×C mapped through the SAME ERM 5×5 bands (1-4/5-9/10-15/16-25)
    assert band_for_score(5 * 5) == "CRITICAL"   # 25
    assert band_for_score(3 * 4) == "HIGH"        # 12
    assert band_for_score(2 * 3) == "MEDIUM"      # 6
    assert band_for_score(1 * 3) == "LOW"         # 3


# ─── Feature 5 — escalation decision ────────────────────────────────────────

def test_escalation_on_high_score():
    reasons = sev.escalation_reasons(score=15, recurrence=0)
    assert len(reasons) == 1 and "threshold" in reasons[0]


def test_escalation_on_recurrence_regardless_of_score():
    # 3 in the same equipment category escalates even at a low score
    reasons = sev.escalation_reasons(score=4, recurrence=3)
    assert len(reasons) == 1 and "equipment category" in reasons[0]


def test_no_escalation_below_both_thresholds():
    assert sev.escalation_reasons(score=8, recurrence=2) == []


def test_both_triggers_give_both_reasons():
    assert len(sev.escalation_reasons(score=20, recurrence=5)) == 2


# ─── Feature 3 seed — matcher token overlap + normalisation ─────────────────

def test_matcher_weights_and_floor_constants():
    assert sim._MAX_RAW == 9  # 3+2+2+1+1
    assert sim.MATCH_FLOOR == 40


def test_cause_token_extraction_and_overlap():
    a = sim._tokens(["Guard bypassed"], "operator error")
    b = sim._tokens(["guard removed"])
    assert "guard" in a and "bypassed" in a and "operator" in a
    assert a & b == {"guard"}  # overlap detected → cause weight would apply
    assert sim._tokens(None, "") == set()  # nothing from empties


# ─── Feature 2 — computed confidence (not model-guessed) ────────────────────

def _match(number, itype):
    return {"incidentId": number, "number": number, "type": itype, "rootCauses": []}


def test_confidence_is_category_overlap_ratio():
    # 3 of 4 retrieved matches share the current incident's type → 75%
    matches = [_match("A", "FIRE"), _match("B", "FIRE"), _match("C", "FIRE"), _match("D", "LTI")]
    cur = matches
    matching = sum(1 for m in cur if m["type"] == "FIRE")
    assert round(matching / len(cur) * 100) == 75


def test_ai_helpers_are_failsoft_when_unconfigured(monkeypatch):
    # No provider configured → helpers return None, never raise (UI degrades to manual)
    monkeypatch.setattr(incident_ai, "is_configured", lambda: False)
    import asyncio

    inc = SimpleNamespace(
        id="i1", number="INC-1", type=SimpleNamespace(value="FIRE"),
        location="Bay 3", specificLocation="", initialDescription="x",
        description="y", immediateAction="", immediateCauses=[], underlyingCauses=[],
    )
    assert asyncio.run(incident_ai.draft_summary(None, inc)) is None
    assert asyncio.run(incident_ai.suggest_root_cause(None, inc)) is None


# ─── Slice 2 — Feature 4 statutory (pure gate + renderer) ───────────────────

def _inc(itype="PROPERTY_DAMAGE", severity="MEDIUM", reportable=False):
    return SimpleNamespace(type=SimpleNamespace(value=itype), severity=severity, isReportable=reportable)


def test_statutory_trigger_gate():
    from app.services.statutory_forms import _matches

    # property-damage-only, non-reportable → does NOT match a reportable-flag rule
    assert _matches({"reportableFlag": True}, _inc("PROPERTY_DAMAGE", "MEDIUM", reportable=False)) is False
    # LTI, reportable, HIGH → matches an LTI/min-HIGH/reportable rule
    assert _matches({"incidentType": ["LTI"], "minSeverity": "HIGH", "reportableFlag": True},
                    _inc("LTI", "HIGH", reportable=True)) is True
    # severity below threshold fails
    assert _matches({"minSeverity": "CRITICAL"}, _inc("FIRE", "HIGH", reportable=True)) is False


def test_statutory_pdf_renders_valid_bytes():
    from app.services.statutory_forms import render_form_pdf

    b = render_form_pdf("FORM_18", "Factories Act - Form 18",
                        {"Incident Number": "INC-NW-DEMO-005", "Injured Person": "—", "Days Lost": 0},
                        {"version": 1, "incidentNumber": "INC-NW-DEMO-005", "generatedAt": "2026-07-10", "jurisdiction": "UP"})
    assert b[:4] == b"%PDF" and len(b) > 500


# ─── Slice 2 — Feature 6 WhatsApp (OTP hashing, verified gate, fail-soft) ────

def test_whatsapp_otp_hash_deterministic_and_distinct():
    from app.services import whatsapp

    assert whatsapp._hash_otp("123456") == whatsapp._hash_otp("123456")  # deterministic
    assert whatsapp._hash_otp("123456") != whatsapp._hash_otp("654321")  # distinct
    assert len(whatsapp._hash_otp("000000")) == 64  # sha256 hex


def test_whatsapp_verified_gate():
    from app.services import whatsapp

    assert whatsapp.is_verified(None) is False
    assert whatsapp.is_verified(SimpleNamespace(verifiedAt=None, employeeId="u1")) is False
    assert whatsapp.is_verified(SimpleNamespace(verifiedAt=datetime.now(timezone.utc), employeeId=None)) is False
    assert whatsapp.is_verified(SimpleNamespace(verifiedAt=datetime.now(timezone.utc), employeeId="u1")) is True


def test_whatsapp_classify_failsoft(monkeypatch):
    import asyncio
    from app.services.ai import anthropic_client
    from app.services import whatsapp

    monkeypatch.setattr(anthropic_client, "is_configured", lambda: False)
    out = asyncio.run(whatsapp.classify_transcript("boiler valve leaked steam"))
    assert out["type"] in {e for e in ("FIRST_AID", "MTC", "RWC", "LTI", "FATALITY", "PROPERTY_DAMAGE", "ENVIRONMENTAL", "FIRE", "PROCESS_SAFETY", "HIPO_NEAR_MISS")}
    assert out["severity"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


# ─── Slice 2 — Feature 8 cost coercion ──────────────────────────────────────

def test_cost_float_coercion():
    from app.services.incident_cost import _f

    assert _f(None) == 0.0 and _f("") == 0.0 and _f("12.5") == 12.5 and _f(3) == 3.0
