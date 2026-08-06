"""Calendar bookings router — the audit's claim on people's time.

Permission codes are the EXISTING CAMS set, matching the choice `assurance.py`
made and for the same reason: a tenant that has already granted CAMS rights gets
these surfaces without an RBAC migration, and the seeds stay untouched.

  CAMS.READ       see what is booked, and the provider's configuration state
  CAMS.SCHEDULE   sync, reschedule a meeting, cancel a booking

`AUDIT` resolves to `ComplianceAudit` and `INSPECTION` to `CamsEngagement` — the
same discriminator the assurance and independence layers already use, so one
router serves both engines.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.calendar import CalendarBooking
from app.models.user import User
from app.services import calendar_booking as svc
from app.services import calendar_providers as providers
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


async def _require(db: AsyncSession, user: User, code: str, *, plant_id=None) -> None:
    res = await can(db, user.id, code, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


async def _site_of(db: AsyncSession, kind: str, engagement_id: str) -> str | None:
    """The engagement's plant, so the permission check is plant-scoped rather
    than global — an OWN_PLANT scheduler must not be able to re-sync another
    site's audit."""
    kind = (kind or "").upper()
    if kind == "AUDIT":
        from app.models.audit_compliance import ComplianceAudit

        a = await db.get(ComplianceAudit, engagement_id)
        if a is None or a.isDeleted:
            raise HTTPException(404, "Audit not found")
        return a.plantId
    if kind == "INSPECTION":
        from app.models.cams import CamsEngagement

        e = await db.get(CamsEngagement, engagement_id)
        if e is None or e.isDeleted:
            raise HTTPException(404, "Engagement not found")
        return e.siteId
    raise HTTPException(400, "engagementKind must be AUDIT or INSPECTION")


# ─────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────


@router.get("/bookings")
async def list_bookings(
    engagementKind: Literal["AUDIT", "INSPECTION"] = Query("AUDIT"),
    engagementId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.READ")
    return await svc.bookings_for(
        db, engagement_kind=engagementKind, engagement_id=engagementId
    )


@router.get("/rooms")
async def list_rooms(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Bookable meeting rooms from the Microsoft 365 directory.

    Never 404s or 500s on an unconfigured tenant — it returns an empty list with
    the reason, because a room can still be booked by typing its mailbox address
    and a picker that errors would conceal that.
    """
    await _require(db, user, "CAMS.READ")
    return await svc.list_rooms(db)


@router.get("/status")
async def calendar_status(
    probe: bool = Query(
        False,
        description="Fetch a live Microsoft Graph token to prove the credentials work, "
        "rather than only reporting that they are present.",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Which channel bookings go out over, and what is missing if none does."""
    await _require(db, user, "CAMS.READ")
    return await providers.provider_status(probe=probe)


# ─────────────────────────────────────────────────────────────────────
# Write
# ─────────────────────────────────────────────────────────────────────


class SyncBody(BaseModel):
    engagementKind: Literal["AUDIT", "INSPECTION"] = "AUDIT"
    engagementId: str
    # Re-send even when nothing changed. For the case the diff cannot see: an
    # attendee deleted the invitation out of their own calendar.
    force: bool = False


@router.post("/bookings/sync")
async def sync_bookings(
    body: SyncBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Recompute this engagement's bookings from the record and deliver the diff.

    Automatic on audit creation and on every team change — this endpoint is for
    the operator who needs it now: after credentials are added, after a mailbox
    is fixed, or after someone deleted a meeting out of Outlook.
    """
    plant_id = await _site_of(db, body.engagementKind, body.engagementId)
    await _require(db, user, "CAMS.SCHEDULE", plant_id=plant_id)
    out = await svc.sync_engagement(
        db,
        engagement_kind=body.engagementKind,
        engagement_id=body.engagementId,
        actor_id=user.id,
        force=body.force,
    )
    await db.commit()
    # A delivery failure is reported in the body, not as a 5xx: the bookings
    # were still recorded and the retry job will drain them, so the caller has
    # something true to render rather than an error page.
    return out


class RescheduleBody(BaseModel):
    startAt: datetime
    endAt: datetime


@router.patch("/bookings/{booking_id}")
async def reschedule(
    booking_id: str,
    body: RescheduleBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Move an opening or closing meeting within the audit.

    The audit block itself is not movable here — its time is derived from the
    audit's scheduled date and duration, and allowing it to be edited from the
    calendar panel would give the schedule two owners that could disagree.
    """
    b = await db.get(CalendarBooking, booking_id)
    if b is None:
        raise HTTPException(404, "Booking not found")
    await _require(db, user, "CAMS.SCHEDULE", plant_id=b.siteId)
    try:
        out = await svc.reschedule_booking(
            db, booking_id=booking_id, start=body.startAt, end=body.endAt, actor_id=user.id
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await db.commit()
    return out


class RoomBody(BaseModel):
    # Explicitly nullable: `null` means "this meeting has no room" and is a
    # decision, not an absence. The site default does not come back afterwards.
    roomEmail: str | None = None
    roomName: str | None = None


@router.put("/bookings/{booking_id}/room")
async def set_room(
    booking_id: str,
    body: RoomBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Hold a meeting room for this booking, or release it.

    The room is added to the event as an Exchange `resource` attendee, so its
    own booking assistant decides. The immediate answer is therefore PENDING,
    and the maintenance job settles it to ACCEPTED or DECLINED — a decline being
    the case that matters, since it means the room was already taken.
    """
    b = await db.get(CalendarBooking, booking_id)
    if b is None:
        raise HTTPException(404, "Booking not found")
    await _require(db, user, "CAMS.SCHEDULE", plant_id=b.siteId)
    try:
        out = await svc.set_room(
            db,
            booking_id=booking_id,
            room_email=body.roomEmail,
            room_name=body.roomName,
            actor_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await db.commit()
    return out


class CancelBody(BaseModel):
    reason: str = ""


@router.post("/bookings/{booking_id}/cancel")
async def cancel(
    booking_id: str,
    body: CancelBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Withdraw one booking from participants' calendars.

    A cancellation notice, not a deletion — the row survives with its reason, so
    the audit can still show that time was held and then released.
    """
    b = await db.get(CalendarBooking, booking_id)
    if b is None:
        raise HTTPException(404, "Booking not found")
    await _require(db, user, "CAMS.SCHEDULE", plant_id=b.siteId)
    try:
        out = await svc.cancel_booking(
            db, booking_id=booking_id, reason=body.reason, actor_id=user.id
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await db.commit()
    return out
