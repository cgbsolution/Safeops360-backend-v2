"""Domain-event bus (outbox writer).

``emit()`` inserts a DomainEvent row into the CALLER'S session so the event
commits or rolls back atomically with the entity write it describes — the
Postgres equivalent of the spec's "wrap emit + entity write in the same
transaction". Emit from the SERVICE layer (or the router right where the
state change happens), never from generic controllers, so every code path is
covered.

Consumption happens elsewhere: the ``alerts_impact_resolver`` scheduler job
reads ``processedAt IS NULL`` rows and runs the impact-rule registry
(app/services/alerts/). Nothing here blocks or notifies — writing the row is
the whole contract.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import DomainEvent

# Canonical event types (keep in sync with app/services/alerts/rules/)
RCA_COMPLETED = "rca.completed"
RCA_REOPENED = "rca.reopened"
PTW_MODIFIED = "ptw.modified"
PTW_SUSPENDED = "ptw.suspended"
PTW_RESUMED = "ptw.resumed"
PTW_REJECTED = "ptw.rejected"
PTW_EXPIRING = "ptw.expiring"
PTW_EXPIRED = "ptw.expired"
CAPA_CREATED = "capa.created"
CAPA_OVERDUE = "capa.overdue"
CAPA_CLOSED = "capa.closed"
OBSERVATION_TRIAGED_HIGH = "observation.triaged_high"
HIRA_CONTROL_FAILED = "hira.control_failed"
CAPTURE_SUBMITTED = "capture.submitted"


def emit(
    db: AsyncSession,
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    entity_ref: str | None = None,
    site_id: str | None = None,
    actor_id: str | None = None,
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> DomainEvent:
    """Stage a domain event in the caller's transaction. Synchronous on
    purpose — it is just a session.add(); the caller's commit publishes it."""
    ev = DomainEvent(
        eventType=event_type,
        entityType=entity_type,
        entityId=entity_id,
        entityRef=entity_ref,
        siteId=site_id,
        actorId=actor_id,
        payload=payload or {},
        correlationId=correlation_id,
    )
    db.add(ev)
    return ev


__all__ = [
    "emit",
    "RCA_COMPLETED", "RCA_REOPENED",
    "PTW_MODIFIED", "PTW_SUSPENDED", "PTW_RESUMED", "PTW_REJECTED", "PTW_EXPIRING", "PTW_EXPIRED",
    "CAPA_CREATED", "CAPA_OVERDUE", "CAPA_CLOSED",
    "OBSERVATION_TRIAGED_HIGH", "HIRA_CONTROL_FAILED", "CAPTURE_SUBMITTED",
]
