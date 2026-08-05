"""WP-43 - CAMS notification catalogue + SMTP digests.

docs/cams/09 §3.3.

**Reuses the platform substrate.** `Notification` (in-app, with `isRead`,
`entityType`/`entityId`, `linkUrl`) and `app.services.notifications` already
exist and are used by ERM and MOC. This module adds the CAMS event catalogue on
top - it does NOT introduce a second notification system, per the brief's
"reuse any existing platform notification substrate - verify before building
one".

**Two delivery channels only: in-platform and per-tenant SMTP.** No WhatsApp.

**Digest, not a mail storm.** Fourteen event types across a 1,500-checkpoint
engagement would mean hundreds of emails a day. In-app notifications fire
immediately (they are cheap and the bell is the inbox); SMTP is batched into a
per-user digest by `build_digest`, which the scheduler sends. `IMMEDIATE_EMAIL`
below is the short list where waiting for a digest would be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification
from app.models.user import User


@dataclass(frozen=True)
class EventSpec:
    """One notification type. `severity` drives the bell styling and digest order."""

    code: str
    label: str
    severity: str  # INFO | WARNING | CRITICAL
    # Immediate email, or hold for the digest? Reserved for events where a
    # delay changes the outcome, not merely the mood.
    immediate_email: bool = False


# The catalogue. The brief lists eleven minimum events; these are those plus the
# three the sign-off and programme work introduced.
CATALOGUE: dict[str, EventSpec] = {
    e.code: e
    for e in [
        # ── assignment ──
        EventSpec("AUDITOR_ASSIGNED", "You were assigned as an auditor", "INFO"),
        EventSpec("AUDITEE_ASSIGNED", "You were named as an auditee owner", "INFO"),
        EventSpec("CHECKPOINTS_ALLOCATED", "Checkpoints were allocated to you", "INFO"),
        # ── execution ──
        EventSpec(
            "ENGAGEMENT_STARTING_INCOMPLETE_TEAM",
            "An engagement starts soon without a full team",
            "WARNING",
            immediate_email=True,
        ),
        EventSpec("FINDING_ROUTED", "A finding was routed to you", "WARNING"),
        EventSpec("RESPONSE_RECEIVED", "An auditee responded to your finding", "INFO"),
        EventSpec("REVIEW_REQUESTED", "A finding was escalated for your review", "WARNING"),
        # ── CAPA ──
        EventSpec("CAPA_DUE", "A CAPA from an audit finding is due", "WARNING"),
        EventSpec(
            "CAPA_OVERDUE", "A CAPA from an audit finding is overdue", "CRITICAL",
            immediate_email=True,
        ),
        # ── sign-off & closure (WP-41) ──
        EventSpec("SIGNOFF_REQUESTED", "Your sign-off is required", "WARNING"),
        EventSpec("AUDIT_CLOSED", "An audit you were involved in was closed", "INFO"),
        # ── programme (WP-28..30) ──
        EventSpec("SLOT_WINDOW_OPENING", "A planned audit window opens soon", "INFO"),
        EventSpec(
            "COVERAGE_GAP_ESCALATION",
            "A programme coverage gap is overdue",
            "CRITICAL",
            immediate_email=True,
        ),
        EventSpec(
            "DEFERRAL_PENDING_APPROVAL",
            "A slot deferral is awaiting your approval",
            "WARNING",
        ),
    ]
}

IMMEDIATE_EMAIL = {c for c, e in CATALOGUE.items() if e.immediate_email}

# Digest cadence. A user with no preference row gets DAILY, which is the setting
# that keeps people subscribed.
DIGEST_FREQUENCIES = ("IMMEDIATE", "DAILY", "WEEKLY", "OFF")
DEFAULT_FREQUENCY = "DAILY"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def deep_link(entity_type: str, entity_id: str, *, checkpoint_id: str | None = None) -> str:
    """The URL that opens the EXACT record, not its list.

    A notification that lands the user on a register they then have to search is
    a notification they learn to ignore.
    """
    if entity_type == "ComplianceAudit":
        base = f"/cams/audits/{entity_id}"
        return f"{base}?checkpoint={checkpoint_id}" if checkpoint_id else base
    if entity_type == "CamsEngagement":
        return f"/cams/engagements/{entity_id}"
    if entity_type == "CamsFinding":
        return f"/cams/findings/{entity_id}"
    if entity_type == "Capa":
        return f"/capa/{entity_id}"
    if entity_type == "ProgrammeCycle":
        return f"/cams/programme?cycle={entity_id}"
    if entity_type == "AuditProgramme":
        return f"/cams/programme/{entity_id}"
    return "/cams"


async def notify(
    db: AsyncSession,
    *,
    user_ids: Iterable[str | None],
    event: str,
    title: str,
    body: str = "",
    entity_type: str | None = None,
    entity_id: str | None = None,
    checkpoint_id: str | None = None,
    actor_id: str | None = None,
) -> int:
    """Raise an in-app notification for each recipient. Returns the count sent.

    **Never notifies the actor about their own action** - the single most common
    way a notification system trains people to ignore it.
    """
    spec = CATALOGUE.get(event)
    if spec is None:
        raise ValueError(f"Unknown CAMS notification event: {event}")

    targets = {u for u in user_ids if u and u != actor_id}
    if not targets:
        return 0

    link = (
        deep_link(entity_type, entity_id, checkpoint_id=checkpoint_id)
        if entity_type and entity_id
        else None
    )
    for uid in targets:
        db.add(
            Notification(
                userId=uid,
                type=event,
                severity=spec.severity,
                title=title,
                body=body,
                entityType=entity_type,
                entityId=entity_id,
                linkUrl=link,
            )
        )
    await db.flush()
    return len(targets)


async def build_digest(
    db: AsyncSession, *, user_id: str, since: datetime | None = None
) -> dict[str, Any]:
    """Assemble one user's unread CAMS notifications into a single digest.

    Ordered CRITICAL -> WARNING -> INFO, then newest first: a digest that opens
    with an overdue CAPA gets read, one that opens with an assignment does not.
    Returns `{"empty": True}` when there is nothing - and the caller must then
    send NOTHING, because an empty digest is how people learn to filter you.
    """
    since = since or (_utcnow() - timedelta(days=1))
    rows = (
        await db.execute(
            select(Notification).where(
                Notification.userId == user_id,
                Notification.isRead.is_(False),
                Notification.createdAt >= since,
                Notification.type.in_(list(CATALOGUE)),
            )
        )
    ).scalars().all()

    if not rows:
        return {"empty": True, "userId": user_id, "count": 0, "sections": []}

    order = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    rows = sorted(
        rows,
        key=lambda n: (order.get(n.severity, 3), -(n.createdAt.timestamp() if n.createdAt else 0)),
    )

    sections: dict[str, list[dict[str, Any]]] = {}
    for n in rows:
        sections.setdefault(n.severity, []).append(
            {
                "type": n.type,
                "label": CATALOGUE[n.type].label if n.type in CATALOGUE else n.type,
                "title": n.title,
                "body": n.body,
                "linkUrl": n.linkUrl,
                "createdAt": n.createdAt.isoformat() if n.createdAt else None,
            }
        )

    user = await db.get(User, user_id)
    crit = len(sections.get("CRITICAL", []))
    return {
        "empty": False,
        "userId": user_id,
        "userName": user.name if user else user_id,
        "email": user.email if user else None,
        "count": len(rows),
        "criticalCount": crit,
        # The subject line does the work: lead with the count that matters.
        "subject": (
            f"SafeOps360 audit digest - {crit} critical item(s) need you"
            if crit
            else f"SafeOps360 audit digest - {len(rows)} update(s)"
        ),
        "sections": [
            {"severity": sev, "items": sections[sev]}
            for sev in ("CRITICAL", "WARNING", "INFO")
            if sev in sections
        ],
    }


def render_digest_text(digest: dict[str, Any]) -> str:
    """Plain-text digest body. Deterministic, airgap-safe, no templating engine."""
    if digest.get("empty"):
        return ""
    lines = [f"Hello {digest.get('userName', '')},", ""]
    if digest.get("criticalCount"):
        lines.append(
            f"{digest['criticalCount']} item(s) need immediate attention." "\n"
        )
    for sec in digest["sections"]:
        lines.append(f"-- {sec['severity']} --")
        for it in sec["items"]:
            lines.append(f"  * {it['title']}")
            if it.get("body"):
                lines.append(f"    {it['body']}")
            if it.get("linkUrl"):
                lines.append(f"    {it['linkUrl']}")
        lines.append("")
    lines.append("You are receiving this because you are named on these engagements.")
    lines.append("Change your digest frequency in SafeOps360 > Notifications.")
    return "\n".join(lines)


__all__ = [
    "CATALOGUE",
    "EventSpec",
    "IMMEDIATE_EMAIL",
    "DIGEST_FREQUENCIES",
    "DEFAULT_FREQUENCY",
    "deep_link",
    "notify",
    "build_digest",
    "render_digest_text",
]


# ─────────────────────────────────────────────────────────────────────
# WP-43 - per-user preferences
# ─────────────────────────────────────────────────────────────────────
#
# Keyed on an event CLASS, not each of the 14 codes: nobody wants fourteen
# toggles, and a class is the granularity people actually think in.

EVENT_CLASS: dict[str, str] = {
    "AUDITOR_ASSIGNED": "ASSIGNMENT",
    "AUDITEE_ASSIGNED": "ASSIGNMENT",
    "CHECKPOINTS_ALLOCATED": "ASSIGNMENT",
    "ENGAGEMENT_STARTING_INCOMPLETE_TEAM": "EXECUTION",
    "FINDING_ROUTED": "EXECUTION",
    "RESPONSE_RECEIVED": "EXECUTION",
    "REVIEW_REQUESTED": "EXECUTION",
    "CAPA_DUE": "CAPA",
    "CAPA_OVERDUE": "CAPA",
    "SIGNOFF_REQUESTED": "SIGNOFF",
    "AUDIT_CLOSED": "SIGNOFF",
    "SLOT_WINDOW_OPENING": "PROGRAMME",
    "COVERAGE_GAP_ESCALATION": "PROGRAMME",
    "DEFERRAL_PENDING_APPROVAL": "PROGRAMME",
}

EVENT_CLASSES: tuple[str, ...] = ("ASSIGNMENT", "EXECUTION", "CAPA", "SIGNOFF", "PROGRAMME")

CLASS_LABEL: dict[str, str] = {
    "ASSIGNMENT": "Assignments",
    "EXECUTION": "Audit execution",
    "CAPA": "Corrective actions",
    "SIGNOFF": "Sign-off & closure",
    "PROGRAMME": "Audit programme",
}


def event_class(event: str) -> str:
    """Which class an event belongs to. Unknown -> EXECUTION.

    A new event type must inherit a real class rather than falling into a
    bucket nobody has a preference row for, which would mute it silently.
    """
    return EVENT_CLASS.get(event, "EXECUTION")


async def preferences_for(db: AsyncSession, user_id: str) -> list[dict[str, Any]]:
    """Every class with the user's setting, defaults filled in.

    Returns all five classes always: a screen that only lists rows a user has
    already saved is a screen they cannot use to change anything.
    """
    from app.models.cams_completion import NotificationPreference

    rows = (
        await db.execute(
            select(NotificationPreference).where(
                NotificationPreference.userId == user_id,
                NotificationPreference.module == "CAMS",
            )
        )
    ).scalars().all()
    by_class = {r.eventClass: r for r in rows}

    out = []
    for cls in EVENT_CLASSES:
        r = by_class.get(cls)
        events = sorted(e for e, c in EVENT_CLASS.items() if c == cls)
        out.append({
            "eventClass": cls,
            "label": CLASS_LABEL[cls],
            "inAppEnabled": r.inAppEnabled if r else True,
            "emailFrequency": r.emailFrequency if r else DEFAULT_FREQUENCY,
            "isDefault": r is None,
            "eventCount": len(events),
            "events": events,
            # Some events ignore the digest entirely; say so rather than letting
            # a user believe WEEKLY will hold back an overdue CAPA.
            "alwaysImmediate": sorted(e for e in events if e in IMMEDIATE_EMAIL),
        })
    return out


async def set_preference(
    db: AsyncSession, *, user_id: str, event_class_code: str,
    in_app: bool, email_frequency: str,
) -> dict[str, Any]:
    from app.models.cams_completion import NotificationPreference

    cls = (event_class_code or "").upper()
    if cls not in EVENT_CLASSES:
        raise ValueError(f"eventClass must be one of {', '.join(EVENT_CLASSES)}")
    if email_frequency not in DIGEST_FREQUENCIES:
        raise ValueError(f"emailFrequency must be one of {', '.join(DIGEST_FREQUENCIES)}")

    row = (
        await db.execute(
            select(NotificationPreference).where(
                NotificationPreference.userId == user_id,
                NotificationPreference.module == "CAMS",
                NotificationPreference.eventClass == cls,
            )
        )
    ).scalars().first()
    if row is None:
        row = NotificationPreference(userId=user_id, module="CAMS", eventClass=cls)
        db.add(row)
    row.inAppEnabled = in_app
    row.emailFrequency = email_frequency
    await db.flush()
    return {"ok": True, "eventClass": cls}


async def should_deliver(
    db: AsyncSession, *, user_id: str, event: str
) -> dict[str, bool]:
    """Channel decision for one user + one event.

    An IMMEDIATE_EMAIL event overrides an OFF preference. That is deliberate and
    narrow: three events (overdue CAPA, coverage-gap escalation, an engagement
    starting without a team) carry consequences a user cannot opt out of and
    still be doing their job. Everything else honours the setting exactly.
    """
    from app.models.cams_completion import NotificationPreference

    cls = event_class(event)
    row = (
        await db.execute(
            select(NotificationPreference).where(
                NotificationPreference.userId == user_id,
                NotificationPreference.module == "CAMS",
                NotificationPreference.eventClass == cls,
            )
        )
    ).scalars().first()

    in_app = row.inAppEnabled if row else True
    freq = row.emailFrequency if row else DEFAULT_FREQUENCY
    forced = event in IMMEDIATE_EMAIL
    return {
        "inApp": in_app,
        "emailNow": forced or freq == "IMMEDIATE",
        "emailDigest": (not forced) and freq in ("DAILY", "WEEKLY"),
        "overriddenByUrgency": forced and freq == "OFF",
    }
