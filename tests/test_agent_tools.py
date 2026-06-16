"""Tests for the RCA agent tools (Commit 2).

Scope note: same DB-harness constraint as test_agent_service.py — these
tests cover the parts that don't need a database:

  • Each tool exposes a DEFINITION dict matching Anthropic's schema
  • Each tool's input_schema is well-formed JSON Schema
  • The tool registry includes the canonical RCA tool list
  • RCA_AGENT_TOOL_NAMES references exactly the registered names
  • get_industry_benchmark is a pure function — full behaviour can be
    tested without a DB

DB-bound query behaviour (find_*, get_*, search_*, check_*) is gated
by the same DB test harness deferred to a follow-up commit.

Run from the backend root with:
    pytest tests/test_agent_tools.py -v
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.services.agents.tools import (
    RCA_AGENT_TOOL_NAMES,
    TOOL_REGISTRY,
    get_industry_benchmark,
    get_tool_definitions,
    get_tool_handler,
)


# ─── Registry shape ────────────────────────────────────────────────────


def test_registry_contains_all_nine_rca_tools() -> None:
    expected = {
        "find_similar_incidents",
        "find_related_observations",
        "find_related_near_misses",
        "get_equipment_history",
        "get_training_records",
        "get_active_permits_at_time",
        "search_documents_reviewed",
        "check_recent_changes",
        "get_industry_benchmark",
    }
    assert expected.issubset(set(TOOL_REGISTRY.keys()))


def test_rca_tool_names_matches_registry() -> None:
    # Every entry in RCA_AGENT_TOOL_NAMES MUST be in the registry — if
    # this fails, the agent will fail at config time with KeyError.
    for name in RCA_AGENT_TOOL_NAMES:
        assert name in TOOL_REGISTRY, f"RCA agent references unregistered tool {name!r}"


def test_get_tool_definitions_preserves_order() -> None:
    requested = ["find_similar_incidents", "get_equipment_history", "get_industry_benchmark"]
    defs = get_tool_definitions(requested)
    assert [d["name"] for d in defs] == requested


def test_get_tool_definitions_raises_on_unknown() -> None:
    with pytest.raises(KeyError):
        get_tool_definitions(["this_tool_does_not_exist"])


def test_get_tool_handler_raises_on_unknown() -> None:
    with pytest.raises(KeyError):
        get_tool_handler("this_tool_does_not_exist")


# ─── Definition shape (per tool) ───────────────────────────────────────


@pytest.mark.parametrize("tool_name", list(TOOL_REGISTRY.keys()))
def test_each_tool_definition_has_required_keys(tool_name: str) -> None:
    definition, _handler = TOOL_REGISTRY[tool_name]
    assert "name" in definition
    assert definition["name"] == tool_name, "Tool name in DEFINITION must match registry key"
    assert "description" in definition
    assert isinstance(definition["description"], str)
    assert len(definition["description"]) > 50, (
        f"{tool_name}: description should be substantive (>50 chars) so the "
        "model can pick the right tool"
    )
    assert "input_schema" in definition


@pytest.mark.parametrize("tool_name", list(TOOL_REGISTRY.keys()))
def test_each_input_schema_is_valid_jsonschema_object(tool_name: str) -> None:
    definition, _handler = TOOL_REGISTRY[tool_name]
    schema = definition["input_schema"]
    assert schema["type"] == "object", f"{tool_name}: input_schema.type must be 'object'"
    assert "properties" in schema, f"{tool_name}: input_schema must define properties"
    assert "required" in schema, f"{tool_name}: input_schema must define required (may be empty)"
    # Required must be a subset of properties
    if schema["required"]:
        for req in schema["required"]:
            assert req in schema["properties"], (
                f"{tool_name}: required field {req!r} is not in properties"
            )


@pytest.mark.parametrize("tool_name", list(TOOL_REGISTRY.keys()))
def test_each_handler_is_async_callable(tool_name: str) -> None:
    _definition, handler = TOOL_REGISTRY[tool_name]
    assert callable(handler), f"{tool_name}: handler is not callable"
    assert inspect.iscoroutinefunction(handler), (
        f"{tool_name}: handler must be async (the runtime awaits it)"
    )


# ─── get_industry_benchmark (pure function — full behaviour test) ──────


@pytest.mark.asyncio
async def test_industry_benchmark_returns_curated_patterns_for_lti_cement() -> None:
    result = await get_industry_benchmark.handle(
        {"incidentType": "LTI", "industryContext": "CEMENT"},
        db=None,
        source_record_id=None,
        source_module=None,
    )
    assert result["incidentType"] == "LTI"
    assert result["industryContext"] == "CEMENT"
    assert len(result["patterns"]) >= 1
    # Each pattern must have the documented shape
    for p in result["patterns"]:
        assert "pattern" in p
        assert "commonRootCauses" in p
        assert "contributingFactors" in p
        assert "source" in p  # citation is mandatory — no source-less claims


@pytest.mark.asyncio
async def test_industry_benchmark_falls_back_to_general() -> None:
    # PROCESS_SAFETY only has a GENERAL entry, not a CEMENT-specific one.
    result = await get_industry_benchmark.handle(
        {"incidentType": "PROCESS_SAFETY", "industryContext": "CEMENT"},
        db=None,
        source_record_id=None,
        source_module=None,
    )
    assert len(result["patterns"]) >= 1


@pytest.mark.asyncio
async def test_industry_benchmark_returns_empty_with_note_when_no_match() -> None:
    result = await get_industry_benchmark.handle(
        {"incidentType": "FIRST_AID", "industryContext": "CEMENT"},
        db=None,
        source_record_id=None,
        source_module=None,
    )
    # No curated patterns for FIRST_AID — should return empty with note
    assert result["patterns"] == []
    assert "_note" in result


@pytest.mark.asyncio
async def test_industry_benchmark_includes_disclaimer_on_match() -> None:
    # When patterns ARE returned, the agent needs to see the
    # "hand-curated, not site-specific" disclaimer.
    result = await get_industry_benchmark.handle(
        {"incidentType": "LTI", "industryContext": "CEMENT"},
        db=None,
        source_record_id=None,
        source_module=None,
    )
    assert "_disclaimer" in result
