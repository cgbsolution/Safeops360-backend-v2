"""06:00 site-local daily digest (build spec 2.2).

The scheduler is interval-based (no cron), so this job runs every 15 min and
fires a subscription only when its site-local clock is inside the 06:00-06:29
window, deduped to one send/day via ``lastSentOn`` (DECISIONS.md D19). The
same alert cards are rendered server-side into a navy/gold HTML email (plus a
plain-text fallback) using the shared renderer below, and delivered through
the platform's existing ``send_email``.

Gated by features.dailyBriefDigest + a configured SMTP transport.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import Alert, AlertSubscription
from app.models.user import Role, User, UserRole

SEVERITY_ORDER = {"critical": 0, "attention": 1, "info": 2}
SEVERITY_MIN_RANK = {"critical": 0, "attention": 1, "info": 2}
_MX = {"navy": "#0B1F4D", "gold": "#C9A961", "ice": "#E8EEF7", "red": "#C0392B", "green": "#2E7D5B"}
_TIER_FROM_SEV = {"critical": "CRITICAL", "attention": "ATTENTION", "info": "WATCH"}


def _tier_label(a: Alert) -> str:
    """Tier chip for the digest (spec §4 content: headline + tier + refs + link).
    Sentinel cards carry an explicit tier in bodyParams; event cards derive it."""
    t = (a.bodyParams or {}).get("tier")
    return (t or _TIER_FROM_SEV.get(a.severity, "INFO")).upper()


def _app_base() -> str:
    return (os.getenv("APP_BASE_URL") or os.getenv("NEXT_PUBLIC_APP_URL") or "").rstrip("/")


def _deep_link(a: Alert) -> str:
    """Absolute deep link back into the platform. Sensitive specifics stay behind
    this authenticated link, not in the email body (spec §4 privacy)."""
    base = _app_base()
    path = a.deepLink or "/dashboard/daily"
    return f"{base}{path}" if base else ""


def _digest_enabled() -> bool:
    if (os.getenv("DAILY_BRIEF_DIGEST") or "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        from app.licensing.state import get_state

        state = get_state()
        return bool(state.payload and state.payload.feature_flags.get("dailyBriefDigest"))
    except Exception:  # noqa: BLE001
        return False


def render_digest_html(site_name: str, date_str: str, alerts: list[Alert]) -> str:
    """Navy header, gold accents, per-severity alert cards. Table-based so it
    survives the plain-safe fallback of most mail clients."""
    rows = []
    for a in alerts:
        color = {"critical": _MX["red"], "attention": _MX["gold"], "info": _MX["ice"]}.get(a.severity, _MX["ice"])
        pills = "".join(
            f'<span style="display:inline-block;border:1px solid {_MX["ice"]};border-radius:12px;'
            f'padding:2px 8px;margin:2px 4px 2px 0;font:12px monospace;color:{_MX["navy"]}">{e.get("ref") or e.get("label")}</span>'
            for e in (a.impactedEntities or [])[:6]
        )
        badge = f'<span style="background:{color};color:#fff;border-radius:10px;padding:2px 8px;font-size:11px;font-weight:700">{_tier_label(a)}</span>'
        count = f' <b>×{a.count}</b>' if a.count > 1 else ""
        link = _deep_link(a)
        link_html = (
            f'<br/><a href="{link}" style="color:{_MX["navy"]};font-size:12px;font-weight:700;text-decoration:none">Open in SafeOps360 →</a>'
            if link
            else ""
        )
        rows.append(
            f'<tr><td style="padding:14px 18px;border-left:4px solid {color};background:#fff">'
            f'{badge}{count}<br/>'
            f'<b style="color:{_MX["navy"]};font-size:15px">{a.title}</b><br/>'
            f'<span style="color:#37415a;font-size:13px">{a.bodyText}</span><br/>{pills}{link_html}</td></tr>'
            '<tr><td style="height:10px"></td></tr>'
        )
    body = "".join(rows) or (
        f'<tr><td style="padding:24px;text-align:center;color:#5A6273">'
        f'No new impacts since yesterday. Have a safe day.</td></tr>'
    )
    return (
        f'<div style="max-width:640px;margin:0 auto;font-family:Calibri,Segoe UI,Arial,sans-serif;background:#F4F7FC;padding:16px">'
        f'<div style="background:{_MX["navy"]};border-bottom:3px solid {_MX["gold"]};padding:22px;border-radius:10px 10px 0 0">'
        f'<div style="color:{_MX["gold"]};font-size:11px;letter-spacing:2px;font-weight:700">DAILY BRIEF</div>'
        f'<div style="color:#fff;font-size:22px;font-family:Georgia,serif">{site_name}</div>'
        f'<div style="color:#c7cfe6;font-size:13px">{date_str}</div></div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:12px">{body}</table>'
        f'<div style="color:#5A6273;font-size:11px;text-align:center;padding:16px">SafeOps360 — this brief is generated from your event feed. '
        f'Open the platform for the live view.</div></div>'
    )


def render_digest_text(site_name: str, date_str: str, alerts: list[Alert]) -> str:
    lines = [f"DAILY BRIEF — {site_name} — {date_str}", ""]
    if not alerts:
        lines.append("No new impacts since yesterday. Have a safe day.")
    for a in alerts:
        lines.append(f"[{_tier_label(a)}]{' x' + str(a.count) if a.count > 1 else ''} {a.title}")
        lines.append(f"    {a.bodyText}")
        refs = ", ".join(e.get("ref") or e.get("label") or "" for e in (a.impactedEntities or [])[:6])
        if refs:
            lines.append(f"    → {refs}")
        link = _deep_link(a)
        if link:
            lines.append(f"    Open: {link}")
        lines.append("")
    return "\n".join(lines)


async def _recipients(db: AsyncSession, role_code: str, site_id: str | None) -> list[User]:
    stmt = (
        select(User)
        .join(UserRole, UserRole.userId == User.id)
        .join(Role, Role.id == UserRole.roleId)
        .where(Role.code == role_code)
    )
    users = (await db.execute(stmt)).scalars().unique().all()
    if site_id:
        scoped = [u for u in users if u.plantId == site_id]
        return scoped or users
    return users


async def run_alert_digest(db: AsyncSession) -> dict:
    if not _digest_enabled():
        return {"skipped": "digest_disabled"}
    from app.services.notifications import send_email

    now_utc = datetime.now(timezone.utc)
    subs = (
        await db.execute(select(AlertSubscription).where(AlertSubscription.active.is_(True)))
    ).scalars().all()

    sent = 0
    skipped_window = 0
    for sub in subs:
        try:
            tz = ZoneInfo(sub.timezone or "Asia/Kolkata")
        except Exception:  # noqa: BLE001
            tz = ZoneInfo("Asia/Kolkata")
        local = now_utc.astimezone(tz)
        today_str = local.strftime("%Y-%m-%d")
        # fire only inside the 06:00-06:29 local window, once per local day
        if not (local.hour == 6 and local.minute < 30):
            skipped_window += 1
            continue
        if sub.lastSentOn == today_str:
            continue

        min_rank = SEVERITY_MIN_RANK.get(sub.minSeverity, 1)
        stmt = (
            select(Alert)
            .where(Alert.isDeleted.is_(False))
            .where(Alert.status.in_(("new", "acknowledged")))
            .where(Alert.createdAt >= now_utc - timedelta(hours=24))
        )
        if sub.siteId:
            stmt = stmt.where(Alert.siteId == sub.siteId)
        alerts = (await db.execute(stmt)).scalars().all()
        alerts = sorted(
            [a for a in alerts if SEVERITY_ORDER.get(a.severity, 3) <= min_rank],
            key=lambda a: (SEVERITY_ORDER.get(a.severity, 3), a.createdAt or now_utc),
        )

        recipients = await _recipients(db, sub.roleCode, sub.siteId)
        emails = [u.email for u in recipients if u.email]
        if emails and "email" in (sub.channels or []):
            site_name = "All sites"
            if sub.siteId:
                from app.models.plant import Plant

                plant = await db.get(Plant, sub.siteId)
                # Goes straight into the digest email subject line.
                site_name = plant.name.split("—")[0].strip() if plant else "Unknown site"
            date_str = local.strftime("%A, %d %B %Y")
            html = render_digest_html(site_name, date_str, alerts)
            text = render_digest_text(site_name, date_str, alerts)
            await send_email(emails, f"[SafeOps360] Daily Brief — {site_name} — {date_str}", text, html=html)
            sent += 1

        sub.lastSentOn = today_str

    await db.commit()
    return {"subscriptions": len(subs), "sent": sent, "outsideWindow": skipped_window}
