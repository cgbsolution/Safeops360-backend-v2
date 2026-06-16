"""Tests for the RCA Assistant system prompt (Commit 3).

These are structural validations — the prompt is a long Markdown document
and we can't easily test its prose. What we CAN test is that everything
the runtime depends on is present:

  • All 9 RCA tool names are mentioned (otherwise the model won't know
    they exist and won't call them)
  • All 6 methodology codes are mentioned (FIVE_WHY / FISHBONE / FTA /
    BOWTIE / TAPROOT / CAUSE_MAP)
  • Critical rule fragments are present (anti-hallucination, output
    format, structured tags)
  • The output-format tags the parser expects are documented in the
    prompt (<reasoning>, <suggestion>, <confidence>)
  • The methodology JSON shapes reference the field names the
    rca-editor.tsx components actually expect (problemStatement,
    whys, categories, topEvent, snapChart, causeNodes, etc.)
  • Drift check: the seed-script's RCA_TOOLS array matches the
    Python registry's RCA_AGENT_TOOL_NAMES

Run from the backend root:
    pytest tests/test_rca_prompt.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.agents.rca_prompts import (
    RCA_ASSISTANT_ACTIVE_VERSION,
    RCA_ASSISTANT_AGENT_CODE,
    load_rca_assistant_prompt,
)
from app.services.agents.tools import RCA_AGENT_TOOL_NAMES


@pytest.fixture(scope="module")
def prompt_text() -> str:
    return load_rca_assistant_prompt()


# ─── Tool coverage ────────────────────────────────────────────────────


@pytest.mark.parametrize("tool_name", RCA_AGENT_TOOL_NAMES)
def test_prompt_mentions_every_rca_tool_by_name(tool_name: str, prompt_text: str) -> None:
    # Each tool name must appear verbatim somewhere in the prompt so the
    # model can match the description text to the tool definition.
    assert tool_name in prompt_text, (
        f"Tool {tool_name!r} is in RCA_AGENT_TOOL_NAMES but never named in the "
        "prompt — the model won't know it exists."
    )


# ─── Methodology coverage ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "method_code",
    ["FIVE_WHY", "FISHBONE", "FTA", "BOWTIE", "TAPROOT", "CAUSE_MAP"],
)
def test_prompt_mentions_every_methodology_code(method_code: str, prompt_text: str) -> None:
    assert method_code in prompt_text, (
        f"Methodology code {method_code!r} not mentioned in the prompt — the "
        "model won't know it's a valid recommendedMethod value."
    )


# ─── Output format tags (parser depends on these) ─────────────────────


@pytest.mark.parametrize("tag", ["<reasoning>", "<suggestion>", "<confidence>"])
def test_prompt_documents_output_tags(tag: str, prompt_text: str) -> None:
    assert tag in prompt_text, (
        f"Output tag {tag!r} missing from prompt — the parser in "
        "agent_service._parse_final_response keys off these tags."
    )


# ─── Methodology JSON shape fields (rca-editor.tsx compatibility) ─────


# Field names per methodology that MUST appear in the prompt's JSON
# schema docs so the model produces data the existing RcaEditor can
# load. Sourced from src/lib/rca/types.ts.
_METHODOLOGY_REQUIRED_FIELDS = {
    "FIVE_WHY": ["problemStatement", '"whys"', "rootCause"],
    "FISHBONE": [
        "problemStatement",
        '"categories"',
        '"manpower"',
        '"machine"',
        '"method"',
        '"material"',
        '"measurement"',
        '"environment"',
        '"rootCauses"',
    ],
    "FTA": ["topEvent", "rootNode", "AND_GATE", "OR_GATE", "BASIC_EVENT"],
    "BOWTIE": ['"threats"', '"consequences"', '"preventiveBarriers"', '"mitigativeBarriers"', '"WORKED"', '"FAILED"', '"ABSENT"'],
    "TAPROOT": ['"snapChart"', '"causalFactors"', '"genericCauses"', '"correctiveActions"', "rootCauseTree"],
    "CAUSE_MAP": ['"impacts"', '"rootEvent"', '"causeNodes"', "parentId"],
}


@pytest.mark.parametrize(
    "method,required",
    [(m, f) for m, fields in _METHODOLOGY_REQUIRED_FIELDS.items() for f in fields],
)
def test_methodology_field_documented(method: str, required: str, prompt_text: str) -> None:
    assert required in prompt_text, (
        f"Methodology {method} requires {required!r} but it's missing from the "
        "prompt's JSON shape doc — model-generated draftAnalysis won't match "
        "the rca-editor.tsx loader."
    )


# ─── Critical rule fragments ──────────────────────────────────────────


_CRITICAL_RULE_KEYWORDS = [
    "NEVER invent record IDs",        # anti-hallucination
    "NEVER state opinion as fact",    # epistemic humility
    "systemic",                       # systems-thinking framing
    "human-in",                       # human-in-loop framing
    "evidenceGaps",                   # structured-output field
    "caveats",                        # structured-output field
    "proposedRootCauses",             # structured-output field
    "similarCasesReferenced",         # structured-output field
]


@pytest.mark.parametrize("keyword", _CRITICAL_RULE_KEYWORDS)
def test_prompt_includes_critical_rule_keyword(keyword: str, prompt_text: str) -> None:
    assert keyword in prompt_text, (
        f"Critical-rule keyword {keyword!r} missing from prompt — the rule "
        "or structured-output field it represents is at risk of being ignored "
        "by the model."
    )


# ─── Prompt length sanity check ───────────────────────────────────────


def test_prompt_is_substantive_but_not_runaway(prompt_text: str) -> None:
    # Sanity: somewhere between "a few paragraphs" and "a small novella".
    # Bumps in either direction are likely a regression worth flagging.
    length = len(prompt_text)
    assert 5_000 < length < 30_000, (
        f"Prompt length {length} chars is out of expected band "
        "(5K-30K). Check for accidental truncation or unbounded growth."
    )


# ─── Constants consistency ────────────────────────────────────────────


def test_constants_pin_to_expected_values() -> None:
    # The Python runtime and the TS seed both reference these constants.
    # If they drift apart, the seed will write to one agent while the
    # runtime queries another.
    assert RCA_ASSISTANT_AGENT_CODE == "RCA_ASSISTANT"
    assert RCA_ASSISTANT_ACTIVE_VERSION == 1


def test_loader_returns_prompt_content() -> None:
    text = load_rca_assistant_prompt()
    assert text.startswith("You are the RcaAssistantAgent"), (
        "Prompt file content has drifted from expected opening; check for "
        "accidental truncation."
    )


# ─── Drift check: seed RCA_TOOLS vs registry RCA_AGENT_TOOL_NAMES ─────


def test_seed_script_tool_list_matches_python_registry() -> None:
    """Guards against the TS seed and Python registry drifting apart.

    Parses the RCA_TOOLS array out of seed-agents.ts using a regex over
    the file text — we don't run TypeScript here, but the array literal
    is simple enough to extract.
    """
    seed_path = (
        Path(__file__).resolve().parents[2]
        / "safeops_360"
        / "prisma"
        / "seed-agents.ts"
    )
    if not seed_path.is_file():
        pytest.skip(
            f"seed-agents.ts not found at {seed_path} — running from a "
            "checkout that doesn't include the frontend. Skip the drift "
            "check (the test is still meaningful in CI / full repo)."
        )

    text = seed_path.read_text(encoding="utf-8")
    match = re.search(r"const RCA_TOOLS\s*=\s*\[(.*?)\]\s*as const;", text, re.DOTALL)
    assert match is not None, "Could not find RCA_TOOLS array in seed-agents.ts"

    # Extract the quoted tool names from the array literal.
    seed_names = re.findall(r'"([^"]+)"', match.group(1))
    assert seed_names == RCA_AGENT_TOOL_NAMES, (
        "RCA_TOOLS in seed-agents.ts does not match RCA_AGENT_TOOL_NAMES in "
        "the Python registry.\n"
        f"  seed:     {seed_names}\n"
        f"  registry: {RCA_AGENT_TOOL_NAMES}\n"
        "When adding a tool, update both lists."
    )
