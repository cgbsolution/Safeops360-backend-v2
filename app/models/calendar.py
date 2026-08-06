"""Calendar bookings — the audit's claim on people's time, as a row.

CAMS could schedule an audit for nine people and leave every one of those nine
calendars empty. The audit existed in SafeOps360; the *time* did not exist
anywhere the participants actually look. This is the table that fixes that.

**One row per calendar event we own**, never per attendee. The booking is a
single meeting in the organiser's mailbox with an attendee list — that is what
Microsoft Graph creates, what an .ics REQUEST describes, and what a participant
sees. Modelling per-attendee rows would produce N unrelated meetings and no
shared thread, which is not what "book the calendar" means to anyone.

Three booking types per engagement, from `services/calendar_booking.py`:

  AUDIT_BLOCK      the fieldwork window itself — created the moment the audit is
  OPENING_MEETING  ISO 19011 §6.4.2, at the head of the window
  CLOSING_MEETING  ISO 19011 §6.4.9, at the foot of it

`attendees` is JSON — the same shape and for the same reason as
`EngagementMeeting.attendees`: a buyer auditor or a supplier's factory manager
attends and holds no platform seat, so `{name, email}` without a `userId` has to
be expressible. Each entry carries `addedAt`, which is what makes the incremental
case honest: auditees named a week after the audit was set show their own
join date rather than inheriting the booking's.

Delivery state (`status`, `attemptCount`, `lastError`) lives on the row because
calendar delivery is a best-effort side effect over a network we do not own. An
audit must never fail to be created because Exchange was slow — so the write
succeeds, the booking sits at PENDING, and the retry job drains it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin

# ── Vocabulary ───────────────────────────────────────────────────────

BOOKING_TYPES = ("AUDIT_BLOCK", "OPENING_MEETING", "CLOSING_MEETING")

# PENDING   — desired, not yet delivered (or awaiting retry after a failure)
# BOOKED    — the provider accepted it; `providerEventId` is real
# FAILED    — the provider rejected it and the retry budget is spent
# CANCELLED — withdrawn from the participants' calendars
# SKIPPED   — nothing to deliver (no provider configured, or no attendee has an
#             email address). Deliberately distinct from FAILED: nothing broke.
BOOKING_STATUSES = ("PENDING", "BOOKED", "FAILED", "CANCELLED", "SKIPPED")

ATTENDEE_ROLES = (
    "LEAD_AUDITOR",
    "CO_AUDITOR",
    "AUDITEE",
    "PLANT_MANAGER",
    "SUPPLIER_CONTACT",
    "OTHER",
)


class CalendarBooking(Base, IdMixin):
    __tablename__ = "CalendarBooking"

    # Polymorphic over both engines, exactly as EngagementMeeting and
    # IndependenceWaiver already are — AUDIT = ComplianceAudit,
    # INSPECTION = CamsEngagement. One service serves both.
    engagementKind: Mapped[str] = mapped_column(String, nullable=False)
    engagementId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    bookingType: Mapped[str] = mapped_column(String, nullable=False)

    siteId: Mapped[str | None] = mapped_column(String, index=True)

    subject: Mapped[str] = mapped_column(String, nullable=False)
    bodyHtml: Mapped[str] = mapped_column(Text, nullable=False, default="")
    location: Mapped[str] = mapped_column(String, nullable=False, default="")

    # Stored in UTC; `timezone` is the IANA zone the times were composed in and
    # the one the invite is expressed in. Sending a naive UTC instant to a
    # participant in a different zone is how a 09:00 audit shows up at 03:30.
    startAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    endAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String, nullable=False, default="Asia/Kolkata")

    # Whose mailbox holds the meeting. The lead auditor owns the audit's time,
    # so they organise it; falls back to the configured service mailbox when the
    # lead has no routable address.
    organizerUserId: Mapped[str | None] = mapped_column(String)
    organizerEmail: Mapped[str | None] = mapped_column(String)

    # [{userId, email, name, role, required, addedAt, removedAt}]
    attendees: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # ── Meeting room (Exchange resource mailbox) ──────────────────────
    #
    # A room is NOT an attendee row. It is a resource mailbox that answers for
    # itself: Exchange's booking assistant accepts or DECLINES on the room's own
    # policy, and a decline means somebody else already has it. Storing the room
    # alongside its verdict is the difference between "we asked for conf-east"
    # and "we have conf-east" — and an audit that assumes the first is how nine
    # people arrive at an occupied room.
    #
    # Sticky, unlike everything else on this row. The rest of the booking is
    # recomputed from the engagement on every sync; the room is a human decision
    # that nothing in the audit record implies, so a sync carries it forward
    # rather than recomputing it away.
    roomEmail: Mapped[str | None] = mapped_column(String)
    roomName: Mapped[str | None] = mapped_column(String)
    # Set once a human chooses a room — or deliberately clears one. Without it,
    # "no room" and "nobody has picked a room yet" are the same value, and the
    # site default would keep coming back to re-book a room somebody removed on
    # purpose.
    roomPinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # NONE | PENDING | ACCEPTED | DECLINED — the room's own answer, refreshed by
    # the maintenance job because Exchange answers asynchronously (a room that
    # has not replied yet is PENDING, which is not the same as having it).
    roomStatus: Mapped[str] = mapped_column(String, nullable=False, default="NONE")

    isOnlineMeeting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    onlineMeetingUrl: Mapped[str | None] = mapped_column(Text)

    # GRAPH | ICS | NONE — which provider actually delivered this row. Recorded
    # per booking rather than read from config at display time, because a tenant
    # that switches to Graph mid-programme still has ICS-delivered bookings whose
    # provenance should not be silently rewritten.
    provider: Mapped[str] = mapped_column(String, nullable=False, default="NONE")
    providerEventId: Mapped[str | None] = mapped_column(String)
    # Graph's idempotency key. Sending the same transactionId twice returns the
    # first event instead of creating a duplicate — which is what makes the retry
    # job safe when a create succeeded but its response never reached us.
    transactionId: Mapped[str | None] = mapped_column(String)

    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING", index=True)
    # Bumped on every material change; the .ics SEQUENCE, which is what tells
    # Outlook an invite is an update to hold rather than a second meeting.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Hash of the delivered payload. A re-sync that finds it unchanged sends
    # nothing — nobody should get a fresh invite because a screen was reloaded.
    contentHash: Mapped[str | None] = mapped_column(String)

    attemptCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lastAttemptAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastSyncedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastError: Mapped[str | None] = mapped_column(Text)

    cancelledAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelReason: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    updatedBy: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        # One booking of each type per engagement. This constraint is the whole
        # duplicate-invite defence: re-running the sync updates a row, it cannot
        # create a second opening meeting.
        UniqueConstraint(
            "engagementKind", "engagementId", "bookingType", name="uq_CalendarBooking_type"
        ),
        Index("ix_CalendarBooking_engagement", "engagementKind", "engagementId"),
        Index("ix_CalendarBooking_status_start", "status", "startAt"),
        Index("ix_CalendarBooking_site", "siteId"),
    )


__all__ = ["CalendarBooking", "BOOKING_TYPES", "BOOKING_STATUSES", "ATTENDEE_ROLES"]
