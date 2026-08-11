"""Tell the audit team they are on the audit.

Scheduling an audit used to be silent. `create_audit` wrote the row, booked the
calendar and returned — and the lead auditor, the co-auditors, the plant head
and every auditee found out either from the calendar invite (which says when,
not what, and is routinely declined unread) or from the auditor turning up.

This module closes that. Every seat named on an audit gets, at the moment they
are seated:

  • an in-app notification, which is the bell/Inbox record, and
  • an email — sent at source, not batched into tomorrow's digest, because the
    entire value of the message is lead time,

both carrying a **role-specific** deep link. A lead auditor is sent to the
conduct screen; an auditee is sent to the audit they will have to answer for.
The link is a `/go` URL, so it works from Outlook whether or not the reader's
session is still alive (see `cams_notifications.login_aware_url`).

**Best-effort by contract.** Every public function here swallows its own
exceptions. `create_audit` calls into this mid-transaction, and an audit must
never fail to be created because an SMTP server was down — exactly the
discipline `calendar_booking.sync_engagement` already follows.

**Never notifies the actor.** The person clicking "Schedule audit" does not
need an email telling them they scheduled an audit; `deliver` drops the actor.
"""

from __future__ import annotations

import sys
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plant import Plant
from app.services import cams_notifications as notif

# Where each seat should LAND. The register is not a destination — a link that
# opens a list the recipient then has to search is the thing that trains people
# to stop clicking notification links.
_CONDUCT = "/cams/audits/{id}/conduct"
_DETAIL = "/cams/audits/{id}"


def _audit_path(audit_id: str, *, conducting: bool) -> str:
    return (_CONDUCT if conducting else _DETAIL).format(id=audit_id)


def _ids(rows: Iterable[Any] | None) -> list[str]:
    """`[{"userId": "u1", ...}, "u2"]` → `["u1", "u2"]`.

    The team columns are JSON and have accumulated both shapes over time —
    `update_audit_team` accepts a bare id as well as a dict.
    """
    out: list[str] = []
    for r in rows or []:
        uid = r.get("userId") if isinstance(r, dict) else r
        if uid and uid not in out:
            out.append(uid)
    return out


def _disciplines_for(rows: Iterable[Any] | None, user_id: str, key: str) -> list[str]:
    for r in rows or []:
        if isinstance(r, dict) and r.get("userId") == user_id:
            return [c for c in (r.get(key) or []) if c]
    return []


def _when(audit) -> str:
    """"18 Aug 2026 at 09:00" — the single most-read line in the email."""
    d = getattr(audit, "scheduledDate", None)
    if d is None:
        return "date to be confirmed"
    day = d.strftime("%d %b %Y")
    start = (getattr(audit, "scheduledStartTime", None) or "").strip()
    return f"{day} at {start}" if start else day


async def _facts(db: AsyncSession, audit) -> list[tuple[str, str]]:
    """The four things every recipient asks before opening the link."""
    plant = None
    if getattr(audit, "plantId", None):
        try:
            plant = await db.get(Plant, audit.plantId)
        except Exception:  # noqa: BLE001 — a missing plant must not stop the email
            plant = None
    rows: list[tuple[str, str]] = [
        ("Audit", f"{audit.auditNumber} — {audit.title}"),
        ("Scheduled", _when(audit)),
    ]
    if plant is not None:
        rows.append(("Site", f"{plant.name} ({plant.code})"))
    total = getattr(audit, "totalCheckpoints", None)
    if total:
        rows.append(("Checkpoints", str(total)))
    return rows


async def _send(
    db: AsyncSession,
    *,
    audit,
    user_ids: list[str],
    event: str,
    title: str,
    body: str,
    conducting: bool,
    actor_id: str | None,
    cta: str,
    facts: list[tuple[str, str]],
) -> dict[str, int]:
    if not user_ids:
        return {"inApp": 0, "email": 0}
    return await notif.deliver(
        db,
        user_ids=user_ids,
        event=event,
        title=title,
        body=body,
        entity_type="ComplianceAudit",
        entity_id=audit.id,
        link_path=_audit_path(audit.id, conducting=conducting),
        actor_id=actor_id,
        cta=cta,
        facts=facts,
    )


# ─────────────────────────────────────────────────────────────────────
# Public surface
# ─────────────────────────────────────────────────────────────────────


async def notify_audit_scheduled(
    db: AsyncSession, *, audit, actor_id: str | None = None
) -> dict[str, Any]:
    """Fan out to the whole cast the moment an audit is scheduled.

    Four messages, not one broadcast, because the four seats have genuinely
    different jobs: the lead owns the audit, a co-auditor owns their
    disciplines, the plant head reviews, and an auditee answers findings. One
    generic "you're on an audit" would leave each of them guessing which.
    """
    try:
        facts = await _facts(db, audit)
        lead = getattr(audit, "leadAuditorUserId", None)
        co_ids = [u for u in _ids(getattr(audit, "coAuditors", None)) if u != lead]
        auditee_ids = [
            u for u in _ids(getattr(audit, "auditees", None))
            if u != lead and u not in co_ids
        ]
        plant_head = getattr(audit, "plantManagerUserId", None)

        sent = {"inApp": 0, "email": 0}

        def _add(r: dict[str, int]) -> None:
            sent["inApp"] += r.get("inApp", 0)
            sent["email"] += r.get("email", 0)

        # ── Lead auditor ──
        if lead:
            _add(await _send(
                db, audit=audit, user_ids=[lead], event="AUDITOR_ASSIGNED",
                title=f"You are the lead auditor for {audit.auditNumber}",
                body=(
                    f"You have been named lead auditor for “{audit.title}”, scheduled "
                    f"for {_when(audit)}.\n\n"
                    "As lead you own the plan, conduct any discipline not assigned to a "
                    "co-auditor, and raise the findings."
                ),
                conducting=True, actor_id=actor_id,
                cta="Open the audit", facts=facts,
            ))

        # ── Co-auditors — each told which disciplines are THEIRS ──
        for uid in co_ids:
            mine = _disciplines_for(getattr(audit, "coAuditors", None), uid, "disciplineIds")
            scope = (
                f"You are assigned {len(mine)} discipline(s): {', '.join(mine)}."
                if mine
                else "Your disciplines will be allocated by the lead auditor."
            )
            _add(await _send(
                db, audit=audit, user_ids=[uid], event="AUDITOR_ASSIGNED",
                title=f"You are a co-auditor on {audit.auditNumber}",
                body=(
                    f"You have been named a co-auditor on “{audit.title}”, scheduled "
                    f"for {_when(audit)}.\n\n{scope}"
                ),
                conducting=True, actor_id=actor_id,
                cta="Open the audit", facts=facts,
            ))

        # ── Plant head / reviewer ──
        if plant_head:
            _add(await _send(
                db, audit=audit, user_ids=[plant_head], event="PLANT_HEAD_ASSIGNED",
                title=f"You are the reviewing plant head for {audit.auditNumber}",
                body=(
                    f"“{audit.title}” is scheduled for {_when(audit)} at your site.\n\n"
                    "Findings escalated during the audit come to you, and the audit "
                    "cannot close without your review."
                ),
                conducting=False, actor_id=actor_id,
                cta="Review the audit", facts=facts,
            ))

        # ── Auditees — each told which disciplines they answer for ──
        for uid in auditee_ids:
            mine = _disciplines_for(
                getattr(audit, "auditees", None), uid, "responsibleCategories"
            )
            scope = (
                f"You are the responsible owner for {len(mine)} discipline(s): "
                f"{', '.join(mine)}."
                if mine
                else "Your responsible disciplines will be confirmed at the opening meeting."
            )
            _add(await _send(
                db, audit=audit, user_ids=[uid], event="AUDITEE_ASSIGNED",
                title=f"You are an auditee on {audit.auditNumber}",
                body=(
                    f"“{audit.title}” is scheduled for {_when(audit)}.\n\n{scope}\n\n"
                    "Any checkpoint that fails in your area will be routed to you for a "
                    "response and corrective action."
                ),
                conducting=False, actor_id=actor_id,
                cta="Open the audit", facts=facts,
            ))

        return {"ok": True, **sent}
    except Exception as e:  # noqa: BLE001 — see the module docstring
        print(
            f"[cams_audit_notifications] schedule fan-out failed for "
            f"{getattr(audit, 'id', '?')}: {e}",
            file=sys.stderr,
        )
        return {"ok": False, "error": str(e), "inApp": 0, "email": 0}


def team_snapshot(audit) -> dict[str, Any]:
    """Who is seated right now — capture BEFORE mutating, compare after.

    `update_audit_team` overwrites the JSON columns in place, so the previous
    cast is unrecoverable by the time the notification decision is made. Without
    this, a team edit that adds one auditee would re-email the whole team.
    """
    return {
        "lead": getattr(audit, "leadAuditorUserId", None),
        "co": set(_ids(getattr(audit, "coAuditors", None))),
        "auditees": set(_ids(getattr(audit, "auditees", None))),
        "plantHead": getattr(audit, "plantManagerUserId", None),
    }


async def notify_team_changed(
    db: AsyncSession, *, audit, before: dict[str, Any], actor_id: str | None = None
) -> dict[str, Any]:
    """Notify only the seats that are NEW since `before`.

    Deliberately additive-only. Re-notifying someone who was already on the team
    because a colleague was added elsewhere is precisely how a notification
    channel gets muted, and "you have been removed" is a message the audit
    record does not currently support sending honestly (a removal is often a
    re-seat within the same call).
    """
    try:
        facts = await _facts(db, audit)
        lead = getattr(audit, "leadAuditorUserId", None)
        co_now = set(_ids(getattr(audit, "coAuditors", None)))
        au_now = set(_ids(getattr(audit, "auditees", None)))
        head_now = getattr(audit, "plantManagerUserId", None)

        new_co = [u for u in _ids(getattr(audit, "coAuditors", None))
                  if u in co_now - before.get("co", set()) and u != lead]
        new_au = [u for u in _ids(getattr(audit, "auditees", None))
                  if u in au_now - before.get("auditees", set())]
        new_head = head_now if head_now and head_now != before.get("plantHead") else None
        new_lead = lead if lead and lead != before.get("lead") else None

        sent = {"inApp": 0, "email": 0}

        def _add(r: dict[str, int]) -> None:
            sent["inApp"] += r.get("inApp", 0)
            sent["email"] += r.get("email", 0)

        if new_lead:
            _add(await _send(
                db, audit=audit, user_ids=[new_lead], event="AUDITOR_ASSIGNED",
                title=f"You are now the lead auditor for {audit.auditNumber}",
                body=(
                    f"The team on “{audit.title}” ({_when(audit)}) has been changed and "
                    "you are now its lead auditor."
                ),
                conducting=True, actor_id=actor_id,
                cta="Open the audit", facts=facts,
            ))

        for uid in new_co:
            mine = _disciplines_for(getattr(audit, "coAuditors", None), uid, "disciplineIds")
            scope = (
                f"You are assigned {len(mine)} discipline(s): {', '.join(mine)}."
                if mine
                else "Your disciplines will be allocated by the lead auditor."
            )
            _add(await _send(
                db, audit=audit, user_ids=[uid], event="AUDITOR_ASSIGNED",
                title=f"You have been added as a co-auditor on {audit.auditNumber}",
                body=(
                    f"You have been added to the team on “{audit.title}”, scheduled for "
                    f"{_when(audit)}.\n\n{scope}"
                ),
                conducting=True, actor_id=actor_id,
                cta="Open the audit", facts=facts,
            ))

        if new_head:
            _add(await _send(
                db, audit=audit, user_ids=[new_head], event="PLANT_HEAD_ASSIGNED",
                title=f"You are now the reviewing plant head for {audit.auditNumber}",
                body=(
                    f"“{audit.title}” is scheduled for {_when(audit)} at your site. "
                    "Escalated findings come to you and the audit cannot close without "
                    "your review."
                ),
                conducting=False, actor_id=actor_id,
                cta="Review the audit", facts=facts,
            ))

        for uid in new_au:
            mine = _disciplines_for(
                getattr(audit, "auditees", None), uid, "responsibleCategories"
            )
            scope = (
                f"You are the responsible owner for {len(mine)} discipline(s): "
                f"{', '.join(mine)}."
                if mine
                else "Your responsible disciplines will be confirmed by the lead auditor."
            )
            _add(await _send(
                db, audit=audit, user_ids=[uid], event="AUDITEE_ASSIGNED",
                title=f"You have been named an auditee on {audit.auditNumber}",
                body=(
                    f"“{audit.title}” is scheduled for {_when(audit)}.\n\n{scope}\n\n"
                    "Any checkpoint that fails in your area will be routed to you for a "
                    "response and corrective action."
                ),
                conducting=False, actor_id=actor_id,
                cta="Open the audit", facts=facts,
            ))

        return {"ok": True, **sent}
    except Exception as e:  # noqa: BLE001
        print(
            f"[cams_audit_notifications] team-change fan-out failed for "
            f"{getattr(audit, 'id', '?')}: {e}",
            file=sys.stderr,
        )
        return {"ok": False, "error": str(e), "inApp": 0, "email": 0}


__all__ = [
    "notify_audit_scheduled",
    "notify_team_changed",
    "team_snapshot",
]
