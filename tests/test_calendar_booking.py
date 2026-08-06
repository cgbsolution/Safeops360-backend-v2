"""CAMS calendar bookings — window arithmetic, attendee diffing, iCalendar output.

These pin the three things that are silently wrong-able and expensive to
discover in production:

  1. The audit window. `scheduledDate` is an instant and `scheduledStartTime` is
     a local "HH:MM" string. Combining them naively moves a 09:00 audit by the
     UTC offset, and nobody notices until an auditor is invited to 03:30.

  2. The attendee diff. The feature's whole incremental promise — auditees named
     a week after the audit was set get booked, and nobody already booked is
     re-invited — is one function, and it has to be idempotent.

  3. iCalendar line folding. An unfolded line over 75 octets is why an invite
     renders as a plain attachment in Outlook instead of Accept/Decline, which
     looks like "the feature does not work" rather than a formatting bug.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services.calendar_booking import _merge_attendees, _meeting_slots, _window
from app.services.calendar_providers import Attendee, EventSpec, build_ics

IST = ZoneInfo("Asia/Kolkata")


def _spec(**kw):
    base = dict(
        subject="Audit: Line 3",
        start=datetime(2026, 8, 12, 9, 0, tzinfo=IST),
        end=datetime(2026, 8, 12, 13, 0, tzinfo=IST),
        timezone="Asia/Kolkata",
        organizer_email="lead@example.com",
        organizer_name="Lead Auditor",
        attendees=[Attendee("a@example.com", "A", True)],
        transaction_id="safeops360-abc",
    )
    base.update(kw)
    return EventSpec(**base)


# ── window arithmetic ────────────────────────────────────────────────


def test_window_uses_the_local_start_time_not_the_instants_clock():
    """The scheduling form sends `new Date("2026-08-12T09:00:00").toISOString()`,
    which for an IST browser is 03:30Z. The booking must still start at 09:00."""
    sched = datetime(2026, 8, 12, 3, 30, tzinfo=timezone.utc)
    start, end = _window(sched.astimezone(IST).date(), "09:00", 4, IST)
    assert (start.hour, start.minute) == (9, 0)
    assert start.date().isoformat() == "2026-08-12"
    assert (end - start).total_seconds() == 4 * 3600


def test_window_falls_back_to_0900_on_an_unparseable_time():
    start, _ = _window(datetime(2026, 8, 12).date(), "not-a-time", 4, IST)
    assert (start.hour, start.minute) == (9, 0)


def test_window_refuses_a_zero_or_negative_duration():
    """A non-positive duration produces an event no calendar client accepts."""
    start, end = _window(datetime(2026, 8, 12).date(), "09:00", -5, IST)
    assert end > start


# ── opening / closing placement ──────────────────────────────────────


def test_meetings_bracket_the_window():
    start = datetime(2026, 8, 12, 9, 0, tzinfo=IST)
    end = datetime(2026, 8, 12, 13, 0, tzinfo=IST)
    (op_s, op_e), (cl_s, cl_e) = _meeting_slots(start, end)
    assert op_s == start and cl_e == end
    assert op_e <= cl_s, "the opening meeting must end before the closing one starts"


def test_short_window_shrinks_the_meetings_rather_than_overlapping_them():
    """A 45-minute inspection cannot give 30 minutes to each meeting. It must
    shrink them, not produce an opening meeting that runs past the closing."""
    start = datetime(2026, 8, 12, 14, 0, tzinfo=IST)
    end = datetime(2026, 8, 12, 14, 45, tzinfo=IST)
    (op_s, op_e), (cl_s, cl_e) = _meeting_slots(start, end)
    assert op_s >= start and cl_e <= end
    assert op_e <= cl_s


# ── attendee diffing ─────────────────────────────────────────────────


def test_new_people_are_added_and_existing_ones_keep_their_original_added_at():
    """`addedAt` is the audit trail for "when was this person's calendar booked".
    Rewriting it on every sync would destroy the only record of it."""
    existing = [
        {"email": "lead@x.com", "role": "LEAD_AUDITOR", "required": True,
         "addedAt": "2026-08-01T00:00:00+00:00"},
    ]
    desired = [
        {"email": "lead@x.com", "role": "LEAD_AUDITOR", "required": True},
        {"email": "auditee@x.com", "role": "AUDITEE", "required": True},
    ]
    merged, added, removed = _merge_attendees(existing, desired)
    assert added == {"auditee@x.com"} and removed == set()
    lead = next(a for a in merged if a["email"] == "lead@x.com")
    assert lead["addedAt"] == "2026-08-01T00:00:00+00:00"


def test_removed_people_are_tombstoned_not_deleted():
    """An invitation that went out and was later withdrawn is a fact about
    someone's calendar, not a mistake to erase."""
    existing = [{"email": "gone@x.com", "role": "CO_AUDITOR", "required": True,
                 "addedAt": "2026-08-01T00:00:00+00:00"}]
    merged, added, removed = _merge_attendees(existing, [])
    assert removed == {"gone@x.com"} and added == set()
    assert merged[0]["removedAt"]


def test_merge_is_idempotent():
    """The re-sync path runs on every team edit and every retry. A merge that
    reported churn on an unchanged cast would re-invite everybody, every time."""
    desired = [{"email": "a@x.com", "role": "AUDITEE", "required": True}]
    once, _, _ = _merge_attendees([], desired)
    twice, added, removed = _merge_attendees(once, desired)
    assert added == set() and removed == set()
    assert [a["addedAt"] for a in once] == [a["addedAt"] for a in twice]


def test_email_matching_is_case_insensitive():
    """Exchange treats addresses case-insensitively; a case change in the
    directory must not read as one person leaving and another joining."""
    existing = [{"email": "Lead@X.com", "role": "LEAD_AUDITOR", "required": True,
                 "addedAt": "2026-08-01T00:00:00+00:00"}]
    _, added, removed = _merge_attendees(
        existing, [{"email": "lead@x.com", "role": "LEAD_AUDITOR", "required": True}]
    )
    assert added == set() and removed == set()


# ── iCalendar output ─────────────────────────────────────────────────


def test_ics_lines_are_folded_to_75_octets():
    """RFC 5545 §3.1. An unfolded line is why Outlook renders an invite as a
    plain attachment with no Accept button."""
    ics = build_ics(_spec(attendees=[
        Attendee("a.very.long.mailbox.name@a-long-corporate-domain.example.com",
                 "A Person With A Rather Long Display Name", True)
    ]))
    assert all(len(line.encode("utf-8")) <= 75 for line in ics.split("\r\n"))


def test_ics_marks_the_time_busy():
    """Without BUSYSTATUS the accepted slot still shows as free in Outlook, which
    is the difference between an invitation and a booking."""
    assert "X-MICROSOFT-CDO-BUSYSTATUS:BUSY" in build_ics(_spec())


def test_ics_escapes_the_separators_that_would_corrupt_the_payload():
    ics = build_ics(_spec(subject="Audit: fire; safety, annual"))
    assert "SUMMARY:Audit: fire\\; safety\\, annual" in ics


def test_cancel_uses_the_cancel_method_and_status():
    ics = build_ics(_spec(), method="CANCEL")
    assert "METHOD:CANCEL" in ics and "STATUS:CANCELLED" in ics


def test_sequence_is_carried_so_an_update_replaces_rather_than_duplicates():
    """A second REQUEST with the same UID and a HIGHER sequence is an update; the
    same sequence is ignored, and a new UID is a second meeting in the diary."""
    assert "SEQUENCE:3" in build_ics(_spec(sequence=3))
    assert "UID:safeops360-abc" in build_ics(_spec(sequence=3))


# ── meeting rooms ────────────────────────────────────────────────────


def _booking(**kw):
    """A CalendarBooking detached from any session — these are pure-logic tests."""
    from app.models.calendar import CalendarBooking

    base = dict(
        id="b1", engagementKind="AUDIT", engagementId="a1", bookingType="OPENING_MEETING",
        subject="Opening meeting", bodyHtml="", location="",
        startAt=datetime.now(timezone.utc) + timedelta(days=30),
        endAt=datetime.now(timezone.utc) + timedelta(days=30, minutes=30),
        timezone="Asia/Kolkata", organizerEmail="lead@x.com", attendees=[],
        isOnlineMeeting=True, provider="GRAPH", status="BOOKED", revision=1,
        attemptCount=0, roomEmail=None, roomName=None, roomStatus="NONE", roomPinned=False,
    )
    base.update(kw)
    return CalendarBooking(**base)


def test_room_is_part_of_the_fingerprint():
    """Changing the room must re-send the invitation — the attendees are being
    told to walk to a different door."""
    from app.services.calendar_booking import _fingerprint

    a = _booking(roomEmail="huddle@x.com")
    b = _booking(roomEmail="conf-east@x.com")
    assert _fingerprint(a) != _fingerprint(b)


def test_room_beyond_the_booking_window_is_deferred():
    """Exchange rooms decline past `BookingWindowInDays` (180 by default).
    Verified against a live tenant: 30 days accepted, 200 days declined."""
    from app.services.calendar_booking import _room_deferred

    near = _booking(roomEmail="huddle@x.com",
                    startAt=datetime.now(timezone.utc) + timedelta(days=30))
    far = _booking(roomEmail="huddle@x.com",
                   startAt=datetime.now(timezone.utc) + timedelta(days=300))
    assert not _room_deferred(near)
    assert _room_deferred(far)


def test_no_room_is_never_deferred():
    """Deferral is about rooms only — a roomless booking must go out now."""
    from app.services.calendar_booking import _room_deferred

    assert not _room_deferred(
        _booking(startAt=datetime.now(timezone.utc) + timedelta(days=300))
    )


def test_only_meetings_take_the_site_default_room():
    """The fieldwork block is walked, not sat in. Holding a room for a whole
    audit day would take it out of circulation for nothing."""
    from app.services.calendar_booking import _default_room

    site = ("huddle@x.com", "huddle")
    assert _default_room("OPENING_MEETING", site) == site
    assert _default_room("CLOSING_MEETING", site) == site
    assert _default_room("AUDIT_BLOCK", site) == (None, None)


def test_ics_sends_the_room_as_a_room_cutype():
    """CUTYPE=ROOM is what lets a resource mailbox treat an emailed invitation
    as a booking request rather than filing it as correspondence."""
    ics = build_ics(_spec(room_email="huddle@x.com", room_name="Huddle"))
    assert "CUTYPE=ROOM" in ics
    assert "LOCATION:Huddle" in ics


def test_graph_sends_the_room_as_a_resource_attendee():
    """`resource`, not `required` — the type is what makes Exchange's booking
    assistant answer for the room instead of it being a silent mailbox."""
    from app.services.calendar_providers import GraphCalendarProvider

    body = GraphCalendarProvider()._event_body(
        _spec(room_email="huddle@x.com", room_name="Huddle")
    )
    types = [a["type"] for a in body["attendees"]]
    assert "resource" in types
    assert body["location"]["locationEmailAddress"] == "huddle@x.com"
    assert body["location"]["locationType"] == "conferenceRoom"
