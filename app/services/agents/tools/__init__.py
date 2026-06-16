"""Agent tool registry.

Each tool is a Python module exposing two top-level names:
  • DEFINITION — a dict matching Anthropic's tool input_schema shape:
      { "name": str, "description": str, "input_schema": dict }
  • handle    — async def handle(input: dict, *, db: AsyncSession,
                                 source_record_id: str, source_module: str)
                Returns a JSON-serialisable result. May raise; the agent
                runtime catches and feeds the error back to the model.

Registration is explicit (not magic auto-discovery) so the registry stays
predictable when the tool list is reviewed for audit. Add new tools to
TOOL_REGISTRY below; the agent_service picks them up from Agent.availableTools.

Commit 2 ships the 9 RCA-specific tools. Tool naming convention: action
prefixes (find_*, get_*, search_*, check_*) keep the model's tool
selection legible in its reasoning.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.services.agents.tools import (
    check_crew_training_currency,
    check_recent_changes,
    echo_incident_summary,
    find_concurrent_permits,
    find_recent_findings_in_area,
    find_related_near_misses,
    find_related_observations,
    find_similar_incidents,
    find_similar_past_incidents_for_permit,
    get_active_permits_at_time,
    get_equipment_history,
    get_industry_benchmark,
    get_training_records,
    search_documents_reviewed,
)


# A tool handler signature: receives the model's input dict + agent
# runtime context, returns a JSON-serialisable result.
ToolHandler = Callable[..., Awaitable[Any]]


# Registry: tool name → (Anthropic-format definition, handler callable).
# Order in this dict is the order the model sees tools in its prompt —
# leading tools have slightly stronger affordance. We list discovery
# tools (find_*) before analysis tools (get_*) before reference tools
# (search_documents_reviewed, get_industry_benchmark, check_recent_changes).
TOOL_REGISTRY: dict[str, tuple[dict[str, Any], ToolHandler]] = {
    find_similar_incidents.DEFINITION["name"]: (
        find_similar_incidents.DEFINITION,
        find_similar_incidents.handle,
    ),
    find_related_observations.DEFINITION["name"]: (
        find_related_observations.DEFINITION,
        find_related_observations.handle,
    ),
    find_related_near_misses.DEFINITION["name"]: (
        find_related_near_misses.DEFINITION,
        find_related_near_misses.handle,
    ),
    get_equipment_history.DEFINITION["name"]: (
        get_equipment_history.DEFINITION,
        get_equipment_history.handle,
    ),
    get_training_records.DEFINITION["name"]: (
        get_training_records.DEFINITION,
        get_training_records.handle,
    ),
    get_active_permits_at_time.DEFINITION["name"]: (
        get_active_permits_at_time.DEFINITION,
        get_active_permits_at_time.handle,
    ),
    search_documents_reviewed.DEFINITION["name"]: (
        search_documents_reviewed.DEFINITION,
        search_documents_reviewed.handle,
    ),
    check_recent_changes.DEFINITION["name"]: (
        check_recent_changes.DEFINITION,
        check_recent_changes.handle,
    ),
    get_industry_benchmark.DEFINITION["name"]: (
        get_industry_benchmark.DEFINITION,
        get_industry_benchmark.handle,
    ),
    # ─── PermitRiskReviewerAgent tools ─────────────────────────────────
    find_concurrent_permits.DEFINITION["name"]: (
        find_concurrent_permits.DEFINITION,
        find_concurrent_permits.handle,
    ),
    find_similar_past_incidents_for_permit.DEFINITION["name"]: (
        find_similar_past_incidents_for_permit.DEFINITION,
        find_similar_past_incidents_for_permit.handle,
    ),
    check_crew_training_currency.DEFINITION["name"]: (
        check_crew_training_currency.DEFINITION,
        check_crew_training_currency.handle,
    ),
    find_recent_findings_in_area.DEFINITION["name"]: (
        find_recent_findings_in_area.DEFINITION,
        find_recent_findings_in_area.handle,
    ),
    # Smoke-test tool from Commit 1. Kept registered so the test harness
    # can exercise the agent runtime against a minimal known-good tool.
    echo_incident_summary.DEFINITION["name"]: (
        echo_incident_summary.DEFINITION,
        echo_incident_summary.handle,
    ),
}


def get_tool_definitions(tool_names: list[str]) -> list[dict[str, Any]]:
    """Return Anthropic-format tool definitions for the given names, in
    the order requested. Unknown tool names raise KeyError — surface this
    as an agent misconfiguration rather than silently swallowing.
    """
    return [TOOL_REGISTRY[name][0] for name in tool_names]


def get_tool_handler(tool_name: str) -> ToolHandler:
    """Look up a tool handler by name. KeyError if not registered."""
    return TOOL_REGISTRY[tool_name][1]


# Canonical RCA agent tool list. Used by the seeding scripts in Commit 3
# to populate Agent.availableTools for RCA_ASSISTANT. Living here (not
# the seeder) so adding a tool means touching one file.
RCA_AGENT_TOOL_NAMES: list[str] = [
    "find_similar_incidents",
    "find_related_observations",
    "find_related_near_misses",
    "get_equipment_history",
    "get_training_records",
    "get_active_permits_at_time",
    "search_documents_reviewed",
    "check_recent_changes",
    "get_industry_benchmark",
]


# Canonical PermitRiskReviewer tool list. Used by the agent seeding
# script to populate Agent.availableTools for PERMIT_RISK_REVIEWER.
# A Python test guards against drift between this list and the registry.
PERMIT_RISK_REVIEWER_TOOL_NAMES: list[str] = [
    "find_concurrent_permits",
    "find_similar_past_incidents_for_permit",
    "check_crew_training_currency",
    "find_recent_findings_in_area",
    "get_training_records",
    "get_equipment_history",
]


__all__ = [
    "TOOL_REGISTRY",
    "ToolHandler",
    "get_tool_definitions",
    "get_tool_handler",
    "RCA_AGENT_TOOL_NAMES",
    "PERMIT_RISK_REVIEWER_TOOL_NAMES",
]
