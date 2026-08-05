"""MOC automated notifications — temporary-change expiry reminders and approval
escalations. Two scheduler jobs (async (db) -> dict), registered in
app.services.scheduler.JOBS.

Best-effort throughout (never raises into the scheduler). Reuses the ERM
notification primitives — create_notification / dedup / role resolution — so the
in-app + email surface and the once-per-window discipline stay identical.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.moc import ChangeRequest, MocApprovalStep, MocStateHistory
from app.services.erm_notifications import (
    _aware,
    _recent_notification_exists,
    _users_with_role,
    create_notification,
)

# Kept in step with app.routers.moc.DEFAULT_ESCALATION_DAYS.
ESCALATION_DAYS = 5

_CLOSED_STATES = {
    "closed_successful",
    "closed_aborted",
    "closed_rejected",
    "withdrawn",
    "expired",
    "rolled_back",
}


async def run_moc_temp_expiry_reminders(db: AsyncSession) -> dict:
    """Scheduler job. For active temporary changes:
      • T-7…T-1 days before expiry → a daily WARNING reminder to the initiator;
      • past expiry (not returned to normal) → a CRITICAL overdue flag to the
        initiator + escalation to the plant HSE_MANAGER.
    One notification per change per 24h (deduped by type + entity)."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=7)
    rows = (
        await db.execute(
            select(ChangeRequest).where(
                ChangeRequest.isTemporary.is_(True),
                ChangeRequest.temporaryExpiryDate.is_not(None),
                ChangeRequest.returnToNormalCompletedAt.is_(None),
            )
        )
    ).scalars().all()

    reminded = 0
    overdue = 0
    for cr in rows:
        if cr.status in _CLOSED_STATES:
            continue
        exp = _aware(cr.temporaryExpiryDate)
        if exp is None:
            continue
        link = f"/moc/{cr.id}"

        if exp < now:
            if await _recent_notification_exists(db, type="MOC_TEMP_OVERDUE", entity_id=cr.id, within=timedelta(hours=24)):
                continue
            days = max(0, (now - exp).days)
            body = (
                f"Temporary change {cr.number} — {cr.title} passed its expiry date "
                f"({exp.date().isoformat()}) {days} day(s) ago and has not been "
                f"returned to normal. Close, extend, or revert it."
            )
            await create_notification(
                db, user_id=cr.initiatedByUserId, type="MOC_TEMP_OVERDUE",
                title=f"Temporary MOC {cr.number} is overdue for return-to-normal",
                body=body, severity="CRITICAL", entity_type="ChangeRequest", entity_id=cr.id, link_url=link,
            )
            overdue += 1
            for u in await _users_with_role(db, "HSE_MANAGER", plant_id=cr.plantId):
                if u.id == cr.initiatedByUserId:
                    continue
                await create_notification(
                    db, user_id=u.id, type="MOC_TEMP_OVERDUE",
                    title=f"[Escalation] Temporary MOC {cr.number} overdue for return-to-normal",
                    body=body, severity="CRITICAL", entity_type="ChangeRequest", entity_id=cr.id, link_url=link,
                )
            continue

        if now <= exp <= horizon:
            if await _recent_notification_exists(db, type="MOC_TEMP_EXPIRY", entity_id=cr.id, within=timedelta(hours=24)):
                continue
            days = max(0, (exp - now).days)
            day_word = "day" if days == 1 else "days"
            body = (
                f"Temporary change {cr.number} — {cr.title} expires in {days} {day_word} "
                f"({exp.date().isoformat()}). Plan its closure, extension, or return to normal."
            )
            await create_notification(
                db, user_id=cr.initiatedByUserId, type="MOC_TEMP_EXPIRY",
                title=f"Temporary MOC {cr.number} expires in {days} {day_word}",
                body=body, severity="WARNING", entity_type="ChangeRequest", entity_id=cr.id, link_url=link,
            )
            reminded += 1

    return {"reminded": reminded, "overdue": overdue, "evaluated": len(rows)}


async def _entered_under_approval_at(db: AsyncSession, cr_id: str) -> datetime | None:
    """When the change most recently entered the under_approval state."""
    row = (
        await db.execute(
            select(MocStateHistory.transitionedAt)
            .where(MocStateHistory.changeRequestId == cr_id)
            .where(MocStateHistory.toState == "under_approval")
            .order_by(MocStateHistory.transitionedAt.desc())
            .limit(1)
        )
    ).first()
    return _aware(row[0]) if row and row[0] else None


async def run_moc_approval_escalations(db: AsyncSession) -> dict:
    """Scheduler job. For changes stuck in under_approval beyond the SLA
    (ESCALATION_DAYS), remind the pending reviewer(s) and escalate to the plant
    head (fallback HSE_MANAGER). Once per change per 24h."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=ESCALATION_DAYS)
    rows = (
        await db.execute(select(ChangeRequest).where(ChangeRequest.status == "under_approval"))
    ).scalars().all()

    escalated = 0
    for cr in rows:
        entered = await _entered_under_approval_at(db, cr.id)
        # Fall back to the record's updatedAt when no history row exists.
        anchor = entered or _aware(cr.updatedAt)
        if anchor is None or anchor > cutoff:
            continue
        if await _recent_notification_exists(db, type="MOC_APPROVAL_OVERDUE", entity_id=cr.id, within=timedelta(hours=24)):
            continue

        days = max(0, (now - anchor).days)
        link = f"/moc/{cr.id}"
        body = (
            f"Change request {cr.number} — {cr.title} has been awaiting approval for "
            f"{days} day(s), beyond the {ESCALATION_DAYS}-day SLA. Please review and decide."
        )

        steps = (
            await db.execute(
                select(MocApprovalStep)
                .where(MocApprovalStep.changeRequestId == cr.id)
                .where(MocApprovalStep.decision == "pending")
            )
        ).scalars().all()
        notified: set[str] = set()
        for s in steps:
            if s.specificUserId and s.specificUserId not in notified:
                await create_notification(
                    db, user_id=s.specificUserId, type="MOC_APPROVAL_OVERDUE",
                    title=f"Approval overdue — MOC {cr.number}",
                    body=body, severity="WARNING", entity_type="ChangeRequest", entity_id=cr.id, link_url=link,
                )
                notified.add(s.specificUserId)

        managers = await _users_with_role(db, "PLANT_HEAD", plant_id=cr.plantId)
        if not managers:
            managers = await _users_with_role(db, "HSE_MANAGER", plant_id=cr.plantId)
        for u in managers:
            if u.id in notified:
                continue
            await create_notification(
                db, user_id=u.id, type="MOC_APPROVAL_OVERDUE",
                title=f"[Escalation] Approval overdue — MOC {cr.number}",
                body=body, severity="CRITICAL", entity_type="ChangeRequest", entity_id=cr.id, link_url=link,
            )
            notified.add(u.id)
        escalated += 1

    return {"escalated": escalated, "evaluated": len(rows)}
