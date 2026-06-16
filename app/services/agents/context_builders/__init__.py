"""Module-specific context builders for the agent platform.

Each module (INCIDENT, OBSERVATION, NEAR_MISS, ...) has its own builder
that knows how to assemble the rich record payload an agent needs to
analyse. Builders live here so the agent_service stays generic.

To register a new builder:
  1. Drop app/services/agents/context_builders/<module>.py exporting
     `build_context(db, record_id) -> dict`.
  2. Add it to CONTEXT_BUILDERS below.
  3. The agent_service dispatch picks it up.

If a module has no registered builder, agent_service falls back to a
minimal record-fetch stub. That keeps invocations for less-developed
modules running, but the agent will have thin context.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.agents.context_builders import capa as capa_builder
from app.services.agents.context_builders import hira as hira_builder
from app.services.agents.context_builders import incident as incident_builder
from app.services.agents.context_builders import permit as permit_builder
from app.services.agents.context_builders import triage as triage_builder


ContextBuilder = Callable[[AsyncSession, str], Awaitable[dict[str, Any]]]


# module code → builder function
CONTEXT_BUILDERS: dict[str, ContextBuilder] = {
    "INCIDENT": incident_builder.build_context,
    "PTW": permit_builder.build_context,
    "OBSERVATION": triage_builder.build_observation_context,
    "NEAR_MISS": triage_builder.build_near_miss_context,
    "CAPA": capa_builder.build_context,
    "HIRA": hira_builder.build_context,
}


__all__ = ["CONTEXT_BUILDERS", "ContextBuilder"]
