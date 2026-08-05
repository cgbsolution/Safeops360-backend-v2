"""Request-scoped actor context for the unified audit trail (P1-1).

A contextvar carries WHO is acting (set by get_current_user for human requests,
or by a SystemActor for jobs) so the ORM-level audit capture can attribute every
change without threading the actor through every service call.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass
class AuditActor:
    actor_id: str | None = None
    actor_type: str = "SYSTEM"  # USER | SYSTEM | AGENT | IMPORT
    actor_ip: str | None = None
    correlation_id: str | None = None
    reason: str | None = None  # from the x-audit-reason header, for sensitive actions


_ctx: contextvars.ContextVar[AuditActor] = contextvars.ContextVar(
    "audit_actor", default=AuditActor()
)


def set_actor(actor: AuditActor) -> contextvars.Token:
    return _ctx.set(actor)


def get_actor() -> AuditActor:
    return _ctx.get()


def reset_actor(token: contextvars.Token) -> None:
    _ctx.reset(token)


def set_system_actor(job_name: str) -> None:
    """For background jobs — attributes audit entries to a named system process."""
    _ctx.set(AuditActor(actor_id=f"SYSTEM:{job_name}", actor_type="SYSTEM"))
