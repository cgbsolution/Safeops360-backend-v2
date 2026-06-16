"""Placeholder tool used to smoke-test the agent runtime end-to-end
in Commit 1. Reads the incident pointed to by source_record_id and
returns a small summary dict. Real RCA tools (find_similar_incidents,
get_equipment_history, etc.) replace the placeholder usage in Commit 2.

This tool reads INCIDENT only — it does not match the agent runtime's
broader "source_module" parameter. If called with a non-INCIDENT source
the handler raises so the loop surfaces the misconfiguration rather
than returning a misleading empty result.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident


DEFINITION: dict[str, Any] = {
    "name": "echo_incident_summary",
    "description": (
        "Return a small structured summary of the incident the agent is "
        "currently analysing. Used to confirm the agent runtime can dispatch "
        "tools end-to-end. Returns the incident number, type, severity, "
        "occurredAt, and the first 200 characters of the description."
    ),
    # No input parameters — the incident is implicit from the agent's
    # invocation context. Anthropic still requires a JSON Schema; an
    # empty-properties object with no required keys is the canonical form.
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


async def handle(
    input: dict[str, Any],  # noqa: A002 — Anthropic's API uses "input" so keeping the name
    *,
    db: AsyncSession,
    source_record_id: str,
    source_module: str,
) -> dict[str, Any]:
    """Look up the incident and return a summary dict.

    Raises ValueError if called with the wrong source_module or if the
    record doesn't exist. The agent_service's tool dispatcher catches
    these and feeds them to the model as a tool error.
    """
    if source_module != "INCIDENT":
        raise ValueError(
            f"echo_incident_summary only supports INCIDENT records, got {source_module}"
        )

    stmt = select(Incident).where(Incident.id == source_record_id)
    incident = (await db.execute(stmt)).scalar_one_or_none()
    if incident is None:
        raise ValueError(f"Incident {source_record_id} not found")

    # Match field names to what the brief assumes (incidentNumber) where
    # they differ from the actual schema (number) — keeps the model's
    # input/output language consistent across tools.
    description = getattr(incident, "initialDescription", None) or getattr(
        incident, "description", ""
    )
    return {
        "incidentNumber": incident.number,
        "incidentType": str(incident.type.value if hasattr(incident.type, "value") else incident.type),
        "severity": getattr(incident, "severity", None),
        "occurredAt": (
            getattr(incident, "occurredAt", None) or incident.date
        ).isoformat()
        if (getattr(incident, "occurredAt", None) or incident.date)
        else None,
        "descriptionPreview": (description or "")[:200],
    }
