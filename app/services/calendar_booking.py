"""Calendar booking service — turning a scheduled audit into held time.

The rule the module is built around: **the audit record is the source of truth,
the calendar is a projection of it.** Every entry point does the same thing —
recompute what the bookings SHOULD be from the engagement as it now stands,
diff that against what was last delivered, and send only the difference. There
is no "add attendee" call and no "reschedule" call, because either would be a
second place that decides what is true.

That single rule is what makes the incremental case work without any extra
machinery. When an audit is created with a lead auditor alone, the desired
attendee list is one person and one invite goes out. When auditees and
co-auditors are named a week later — which is when they are actually known,
see `update_audit_team` — the recomputed list is longer, the diff is the new
people, and they receive the invite while nobody already booked is re-invited.

What gets booked, per engagement:

  AUDIT_BLOCK      the fieldwork window. Auditors required, auditees optional —
                   an auditee is not sitting in the audit for eight hours, but
                   their day should show it is happening.
  OPENING_MEETING  ISO 19011 §6.4.2, at the head of the window. Everyone required.
  CLOSING_MEETING  ISO 19011 §6.4.9, at the foot of it. Everyone required.

Sync is best-effort and never raises into its caller. An audit that could not be
created because Exchange was unreachable would be a worse product than one whose
invites arrive four minutes late via the retry job.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.calendar import CalendarBooking
from app.models.user import User
from app.services import calendar_providers as providers

log = logging.getLogger("safeops360.calendar")

AUDIT_BLOCK = "AUDIT_BLOCK"
OPENING_MEETING = "OPENING_MEETING"
CLOSING_MEETING = "CLOSING_MEETING"

# Statuses at which an engagement's time no longer needs holding. Reaching one
# releases the bookings — a cancelled audit that leaves nine calendars blocked
# is the single most irritating way this feature could fail.
_AUDIT_DEAD_STATUSES = {"cancelled"}
_AUDIT_DONE_STATUSES = {"closed"}
_ENGAGEMENT_DEAD_STATUSES = {"CANCELLED"}
_ENGAGEMENT_DONE_STATUSES = {"CLOSED"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().calendar_default_timezone)
    except Exception:  # noqa: BLE001
        return ZoneInfo("Asia/Kolkata")


# ─────────────────────────────────────────────────────────────────────
# Desired state — what SHOULD be on the calendar
# ─────────────────────────────────────────────────────────────────────


class _Spec:
    """The desired shape of one booking, before it meets the database."""

    def __init__(
        self,
        booking_type: str,
        subject: str,
        start: datetime,
        end: datetime,
        attendees: list[dict[str, Any]],
        body_html: str,
        location: str,
    ) -> None:
        self.booking_type = booking_type
        self.subject = subject
        self.start = start
        self.end = end
        self.attendees = attendees
        self.body_html = body_html
        self.location = location


def _window(
    day: date, start_hhmm: str, duration_hours: float, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    """Compose the audit window from the date, the local start time and the
    estimated duration.

    `scheduledStartTime` is a "HH:MM" string and `scheduledDate` is an instant;
    the DATE is taken from the instant *in the site's zone* and the TIME from the
    string, because that is how the scheduling form collects them. Treating the
    instant's own clock time as the start would silently move a 09:00 audit to
    whatever the browser's timezone offset made of it.
    """
    try:
        hh, mm = (int(p) for p in (start_hhmm or "09:00").split(":")[:2])
    except Exception:  # noqa: BLE001
        hh, mm = 9, 0
    hh = min(max(hh, 0), 23)
    mm = min(max(mm, 0), 59)
    start = datetime.combine(day, time(hh, mm), tzinfo=tz)
    hours = float(duration_hours or 2) or 2.0
    # A zero or negative duration would produce an event no client will accept.
    hours = min(max(hours, 0.25), 24 * 14)
    return start, start + timedelta(hours=hours)


def _meeting_slots(
    start: datetime, end: datetime
) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
    """Opening at the head of the window, closing at the foot.

    Short windows are the interesting case: a two-hour inspection cannot give
    thirty minutes to each meeting and still be an inspection, so both shrink to
    a quarter of the window rather than overlapping each other or spilling past
    the end. The invariant that matters is opening-ends ≤ closing-starts.
    """
    s = get_settings()
    total = (end - start).total_seconds() / 60.0
    open_m = float(s.calendar_opening_meeting_minutes or 30)
    close_m = float(s.calendar_closing_meeting_minutes or 30)
    if open_m + close_m > total:
        open_m = close_m = max(total / 4.0, 5.0)
    opening = (start, start + timedelta(minutes=open_m))
    closing = (end - timedelta(minutes=close_m), end)
    if opening[1] > closing[0]:
        mid = start + (end - start) / 2
        opening = (start, mid)
        closing = (mid, end)
    return opening, closing


async def _emails(db: AsyncSession, ids: Iterable[str | None]) -> dict[str, User]:
    clean = {i for i in ids if i}
    if not clean:
        return {}
    rows = (await db.execute(select(User).where(User.id.in_(list(clean))))).scalars().all()
    return {u.id: u for u in rows}


def _attendee(u: User | None, user_id: str | None, role: str, required: bool) -> dict[str, Any] | None:
    """One attendee entry, or None when the person cannot be reached.

    A seat filled by someone with no email is dropped from the invite and stays
    visible in the audit team — reporting them as invited would be a lie the
    screen would then repeat.
    """
    if u is None or not (u.email or "").strip():
        return None
    return {
        "userId": user_id,
        "email": u.email.strip(),
        "name": u.name or u.email,
        "role": role,
        "required": required,
    }


def _body(
    *,
    heading: str,
    reference: str,
    title: str,
    site: str,
    scope: str,
    purpose: str,
    link_path: str,
) -> str:
    # The link is a path, not an absolute URL. The backend has no reliable idea
    # of the host the user reaches the web app on (proxy, on-prem hostname,
    # Vercel preview), and a confidently wrong absolute URL in a meeting invite
    # is worse than a path someone can paste after their own base address.
    return (
        f"<p><b>{heading}</b></p>"
        f"<p><b>{reference}</b> &middot; {title}</p>"
        f"<p><b>Site:</b> {site or '—'}<br/>"
        f"<b>Scope:</b> {scope or '—'}</p>"
        f"<p>{purpose}</p>"
        f"<p style='color:#64748b;font-size:12px'>Open in SafeOps360: {link_path}<br/>"
        "This invitation is maintained by SafeOps360 &mdash; changes to the audit team or "
        "schedule update it automatically. Please do not reschedule from your calendar.</p>"
    )


async def _specs_for_audit(db: AsyncSession, audit) -> list[_Spec]:
    """Desired bookings for a ComplianceAudit."""
    tz = _tz()
    sched = audit.scheduledDate
    if sched is None:
        return []
    if sched.tzinfo is None:
        sched = sched.replace(tzinfo=timezone.utc)
    day = sched.astimezone(tz).date()
    start, end = _window(day, audit.scheduledStartTime, audit.estimatedDurationHours, tz)

    co_ids = [c.get("userId") for c in (audit.coAuditors or []) if isinstance(c, dict) and c.get("userId")]
    au_ids = [a.get("userId") for a in (audit.auditees or []) if isinstance(a, dict) and a.get("userId")]
    users = await _emails(db, [audit.leadAuditorUserId, audit.plantManagerUserId, *co_ids, *au_ids])

    def cast(auditees_required: bool) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for uid, role, req in [
            (audit.leadAuditorUserId, "LEAD_AUDITOR", True),
            *[(c, "CO_AUDITOR", True) for c in co_ids],
            (audit.plantManagerUserId, "PLANT_MANAGER", auditees_required),
            *[(a, "AUDITEE", auditees_required) for a in au_ids],
        ]:
            ent = _attendee(users.get(uid) if uid else None, uid, role, req)
            if ent and not any(e["email"].lower() == ent["email"].lower() for e in out):
                out.append(ent)
        return out

    # The supplier's own contact, for a vendor audit. They hold no platform seat
    # (WP-45's lightweight-participation gap), so they are addressed by the email
    # captured on the link — the only way they learn when the audit is.
    supplier_contact: dict[str, Any] | None = None
    try:
        from app.models.cams_completion import SupplierAuditLink

        link = (
            await db.execute(
                select(SupplierAuditLink).where(
                    SupplierAuditLink.engagementKind == "AUDIT",
                    SupplierAuditLink.engagementId == audit.id,
                )
            )
        ).scalars().first()
        if link and (link.supplierContactEmail or "").strip():
            supplier_contact = {
                "userId": None,
                "email": link.supplierContactEmail.strip(),
                "name": link.supplierContactName or link.supplierContactEmail,
                "role": "SUPPLIER_CONTACT",
                "required": True,
            }
    except Exception as e:  # noqa: BLE001
        log.debug("supplier contact lookup skipped: %s", e)

    site = getattr(audit, "plantName", None) or audit.plantId or ""
    scope = audit.scopeDescription or ", ".join(audit.scopeDepartments or []) or "Full scope"
    ref = audit.auditNumber
    link_path = f"/cams/audits/{audit.id}"
    location = f"{site} — on site"

    block_cast = cast(auditees_required=False)
    meeting_cast = cast(auditees_required=True)
    if supplier_contact:
        block_cast = [*block_cast, supplier_contact]
        meeting_cast = [*meeting_cast, supplier_contact]

    (op_s, op_e), (cl_s, cl_e) = _meeting_slots(start, end)
    return [
        _Spec(
            AUDIT_BLOCK,
            f"Audit: {audit.title} ({ref})",
            start,
            end,
            block_cast,
            _body(
                heading="Audit fieldwork — please keep this time free",
                reference=ref,
                title=audit.title,
                site=site,
                scope=scope,
                purpose=(
                    "This block reserves the audit window. Auditors are required for the "
                    "full period; auditees are invited as optional so the time shows on "
                    "their calendar and they can be reached during fieldwork."
                ),
                link_path=link_path,
            ),
            location,
        ),
        _Spec(
            OPENING_MEETING,
            f"Opening meeting: {audit.title} ({ref})",
            op_s,
            op_e,
            meeting_cast,
            _body(
                heading="Opening meeting — ISO 19011 §6.4.2",
                reference=ref,
                title=audit.title,
                site=site,
                scope=scope,
                purpose=(
                    "Confirm the audit scope, criteria and plan, introduce the audit team, "
                    "and identify the department owners who will respond to findings."
                ),
                link_path=link_path,
            ),
            location,
        ),
        _Spec(
            CLOSING_MEETING,
            f"Closing meeting: {audit.title} ({ref})",
            cl_s,
            cl_e,
            meeting_cast,
            _body(
                heading="Closing meeting — ISO 19011 §6.4.9",
                reference=ref,
                title=audit.title,
                site=site,
                scope=scope,
                purpose=(
                    "Present the findings and conclusions, agree corrective-action ownership "
                    "and due dates, and record the auditee's acknowledgement."
                ),
                link_path=link_path,
            ),
            location,
        ),
    ]


async def _specs_for_engagement(db: AsyncSession, eng) -> list[_Spec]:
    """Desired bookings for a CamsEngagement (inspections and the CAMS engine).

    The engine carries explicit `scheduledStart`/`scheduledEnd` when it has them
    and only a `plannedDate` when it does not — a planned date with no time is a
    date, not an appointment, so it is given the same default start and a
    two-hour window rather than being silently dropped.
    """
    tz = _tz()
    start = eng.scheduledStart
    end = eng.scheduledEnd
    if start is not None and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if start is None:
        planned = eng.plannedDate
        if planned is None:
            return []
        if planned.tzinfo is None:
            planned = planned.replace(tzinfo=timezone.utc)
        start, end = _window(planned.astimezone(tz).date(), "09:00", 2, tz)
    if end is None or end <= start:
        end = start + timedelta(hours=2)

    team_ids = [t for t in (eng.auditTeamIds or []) if isinstance(t, str)]
    users = await _emails(db, [eng.leadAuditorId, eng.auditeeOwnerId, *team_ids])

    def cast(auditee_required: bool) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for uid, role, req in [
            (eng.leadAuditorId, "LEAD_AUDITOR", True),
            *[(t, "CO_AUDITOR", True) for t in team_ids],
            (eng.auditeeOwnerId, "AUDITEE", auditee_required),
        ]:
            ent = _attendee(users.get(uid) if uid else None, uid, role, req)
            if ent and not any(e["email"].lower() == ent["email"].lower() for e in out):
                out.append(ent)
        return out

    site = eng.siteId or ""
    scope = eng.scopeStatement or eng.areaOrAssetRef or "Full scope"
    ref = eng.engagementCode
    link_path = f"/cams/engagements/{eng.id}"
    location = f"{site} — {eng.areaOrAssetRef}" if eng.areaOrAssetRef else site

    (op_s, op_e), (cl_s, cl_e) = _meeting_slots(start, end)
    return [
        _Spec(
            AUDIT_BLOCK,
            f"{eng.engagementType.replace('_', ' ').title()}: {eng.title} ({ref})",
            start,
            end,
            cast(auditee_required=False),
            _body(
                heading="Engagement fieldwork — please keep this time free",
                reference=ref,
                title=eng.title,
                site=site,
                scope=scope,
                purpose="This block reserves the fieldwork window for this engagement.",
                link_path=link_path,
            ),
            location,
        ),
        _Spec(
            OPENING_MEETING,
            f"Opening meeting: {eng.title} ({ref})",
            op_s,
            op_e,
            cast(auditee_required=True),
            _body(
                heading="Opening meeting — ISO 19011 §6.4.2",
                reference=ref,
                title=eng.title,
                site=site,
                scope=scope,
                purpose="Confirm scope, criteria and plan, and introduce the team.",
                link_path=link_path,
            ),
            location,
        ),
        _Spec(
            CLOSING_MEETING,
            f"Closing meeting: {eng.title} ({ref})",
            cl_s,
            cl_e,
            cast(auditee_required=True),
            _body(
                heading="Closing meeting — ISO 19011 §6.4.9",
                reference=ref,
                title=eng.title,
                site=site,
                scope=scope,
                purpose="Present findings and conclusions and agree corrective actions.",
                link_path=link_path,
            ),
            location,
        ),
    ]


# ─────────────────────────────────────────────────────────────────────
# Sync
# ─────────────────────────────────────────────────────────────────────


async def _site_room(db: AsyncSession, site_id: str | None) -> tuple[str | None, str | None]:
    """The site's default meeting room, if one is configured.

    Read per sync rather than cached: a site that gains a room should have its
    next audit book it, and this is one indexed lookup against a table already
    in the session's identity map most of the time.
    """
    if not site_id:
        return None, None
    try:
        from app.models.plant import Plant

        p = await db.get(Plant, site_id)
        if p is None:
            return None, None
        return p.defaultMeetingRoomEmail, p.defaultMeetingRoomName
    except Exception as e:  # noqa: BLE001
        # The columns may not be applied yet on this deployment. A missing room
        # is a missing room, not a reason to stop booking calendars.
        log.debug("site room lookup skipped: %s", e)
        return None, None


def _default_room(
    booking_type: str, site_room: tuple[str | None, str | None]
) -> tuple[str | None, str | None]:
    """Which bookings get the site default.

    The two MEETINGS do; the fieldwork block does not. An audit is walked — the
    auditors are on the floor, not sitting in a conference room — so holding a
    room for the whole window would take it out of circulation for a day to no
    purpose. A block CAN still be given a room explicitly, which pins it.
    """
    if booking_type == AUDIT_BLOCK:
        return None, None
    return site_room


def _room_deferred(b: CalendarBooking) -> bool:
    """Is this booking too far out for a room mailbox to accept yet?

    Exchange rooms decline anything beyond `BookingWindowInDays` (default 180).
    Sending the request anyway earns a DECLINED that reads exactly like "the
    room is taken" and is nothing of the sort — so the room is held back and
    attached later, which is the only behaviour that lets an annual programme
    end up with rooms at all.
    """
    if not b.roomEmail:
        return False
    days = get_settings().calendar_room_booking_window_days or 0
    if days <= 0:
        return False
    start = b.startAt if b.startAt.tzinfo else b.startAt.replace(tzinfo=timezone.utc)
    return start > _utcnow() + timedelta(days=days)


def _fingerprint(b: CalendarBooking) -> str:
    """What a participant would notice. Only these fields justify re-inviting.

    `revision`, `attemptCount` and every timestamp are excluded on purpose — a
    retry must not look like a change, or every failed attempt would re-invite
    everyone who was already booked.
    """
    payload = {
        "subject": b.subject,
        "start": b.startAt.isoformat() if b.startAt else None,
        "end": b.endAt.isoformat() if b.endAt else None,
        "location": b.location,
        "body": b.bodyHtml,
        "organizer": (b.organizerEmail or "").lower(),
        # Changing the room is a change the participants must be told about —
        # they are walking to a different door.
        "room": (b.roomEmail or "").lower(),
        "attendees": sorted(
            f"{a.get('email','').lower()}:{'R' if a.get('required') else 'O'}"
            for a in (b.attendees or [])
            if a.get("email") and not a.get("removedAt")
        ),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]


def _merge_attendees(
    existing: list[dict], desired: list[dict]
) -> tuple[list[dict], set[str], set[str]]:
    """Desired list wins, but `addedAt` on someone already booked is preserved.

    That preservation is the whole audit trail for "when was this person's
    calendar booked" — the question the client asked for when they described
    auditees being added after the audit was set. Removed people are kept as
    tombstones with `removedAt` for the same reason: an invite that went out and
    was later withdrawn is a fact about someone's calendar, not a mistake to
    erase.

    Returns the merged list plus the SETS of addresses added and removed — not
    counts. The caller unions them across an engagement's three bookings, and a
    count would make one person joining an audit read as "3 invitations sent".
    """
    now = _utcnow().isoformat()
    by_email = {
        (a.get("email") or "").lower(): a for a in (existing or []) if a.get("email")
    }
    out: list[dict] = []
    added: set[str] = set()
    desired_emails = set()
    for d in desired:
        key = (d.get("email") or "").lower()
        desired_emails.add(key)
        prev = by_email.get(key)
        if prev and not prev.get("removedAt"):
            merged = {**prev, **d, "addedAt": prev.get("addedAt") or now}
            merged.pop("removedAt", None)
        else:
            merged = {**d, "addedAt": now}
            added.add(key)
        out.append(merged)
    removed: set[str] = set()
    for key, prev in by_email.items():
        if key in desired_emails or prev.get("removedAt"):
            continue
        out.append({**prev, "removedAt": now})
        removed.add(key)
    return out, added, removed


def _organizer(attendees: list[dict[str, Any]]) -> tuple[str | None, str | None, str | None]:
    """(userId, email, name) of whoever owns the meeting.

    The lead auditor organises their own audit; the configured service mailbox
    stands in when the lead has no routable address. With neither, there is no
    mailbox to write into and the booking is skipped rather than guessed at.
    """
    lead = next((a for a in attendees if a.get("role") == "LEAD_AUDITOR"), None)
    if lead and lead.get("email"):
        return lead.get("userId"), lead["email"], lead.get("name")
    fallback = (get_settings().calendar_organizer_email or "").strip()
    if fallback:
        return None, fallback, "SafeOps360"
    first = next((a for a in attendees if a.get("email")), None)
    if first:
        return first.get("userId"), first["email"], first.get("name")
    return None, None, None


def _to_event_spec(b: CalendarBooking) -> providers.EventSpec:
    return providers.EventSpec(
        subject=b.subject,
        start=b.startAt if b.startAt.tzinfo else b.startAt.replace(tzinfo=timezone.utc),
        end=b.endAt if b.endAt.tzinfo else b.endAt.replace(tzinfo=timezone.utc),
        timezone=b.timezone,
        organizer_email=b.organizerEmail or "",
        organizer_name="",
        attendees=[
            providers.Attendee(
                email=a["email"],
                name=a.get("name") or a["email"],
                required=bool(a.get("required", True)),
                userId=a.get("userId"),
                role=a.get("role", "OTHER"),
            )
            for a in (b.attendees or [])
            if a.get("email") and not a.get("removedAt")
        ],
        body_html=b.bodyHtml,
        location=b.location,
        online_meeting=b.isOnlineMeeting,
        room_email=b.roomEmail,
        room_name=b.roomName,
        transaction_id=b.transactionId or b.id,
        sequence=b.revision,
    )


async def _deliver(b: CalendarBooking, *, force: bool = False) -> str:
    """Push one booking to the provider and record the outcome. Never raises.

    Touches no database — it awaits the provider and mutates the ORM object,
    nothing more. That is what makes it safe to run several of these
    concurrently on one session, which is how a three-booking sync costs one
    network round trip instead of three.
    """
    settings = get_settings()
    fp = _fingerprint(b)
    if not force and b.status == "BOOKED" and b.contentHash == fp:
        return "unchanged"
    if not b.organizerEmail:
        b.status = "SKIPPED"
        b.lastError = "No organiser mailbox — the lead auditor has no email address and no fallback mailbox is configured"
        b.lastAttemptAt = _utcnow()
        return "skipped"
    if b.attemptCount >= settings.calendar_max_attempts and b.status == "FAILED" and not force:
        return "exhausted"

    provider = providers.resolve_provider()
    spec = _to_event_spec(b)
    # Beyond the room's booking window the request would be declined for a
    # reason nobody can act on, so the event goes out WITHOUT the room and the
    # maintenance job attaches it once the date is close enough.
    deferred_room = _room_deferred(b)
    if deferred_room:
        spec.room_email = None
        spec.room_name = None
    if not spec.attendees:
        b.status = "SKIPPED"
        b.provider = provider.name
        b.lastError = "No participant has an email address on file"
        b.lastAttemptAt = _utcnow()
        b.contentHash = fp
        return "skipped"

    # A content change is a new revision, and the ICS SEQUENCE has to rise for
    # Outlook to treat the second invite as an update to the first.
    if b.contentHash != fp:
        b.revision += 1
        spec.sequence = b.revision
    b.attemptCount += 1
    b.lastAttemptAt = _utcnow()

    # ICS-delivered bookings have no server-side event to PATCH — re-sending the
    # REQUEST with a higher SEQUENCE *is* the update. Only Graph can update.
    reuse_id = b.providerEventId if (b.providerEventId or "").strip() else None
    graph_update = provider.name == "GRAPH" and reuse_id and not reuse_id.startswith("ics:")
    res = (
        await provider.update(spec, reuse_id)
        if graph_update
        else await provider.create(spec)
    )

    b.provider = res.provider
    if res.ok and res.skipped:
        b.status = "SKIPPED"
        b.lastError = res.error
        b.contentHash = fp
        return "skipped"
    if res.ok:
        b.status = "BOOKED"
        if not b.roomEmail:
            b.roomStatus = "NONE"
        elif deferred_room:
            b.roomStatus = "DEFERRED"
        else:
            b.roomStatus = res.room_status
        b.providerEventId = res.event_id or b.providerEventId
        if res.join_url:
            b.onlineMeetingUrl = res.join_url
        b.lastSyncedAt = _utcnow()
        b.lastError = None
        b.contentHash = fp
        return "booked"
    # Permanent rejections stop immediately; transient ones stay PENDING for the
    # retry job until the attempt budget is spent.
    exhausted = (not res.retryable) or b.attemptCount >= settings.calendar_max_attempts
    b.status = "FAILED" if exhausted else "PENDING"
    b.lastError = res.error
    log.warning("calendar booking %s %s: %s", b.id, b.status, res.error)
    return "failed"


class _Savepoint:
    """Run best-effort calendar work inside a SAVEPOINT.

    Catching the exception is NOT sufficient on PostgreSQL. A failed statement
    aborts the whole transaction, so every later statement raises
    `InFailedSqlTransaction` — meaning a swallowed calendar error would still
    take down the audit creation that called us. Verified against the live
    database: with `CalendarBooking` absent, a caught error left the session
    unusable and the next query failed.

    A savepoint scopes the damage: rolling back to it leaves the enclosing
    transaction — the one that is actually creating the audit — intact.

    This is what makes "best-effort" true rather than merely intended, and it is
    what lets this feature be deployed before its migration has run.
    """

    def __init__(self, db: AsyncSession, label: str) -> None:
        self._db = db
        self._label = label
        self._sp = None
        self.error: str | None = None

    async def __aenter__(self):
        self._sp = await self._db.begin_nested()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._sp is None:
            return False
        if exc_type is None:
            await self._sp.commit()
            return False
        try:
            await self._sp.rollback()
        except Exception as e:  # noqa: BLE001
            log.debug("savepoint rollback failed (%s): %s", self._label, e)
        self.error = str(exc)[:400]
        log.warning("calendar %s failed, rolled back to savepoint: %s", self._label, exc)
        return True  # handled — the caller's transaction continues


async def _sync_engagement_inner(
    db: AsyncSession,
    *,
    engagement_kind: str,
    engagement_id: str,
    actor_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Recompute and deliver every booking for one engagement.

    Idempotent by construction — the same engagement state produces the same
    fingerprint, and an unchanged fingerprint sends nothing. Safe to call from
    creation, from a team edit, from a retry job and from a user pressing a
    button, which is precisely why all four do call it.

    Never raises: returns `{"ok": False, "error": ...}` instead. Callers run
    inside the transaction that created the audit and must not be rolled back
    because a mail server timed out.
    """
    kind = (engagement_kind or "").upper()
    try:
        settings = get_settings()
        if not settings.calendar_bookings_enabled:
            return {"ok": True, "skipped": True, "reason": "Calendar bookings are disabled"}

        subject, site_id, cancelled, finished = await _load_subject(db, kind, engagement_id)
        if subject is None:
            return {"ok": False, "error": "Engagement not found"}

        if cancelled:
            return await _cancel_engagement_inner(
                db,
                engagement_kind=kind,
                engagement_id=engagement_id,
                reason="The audit engagement was cancelled.",
                actor_id=actor_id,
            )
        if finished:
            # A closed audit keeps its history. Future time is released — nobody
            # should still be blocked for fieldwork that already concluded — but
            # a block that has already happened stays as the record of it.
            return await _release_future(db, kind, engagement_id, actor_id)

        specs = (
            await _specs_for_audit(db, subject)
            if kind == "AUDIT"
            else await _specs_for_engagement(db, subject)
        )
        if not specs:
            return {"ok": True, "skipped": True, "reason": "The engagement has no scheduled date"}

        existing = {
            b.bookingType: b
            for b in (
                await db.execute(
                    select(CalendarBooking).where(
                        CalendarBooking.engagementKind == kind,
                        CalendarBooking.engagementId == engagement_id,
                    )
                )
            ).scalars().all()
        }

        site_room = await _site_room(db, site_id)
        tzname = settings.calendar_default_timezone
        # Unions across the three bookings, so a person who joins the audit is
        # counted once rather than once per meeting they were added to.
        attendees_added: set[str] = set()
        attendees_removed: set[str] = set()
        pending: list[CalendarBooking] = []
        for spec in specs:
            b = existing.get(spec.booking_type)
            if b is None:
                b = CalendarBooking(
                    engagementKind=kind,
                    engagementId=engagement_id,
                    bookingType=spec.booking_type,
                    siteId=site_id,
                    subject=spec.subject,
                    createdBy=actor_id,
                    startAt=spec.start,
                    endAt=spec.end,
                    timezone=tzname,
                    attendees=[],
                    isOnlineMeeting=settings.calendar_online_meetings,
                )
                db.add(b)
                await db.flush()  # need the id to mint a stable transactionId
                b.transactionId = f"safeops360-{b.id}"
            elif b.status == "CANCELLED":
                # A cancelled booking coming back to life (audit reopened, or an
                # accidental cancel) starts a fresh attempt budget rather than
                # inheriting the exhausted one that stopped it.
                b.status = "PENDING"
                b.attemptCount = 0
                b.cancelledAt = None
                b.cancelReason = None

            b.subject = spec.subject
            b.startAt = spec.start
            b.endAt = spec.end
            b.timezone = tzname
            b.bodyHtml = spec.body_html
            b.location = spec.location
            b.siteId = site_id
            merged, added, removed = _merge_attendees(list(b.attendees or []), spec.attendees)
            b.attendees = merged
            attendees_added |= added
            attendees_removed |= removed
            org_id, org_email, _org_name = _organizer(spec.attendees)
            b.organizerUserId = org_id
            b.organizerEmail = org_email
            # Room. Sticky once a human has touched it — everything else on this
            # row is recomputed from the audit, but nothing in the audit record
            # implies which room, so a re-sync must not overwrite the choice.
            if not b.roomPinned:
                room_email, room_name = _default_room(spec.booking_type, site_room)
                b.roomEmail, b.roomName = room_email, room_name
            if not b.roomEmail:
                b.roomStatus = "NONE"
            b.updatedBy = actor_id
            pending.append(b)

        # All three deliveries at once. The session is inside the transaction
        # that created the audit, so the wall-clock cost of this call is time a
        # Postgres transaction stays open — one round trip, not three.
        outcomes = await asyncio.gather(
            *(_deliver(b, force=force) for b in pending), return_exceptions=True
        )
        results: dict[str, str] = {}
        for b, outcome in zip(pending, outcomes):
            if isinstance(outcome, BaseException):
                # A provider that raised despite its contract must still leave a
                # retryable row rather than a half-written one.
                log.warning("calendar delivery raised for %s: %s", b.id, outcome)
                b.status = "PENDING" if b.status != "BOOKED" else b.status
                b.lastError = str(outcome)[:400]
                results[b.bookingType] = "failed"
            else:
                results[b.bookingType] = outcome

        await db.flush()
        return {
            "ok": True,
            "engagementKind": kind,
            "engagementId": engagement_id,
            "provider": providers.resolve_provider().name,
            "results": results,
            "attendeesAdded": len(attendees_added),
            "attendeesRemoved": len(attendees_removed),
        }
    except Exception:  # noqa: BLE001
        # Deliberately re-raised, not swallowed. The savepoint wrapper above is
        # what makes this best-effort: it must SEE the exception to roll back to
        # the savepoint. Returning a value here would leave the transaction
        # aborted and the wrapper would then fail trying to release a savepoint
        # inside it — which is exactly the bug this replaced.
        raise


async def _load_subject(
    db: AsyncSession, kind: str, engagement_id: str
) -> tuple[Any, str | None, bool, bool]:
    """(row, siteId, is_cancelled, is_finished) for either engine."""
    if kind == "AUDIT":
        from app.models.audit_compliance import ComplianceAudit

        a = await db.get(ComplianceAudit, engagement_id)
        if a is None or a.isDeleted:
            return None, None, False, False
        return a, a.plantId, a.status in _AUDIT_DEAD_STATUSES, a.status in _AUDIT_DONE_STATUSES
    if kind == "INSPECTION":
        from app.models.cams import CamsEngagement

        e = await db.get(CamsEngagement, engagement_id)
        if e is None or e.isDeleted:
            return None, None, False, False
        return (
            e,
            e.siteId,
            e.status in _ENGAGEMENT_DEAD_STATUSES,
            e.status in _ENGAGEMENT_DONE_STATUSES,
        )
    return None, None, False, False


async def _release_future(
    db: AsyncSession, kind: str, engagement_id: str, actor_id: str | None
) -> dict[str, Any]:
    """Cancel only the bookings that have not started yet."""
    now = _utcnow()
    rows = (
        await db.execute(
            select(CalendarBooking).where(
                CalendarBooking.engagementKind == kind,
                CalendarBooking.engagementId == engagement_id,
            )
        )
    ).scalars().all()
    released = 0
    for b in rows:
        start = b.startAt if b.startAt.tzinfo else b.startAt.replace(tzinfo=timezone.utc)
        if start <= now or b.status in ("CANCELLED", "SKIPPED"):
            continue
        await _cancel_one(b, "The audit was closed, so this time is released.", actor_id)
        released += 1
    await db.flush()
    return {"ok": True, "closed": True, "released": released}


async def _cancel_one(b: CalendarBooking, reason: str, actor_id: str | None) -> bool:
    provider = providers.resolve_provider()
    spec = _to_event_spec(b)
    ok = True
    if b.status == "BOOKED" and spec.attendees and b.organizerEmail:
        res = await provider.cancel(spec, b.providerEventId, reason)
        ok = res.ok
        if not ok:
            b.lastError = res.error
    b.status = "CANCELLED"
    b.cancelledAt = _utcnow()
    b.cancelReason = reason
    b.updatedBy = actor_id
    if ok:
        b.lastError = None
        b.lastSyncedAt = _utcnow()
    return ok


async def _cancel_engagement_inner(
    db: AsyncSession,
    *,
    engagement_kind: str,
    engagement_id: str,
    reason: str = "",
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Withdraw every booking for an engagement. Never raises."""
    kind = (engagement_kind or "").upper()
    try:
        rows = (
            await db.execute(
                select(CalendarBooking).where(
                    CalendarBooking.engagementKind == kind,
                    CalendarBooking.engagementId == engagement_id,
                    CalendarBooking.status != "CANCELLED",
                )
            )
        ).scalars().all()
        cancelled = 0
        for b in rows:
            if await _cancel_one(b, reason or "This audit engagement was cancelled.", actor_id):
                cancelled += 1
        await db.flush()
        return {"ok": True, "cancelled": cancelled, "total": len(rows)}
    except Exception:  # noqa: BLE001
        raise  # see _sync_engagement_inner — the savepoint must see it


async def cancel_booking(
    db: AsyncSession, *, booking_id: str, reason: str = "", actor_id: str | None = None
) -> dict[str, Any]:
    b = await db.get(CalendarBooking, booking_id)
    if b is None:
        raise ValueError("Booking not found")
    if b.status == "CANCELLED":
        return {"ok": True, "alreadyCancelled": True}
    ok = await _cancel_one(b, reason or "This meeting was cancelled in SafeOps360.", actor_id)
    await db.flush()
    return {"ok": ok, "status": b.status, "error": b.lastError}


async def set_room(
    db: AsyncSession,
    *,
    booking_id: str,
    room_email: str | None,
    room_name: str | None = None,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Hold a meeting room for one booking, or release it.

    Pins the choice either way. Passing `None` is not "fall back to the site
    default" — it is "this meeting has no room", and a later sync must respect
    that rather than helpfully re-booking the room somebody just removed.

    The room is delivered by re-sending the event with the resource attendee
    attached; Exchange's booking assistant then accepts or declines on the
    room's own policy, which is why the answer comes back as PENDING and is
    settled later.
    """
    b = await db.get(CalendarBooking, booking_id)
    if b is None:
        raise ValueError("Booking not found")
    if b.status == "CANCELLED":
        raise ValueError("This booking was cancelled; re-sync the engagement to recreate it")

    email = (room_email or "").strip() or None
    if email and "@" not in email:
        raise ValueError("A meeting room must be an Exchange resource mailbox address")

    b.roomEmail = email
    b.roomName = (room_name or "").strip() or email
    b.roomPinned = True
    b.roomStatus = "PENDING" if email else "NONE"
    b.updatedBy = actor_id
    # A room change is a real change for the attendees — they walk to a
    # different door — so the attempt budget starts again.
    b.attemptCount = 0
    if b.status == "FAILED":
        b.status = "PENDING"
    outcome = await _deliver(b)
    await db.flush()
    return {
        "ok": b.status in ("BOOKED", "SKIPPED"),
        "outcome": outcome,
        "status": b.status,
        "roomEmail": b.roomEmail,
        "roomName": b.roomName,
        "roomStatus": b.roomStatus,
        "error": b.lastError,
    }


async def list_rooms(db: AsyncSession) -> dict[str, Any]:
    """Bookable rooms from the directory, for the picker.

    Returns `rooms: []` with a reason rather than erroring — a tenant that has
    not granted `Place.Read.All` can still book a room by typing its address,
    and a picker that 500s would hide that.
    """
    provider = providers.resolve_provider()
    rooms, err = await provider.list_rooms()
    return {
        "rooms": rooms,
        "total": len(rooms),
        "provider": provider.name,
        "error": err,
        "statement": (
            f"{len(rooms)} room(s) available"
            if rooms
            else err or "No rooms are published in the directory."
        ),
    }


async def reschedule_booking(
    db: AsyncSession,
    *,
    booking_id: str,
    start: datetime,
    end: datetime,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Move ONE booking — the opening meeting that has to start at 08:00 because
    the plant head flies out at 10:00.

    Deliberately scoped to a single booking and not to the engagement: moving the
    audit itself is a change to the audit record, and letting it be done from the
    calendar panel would put the schedule in two places. The audit block resists
    this for the same reason — its time comes from `scheduledDate`, and a re-sync
    would overwrite anything set here.
    """
    b = await db.get(CalendarBooking, booking_id)
    if b is None:
        raise ValueError("Booking not found")
    if b.status == "CANCELLED":
        raise ValueError("This booking was cancelled; re-sync the engagement to recreate it")
    if b.bookingType == AUDIT_BLOCK:
        raise ValueError(
            "The audit block follows the audit's scheduled date and duration — "
            "change those on the audit, and the block moves with them"
        )
    if end <= start:
        raise ValueError("The meeting must end after it starts")
    b.startAt = start
    b.endAt = end
    b.updatedBy = actor_id
    # A move is a real change, so the attempt budget starts again — a booking
    # that failed six times at the old time deserves a fresh try at the new one.
    b.attemptCount = 0
    if b.status == "FAILED":
        b.status = "PENDING"
    outcome = await _deliver(b)
    await db.flush()
    return {"ok": b.status in ("BOOKED", "SKIPPED"), "outcome": outcome, "status": b.status}


# ─────────────────────────────────────────────────────────────────────
# Read + retry
# ─────────────────────────────────────────────────────────────────────


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def to_dict(b: CalendarBooking) -> dict[str, Any]:
    live = [a for a in (b.attendees or []) if not a.get("removedAt")]
    return {
        "id": b.id,
        "engagementKind": b.engagementKind,
        "engagementId": b.engagementId,
        "bookingType": b.bookingType,
        "subject": b.subject,
        "location": b.location,
        "startAt": _iso(b.startAt),
        "endAt": _iso(b.endAt),
        "timezone": b.timezone,
        "status": b.status,
        "provider": b.provider,
        "onlineMeetingUrl": b.onlineMeetingUrl,
        "roomEmail": b.roomEmail,
        "roomName": b.roomName,
        "roomStatus": b.roomStatus,
        "roomPinned": b.roomPinned,
        "organizerUserId": b.organizerUserId,
        "organizerEmail": b.organizerEmail,
        "attendees": live,
        "attendeeCount": len(live),
        "removedAttendees": [a for a in (b.attendees or []) if a.get("removedAt")],
        "revision": b.revision,
        "attemptCount": b.attemptCount,
        "lastSyncedAt": _iso(b.lastSyncedAt),
        "lastAttemptAt": _iso(b.lastAttemptAt),
        "lastError": b.lastError,
        "cancelledAt": _iso(b.cancelledAt),
        "cancelReason": b.cancelReason,
    }


async def bookings_for(
    db: AsyncSession, *, engagement_kind: str, engagement_id: str
) -> dict[str, Any]:
    """Every booking for an engagement, in agenda order.

    Returns `bookings: []` with a `statement` rather than 404ing when nothing has
    been booked — an audit screen must be able to say "nothing is on anyone's
    calendar", which is the state this feature exists to make visible.
    """
    rows = (
        await db.execute(
            select(CalendarBooking).where(
                CalendarBooking.engagementKind == (engagement_kind or "").upper(),
                CalendarBooking.engagementId == engagement_id,
            )
        )
    ).scalars().all()
    order = {OPENING_MEETING: 0, AUDIT_BLOCK: 1, CLOSING_MEETING: 2}
    rows = sorted(rows, key=lambda b: (b.startAt or _utcnow(), order.get(b.bookingType, 9)))
    items = [to_dict(b) for b in rows]
    booked = sum(1 for i in items if i["status"] == "BOOKED")
    failed = sum(1 for i in items if i["status"] == "FAILED")
    people = len({a["email"] for i in items for a in i["attendees"] if a.get("email")})
    if not items:
        statement = "No calendar bookings have been made for this engagement yet."
    elif failed:
        statement = f"{booked} of {len(items)} bookings delivered · {failed} failed"
    else:
        statement = f"{booked} of {len(items)} bookings delivered to {people} participant(s)"
    return {
        "bookings": items,
        "total": len(items),
        "bookedCount": booked,
        "failedCount": failed,
        "participantCount": people,
        "provider": providers.resolve_provider().name,
        "statement": statement,
    }


async def run_booking_retry(db: AsyncSession) -> dict[str, Any]:
    """Scheduler job — drain PENDING bookings whose delivery has not landed.

    Only future bookings are retried. Re-sending an invitation for a window that
    already closed cannot block anything and merely tells someone they were
    supposed to be somewhere yesterday.
    """
    now = _utcnow()
    settings = get_settings()
    rows = (
        await db.execute(
            select(CalendarBooking)
            .where(
                CalendarBooking.status == "PENDING",
                CalendarBooking.endAt > now,
                CalendarBooking.attemptCount < settings.calendar_max_attempts,
            )
            .order_by(CalendarBooking.startAt)
            .limit(200)
        )
    ).scalars().all()
    booked = failed = skipped = 0
    for b in rows:
        outcome = await _deliver(b, force=True)
        booked += outcome == "booked"
        failed += outcome == "failed"
        skipped += outcome == "skipped"

    # ── Settle room verdicts ──────────────────────────────────────────
    #
    # Exchange's booking assistant answers asynchronously, so a room is PENDING
    # at create time and only later ACCEPTED or DECLINED. Chasing that answer is
    # the point: a DECLINED room means it was already taken, and an audit that
    # goes on believing it has a room sends nine people to an occupied one.
    provider = providers.resolve_provider()
    pending_rooms = (
        await db.execute(
            select(CalendarBooking)
            .where(
                CalendarBooking.status == "BOOKED",
                CalendarBooking.roomStatus == "PENDING",
                CalendarBooking.endAt > now,
            )
            .order_by(CalendarBooking.startAt)
            .limit(200)
        )
    ).scalars().all()
    accepted = declined = 0
    for b in pending_rooms:
        try:
            verdict = await provider.read_room_status(_to_event_spec(b), b.providerEventId)
        except Exception as e:  # noqa: BLE001
            log.debug("room status read failed for %s: %s", b.id, e)
            continue
        if verdict == b.roomStatus:
            continue
        b.roomStatus = verdict
        accepted += verdict == "ACCEPTED"
        if verdict == "ACCEPTED":
            b.lastError = None
        if verdict == "DECLINED":
            declined += 1
            # Surfaced on the audit screen in the same place a delivery failure
            # is, because to the scheduler it is the same kind of problem:
            # something they believed was arranged is not.
            #
            # The wording distinguishes the two causes, because the remedies are
            # opposite: a clash is fixed by picking another room, a booking-window
            # refusal is not fixed by picking ANY room and telling someone to try
            # another would waste their afternoon.
            room = b.roomName or b.roomEmail
            b.lastError = (
                f"{room} declined this booking. It is beyond the room's booking window, "
                "so no room can be held this far ahead — it will be requested "
                "automatically closer to the date."
                if _room_deferred(b)
                else f"{room} declined this booking — it is already taken at this time. "
                "Choose another room."
            )

    # ── Attach rooms that were held back, now that they are in range ──
    #
    # Without this an annual-programme audit would carry DEFERRED forever and
    # arrive with no room, which is the failure this whole mechanism exists to
    # avoid rather than merely relabel.
    deferred = (
        await db.execute(
            select(CalendarBooking)
            .where(
                CalendarBooking.status == "BOOKED",
                CalendarBooking.roomStatus == "DEFERRED",
                CalendarBooking.endAt > now,
            )
            .order_by(CalendarBooking.startAt)
            .limit(200)
        )
    ).scalars().all()
    attached = 0
    for b in deferred:
        if _room_deferred(b):
            continue  # still too far out
        b.attemptCount = 0
        outcome = await _deliver(b, force=True)
        attached += outcome == "booked"

    return {
        "scanned": len(rows),
        "booked": booked,
        "failed": failed,
        "skipped": skipped,
        "roomsChecked": len(pending_rooms),
        "roomsAccepted": accepted,
        "roomsDeclined": declined,
        "roomsAttached": attached,
        "summary": (
            f"{booked} booked, {failed} still failing, {skipped} skipped; "
            f"rooms {accepted} accepted / {declined} declined / {attached} now requested"
        ),
    }


async def sync_engagement(
    db: AsyncSession,
    *,
    engagement_kind: str,
    engagement_id: str,
    actor_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Recompute and deliver every booking for one engagement.

    The public entry point. Everything it does happens inside a SAVEPOINT, so a
    calendar failure — a missing table before the migration has run, a bad row,
    anything — rolls back only the calendar work and leaves the caller's
    transaction healthy. `create_audit` calls this mid-transaction; an audit
    must never fail to be created because a calendar could not be booked.
    """
    out: dict[str, Any] = {"ok": False, "error": "Calendar bookings are unavailable"}
    sp = _Savepoint(db, f"sync {engagement_kind}/{engagement_id}")
    async with sp:
        out = await _sync_engagement_inner(
            db,
            engagement_kind=engagement_kind,
            engagement_id=engagement_id,
            actor_id=actor_id,
            force=force,
        )
    return {"ok": False, "error": sp.error} if sp.error else out


async def cancel_engagement(
    db: AsyncSession,
    *,
    engagement_kind: str,
    engagement_id: str,
    reason: str = "",
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Withdraw every booking for an engagement. Savepoint-scoped, as above."""
    out: dict[str, Any] = {"ok": False, "error": "Calendar bookings are unavailable"}
    sp = _Savepoint(db, f"cancel {engagement_kind}/{engagement_id}")
    async with sp:
        out = await _cancel_engagement_inner(
            db,
            engagement_kind=engagement_kind,
            engagement_id=engagement_id,
            reason=reason,
            actor_id=actor_id,
        )
    return {"ok": False, "error": sp.error} if sp.error else out


__all__ = [
    "AUDIT_BLOCK",
    "list_rooms",
    "set_room",
    "OPENING_MEETING",
    "CLOSING_MEETING",
    "sync_engagement",
    "cancel_engagement",
    "cancel_booking",
    "reschedule_booking",
    "bookings_for",
    "run_booking_retry",
    "to_dict",
]
