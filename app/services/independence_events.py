"""Recording what the independence guard decided, so enforcement is provable.

The guard was already correct and already shared. What it had no way to do was
**leave a trace**: `create_audit` raises `ValueError` and the router's
transaction rolls back, so anything written inside it disappears along with the
rejected audit; preflight returned a verdict and wrote nothing at all.

Two consequences that matter here:

  1. `record_event` runs on **its own session**. Writing to the caller's session
     would put the evidence inside the transaction the block is about to roll
     back — the one case where the record matters most is exactly the case where
     it would be lost.
  2. It never raises. An audit trail that can break the request it is recording
     is worse than no audit trail, and the guard's own behaviour must not depend
     on whether the log succeeded. Same discipline as `drain_audit`.

Preflight fires on every render of the team-assignment step, so identical
verdicts are **deduplicated** inside a short window. Without it a user tabbing
through a form would write a hundred rows saying the same thing, and the
register would be unreadable — a log nobody can read is not evidence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assurance import IndependenceEvent

# Two verdicts for the same person on the same engagement inside this window are
# one event. Long enough to absorb a form being re-rendered, short enough that a
# genuine second attempt an hour later is its own row.
DEDUPE_WINDOW = timedelta(minutes=30)

OUTCOMES = ("BLOCKED", "WARNED", "WAIVED", "CLEARED")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def outcome_for(verdict: Any) -> str:
    """The one mapping from a verdict to a recordable outcome.

    Order matters: a waived block is WAIVED, not BLOCKED — the governance
    decision is the headline, and the underlying conflict is still on the row in
    `rule` / `source` / `conflictDetail`.
    """
    if getattr(verdict, "waived", False):
        return "WAIVED"
    if verdict.blocking:
        return "BLOCKED"
    if verdict.warnings:
        return "WARNED"
    return "CLEARED"


def event_fields_for(verdict: Any) -> dict[str, Any]:
    """Pure: flatten a verdict into `record_event` keyword arguments.

    Snake_case deliberately — these keys are spread straight into
    `record_event(**fields)`, and returning the column names instead would look
    right and fail at the call site.
    """
    lead = (verdict.blocking or verdict.warnings or [None])[0]
    return {
        "outcome": outcome_for(verdict),
        "rule": lead.rule if lead else None,
        "source": lead.source if lead else None,
        "reason": lead.reason if lead else "",
        "conflict_detail": {
            "conflicts": [c.as_dict() for c in verdict.conflicts],
            "blockingCount": len(verdict.blocking),
            "warningCount": len(verdict.warnings),
        },
        "waiver_id": getattr(verdict, "waiverId", None),
    }


async def _recent_duplicate(
    db: AsyncSession, *, subject_user_id: str, engagement_id: str | None,
    engagement_kind: str, outcome: str, rule: str | None, since: datetime,
) -> bool:
    q = select(IndependenceEvent.id).where(
        IndependenceEvent.subjectUserId == subject_user_id,
        IndependenceEvent.engagementKind == engagement_kind,
        IndependenceEvent.outcome == outcome,
        IndependenceEvent.occurredAt >= since,
    )
    q = (
        q.where(IndependenceEvent.engagementId == engagement_id)
        if engagement_id
        else q.where(IndependenceEvent.engagementId.is_(None))
    )
    if rule:
        q = q.where(IndependenceEvent.rule == rule)
    return (await db.execute(q.limit(1))).first() is not None


async def record_event(
    *,
    subject_user_id: str,
    engagement_kind: str,
    outcome: str,
    origin: str,
    attempted_by_user_id: str | None = None,
    engagement_id: str | None = None,
    engagement_code: str | None = None,
    site_id: str | None = None,
    rule: str | None = None,
    source: str | None = None,
    reason: str = "",
    conflict_detail: dict[str, Any] | None = None,
    waiver_id: str | None = None,
    dedupe: bool = True,
    session: AsyncSession | None = None,
) -> str | None:
    """Append one event. Returns its id, or None if deduplicated or it failed.

    `session` is for the seeder and for tests, which want the write inside their
    own transaction so it can be rolled back. Production callers pass nothing and
    get an independent session, which is the whole point.
    """
    if outcome not in OUTCOMES or not subject_user_id:
        return None

    async def _write(db: AsyncSession) -> str | None:
        if dedupe and await _recent_duplicate(
            db,
            subject_user_id=subject_user_id,
            engagement_id=engagement_id,
            engagement_kind=engagement_kind,
            outcome=outcome,
            rule=rule,
            since=_utcnow() - DEDUPE_WINDOW,
        ):
            return None
        row = IndependenceEvent(
            occurredAt=_utcnow(),
            attemptedByUserId=attempted_by_user_id,
            subjectUserId=subject_user_id,
            engagementKind=engagement_kind,
            engagementId=engagement_id,
            engagementCode=engagement_code,
            siteId=site_id,
            outcome=outcome,
            rule=rule,
            source=source,
            reason=reason or "",
            conflictDetail=conflict_detail,
            waiverId=waiver_id,
            origin=origin,
        )
        db.add(row)
        await db.flush()
        return row.id

    if session is not None:
        return await _write(session)

    try:
        # Imported here so the module stays importable in the pure-unit suite,
        # which has no engine.
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as own:
            rid = await _write(own)
            await own.commit()
            return rid
    except Exception:  # noqa: BLE001 — evidence must never break the guard
        return None


async def record_verdicts(
    *,
    verdicts: dict[str, Any],
    engagement_kind: str,
    origin: str,
    attempted_by_user_id: str | None,
    engagement_id: str | None = None,
    engagement_code: str | None = None,
    site_id: str | None = None,
    include_cleared: bool = False,
    session: AsyncSession | None = None,
) -> int:
    """Record a batch of verdicts (the preflight / team-assignment shape).

    `include_cleared` defaults False: a clean candidate is the overwhelming
    majority of verdicts and recording every one would bury the blocks that are
    the actual evidence. The register's claim is "here is every time the guard
    had something to say", not "here is every question anyone asked it".
    """
    n = 0
    for uid, verdict in (verdicts or {}).items():
        fields = event_fields_for(verdict)
        if fields["outcome"] == "CLEARED" and not include_cleared:
            continue
        rid = await record_event(
            subject_user_id=uid,
            engagement_kind=engagement_kind,
            origin=origin,
            attempted_by_user_id=attempted_by_user_id,
            engagement_id=engagement_id,
            engagement_code=engagement_code,
            site_id=site_id,
            session=session,
            **fields,
        )
        n += 1 if rid else 0
    return n


__all__ = [
    "DEDUPE_WINDOW",
    "OUTCOMES",
    "outcome_for",
    "event_fields_for",
    "record_event",
    "record_verdicts",
]
