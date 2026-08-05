"""Deroster review + target-closure-date endpoints for Safety Observations.

Split out of `observations.py` (already ~750 lines) but mounted on the same
`/api/observations` prefix, so the URLs read as one module.

Every state-changing endpoint here enforces its own role check server-side.
The UI hides the buttons from users who cannot act, but that is presentation —
these checks are the boundary, and the spec's §7 test calls them directly.

`{worker_id}` in these routes is the **ObservationWorkerInvolved** id, not a
User or ContractorWorker id. The involved-worker row is the unambiguous handle:
worker references are polymorphic across two tables whose ids could in
principle collide, and one person could be named on the same observation only
once, so the involvement row is the natural key for its review.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.observation import Observation
from app.models.observation_sla import (
    SOURCE_MANUAL_OVERRIDE,
    SOURCE_SECTION_HEAD_REASSIGNED,
    ObservationDeroster,
    ObservationDerosterEvent,
    ObservationTargetDateHistory,
)
from app.models.user import User
from app.schemas.observation_sla import (
    DerosterDecisionIn,
    DerosterEventOut,
    DerosterReinstateIn,
    TargetDateHistoryOut,
    TargetDateOverrideIn,
    TargetDateReassignIn,
    WorkerInvolvedOut,
)
from app.services import observation_deroster as deroster_svc
from app.services import observation_sla as sla
from app.services.permissions import get_user_role_codes

router = APIRouter(prefix="/api/observations", tags=["observations"])


# ── shared guards ────────────────────────────────────────────────────────────
async def _load_observation(db: AsyncSession, observation_id: str) -> Observation:
    obs = await db.get(Observation, observation_id)
    if obs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Observation not found")
    return obs


async def _require_decision_role(db: AsyncSession, user: User, plant_id: str | None) -> None:
    """Section Head / HSE Manager gate for confirm, overrule and reinstate.

    Deliberately a ROLE check, not a permission check: a deroster is a
    people-management decision, and OBSERVATION.UPDATE is held by observers and
    action owners who must not be able to clear a safety hold on themselves or
    a colleague. See DECISION_ROLES for why DEPARTMENT_HEAD stands in for the
    spec's "Section Head".
    """
    codes = set(await get_user_role_codes(db, user.id))
    if not codes & deroster_svc.DECISION_ROLES:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only a Section Head or HSE Manager can act on a safety review.",
        )


async def _load_deroster(
    db: AsyncSession, observation_id: str, worker_id: str
) -> ObservationDeroster:
    row = (
        await db.execute(
            select(ObservationDeroster)
            .where(ObservationDeroster.observationId == observation_id)
            .where(ObservationDeroster.workerInvolvedId == worker_id)
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No safety review for this worker")
    return row


def _map_error(e: deroster_svc.DerosterError) -> HTTPException:
    return HTTPException(e.status_code, str(e))


# ── worker involvement ───────────────────────────────────────────────────────
@router.get("/{observation_id}/workers-involved", response_model=list[WorkerInvolvedOut])
async def list_workers_involved(
    observation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[WorkerInvolvedOut]:
    obs = await _load_observation(db, observation_id)
    await require_permission_with_context("OBSERVATION.READ", user, db, plant_id=obs.plantId)
    rows = await deroster_svc.load_workers_involved(db, observation_id)
    return [WorkerInvolvedOut(**r) for r in rows]


# ── target closure date ──────────────────────────────────────────────────────
@router.post("/{observation_id}/target-closure-date/override", response_model=TargetDateHistoryOut)
async def override_target_closure_date(
    observation_id: str,
    payload: TargetDateOverrideIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TargetDateHistoryOut:
    """Deviate from the SLA policy, with a justification.

    The minimum reason length is enforced here as well as in the form: §7
    requires the server to reject a short reason submitted by direct API call.
    """
    obs = await _load_observation(db, observation_id)
    await require_permission_with_context(
        "OBSERVATION.UPDATE", user, db, plant_id=obs.plantId
    )
    reason = (payload.reason or "").strip()
    if len(reason) < sla.MIN_OVERRIDE_REASON_CHARS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"An override reason of at least {sla.MIN_OVERRIDE_REASON_CHARS} characters is required.",
        )
    new_date = payload.date
    if new_date.tzinfo is None:
        new_date = new_date.replace(tzinfo=timezone.utc)
    if new_date.date() < datetime.now(timezone.utc).date():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Target closure date cannot be in the past.")

    obs.targetDate = new_date
    obs.targetDateSource = SOURCE_MANUAL_OVERRIDE
    obs.targetDateOverrideReason = reason
    # targetDateSlaConfig is deliberately LEFT AS-IS: it records the policy this
    # record was originally held to, which is exactly what makes the override
    # meaningful in an audit. Clearing it would erase what was deviated from.
    row = sla.record_history(
        db,
        observation_id=obs.id,
        target_date=new_date,
        source=SOURCE_MANUAL_OVERRIDE,
        reason=reason,
        sla_config=obs.targetDateSlaConfig,
        changed_by_id=user.id,
    )
    await db.flush()
    return TargetDateHistoryOut.model_validate(row)


@router.patch(
    "/{observation_id}/target-closure-date/section-head-reassign",
    response_model=TargetDateHistoryOut,
)
async def section_head_reassign_target_date(
    observation_id: str,
    payload: TargetDateReassignIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TargetDateHistoryOut:
    """Reset the closure date while assigning the responsible person.

    No reason required — this is a normal workflow step, not a deviation
    (spec §2.2). It is still appended to the history so the trail is complete.
    """
    obs = await _load_observation(db, observation_id)
    await require_permission_with_context(
        "OBSERVATION.APPROVE", user, db, plant_id=obs.plantId
    )
    new_date = payload.date
    if new_date.tzinfo is None:
        new_date = new_date.replace(tzinfo=timezone.utc)
    if new_date.date() < datetime.now(timezone.utc).date():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Target closure date cannot be in the past.")

    obs.targetDate = new_date
    obs.targetDateSource = SOURCE_SECTION_HEAD_REASSIGNED
    row = sla.record_history(
        db,
        observation_id=obs.id,
        target_date=new_date,
        source=SOURCE_SECTION_HEAD_REASSIGNED,
        sla_config=obs.targetDateSlaConfig,
        changed_by_id=user.id,
    )
    await db.flush()
    return TargetDateHistoryOut.model_validate(row)


@router.get(
    "/{observation_id}/target-closure-date/history", response_model=list[TargetDateHistoryOut]
)
async def target_closure_date_history(
    observation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TargetDateHistoryOut]:
    obs = await _load_observation(db, observation_id)
    await require_permission_with_context("OBSERVATION.READ", user, db, plant_id=obs.plantId)
    rows = (
        await db.execute(
            select(ObservationTargetDateHistory)
            .where(ObservationTargetDateHistory.observationId == observation_id)
            .order_by(ObservationTargetDateHistory.changedAt.asc())
        )
    ).scalars().all()
    return [TargetDateHistoryOut.model_validate(r) for r in rows]


# ── deroster decisions ───────────────────────────────────────────────────────
@router.post("/{observation_id}/deroster/{worker_id}/confirm", response_model=WorkerInvolvedOut)
async def confirm_deroster(
    observation_id: str,
    worker_id: str,
    payload: DerosterDecisionIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkerInvolvedOut:
    obs = await _load_observation(db, observation_id)
    await _require_decision_role(db, user, obs.plantId)
    row = await _load_deroster(db, observation_id, worker_id)
    try:
        await deroster_svc.confirm(db, row, actor_id=user.id, reason=payload.reason)
    except deroster_svc.DerosterError as e:
        raise _map_error(e) from e
    await db.flush()
    return await _worker_response(db, observation_id, worker_id)


@router.post("/{observation_id}/deroster/{worker_id}/overrule", response_model=WorkerInvolvedOut)
async def overrule_deroster(
    observation_id: str,
    worker_id: str,
    payload: DerosterDecisionIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkerInvolvedOut:
    obs = await _load_observation(db, observation_id)
    await _require_decision_role(db, user, obs.plantId)
    row = await _load_deroster(db, observation_id, worker_id)
    try:
        await deroster_svc.overrule(db, row, actor_id=user.id, reason=payload.reason)
    except deroster_svc.DerosterError as e:
        raise _map_error(e) from e
    await db.flush()
    return await _worker_response(db, observation_id, worker_id)


@router.post("/{observation_id}/deroster/{worker_id}/reinstate", response_model=WorkerInvolvedOut)
async def reinstate_worker(
    observation_id: str,
    worker_id: str,
    payload: DerosterReinstateIn | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkerInvolvedOut:
    """Return a remediated worker to active duty.

    The corrective-action check runs inside the service, so a direct API call
    that bypasses the disabled button is rejected the same way (409) — spec §7.
    """
    obs = await _load_observation(db, observation_id)
    await _require_decision_role(db, user, obs.plantId)
    row = await _load_deroster(db, observation_id, worker_id)
    try:
        await deroster_svc.reinstate(
            db, row, actor_id=user.id, note=(payload.note if payload else None)
        )
    except deroster_svc.DerosterError as e:
        raise _map_error(e) from e
    await db.flush()
    return await _worker_response(db, observation_id, worker_id)


@router.get(
    "/{observation_id}/deroster/{worker_id}/audit-log", response_model=list[DerosterEventOut]
)
async def deroster_audit_log(
    observation_id: str,
    worker_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DerosterEventOut]:
    obs = await _load_observation(db, observation_id)
    await require_permission_with_context("OBSERVATION.READ", user, db, plant_id=obs.plantId)
    row = await _load_deroster(db, observation_id, worker_id)
    events = (
        await db.execute(
            select(ObservationDerosterEvent)
            .where(ObservationDerosterEvent.derosterId == row.id)
            .order_by(ObservationDerosterEvent.createdAt.asc())
        )
    ).scalars().all()
    return [DerosterEventOut.model_validate(e) for e in events]


async def _worker_response(
    db: AsyncSession, observation_id: str, worker_id: str
) -> WorkerInvolvedOut:
    rows = await deroster_svc.load_workers_involved(db, observation_id)
    for r in rows:
        if r["id"] == worker_id:
            return WorkerInvolvedOut(**r)
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Worker not found on this observation")


__all__ = ["router"]
