"""Notification fan-out: email (SMTP / nodemailer-equivalent) + SMS
(MSG91 default, Twilio fallback). Both are best-effort and configured
via environment variables; if creds are missing, calls log + return
without raising.

Used by:
  • auto_promote_near_miss.py — Critical-severity alerts to Plant HSE
    Manager, Plant Head, Corporate HSE
  • Future workflow escalation cron
"""

from __future__ import annotations

import asyncio
import os
import smtplib
import sys
from email.mime.text import MIMEText
from typing import Iterable

import httpx


def _env(*names: str) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return None


# ─── Email (SMTP / nodemailer-compatible) ────────────────────────────


def _send_email_sync(to_addrs: list[str], subject: str, body: str) -> bool:
    host = _env("SMTP_HOST")
    user = _env("SMTP_USER")
    password = _env("SMTP_PASS", "SMTP_PASSWORD")
    sender = _env("EMAIL_FROM", "SMTP_USER", "FROM_EMAIL") or user
    if not host or not user or not password or not sender or not to_addrs:
        print(
            f"[notify.email] skipped — SMTP not fully configured (host={bool(host)}, user={bool(user)})",
            file=sys.stderr,
        )
        return False
    port = int(_env("SMTP_PORT") or 587)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender if "<" in sender else f"SafeOps360 <{sender}>"
    msg["To"] = ", ".join(to_addrs)
    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            smtp.ehlo()
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPException:
                pass
            smtp.login(user, password)
            smtp.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[notify.email] failed: {e}", file=sys.stderr)
        return False


async def send_email(to_addrs: Iterable[str], subject: str, body: str) -> bool:
    addrs = [a for a in to_addrs if a]
    if not addrs:
        return False
    # smtplib is sync — run in default thread pool to keep the event loop free.
    return await asyncio.to_thread(_send_email_sync, addrs, subject, body)


# ─── SMS (MSG91 first; Twilio fallback) ──────────────────────────────


async def _send_sms_msg91(to: str, message: str) -> bool:
    auth_key = _env("MSG91_AUTH_KEY")
    sender = _env("MSG91_SENDER", "MSG91_SENDER_ID") or "SAFEOP"
    template_id = _env("MSG91_TEMPLATE_ID")
    if not auth_key:
        return False
    # Strip + and spaces so MSG91 receives 91XXXXXXXXXX
    number = to.replace("+", "").replace(" ", "").replace("-", "")
    url = "https://api.msg91.com/api/v5/flow/" if template_id else "https://api.msg91.com/api/v5/sms"
    payload: dict[str, str | dict] = (
        {"flow_id": template_id, "sender": sender, "mobiles": number, "VAR1": message[:200]}
        if template_id
        else {"sender": sender, "route": "4", "country": "91", "sms": [{"message": message[:300], "to": [number]}]}
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers={"authkey": auth_key})
            r.raise_for_status()
            return True
    except Exception as e:  # noqa: BLE001
        print(f"[notify.sms.msg91] failed: {e}", file=sys.stderr)
        return False


async def _send_sms_twilio(to: str, message: str) -> bool:
    sid = _env("TWILIO_ACCOUNT_SID")
    token = _env("TWILIO_AUTH_TOKEN")
    from_num = _env("TWILIO_FROM")
    if not sid or not token or not from_num:
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    try:
        async with httpx.AsyncClient(timeout=10, auth=(sid, token)) as client:
            r = await client.post(url, data={"From": from_num, "To": to, "Body": message[:300]})
            r.raise_for_status()
            return True
    except Exception as e:  # noqa: BLE001
        print(f"[notify.sms.twilio] failed: {e}", file=sys.stderr)
        return False


async def send_sms(to_numbers: Iterable[str], message: str) -> int:
    """Try MSG91 first (cheapest, India-friendly), fall back to Twilio.
    Returns the count of successful sends. Never raises."""
    sent = 0
    nums = [n for n in to_numbers if n]
    if not nums:
        return 0
    has_msg91 = _env("MSG91_AUTH_KEY") is not None
    has_twilio = _env("TWILIO_ACCOUNT_SID") is not None
    if not has_msg91 and not has_twilio:
        print(f"[notify.sms] skipped — no provider configured ({len(nums)} number(s))", file=sys.stderr)
        return 0
    for n in nums:
        ok = False
        if has_msg91:
            ok = await _send_sms_msg91(n, message)
        if not ok and has_twilio:
            ok = await _send_sms_twilio(n, message)
        if ok:
            sent += 1
    return sent
