"""In-app notification feed for the current user.

Backs the dashboard "alerts" bell: list, unread count, mark-one-read,
mark-all-read. Rows are produced by app.services.erm_notifications. A user
can only see and mutate their own notifications.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.notification import Notification
from app.models.user import User

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _serialize(n: Notification) -> dict[str, Any]:
    return {
        "id": n.id,
        "type": n.type,
        "severity": n.severity,
        "title": n.title,
        "body": n.body,
        "entityType": n.entityType,
        "entityId": n.entityId,
        "linkUrl": n.linkUrl,
        "isRead": n.isRead,
        "createdAt": n.createdAt.isoformat() if n.createdAt else None,
    }


@router.get("")
async def list_notifications(
    unreadOnly: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Current user's notifications, unread first then newest."""
    stmt = select(Notification).where(Notification.userId == user.id)
    if unreadOnly:
        stmt = stmt.where(Notification.isRead == False)  # noqa: E712
    stmt = stmt.order_by(Notification.isRead.asc(), Notification.createdAt.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return {"notifications": [_serialize(n) for n in rows]}


@router.get("/unread-count")
async def unread_count(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """Number of unread notifications for the current user."""
    count = (
        await db.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.userId == user.id)
            .where(Notification.isRead == False)  # noqa: E712
        )
    ).scalar_one()
    return {"count": int(count)}


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    """Mark one of the current user's notifications read. No-op if not theirs / missing."""
    notif = await db.get(Notification, notification_id)
    if notif is not None and notif.userId == user.id and not notif.isRead:
        notif.isRead = True
        notif.readAt = datetime.now(timezone.utc)
        await db.flush()
    return {"ok": True}


@router.post("/read-all")
async def mark_all_read(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    """Mark all of the current user's unread notifications read."""
    await db.execute(
        update(Notification)
        .where(Notification.userId == user.id)
        .where(Notification.isRead == False)  # noqa: E712
        .values(isRead=True, readAt=datetime.now(timezone.utc))
    )
    await db.flush()
    return {"ok": True}
