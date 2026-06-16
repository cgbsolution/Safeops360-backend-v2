"""Unit tests for the pure-function parts of the agent runtime.

Scope note: a full end-to-end integration test requires a test database
(Postgres + Alembic apply + Agent/AgentPrompt seed + mocked Anthropic
SDK). The repo has no existing pytest DB harness, so wiring one up
sits outside Commit 1 scope. These tests cover the bits that don't
need DB/API:

  • _parse_final_response — tag extraction + JSON parsing edge cases
  • _compute_cost — pricing-table math
  • _RECORD_ID_PATTERNS regex — confirms ID patterns match real-format
    record numbers and don't false-positive on prose

End-to-end agent invocation (tool loop driving the model + DB writes)
is gated by a follow-up commit that lands a DB test harness. The
agent_service code that touches DB/API is reviewed by reading it; the
parser + pattern matchers are reviewed by these tests passing.

Run from the backend root with:
    pytest tests/test_agent_service.py -v
"""

from __future__ import annotations

import re

import pytest

from app.services.agents.agent_service import (
    _RECORD_ID_PATTERNS,
    _compute_cost,
    _parse_final_response,
)


# ─── _parse_final_response ─────────────────────────────────────────────


def test_parse_extracts_well_formed_response() -> None:
    text = """<reasoning>
The incident appears to be a rotating equipment failure.
</reasoning>
<suggestion>
{"recommendedMethod": "FISHBONE", "proposedRootCauses": ["a", "b"]}
</suggestion>
<confidence>0.78</confidence>
"""
    result = _parse_final_response(text)
    assert "rotating equipment failure" in result["reasoning"]
    assert result["suggestion"] == {
        "recommendedMethod": "FISHBONE",
        "proposedRootCauses": ["a", "b"],
    }
    assert result["confidence"] == 0.78


def test_parse_handles_missing_blocks_gracefully() -> None:
    # An agent that emits free prose without our tags shouldn't crash —
    # we just return Nones so the UI can render the raw text fallback.
    result = _parse_final_response("Just some prose without tags.")
    assert result == {"reasoning": None, "suggestion": None, "confidence": None}


def test_parse_unparsed_suggestion_is_preserved_under_marker() -> None:
    # If the agent emits malformed JSON inside <suggestion>, we keep
    # the raw text under "_unparsed" so prompt engineers can see what
    # the model produced and tune accordingly.
    text = "<suggestion>not valid json {missing brace</suggestion>"
    result = _parse_final_response(text)
    assert result["suggestion"] == {"_unparsed": "not valid json {missing brace"}


def test_parse_confidence_out_of_range_is_dropped() -> None:
    # The contract says confidence is 0..1. Values outside this range
    # are suspicious — drop them rather than store noise.
    assert _parse_final_response("<confidence>1.5</confidence>")["confidence"] is None
    assert _parse_final_response("<confidence>-0.2</confidence>")["confidence"] is None
    assert _parse_final_response("<confidence>0.5</confidence>")["confidence"] == 0.5


def test_parse_confidence_non_numeric_is_dropped() -> None:
    assert _parse_final_response("<confidence>high</confidence>")["confidence"] is None


def test_parse_first_block_wins() -> None:
    # Non-greedy regex picks the first <suggestion>...</suggestion>
    # block. The current implementation is fine with one block; this
    # test pins the behaviour so a future "support multiple" change
    # is a deliberate decision.
    text = '<suggestion>{"x": 1}</suggestion><suggestion>{"x": 2}</suggestion>'
    assert _parse_final_response(text)["suggestion"] == {"x": 1}


# ─── _compute_cost ─────────────────────────────────────────────────────


def test_compute_cost_haiku() -> None:
    # Haiku: $1/M input + $5/M output. 10K input + 2K output should be
    # 0.01 + 0.01 = $0.02. Rounded to 6 dp.
    cost = _compute_cost("claude-haiku-4-5-20251001", 10_000, 2_000)
    assert cost == pytest.approx(0.02)


def test_compute_cost_opus() -> None:
    # Opus: $15/M input + $75/M output. 10K input + 2K output should be
    # 0.15 + 0.15 = $0.30.
    cost = _compute_cost("claude-opus-4-7", 10_000, 2_000)
    assert cost == pytest.approx(0.30)


def test_compute_cost_unknown_model_uses_default() -> None:
    # Sonnet pricing is the conservative default ($3/M in, $15/M out).
    cost_default = _compute_cost("claude-something-new", 10_000, 2_000)
    cost_sonnet = _compute_cost("claude-sonnet-4-6", 10_000, 2_000)
    assert cost_default == cost_sonnet


def test_compute_cost_zero_tokens() -> None:
    assert _compute_cost("claude-opus-4-7", 0, 0) == 0.0


# ─── _RECORD_ID_PATTERNS ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected_matches",
    [
        # Real-format incident numbers should match.
        ("See INC-2025-LUM-0042 for context.", ["INC-2025-LUM-0042"]),
        ("Observation OBS-2025-SON-0001 was related.", ["OBS-2025-SON-0001"]),
        ("Near miss NM-2024-LUM-0007 preceded it.", ["NM-2024-LUM-0007"]),
        ("Permit PTW-LUM-0042 was active.", ["PTW-LUM-0042"]),
        # Multiple references in one block of text.
        (
            "Compare INC-2025-LUM-0042 with INC-2024-SON-0103.",
            ["INC-2025-LUM-0042", "INC-2024-SON-0103"],
        ),
    ],
)
def test_record_patterns_match_real_format(text: str, expected_matches: list[str]) -> None:
    found: list[str] = []
    for pattern, _label, _model in _RECORD_ID_PATTERNS:
        found.extend(re.findall(pattern, text))
    assert sorted(found) == sorted(expected_matches)


def test_record_patterns_skip_unrelated_acronyms() -> None:
    # Prose that mentions things like "INCIDENT" or "OBSERVATION" by
    # name should NOT match. The pattern requires the numeric tail.
    prose = """
    The INCIDENT was severe, but the previous OBSERVATION did warn about
    it. We had PTW process improvements, near misses NM are tracked.
    """
    for pattern, _label, _model in _RECORD_ID_PATTERNS:
        assert re.findall(pattern, prose) == []


def test_record_patterns_catch_invented_ids_with_real_shape() -> None:
    # The whole point of hallucination detection: an agent makes up an
    # ID that LOOKS real but doesn't exist. The regex should match (DB
    # lookup will then reject it). This test pins that the pattern
    # itself fires; the DB-lookup arm is exercised by the integration
    # test deferred to a follow-up commit.
    invented = "Similar to INC-2099-FAKE-9999 (which does not exist)."
    incident_pattern = _RECORD_ID_PATTERNS[0][0]
    assert re.findall(incident_pattern, invented) == ["INC-2099-FAKE-9999"]
