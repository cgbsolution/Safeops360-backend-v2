"""ERM automated notifications — in-app Notification rows + best-effort email.

Every function here is async and best-effort: it NEVER raises into its caller.
Email delivery is wrapped in try/except (same discipline as
app.services.auto_promote_near_miss) so an SMTP outage can never break a risk
create, a treatment assignment, or a scheduler tick.

Surface:
  create_notification(...)                  — the low-level insert (+ optional email)
  notify_risk_owner_assigned(...)           — call at risk-create / owner-change
  notify_treatment_owner_assigned(...)      — call at treatment (RISK_TREATMENT CAPA) create
  run_treatment_pre_due_reminders(db)       — scheduler job (async (db) -> dict)
  run_treatment_overdue_escalations(db)     — scheduler job (async (db) -> dict)

The two run_* functions match app.services.scheduler.Job.fn signature.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capa import Capa
from app.models.notification import Notification
from app.models.user import Role, User, UserRole
from app.services.notifications import send_email

# A RISK_TREATMENT CAPA in any of these states is finished — no reminders /
# escalations. Everything else is treated as "open". Mirrors the closed set
# used across app.services.erm (_CLOSED_CAPA_STATES + terminal variants).
_CLOSED_CAPA_STATES = {"CLOSED", "VERIFIED", "CLOSED_RECURRED", "CANCELLED", "REJECTED"}


def _aware(d: datetime | None) -> datetime | None:
    """Normalise a possibly-naive datetime to timezone-aware UTC."""
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


async def _users_with_role(db: AsyncSession, role_code: str, plant_id: str | None = None) -> list[User]:
    """Users holding `role_code` (active role). Scopes to plant_id when it yields
    matches, else returns all holders. Mirrors auto_promote_near_miss._users_with_role."""
    stmt = (
        select(User)
        .join(UserRole, UserRole.userId == User.id)
        .join(Role, Role.id == UserRole.roleId)
        .where(Role.code == role_code, Role.isActive == True)  # noqa: E712
    )
    rows = (await db.execute(stmt)).scalars().all()
    if plant_id:
        scoped = [u for u in rows if u.plantId == plant_id]
        if scoped:
            return scoped
    return list(rows)


async def create_notification(
    db: AsyncSession,
    *,
    user_id: str,
    type: str,
    title: str,
    body: str = "",
    severity: str = "INFO",
    entity_type: str | None = None,
    entity_id: str | None = None,
    link_url: str | None = None,
    send_mail: bool = True,
) -> Notification:
    """Insert one Notification (db.add + flush). If send_mail and the user has an
    email, also fire a best-effort email. Never raises."""
    notif = Notification(
        userId=user_id,
        type=type,
        title=title,
        body=body or "",
        severity=severity or "INFO",
        entityType=entity_type,
        entityId=entity_id,
        linkUrl=link_url,
    )
    db.add(notif)
    await db.flush()

    if send_mail:
        try:
            user = await db.get(User, user_id)
            if user and user.email:
                await send_email([user.email], f"[SafeOps360] {title}", body or title)
        except Exception as e:  # noqa: BLE001
            print(f"[erm_notifications] email failed: {e}", file=sys.stderr)

    return notif


async def notify_risk_owner_assigned(
    db: AsyncSession,
    *,
    risk_id: str,
    risk_code: str,
    risk_title: str,
    owner_user_id: str | None,
) -> Notification | None:
    """Tell a user they are now the Risk Owner of `risk_code`. No-op if no owner."""
    if not owner_user_id:
        return None
    body = (
        f"You have been assigned as Risk Owner of {risk_code}.\n\n"
        f"Risk: {risk_title}\n\n"
        f"Open it in SafeOps360 to review and progress the risk."
    )
    return await create_notification(
        db,
        user_id=owner_user_id,
        type="RISK_OWNER_ASSIGNED",
        title=f"You are now Risk Owner of {risk_code}",
        body=body,
        severity="INFO",
        entity_type="EnterpriseRisk",
        entity_id=risk_id,
        link_url=f"/erm/register/{risk_id}",
    )


async def notify_treatment_owner_assigned(
    db: AsyncSession,
    *,
    capa: Capa,
) -> Notification | None:
    """Tell the CAPA primary owner a new risk treatment is assigned to them."""
    owner_id = getattr(capa, "primaryOwnerUserId", None)
    if not owner_id:
        return None
    body = (
        f"A new risk treatment has been assigned to you.\n\n"
        f"Treatment: {capa.capaNumber} — {capa.title}\n\n"
        f"Open it in SafeOps360 to plan and execute the treatment actions."
    )
    return await create_notification(
        db,
        user_id=owner_id,
        type="TREATMENT_ASSIGNED",
        title=f"New risk treatment assigned: {capa.capaNumber}",
        body=body,
        severity="INFO",
        entity_type="Capa",
        entity_id=capa.id,
        link_url=f"/capa/{capa.id}",
    )


async def _recent_notification_exists(
    db: AsyncSession,
    *,
    type: str,
    entity_id: str,
    within: timedelta,
) -> bool:
    """True if a notification of `type` for `entity_id` was created inside `within`."""
    cutoff = datetime.now(timezone.utc) - within
    row = (
        await db.execute(
            select(Notification.id)
            .where(Notification.type == type)
            .where(Notification.entityId == entity_id)
            .where(Notification.createdAt >= cutoff)
            .limit(1)
        )
    ).first()
    return row is not None


async def _open_risk_treatments(db: AsyncSession) -> list[Capa]:
    """All RISK_TREATMENT CAPAs not in a closed/cancelled state."""
    capas = (
        await db.execute(
            select(Capa).where(Capa.sourceTypeCode == "RISK_TREATMENT")
        )
    ).scalars().all()
    return [c for c in capas if (c.state or "") not in _CLOSED_CAPA_STATES]


async def run_treatment_pre_due_reminders(db: AsyncSession) -> dict:
    """Scheduler job. Remind treatment owners of open RISK_TREATMENT CAPAs whose
    closureTargetDate falls within the next 7 days. One reminder per CAPA per 24h."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=7)
    capas = await _open_risk_treatments(db)
    sent = 0
    for c in capas:
        due = _aware(c.closureTargetDate)
        if due is None or due < now or due > horizon:
            continue
        owner_id = getattr(c, "primaryOwnerUserId", None)
        if not owner_id:
            continue
        if await _recent_notification_exists(
            db, type="TREATMENT_REMINDER", entity_id=c.id, within=timedelta(hours=24)
        ):
            continue
        days = max(0, (due - now).days)
        day_word = "day" if days == 1 else "days"
        body = (
            f"Risk treatment {c.capaNumber} — {c.title} is due in {days} {day_word} "
            f"(target {due.date().isoformat()}).\n\n"
            f"Please complete and close it before the target date."
        )
        await create_notification(
            db,
            user_id=owner_id,
            type="TREATMENT_REMINDER",
            title=f"Treatment {c.capaNumber} due in {days} {day_word}",
            body=body,
            severity="WARNING",
            entity_type="Capa",
            entity_id=c.id,
            link_url=f"/capa/{c.id}",
        )
        sent += 1
    return {"sent": sent, "evaluated": len(capas)}


async def run_treatment_overdue_escalations(db: AsyncSession) -> dict:
    """Scheduler job. For open RISK_TREATMENT CAPAs past their closureTargetDate:
    notify the owner (CRITICAL) AND escalate to every CRO (fallback HSE_MANAGER).
    Fires at most once per CAPA per 24h."""
    now = datetime.now(timezone.utc)
    capas = await _open_risk_treatments(db)

    # Resolve escalation recipients once (CRO, or HSE_MANAGER fallback).
    escalation_targets = await _users_with_role(db, "CRO")
    if not escalation_targets:
        escalation_targets = await _users_with_role(db, "HSE_MANAGER")

    sent = 0
    escalated = 0
    for c in capas:
        due = _aware(c.closureTargetDate)
        if due is None or due >= now:
            continue
        if await _recent_notification_exists(
            db, type="TREATMENT_OVERDUE", entity_id=c.id, within=timedelta(hours=24)
        ):
            continue
        days_overdue = max(0, (now - due).days)
        day_word = "day" if days_overdue == 1 else "days"
        body = (
            f"Risk treatment {c.capaNumber} — {c.title} is OVERDUE by {days_overdue} "
            f"{day_word} (target {due.date().isoformat()}).\n\n"
            f"Immediate action is required to complete and close this treatment."
        )

        owner_id = getattr(c, "primaryOwnerUserId", None)
        if owner_id:
            await create_notification(
                db,
                user_id=owner_id,
                type="TREATMENT_OVERDUE",
                title=f"Treatment {c.capaNumber} is overdue by {days_overdue} {day_word}",
                body=body,
                severity="CRITICAL",
                entity_type="Capa",
                entity_id=c.id,
                link_url=f"/capa/{c.id}",
            )
            sent += 1

        for u in escalation_targets:
            if u.id == owner_id:
                continue  # already notified as owner
            await create_notification(
                db,
                user_id=u.id,
                type="TREATMENT_OVERDUE",
                title=f"[Escalation] Treatment {c.capaNumber} overdue by {days_overdue} {day_word}",
                body=body,
                severity="CRITICAL",
                entity_type="Capa",
                entity_id=c.id,
                link_url=f"/capa/{c.id}",
            )
            escalated += 1

    return {"sent": sent, "escalated": escalated, "evaluated": len(capas)}


__all__ = [
    "create_notification",
    "notify_risk_owner_assigned",
    "notify_treatment_owner_assigned",
    "run_treatment_pre_due_reminders",
    "run_treatment_overdue_escalations",
]
