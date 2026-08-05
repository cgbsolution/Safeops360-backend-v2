"""Unified audit-trail engine (P1-1).

Capture is at the ORM layer (before_flush/after_flush) so EVERY write to an
audited entity is logged automatically with field-level before/after — no
per-module audit calls (which produced the platform's previous fragmented state).

Flow:
  1. before_flush  — compute before/after diffs for dirty/deleted audited objects
                     (attribute history is intact here) and stash into session.info.
  2. after_flush   — new objects now have ids → stash CREATE events.
  3. drain_audit() — called by get_db() after the request commits; writes the
                     AuditLog rows with a per-entity SHA-256 hash chain, using a
                     SEPARATE session so audit writes never block/rollback the
                     business transaction.

verify_chain() walks a chain and reports any broken link (tamper-evidence).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import event, func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

# Entity classes whose writes are audited. Registered at startup.
_AUDITED: set[type] = set()
# State-ish columns whose change is recorded as STATE_TRANSITION (vs plain UPDATE).
_STATE_COLS = {"state", "status", "lifecycleState", "workflowState", "stage"}
# Columns never worth diffing (noise).
_SKIP_COLS = {"updatedAt", "createdAt", "version"}


def register_audited(*models: type) -> None:
    _AUDITED.update(models)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts_canon(dt: datetime) -> datetime:
    """Canonical timestamp: naive UTC, millisecond precision. Stable across the
    Postgres TIMESTAMP(3) round-trip so the hash recomputes identically on read."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=(dt.microsecond // 1000) * 1000)


def _json_safe(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, dict)):
        return v
    return str(v)


def _entity_code(obj: Any) -> str | None:
    for attr in ("riskCode", "permitNumber", "permitCode", "incidentNumber", "auditNumber",
                 "capaNumber", "eventCode", "equipmentCode", "drillCode", "code"):
        v = getattr(obj, attr, None)
        if v:
            return str(v)
    return None


def _plant_of(obj: Any) -> str | None:
    return getattr(obj, "plantId", None) or getattr(obj, "siteId", None)


def _history_diff(obj: Any) -> tuple[dict, dict, list[str]]:
    """before/after for a dirty object, from attribute history (call in before_flush)."""
    state = sa_inspect(obj)
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    changed: list[str] = []
    for attr in state.mapper.column_attrs:
        key = attr.key
        if key in _SKIP_COLS:
            continue
        hist = state.attrs[key].history
        if hist.has_changes():
            before[key] = _json_safe(hist.deleted[0]) if hist.deleted else None
            after[key] = _json_safe(hist.added[0]) if hist.added else None
            changed.append(key)
    return before, after, changed


def _all_values(obj: Any) -> dict[str, Any]:
    state = sa_inspect(obj)
    return {
        attr.key: _json_safe(getattr(obj, attr.key))
        for attr in state.mapper.column_attrs
        if attr.key not in _SKIP_COLS
    }


def _update_action(before: dict, after: dict, changed: list[str]) -> str:
    if "isDeleted" in changed:
        return "SOFT_DELETE" if after.get("isDeleted") else "RESTORE"
    if any(c in _STATE_COLS for c in changed):
        return "STATE_TRANSITION"
    return "UPDATE"


def _mk_event(obj: Any, action: str, before: dict, after: dict, changed: list[str]) -> dict[str, Any]:
    return {
        "entityType": type(obj).__name__,
        "entityId": getattr(obj, "id", None),
        "entityCode": _entity_code(obj),
        "plantId": _plant_of(obj),
        "action": action,
        "before": before or None,
        "after": after or None,
        "changedFields": changed or None,
    }


# ── ORM capture ──────────────────────────────────────────────────────────────
@event.listens_for(Session, "before_flush")
def _audit_before_flush(session: Session, flush_context, instances) -> None:  # noqa: ANN001
    pending = session.info.setdefault("_audit_pending", [])
    for obj in session.dirty:
        if type(obj) in _AUDITED and session.is_modified(obj, include_collections=False):
            before, after, changed = _history_diff(obj)
            if changed:
                pending.append(_mk_event(obj, _update_action(before, after, changed), before, after, changed))
    for obj in session.deleted:  # only fires for audited-but-not-governed entities
        if type(obj) in _AUDITED:
            pending.append(_mk_event(obj, "DELETE", _all_values(obj), {}, []))
    new_objs = session.info.setdefault("_audit_new", [])
    new_objs.extend(o for o in session.new if type(o) in _AUDITED)


@event.listens_for(Session, "after_flush")
def _audit_after_flush(session: Session, flush_context) -> None:  # noqa: ANN001
    new_objs = session.info.pop("_audit_new", [])
    if not new_objs:
        return
    pending = session.info.setdefault("_audit_pending", [])
    for obj in new_objs:
        after = _all_values(obj)
        pending.append(_mk_event(obj, "CREATE", {}, after, list(after.keys())))


# ── Hash chain + write ───────────────────────────────────────────────────────
def _compute_hash(seq: int, ev: dict, ts: str, prev_hash: str) -> str:
    payload = "||".join([
        str(seq), ev["entityType"], str(ev["entityId"]), ev["action"], ts,
        json.dumps(ev.get("before"), sort_keys=True, default=str),
        json.dumps(ev.get("after"), sort_keys=True, default=str),
        prev_hash or "",
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def write_entries(adb: AsyncSession, events: list[dict], actor) -> int:  # noqa: ANN001
    """Write a batch of audit events with the per-entity hash chain. Uses its own
    session; the caller commits."""
    from app.models.audit_log import AuditLog

    written = 0
    for ev in events:
        if not ev.get("entityId"):
            continue
        last = (
            await adb.execute(
                select(AuditLog.sequenceNo, AuditLog.entryHash)
                .where(AuditLog.entityType == ev["entityType"])
                .where(AuditLog.entityId == ev["entityId"])
                .order_by(AuditLog.sequenceNo.desc())
                .limit(1)
            )
        ).first()
        seq = (last[0] + 1) if last else 1
        prev_hash = last[1] if last else None
        ts = _ts_canon(_now())  # naive-UTC-ms; stored AND hashed identically
        entry_hash = _compute_hash(seq, ev, ts.isoformat(), prev_hash or "")
        adb.add(AuditLog(
            sequenceNo=seq, plantId=ev.get("plantId"),
            entityType=ev["entityType"], entityId=ev["entityId"], entityCode=ev.get("entityCode"),
            action=ev["action"], actorId=actor.actor_id, actorType=actor.actor_type, actorIp=actor.actor_ip,
            timestamp=ts, before=ev.get("before"), after=ev.get("after"), changedFields=ev.get("changedFields"),
            reason=actor.reason or (ev.get("after") or {}).get("deletionReason"),
            correlationId=actor.correlation_id, previousEntryHash=prev_hash, entryHash=entry_hash,
        ))
        # flush per entry so the next event's sequence query sees this row —
        # otherwise two events for the SAME entity in one batch both get seq 1.
        await adb.flush()
        written += 1
    return written


async def drain_audit(session) -> None:  # noqa: ANN001
    """Drain a request's captured audit events after its business commit. Opens a
    fresh session so an audit-write failure never rolls back the business change.
    Best-effort: logs on failure, never raises into the request path."""
    pending = session.info.pop("_audit_pending", None)
    session.info.pop("_audit_new", None)
    if not pending:
        return
    from app.core.audit_context import get_actor
    from app.core.db import AsyncSessionLocal

    actor = get_actor()
    try:
        async with AsyncSessionLocal() as adb:
            await write_entries(adb, pending, actor)
            await adb.commit()
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("safeops360.audit").warning("audit drain failed: %s", e)


async def record_event(
    db: AsyncSession, *, entity_type: str, entity_id: str, action: str,
    entity_code: str | None = None, plant_id: str | None = None,
    before: dict | None = None, after: dict | None = None, reason: str | None = None,
) -> None:
    """Explicit audit entry for actions ORM capture can't see — READ_SENSITIVE,
    EXPORT, SIGN_OFF, LICENCE_EVENT, LOGIN, ACCESS_DENIED. Writes immediately on
    the given session (committed by the caller)."""
    from app.core.audit_context import get_actor

    actor = get_actor()
    if reason:
        actor.reason = reason
    ev = {
        "entityType": entity_type, "entityId": entity_id, "entityCode": entity_code,
        "plantId": plant_id, "action": action, "before": before, "after": after,
        "changedFields": list((after or {}).keys()) or None,
    }
    await write_entries(db, [ev], actor)


# ── Tamper-evidence verifier ─────────────────────────────────────────────────
async def verify_chain(db: AsyncSession, entity_type: str, entity_id: str) -> dict[str, Any]:
    """Walk an entity's hash chain; report the first broken link (and every entry
    after it is suspect). Recomputes each entryHash from stored fields."""
    from app.models.audit_log import AuditLog

    rows = (
        await db.execute(
            select(AuditLog).where(AuditLog.entityType == entity_type).where(AuditLog.entityId == entity_id)
            .order_by(AuditLog.sequenceNo)
            .execution_options(include_deleted=True)
        )
    ).scalars().all()
    prev_hash = ""
    broken_at: int | None = None
    for r in rows:
        ev = {"entityType": r.entityType, "entityId": r.entityId, "action": r.action,
              "before": r.before, "after": r.after}
        expected = _compute_hash(r.sequenceNo, ev, _ts_canon(r.timestamp).isoformat(), prev_hash)
        link_ok = (r.previousEntryHash or "") == (prev_hash or "")
        if expected != r.entryHash or not link_ok:
            broken_at = r.sequenceNo
            break
        prev_hash = r.entryHash
    return {
        "entityType": entity_type, "entityId": entity_id, "entries": len(rows),
        "intact": broken_at is None, "brokenAtSequence": broken_at,
    }
