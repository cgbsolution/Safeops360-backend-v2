"""Calendar delivery providers — how a booking reaches a real calendar.

Three implementations behind one narrow interface, chosen at call time by what
the deployment is actually configured for:

  GraphCalendarProvider   Microsoft Graph, app-only (client credentials). Writes
                          the meeting into the organiser's Exchange mailbox and
                          Exchange fans it out to attendees. This is the only
                          option that genuinely BLOCKS time — Outlook adds a
                          received invite to the calendar as tentative-busy
                          before anyone clicks anything, and a Teams join link
                          comes with it.

  IcsCalendarProvider     iCalendar REQUEST/CANCEL over the SMTP already
                          configured. Universally understood, needs nothing from
                          the customer's IT, and is what keeps this feature alive
                          in the weeks before an Azure app registration is
                          approved. The honest limit: the slot is held only once
                          the recipient accepts, and there is no Teams link.

  NullCalendarProvider    Neither is configured. Records SKIPPED and says so.
                          Not an error — a deployment with no mail gateway and no
                          Graph app has made a choice, and pretending the invite
                          went out would be worse than admitting it did not.

**Why raw httpx rather than msal + msgraph-sdk.** Two SDKs, a transitive tree of
about forty packages, and an airgapped on-prem install that has to vendor all of
it — to obtain one token and issue three REST calls. The token endpoint and the
`/events` resource are stable, versioned, documented HTTP. `httpx` is already a
dependency.

Nothing here raises. Every method returns a `DeliveryResult`; the caller records
it. A calendar server having a bad afternoon must never be able to fail an audit.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import get_settings

log = logging.getLogger("safeops360.calendar")

def _graph_root() -> str:
    return get_settings().ms_graph_base.rstrip("/")


def _token_url(tenant: str) -> str:
    return f"{get_settings().ms_login_base.rstrip('/')}/{tenant}/oauth2/v2.0/token"


def _graph_scope() -> str:
    """`.default` against the Graph host we are actually calling.

    Derived rather than hardcoded: pointing `MS_GRAPH_BASE` at a sovereign cloud
    while still requesting a commercial-cloud scope yields a token the sovereign
    endpoint rejects — an authentication failure that reads like bad credentials
    and is nothing of the sort.
    """
    root = _graph_root()
    scheme, _, rest = root.partition("://")
    host = (rest or scheme).split("/", 1)[0]
    return f"https://{host}/.default"

# Graph's own guidance: retry these, fail the rest. A 403 will still be a 403 in
# fifteen minutes and re-sending it just burns the retry budget that a genuine
# throttle needs.
_RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}


@dataclass
class Attendee:
    email: str
    name: str = ""
    required: bool = True
    userId: str | None = None
    role: str = "OTHER"


@dataclass
class EventSpec:
    """One calendar event, provider-agnostic.

    `start`/`end` are timezone-aware instants; `timezone` is the IANA zone the
    invite should be *expressed* in. Both are needed: the instant fixes when it
    happens, the zone fixes what the participant reads.
    """

    subject: str
    start: datetime
    end: datetime
    timezone: str
    organizer_email: str
    organizer_name: str = ""
    attendees: list[Attendee] = field(default_factory=list)
    body_html: str = ""
    location: str = ""
    online_meeting: bool = True
    # Exchange resource mailbox, when a room is being held. Carried separately
    # from `attendees` because Graph types it differently (`resource`), it must
    # also appear as the event's location, and its accept/decline is a fact
    # about the booking rather than about a person.
    room_email: str | None = None
    room_name: str | None = None
    # Stable per booking. Graph treats it as an idempotency key; the ICS provider
    # uses it as the UID, so an update lands on the existing appointment instead
    # of creating a second one.
    transaction_id: str = ""
    sequence: int = 0


@dataclass
class DeliveryResult:
    ok: bool
    provider: str
    event_id: str | None = None
    join_url: str | None = None
    error: str | None = None
    # False for a permanent rejection — the caller stops retrying immediately
    # instead of spending six attempts on a malformed mailbox address.
    retryable: bool = True
    # Nothing was wrong and nothing was sent (no provider, no addressable
    # attendee). Distinct from a failure, and shown differently.
    skipped: bool = False
    # NONE | PENDING | ACCEPTED | DECLINED. Usually PENDING straight after a
    # create — Exchange's booking assistant answers asynchronously — and settled
    # later by `read_room_status`.
    room_status: str = "NONE"


class CalendarProvider:
    """Interface. Implementations never raise — they return DeliveryResult."""

    name = "NONE"

    async def create(self, spec: EventSpec) -> DeliveryResult:  # pragma: no cover - interface
        raise NotImplementedError

    async def update(self, spec: EventSpec, event_id: str) -> DeliveryResult:  # pragma: no cover
        raise NotImplementedError

    async def cancel(
        self, spec: EventSpec, event_id: str | None, reason: str = ""
    ) -> DeliveryResult:  # pragma: no cover
        raise NotImplementedError

    async def list_rooms(self) -> tuple[list[dict[str, Any]], str | None]:
        """Bookable rooms, or an explanation. Empty is a legitimate answer."""
        return [], "Room booking needs Microsoft Graph"

    async def read_room_status(self, spec: EventSpec, event_id: str | None) -> str:
        """Whether the room has accepted yet. NONE when unknowable."""
        return "NONE"


# ─────────────────────────────────────────────────────────────────────
# Microsoft Graph (app-only)
# ─────────────────────────────────────────────────────────────────────


class _TokenCache:
    """One process-wide app token, refreshed 120s before it expires.

    Guarded by a lock so a burst of bookings at audit-creation time performs one
    token fetch rather than nine — Entra throttles, and nine identical requests
    is how a feature earns a 429 on its first day.
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._token = None

    async def get(self) -> tuple[str | None, str | None]:
        s = get_settings()
        if not s.graph_configured:
            return None, "Microsoft Graph is not configured"
        now = datetime.now(timezone.utc)
        if self._token and now < self._expires_at:
            return self._token, None
        async with self._lock:
            # Re-check: another coroutine may have refreshed while we waited.
            now = datetime.now(timezone.utc)
            if self._token and now < self._expires_at:
                return self._token, None
            url = _token_url(s.ms_graph_tenant_id)
            data = {
                "grant_type": "client_credentials",
                "client_id": s.ms_graph_client_id,
                "client_secret": s.ms_graph_client_secret,
                "scope": _graph_scope(),
            }
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    r = await client.post(url, data=data)
                if r.status_code != 200:
                    return None, f"token endpoint {r.status_code}: {r.text[:300]}"
                payload = r.json()
            except Exception as e:  # noqa: BLE001
                return None, f"token request failed: {e}"
            token = payload.get("access_token")
            if not token:
                return None, "token endpoint returned no access_token"
            self._token = token
            self._expires_at = now + timedelta(seconds=int(payload.get("expires_in", 3600)) - 120)
            return token, None


_tokens = _TokenCache()


def _is_online_meeting_refusal(r: httpx.Response) -> bool:
    """Did Graph reject the request specifically over the Teams-meeting fields?

    Matched on the message text because Graph returns a generic
    `ErrorAccessDenied` / `BadRequest` code for this, with the actual cause only
    in the prose. Deliberately narrow: a false positive would silently strip a
    Teams link the tenant is entitled to.
    """
    if r.status_code not in (400, 403):
        return False
    try:
        detail = str((r.json().get("error") or {}).get("message") or "").lower()
    except Exception:  # noqa: BLE001
        detail = r.text.lower()
    return "onlinemeeting" in detail.replace(" ", "") or "teams" in detail


def _room_status_from(data: dict, spec: "EventSpec") -> str:
    """Read the room's verdict out of a create/update response if it is already
    there, else PENDING. Exchange sometimes answers within the same call for a
    free room, which saves the maintenance job a round trip."""
    want = (spec.room_email or "").lower()
    for a in data.get("attendees", []) or []:
        if ((a.get("emailAddress") or {}).get("address") or "").lower() != want:
            continue
        resp = ((a.get("status") or {}).get("response") or "none").lower()
        return GraphCalendarProvider._ROOM_RESPONSE.get(resp, "PENDING")
    return "PENDING"


def _graph_error(r: httpx.Response) -> tuple[str, bool]:
    """Readable message + whether it is worth retrying."""
    try:
        body = r.json()
        detail = (body.get("error") or {}).get("message") or r.text
    except Exception:  # noqa: BLE001
        detail = r.text
    return f"Graph {r.status_code}: {str(detail)[:400]}", r.status_code in _RETRYABLE_HTTP


class GraphCalendarProvider(CalendarProvider):
    name = "GRAPH"

    def _event_body(self, spec: EventSpec) -> dict[str, Any]:
        s = get_settings()
        body: dict[str, Any] = {
            "subject": spec.subject,
            "body": {"contentType": "HTML", "content": spec.body_html},
            # Sent as UTC instants rather than local-time-plus-zone. Graph does
            # accept IANA zone names, but every client re-renders the event in
            # the reader's own zone anyway, and a UTC instant cannot be shifted
            # by a zone name Exchange resolves differently from Python.
            "start": {
                "dateTime": spec.start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": spec.end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "UTC",
            },
            "attendees": [
                {
                    "emailAddress": {"address": a.email, "name": a.name or a.email},
                    "type": "required" if a.required else "optional",
                }
                for a in spec.attendees
            ]
            # A room joins the SAME attendee array, typed `resource`. That type
            # is what makes Exchange's booking assistant answer for it — added
            # as `required` it would merely be a mailbox that never replies, and
            # the room would never actually be held.
            + (
                [
                    {
                        "emailAddress": {
                            "address": spec.room_email,
                            "name": spec.room_name or spec.room_email,
                        },
                        "type": "resource",
                    }
                ]
                if spec.room_email
                else []
            ),
            # The point of the exercise: the participant's hours read as busy,
            # not free, so a second meeting cannot be booked over the audit.
            "showAs": "busy",
            "responseRequested": True,
            # An audit window is not negotiable by reply — reschedules go through
            # the module so the record and the calendar cannot diverge.
            "allowNewTimeProposals": False,
            "isReminderOn": True,
            "reminderMinutesBeforeStart": 60,
        }
        # The room is also the event's LOCATION. Setting it as a typed room
        # location (not free text) is what makes Outlook show the room in the
        # header and lets Exchange tie the location to the resource booking —
        # otherwise the room is held but the meeting still reads "on site".
        if spec.room_email:
            body["location"] = {
                "displayName": spec.room_name or spec.room_email,
                "locationType": "conferenceRoom",
                "locationEmailAddress": spec.room_email,
            }
        elif spec.location:
            body["location"] = {"displayName": spec.location}
        if spec.online_meeting and s.calendar_online_meetings:
            body["isOnlineMeeting"] = True
            body["onlineMeetingProvider"] = "teamsForBusiness"
        return body

    async def _request(
        self, method: str, path: str, *, json: dict | None = None, retry_auth: bool = True
    ) -> tuple[httpx.Response | None, str | None]:
        token, err = await _tokens.get()
        if err:
            return None, err
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.request(
                    method,
                    f"{_graph_root()}{path}",
                    json=json,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
        except Exception as e:  # noqa: BLE001
            return None, f"Graph request failed: {e}"
        # A cached token can be revoked mid-flight (secret rotated, app consent
        # withdrawn). Drop it and try once more before reporting a failure the
        # operator would have to resolve by restarting the process.
        if r.status_code == 401 and retry_auth:
            _tokens.invalidate()
            return await self._request(method, path, json=json, retry_auth=False)
        return r, None

    async def create(self, spec: EventSpec) -> DeliveryResult:
        payload = self._event_body(spec)
        if spec.transaction_id:
            # Idempotency. If a previous attempt reached Exchange but its
            # response never reached us, Graph returns the SAME event rather
            # than creating a duplicate — which is exactly the failure mode a
            # retry job would otherwise turn into two invites per person.
            payload["transactionId"] = spec.transaction_id[:256]
        r, err = await self._request(
            "POST", f"/users/{spec.organizer_email}/events", json=payload
        )
        if err:
            return DeliveryResult(False, self.name, error=err)

        # A tenant that granted Calendars.ReadWrite but NOT
        # OnlineMeetings.ReadWrite can reject the Teams fields while being
        # perfectly able to create the event. Booking the time matters far more
        # than attaching a join link, so drop the link and keep the booking
        # rather than failing the whole thing over a nice-to-have.
        if r.status_code not in (200, 201) and _is_online_meeting_refusal(r):
            log.info(
                "Graph refused the Teams meeting fields; retrying without them. "
                "Grant OnlineMeetings.ReadWrite (application) to restore join links."
            )
            payload.pop("isOnlineMeeting", None)
            payload.pop("onlineMeetingProvider", None)
            r, err = await self._request(
                "POST", f"/users/{spec.organizer_email}/events", json=payload
            )
            if err:
                return DeliveryResult(False, self.name, error=err)

        if r.status_code not in (200, 201):
            msg, retryable = _graph_error(r)
            return DeliveryResult(False, self.name, error=msg, retryable=retryable)
        data = r.json()
        return DeliveryResult(
            True,
            self.name,
            event_id=data.get("id"),
            join_url=(data.get("onlineMeeting") or {}).get("joinUrl"),
            # PENDING, not ACCEPTED: Exchange's booking assistant has not
            # answered yet at this point, and claiming the room now would be
            # asserting something we have not been told.
            room_status=_room_status_from(data, spec) if spec.room_email else "NONE",
        )

    async def update(self, spec: EventSpec, event_id: str) -> DeliveryResult:
        r, err = await self._request(
            "PATCH", f"/users/{spec.organizer_email}/events/{event_id}", json=self._event_body(spec)
        )
        if err:
            return DeliveryResult(False, self.name, error=err)
        if r.status_code == 404:
            # Somebody deleted the meeting out of Outlook. Re-create rather than
            # leaving the module asserting a booking that no longer exists.
            return await self.create(spec)
        if r.status_code not in (200, 201):
            msg, retryable = _graph_error(r)
            return DeliveryResult(False, self.name, error=msg, retryable=retryable)
        data = r.json()
        return DeliveryResult(
            True,
            self.name,
            event_id=data.get("id", event_id),
            join_url=(data.get("onlineMeeting") or {}).get("joinUrl"),
            room_status=_room_status_from(data, spec) if spec.room_email else "NONE",
        )

    # ── Rooms ────────────────────────────────────────────────────────
    #
    # `Place.Read.All` is a separate grant from Calendars.ReadWrite. A tenant
    # that has not granted it can still book rooms by address — it just cannot
    # LIST them, so the picker degrades to "type the room mailbox" rather than
    # the feature disappearing.

    _ROOM_RESPONSE = {
        "accepted": "ACCEPTED",
        "tentativelyaccepted": "ACCEPTED",
        "declined": "DECLINED",
        "notresponded": "PENDING",
        "none": "PENDING",
    }

    async def list_rooms(self) -> tuple[list[dict[str, Any]], str | None]:
        r, err = await self._request("GET", "/places/microsoft.graph.room?$top=200")
        if err:
            return [], err
        if r.status_code != 200:
            msg, _ = _graph_error(r)
            if r.status_code == 403:
                msg += " — grant the application permission Place.Read.All to list rooms."
            return [], msg
        rooms = []
        for v in r.json().get("value", []):
            email = v.get("emailAddress")
            if not email:
                continue
            rooms.append(
                {
                    "email": email,
                    "name": v.get("displayName") or email,
                    # 0 is Exchange's "not configured", not a zero-seat room —
                    # rendering it as a capacity would be actively misleading.
                    "capacity": v.get("capacity") or None,
                    "building": v.get("building"),
                    "floor": v.get("floorLabel") or v.get("floorNumber"),
                }
            )
        return sorted(rooms, key=lambda x: x["name"].lower()), None

    async def read_room_status(self, spec: EventSpec, event_id: str | None) -> str:
        """Ask the event what the room said.

        Exchange's booking assistant replies asynchronously, so this is a second
        read rather than something the create response can carry. A DECLINED
        room is the outcome that matters: it means the room is already taken and
        the audit has a location it does not actually have.
        """
        if not event_id or not spec.room_email or event_id.startswith("ics:"):
            return "NONE"
        r, err = await self._request(
            "GET", f"/users/{spec.organizer_email}/events/{event_id}?$select=attendees"
        )
        if err or r is None or r.status_code != 200:
            return "PENDING"
        want = spec.room_email.lower()
        for a in r.json().get("attendees", []):
            addr = ((a.get("emailAddress") or {}).get("address") or "").lower()
            if addr != want:
                continue
            resp = ((a.get("status") or {}).get("response") or "none").lower()
            return self._ROOM_RESPONSE.get(resp, "PENDING")
        return "NONE"

    async def cancel(
        self, spec: EventSpec, event_id: str | None, reason: str = ""
    ) -> DeliveryResult:
        if not event_id:
            return DeliveryResult(True, self.name, skipped=True)
        # /cancel (not DELETE) so attendees receive a cancellation notice and the
        # slot is released from their calendars. A DELETE removes it from the
        # organiser's mailbox and leaves everyone else blocked.
        r, err = await self._request(
            "POST",
            f"/users/{spec.organizer_email}/events/{event_id}/cancel",
            json={"comment": reason or "This audit engagement was cancelled in SafeOps360."},
        )
        if err:
            return DeliveryResult(False, self.name, error=err)
        if r.status_code in (202, 204, 200):
            return DeliveryResult(True, self.name, event_id=event_id)
        if r.status_code == 404:
            return DeliveryResult(True, self.name, event_id=event_id, skipped=True)
        msg, retryable = _graph_error(r)
        return DeliveryResult(False, self.name, error=msg, retryable=retryable)


# ─────────────────────────────────────────────────────────────────────
# iCalendar over SMTP
# ─────────────────────────────────────────────────────────────────────


def _ics_escape(text: str) -> str:
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 §3.1 — 75 octets per line, continuations start with a space.

    Unfolded long lines are the classic reason an invite renders as a plain
    attachment in Outlook: the parser gives up before it reaches DTSTART.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > (75 if not out else 74):
            out.append(cur)
            cur = ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n ".join(out)


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(spec: EventSpec, *, method: str = "REQUEST", uid: str | None = None) -> str:
    """A VCALENDAR Outlook, Teams, Google and Apple Calendar all act on.

    Times are emitted in UTC (`...Z`) rather than with a VTIMEZONE block. A
    VTIMEZONE that is even slightly wrong shifts a whole audit by an hour; UTC
    is unambiguous and every client renders it in the reader's own zone.

    `X-MICROSOFT-CDO-BUSYSTATUS:BUSY` is what makes Outlook show the accepted
    slot as blocked instead of free — the difference between an invite and a
    booking, on the one client the client's people actually use.
    """
    uid = uid or spec.transaction_id or "safeops360-booking"
    now = datetime.now(timezone.utc)
    org_cn = _ics_escape(spec.organizer_name or spec.organizer_email)
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//SafeOps360//CAMS Audit Calendar//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_utc(now)}",
        f"DTSTART:{_utc(spec.start)}",
        f"DTEND:{_utc(spec.end)}",
        f"SEQUENCE:{spec.sequence}",
        f"SUMMARY:{_ics_escape(spec.subject)}",
        f"ORGANIZER;CN={org_cn}:mailto:{spec.organizer_email}",
        "STATUS:" + ("CANCELLED" if method == "CANCEL" else "CONFIRMED"),
        "TRANSP:OPAQUE",
        "X-MICROSOFT-CDO-BUSYSTATUS:BUSY",
        "X-MICROSOFT-DISALLOW-COUNTER:TRUE",
    ]
    # The room, when there is one, IS the location — otherwise the invitation
    # holds the room while telling the reader the meeting is somewhere else.
    _loc = spec.room_name or spec.room_email or spec.location
    if _loc:
        lines.append(f"LOCATION:{_ics_escape(_loc)}")
    if spec.body_html:
        # DESCRIPTION is plain text by contract; the HTML body is an X- extension
        # Outlook reads. Sending markup in DESCRIPTION shows tags to everyone else.
        plain = (
            spec.body_html.replace("<br>", "\n")
            .replace("<br/>", "\n")
            .replace("</p>", "\n")
        )
        for tag in ("<p>", "<b>", "</b>", "<i>", "</i>", "<ul>", "</ul>", "<li>", "</li>"):
            plain = plain.replace(tag, "")
        lines.append(f"DESCRIPTION:{_ics_escape(plain.strip())}")
        lines.append(f"X-ALT-DESC;FMTTYPE=text/html:{_ics_escape(spec.body_html)}")
    for a in spec.attendees:
        role = "REQ-PARTICIPANT" if a.required else "OPT-PARTICIPANT"
        cn = _ics_escape(a.name or a.email)
        lines.append(
            f"ATTENDEE;CUTYPE=INDIVIDUAL;ROLE={role};PARTSTAT=NEEDS-ACTION;RSVP=TRUE;"
            f"CN={cn}:mailto:{a.email}"
        )
    if spec.room_email:
        # `CUTYPE=ROOM` is what lets an Exchange resource mailbox process an
        # emailed invitation as a booking request rather than filing it as
        # correspondence. Weaker than the Graph path — the room's booking
        # assistant still decides, and we never learn the answer over SMTP —
        # but it is the difference between asking for the room and not.
        lines.append(
            f"ATTENDEE;CUTYPE=ROOM;ROLE=NON-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE;"
            f"CN={_ics_escape(spec.room_name or spec.room_email)}:mailto:{spec.room_email}"
        )
    if method != "CANCEL":
        lines += ["BEGIN:VALARM", "TRIGGER:-PT60M", "ACTION:DISPLAY", "DESCRIPTION:Reminder", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(ln) for ln in lines) + "\r\n"


class IcsCalendarProvider(CalendarProvider):
    name = "ICS"

    async def _send(self, spec: EventSpec, method: str, note: str) -> DeliveryResult:
        from app.services.notifications import send_email

        # The room mailbox has to RECEIVE the invitation to act on it — an
        # ATTENDEE line naming a room nobody sent it to books nothing.
        addrs = [a.email for a in spec.attendees if a.email]
        if spec.room_email:
            addrs.append(spec.room_email)
        if not addrs:
            return DeliveryResult(True, self.name, skipped=True, error="No addressable attendees")
        ics = build_ics(spec, method=method)
        local_start = spec.start.astimezone(timezone.utc)
        subject = ("Cancelled: " if method == "CANCEL" else "") + spec.subject
        text = (
            f"{note}\n\n"
            f"{spec.subject}\n"
            f"{local_start.strftime('%d %b %Y %H:%M')} UTC "
            f"({spec.timezone} local time shown in your calendar)\n"
            f"{spec.location}\n\n"
            "Accept the attached invitation to hold the time in your calendar."
        )
        ok = await send_email(
            addrs,
            subject,
            text,
            html=spec.body_html or None,
            ics=ics,
            ics_method=method,
            ics_filename="audit-invite.ics",
        )
        if not ok:
            return DeliveryResult(
                False, self.name, error="SMTP send failed or is not configured"
            )
        # There is no server-side event id in this channel — the UID we minted IS
        # the identity, and saying so keeps the update path honest.
        return DeliveryResult(True, self.name, event_id=f"ics:{spec.transaction_id}")

    async def create(self, spec: EventSpec) -> DeliveryResult:
        return await self._send(spec, "REQUEST", "You are invited to the following audit engagement.")

    async def update(self, spec: EventSpec, event_id: str) -> DeliveryResult:
        return await self._send(
            spec, "REQUEST", "This audit engagement has been updated. Your calendar entry will change."
        )

    async def cancel(
        self, spec: EventSpec, event_id: str | None, reason: str = ""
    ) -> DeliveryResult:
        return await self._send(
            spec, "CANCEL", reason or "This audit engagement has been cancelled."
        )


class NullCalendarProvider(CalendarProvider):
    name = "NONE"

    async def create(self, spec: EventSpec) -> DeliveryResult:
        return DeliveryResult(
            True, self.name, skipped=True, error="No calendar provider is configured"
        )

    async def update(self, spec: EventSpec, event_id: str) -> DeliveryResult:
        return await self.create(spec)

    async def cancel(
        self, spec: EventSpec, event_id: str | None, reason: str = ""
    ) -> DeliveryResult:
        return DeliveryResult(True, self.name, skipped=True)


# ─────────────────────────────────────────────────────────────────────
# Resolution + operator-facing status
# ─────────────────────────────────────────────────────────────────────


def resolve_provider() -> CalendarProvider:
    """Best available channel, re-read on every call.

    Not cached deliberately: an operator who pastes Graph credentials into the
    settings and restarts nothing should see the next booking go out over Graph.
    """
    s = get_settings()
    if not s.calendar_bookings_enabled:
        return NullCalendarProvider()
    if s.graph_configured:
        return GraphCalendarProvider()
    if s.smtp_host and s.smtp_user:
        return IcsCalendarProvider()
    return NullCalendarProvider()


async def provider_status(*, probe: bool = False) -> dict[str, Any]:
    """What the admin screen shows — including WHY a channel is unavailable.

    `probe=True` actually fetches a Graph token. Worth the round trip on a
    settings screen, because "credentials present" and "credentials work" are
    different claims and only one of them is useful.
    """
    s = get_settings()
    active = resolve_provider().name
    out: dict[str, Any] = {
        "enabled": s.calendar_bookings_enabled,
        "activeProvider": active,
        "graph": {
            "configured": s.graph_configured,
            "tenantId": s.ms_graph_tenant_id,
            "clientId": s.ms_graph_client_id,
            "onlineMeetings": s.calendar_online_meetings,
            "missing": [
                k
                for k, v in (
                    ("MS_GRAPH_TENANT_ID", s.ms_graph_tenant_id),
                    ("MS_GRAPH_CLIENT_ID", s.ms_graph_client_id),
                    ("MS_GRAPH_CLIENT_SECRET", s.ms_graph_client_secret),
                )
                if not v
            ],
        },
        "smtp": {"configured": bool(s.smtp_host and s.smtp_user), "host": s.smtp_host},
        "fallbackMailbox": s.calendar_organizer_email,
        "timezone": s.calendar_default_timezone,
        "openingMeetingMinutes": s.calendar_opening_meeting_minutes,
        "closingMeetingMinutes": s.calendar_closing_meeting_minutes,
        "statement": _status_statement(active, s.graph_configured),
    }
    if probe and s.graph_configured:
        _tokens.invalidate()
        token, err = await _tokens.get()
        out["graph"]["tokenOk"] = bool(token)
        out["graph"]["tokenError"] = err
    return out


def _status_statement(active: str, graph_configured: bool) -> str:
    if active == "GRAPH":
        return (
            "Bookings are written directly into participants' Microsoft 365 calendars "
            "with a Teams link. Time is held as busy without the participant accepting."
        )
    if active == "ICS":
        return (
            "Bookings are emailed as calendar invitations. They appear in Outlook and Teams, "
            "but the time is held only once the participant accepts. Add Microsoft Graph "
            "credentials to book calendars directly and attach Teams links."
            + (" Graph credentials are incomplete." if graph_configured else "")
        )
    return (
        "No calendar channel is configured, so bookings are recorded but nothing is sent. "
        "Configure SMTP for calendar invitations, or Microsoft Graph to write directly "
        "into Microsoft 365 calendars."
    )


__all__ = [
    "Attendee",
    "EventSpec",
    "DeliveryResult",
    "CalendarProvider",
    "GraphCalendarProvider",
    "IcsCalendarProvider",
    "NullCalendarProvider",
    "resolve_provider",
    "provider_status",
    "build_ics",
]
