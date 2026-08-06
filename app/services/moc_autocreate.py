"""Programmatic MOC creation for automatic triggers.

`routers/moc.py::create_change_request` is a request handler: it depends on a
`User` from the session, calls `require_permission_with_context`, and commits.
None of that is available to a trigger firing inside someone else's
transaction, so auto-triggered MOCs previously had no path at all — which is
part of why `hira_moc_receiver.py` still carries a docstring saying "MOC module
is not yet built" months after the MOC module shipped.

This is that path. Deliberately narrow:

  * **Never commits.** It participates in the caller's transaction so an MOC and
    the ledger row that justified it are atomic. A trigger that commits
    independently can leave an MOC referencing a receipt that rolled back.
  * **No permission check.** The actor is the system, acting on a rule an Admin
    configured. Authorisation happened when the ThresholdRule was created.
  * **Number allocation retries.** `_generate_moc_number`'s read-max-then-insert
    is racy, and `ChangeRequest.number` is UNIQUE. Under the concurrent receipt
    volume this module's threshold trigger actually sees, that race is not
    theoretical — two simultaneous receipts at the same plant is an ordinary
    Tuesday in a chemical store. Losing the MOC to an IntegrityError would
    reproduce, exactly, the "trigger silently didn't work" failure this whole
    build exists to prevent, so the allocation is retried inside a SAVEPOINT.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.moc import ChangeRequest, MocApprovalStep, MocStateHistory
from app.models.plant import Plant

logger = logging.getLogger(__name__)

_NUMBER_ALLOCATION_ATTEMPTS = 5


class MocAutoCreateError(RuntimeError):
    """Raised when an MOC could not be created. The trigger engine turns this
    into a FAILED log row with a non-empty reason, so it must always carry a
    message a store manager can act on."""


async def _allocate_number(db: AsyncSession, plant: Plant) -> str:
    year = datetime.now(timezone.utc).year
    prefix = f"MOC-{year}-{plant.code}-"
    existing = (
        await db.execute(select(ChangeRequest.number).where(ChangeRequest.number.like(f"{prefix}%")))
    ).scalars().all()
    max_n = 0
    for n in existing:
        try:
            max_n = max(max_n, int(n.rsplit("-", 1)[-1]))
        except ValueError:
            continue
    return f"{prefix}{max_n + 1:04d}"


async def create_auto_moc(
    db: AsyncSession,
    *,
    plant_id: str,
    title: str,
    description: str,
    category: str,
    classification: str = "major",
    origin: str = "auto_trigger",
    origin_source_type: str | None = None,
    origin_source_id: str | None = None,
    initiated_by_user_id: str,
    business_justification: str | None = None,
    hazard_categories: Sequence[str] | None = None,
    affected_locations: Sequence[str] | None = None,
    reviewers: Sequence[dict[str, Any]] = (),
    rationale: str = "Automatically raised by a platform trigger",
) -> ChangeRequest:
    """Create (but do not commit) a ChangeRequest on behalf of the system.

    Raises MocAutoCreateError with an actionable message on any failure — the
    caller records that message verbatim in MocTriggerLog.failureReason.
    """
    plant = await db.get(Plant, plant_id)
    if plant is None:
        raise MocAutoCreateError(
            f"Plant {plant_id} not found; cannot raise an MOC without a plant context."
        )

    cr: ChangeRequest | None = None
    last_error: Exception | None = None
    for attempt in range(1, _NUMBER_ALLOCATION_ATTEMPTS + 1):
        number = await _allocate_number(db, plant)
        candidate = ChangeRequest(
            plantId=plant_id,
            number=number,
            title=title[:255],
            description=description,
            category=category,
            classification=classification,
            origin=origin,
            originSourceType=origin_source_type,
            originSourceId=origin_source_id,
            initiatedByUserId=initiated_by_user_id,
            businessJustification=business_justification,
            # Auto-raised MOCs enter as `submitted`, not `draft`. A draft sits in
            # the initiator's queue, and the initiator here is the system — it
            # would wait forever. Submitting puts it in front of an approver,
            # which is the entire point of raising it.
            status="submitted",
            hazardCategories=list(hazard_categories) if hazard_categories else None,
            affectedLocations=list(affected_locations) if affected_locations else None,
            pssrRequired=classification in ("major", "critical"),
        )
        try:
            async with db.begin_nested():
                db.add(candidate)
                await db.flush()
            cr = candidate
            break
        except IntegrityError as exc:
            # Another transaction took this number between our SELECT and our
            # INSERT. Recompute and try again — the savepoint rollback has
            # already cleaned up the failed row.
            last_error = exc
            db.expunge(candidate)
            logger.info(
                "[moc_autocreate] number %s collided (attempt %d/%d); retrying",
                number, attempt, _NUMBER_ALLOCATION_ATTEMPTS,
            )

    if cr is None:
        raise MocAutoCreateError(
            f"Could not allocate an MOC number for plant {plant.code} after "
            f"{_NUMBER_ALLOCATION_ATTEMPTS} attempts: {last_error}"
        )

    for i, rv in enumerate(reviewers, start=1):
        db.add(
            MocApprovalStep(
                changeRequestId=cr.id,
                sequence=i,
                role=rv.get("role", "HSE_MANAGER"),
                specificUserId=rv.get("specificUserId"),
                isRequired=bool(rv.get("isRequired", True)),
                decision="pending",
            )
        )

    db.add(
        MocStateHistory(
            changeRequestId=cr.id,
            fromState=None,
            toState="submitted",
            transitionedByUserId=initiated_by_user_id,
            rationale=rationale,
        )
    )
    await db.flush()
    return cr


__all__ = ["create_auto_moc", "MocAutoCreateError"]
