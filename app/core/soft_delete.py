"""Platform-wide soft-delete integrity (P1-3).

A *governed* entity is one that carries an audit trail, is referenced by other
records, or is subject to regulatory retention. Governed entities are NEVER
hard-deleted — a `before_flush` ORM guard raises if any code path attempts
`session.delete()` on one. The only permitted deletion path is `soft_delete()`,
which sets `isDeleted`/`deletedAt`/`deletedBy`/`deletionReason` (an UPDATE the
guard ignores). `restore()` reverses it within a window.

Enforcement is at the ORM/data layer so it cannot be bypassed per-module. A
model is added to the governed registry ONLY once its delete endpoints have been
converted to `soft_delete()` — so registering a model never breaks a live flow.

The audit-trail middleware (P1-1) records SOFT_DELETE / RESTORE automatically.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

# Models whose hard-delete is blocked. Keyed by class; populated by
# register_governed() at import time from the models that opt in.
_GOVERNED: set[type] = set()
# AuditLog + AuditReport are append-only: NO delete at all (not even soft).
_APPEND_ONLY: set[str] = {"AuditLog", "AuditReport"}

# How long after a soft-delete a restore is permitted.
RESTORE_WINDOW_DAYS = 30


class HardDeleteBlocked(Exception):
    """Raised when code attempts to hard-delete a governed entity."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def register_governed(*models: type) -> None:
    """Register model classes as governed (hard-delete blocked). Idempotent."""
    _GOVERNED.update(models)


def is_governed(obj: Any) -> bool:
    return type(obj) in _GOVERNED


def soft_delete(entity: Any, actor_id: str | None, reason: str) -> None:
    """The ONLY permitted deletion path for a governed entity. Sets the
    soft-delete columns; the caller commits. Requires a substantive reason."""
    name = type(entity).__name__
    if name in _APPEND_ONLY:
        raise HardDeleteBlocked(f"{name} is append-only and cannot be deleted.")
    if not reason or len(reason.strip()) < 10:
        raise ValueError("Deletion reason required (min 10 chars) for a governed entity.")
    entity.isDeleted = True
    entity.deletedAt = _now()
    entity.deletedBy = actor_id
    entity.deletionReason = reason.strip()


def restore(entity: Any, actor_id: str | None) -> None:
    """Reverse a soft-delete within RESTORE_WINDOW_DAYS. Caller commits."""
    if not getattr(entity, "isDeleted", False):
        raise ValueError("Entity is not deleted.")
    deleted_at = getattr(entity, "deletedAt", None)
    if deleted_at is not None:
        da = deleted_at if deleted_at.tzinfo else deleted_at.replace(tzinfo=timezone.utc)
        if _now() - da > timedelta(days=RESTORE_WINDOW_DAYS):
            raise PermissionError(
                f"Restore window of {RESTORE_WINDOW_DAYS} days has passed; record is archived."
            )
    entity.isDeleted = False
    entity.deletedAt = None
    entity.deletedBy = None
    entity.deletionReason = None
    # leave a marker so the audit trail/restore is attributable
    if hasattr(entity, "updatedBy"):
        entity.updatedBy = actor_id


def register_default_governed() -> None:
    """Register the governed entities whose delete paths are soft-delete-safe.
    Called once at app startup (after models import). Only models whose hard-delete
    endpoints have been converted to soft_delete() are registered here — adding a
    model never breaks a live flow.
    """
    from app.models.alerts import Alert
    from app.models.audit_compliance import ComplianceAudit
    from app.models.capa import Capa
    from app.models.capture import CaptureSubmission, RcaFieldRequest
    from app.models.fire_safety import FireDrill, FireEmergencyPlan, FireEquipment
    from app.models.incident import Incident
    from app.models.permit import Permit
    from app.models.rca import RootCauseAnalysis

    register_governed(
        Incident, Capa, Permit, ComplianceAudit, FireEquipment, FireEmergencyPlan, FireDrill,
        RootCauseAnalysis,
        CaptureSubmission, RcaFieldRequest, Alert,
    )


@event.listens_for(Session, "before_flush")
def _prevent_governed_hard_delete(session: Session, flush_context, instances) -> None:  # noqa: ANN001
    """ORM-level guard: a governed entity (or any append-only entity) in the
    pending-delete set aborts the flush. This is the un-bypassable boundary —
    no router, service, or seed can hard-delete a governed record."""
    for obj in session.deleted:
        name = type(obj).__name__
        if name in _APPEND_ONLY:
            raise HardDeleteBlocked(
                f"{name} is append-only (id={getattr(obj, 'id', '?')}); it can never be deleted."
            )
        if type(obj) in _GOVERNED:
            raise HardDeleteBlocked(
                f"Hard delete blocked on {name} id={getattr(obj, 'id', '?')}. "
                f"Use app.core.soft_delete.soft_delete(entity, actor, reason)."
            )


@event.listens_for(Session, "do_orm_execute")
def _filter_soft_deleted(execute_state) -> None:  # noqa: ANN001
    """Invisible soft-delete: every ORM SELECT on a governed entity auto-excludes
    isDeleted=True rows. An admin/audit view bypasses it with
    `.execution_options(include_deleted=True)`. This is the async-SQLAlchemy
    equivalent of a soft-delete query subclass — applied globally, no per-query edit."""
    if (
        not execute_state.is_select
        or execute_state.is_column_load
        or execute_state.is_relationship_load
        or execute_state.execution_options.get("include_deleted", False)
    ):
        return
    for model in _GOVERNED:
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(model, model.isDeleted == False, include_aliases=True)  # noqa: E712
        )
