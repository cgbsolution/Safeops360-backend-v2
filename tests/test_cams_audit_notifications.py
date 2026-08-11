"""Scheduling an audit must tell the people who are on it.

`create_audit` used to write the row, book the calendar and return — the lead
auditor, co-auditors, plant head and auditees found out from a system calendar
invite or from the auditor arriving. These tests pin the pieces that close it:
the event catalogue, the at-source email rule, the link that survives a dead
session, and the team diff that stops a one-person change re-mailing everybody.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services import cams_audit_notifications as fanout
from app.services.cams_notifications import (
    CATALOGUE,
    EMAIL_AT_SOURCE,
    EVENT_CLASS,
    EVENT_CLASSES,
    IMMEDIATE_EMAIL,
    login_aware_url,
    render_event_email,
    should_deliver,
)


# ── the catalogue ────────────────────────────────────────────────────


def test_every_seat_on_a_scheduled_audit_has_an_event():
    """Four seats are named at scheduling — lead auditor, co-auditor, plant head
    and auditee — and each needs its own event so a user can mute or filter on
    it, and so the message can say what THAT seat is expected to do."""
    for code in ("AUDIT_SCHEDULED", "AUDITOR_ASSIGNED", "AUDITEE_ASSIGNED", "PLANT_HEAD_ASSIGNED"):
        assert code in CATALOGUE, code
        assert EVENT_CLASS[code] in EVENT_CLASSES


def test_assignment_is_emailed_at_source_not_batched():
    """The whole value of "you are on Thursday's audit" is lead time. Holding it
    for a daily digest can deliver it after the audit has run."""
    assert {
        "AUDIT_SCHEDULED", "AUDITOR_ASSIGNED", "AUDITEE_ASSIGNED", "PLANT_HEAD_ASSIGNED",
    } <= EMAIL_AT_SOURCE


def test_at_source_is_not_the_same_lever_as_urgency():
    """IMMEDIATE_EMAIL overrides an OFF preference; at-source does not. Muddling
    them would make every audit assignment un-muteable, which is how a channel
    gets filtered wholesale."""
    assert not (EMAIL_AT_SOURCE & IMMEDIATE_EMAIL)


def test_high_volume_events_stay_in_the_digest():
    """Assignment fires once per person per audit. Checkpoint-level events fire
    hundreds of times on one engagement and must keep batching."""
    for code in ("CHECKPOINTS_ALLOCATED", "FINDING_ROUTED", "RESPONSE_RECEIVED"):
        assert code not in EMAIL_AT_SOURCE, code


# ── preference resolution ────────────────────────────────────────────


class _NoPreferenceRow:
    """A session that finds no saved preference — i.e. every user, by default."""

    async def execute(self, _stmt):
        class _R:
            def scalars(self_inner):
                class _S:
                    def first(self_s):
                        return None

                return _S()

        return _R()


class _Row:
    def __init__(self, in_app: bool, freq: str):
        self.inAppEnabled = in_app
        self.emailFrequency = freq


class _WithPreference:
    def __init__(self, row: _Row):
        self._row = row

    async def execute(self, _stmt):
        row = self._row

        class _R:
            def scalars(self_inner):
                class _S:
                    def first(self_s):
                        return row

                return _S()

        return _R()


@pytest.mark.asyncio
async def test_default_user_gets_the_assignment_email_immediately():
    """Default frequency is DAILY. An at-source event must still send now —
    that is the entire point of the flag."""
    out = await should_deliver(_NoPreferenceRow(), user_id="u1", event="AUDITOR_ASSIGNED")
    assert out["inApp"] is True
    assert out["emailNow"] is True
    assert out["emailDigest"] is False  # not sent twice


@pytest.mark.asyncio
async def test_turning_assignment_email_off_is_respected():
    """Unlike an overdue CAPA, an assignment is something a user is allowed to
    mute — so OFF means off, and `overriddenByUrgency` stays false."""
    db = _WithPreference(_Row(in_app=True, freq="OFF"))
    out = await should_deliver(db, user_id="u1", event="AUDITEE_ASSIGNED")
    assert out["emailNow"] is False
    assert out["emailDigest"] is False
    assert out["overriddenByUrgency"] is False


@pytest.mark.asyncio
async def test_urgent_events_still_override_off():
    """The narrow exception survives the new flag."""
    db = _WithPreference(_Row(in_app=True, freq="OFF"))
    out = await should_deliver(db, user_id="u1", event="CAPA_OVERDUE")
    assert out["emailNow"] is True
    assert out["overriddenByUrgency"] is True


# ── links that survive a dead session ────────────────────────────────


def test_email_link_routes_through_go_so_login_returns_you(monkeypatch):
    """The reader is in Outlook and half the time signed out. A bare path would
    bounce them to /login and then drop them on the dashboard, losing the
    record the email existed to deliver."""
    monkeypatch.setenv("APP_PUBLIC_URL", "https://safeops.example.com/")
    url = login_aware_url("/cams/audits/a1/conduct")
    assert url == "https://safeops.example.com/go?to=%2Fcams%2Faudits%2Fa1%2Fconduct"


def test_link_degrades_to_a_path_when_no_public_url_is_set(monkeypatch):
    """Local dev and tests have no APP_PUBLIC_URL. Emitting a broken absolute
    URL would be worse than a relative one."""
    monkeypatch.delenv("APP_PUBLIC_URL", raising=False)
    assert login_aware_url("/cams/audits/a1") == "/cams/audits/a1"


def test_query_strings_in_the_destination_survive_encoding(monkeypatch):
    monkeypatch.setenv("APP_PUBLIC_URL", "https://x.test")
    url = login_aware_url("/cams/audits/a1?checkpoint=c9")
    assert "%3Fcheckpoint%3Dc9" in url


# ── the rendered email ───────────────────────────────────────────────


def test_email_carries_the_link_in_both_parts():
    """A recipient whose client strips HTML must still be able to get there."""
    text, html = render_event_email(
        recipient_name="Asha",
        title="You are the lead auditor for AC-GT-0007",
        body="Scheduled for 18 Aug 2026 at 09:00.",
        link="https://x.test/go?to=%2Fcams%2Faudits%2Fa1",
        facts=[("Site", "Tirupur (TIR)")],
    )
    for part in (text, html):
        assert "https://x.test/go?to=%2Fcams%2Faudits%2Fa1" in part
        assert "AC-GT-0007" in part
    assert "Asha" in text
    assert "Tirupur (TIR)" in html


def test_rendered_html_escapes_the_audit_title():
    """Audit titles are free text typed by a scheduler. Interpolated raw, one
    containing markup would break the mail — or inject into it."""
    _, html = render_event_email(
        recipient_name=None,
        title="<script>alert(1)</script>",
        body="",
        link="https://x.test/go",
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ── the team diff ────────────────────────────────────────────────────


class _Audit:
    def __init__(self, **kw):
        self.id = kw.get("id", "a1")
        self.auditNumber = kw.get("auditNumber", "AC-GT-0007")
        self.title = kw.get("title", "Q3 Integrated Audit")
        self.plantId = kw.get("plantId")
        self.scheduledDate = kw.get("scheduledDate")
        self.scheduledStartTime = kw.get("scheduledStartTime", "09:00")
        self.totalCheckpoints = kw.get("totalCheckpoints", 120)
        self.leadAuditorUserId = kw.get("leadAuditorUserId")
        self.coAuditors = kw.get("coAuditors", [])
        self.auditees = kw.get("auditees", [])
        self.plantManagerUserId = kw.get("plantManagerUserId")


def test_team_ids_read_both_stored_shapes():
    """`update_audit_team` accepts a bare id as well as a dict, so both shapes
    exist in the JSON column."""
    assert fanout._ids([{"userId": "u1"}, "u2", {"userId": "u1"}]) == ["u1", "u2"]
    assert fanout._ids(None) == []


def test_snapshot_is_taken_before_the_columns_are_overwritten():
    """The point of the snapshot: `update_audit_team` mutates in place, so the
    previous cast is unrecoverable afterwards."""
    audit = _Audit(
        leadAuditorUserId="lead",
        coAuditors=[{"userId": "co1"}],
        auditees=[{"userId": "ae1"}],
        plantManagerUserId="head",
    )
    before = fanout.team_snapshot(audit)
    audit.coAuditors = [{"userId": "co1"}, {"userId": "co2"}]
    assert before["co"] == {"co1"}
    assert before["lead"] == "lead"
    assert before["plantHead"] == "head"


def test_auditors_land_on_conduct_and_everyone_else_on_the_audit():
    """A lead auditor sent to the read-only detail page has to find the conduct
    screen themselves; a plant head sent to the conduct screen is being offered
    a job that is not theirs."""
    assert fanout._audit_path("a1", conducting=True) == "/cams/audits/a1/conduct"
    assert fanout._audit_path("a1", conducting=False) == "/cams/audits/a1"


def test_each_person_is_told_their_own_disciplines():
    """A co-auditor on two of eleven disciplines needs to know WHICH two."""
    co = [{"userId": "co1", "disciplineIds": ["FIRE", "EHS"]}, {"userId": "co2", "disciplineIds": []}]
    assert fanout._disciplines_for(co, "co1", "disciplineIds") == ["FIRE", "EHS"]
    assert fanout._disciplines_for(co, "co2", "disciplineIds") == []
    assert fanout._disciplines_for(co, "nobody", "disciplineIds") == []


def test_the_date_line_reads_as_a_date():
    audit = _Audit(scheduledDate=datetime(2026, 8, 18, 9, 0), scheduledStartTime="09:00")
    assert fanout._when(audit) == "18 Aug 2026 at 09:00"


def test_a_missing_date_says_so_rather_than_crashing():
    """Programme-materialised audits can arrive without a confirmed date."""
    assert fanout._when(_Audit(scheduledDate=None)) == "date to be confirmed"


@pytest.mark.asyncio
async def test_fanout_never_raises_into_the_transaction():
    """`create_audit` calls this mid-transaction. An audit must not fail to be
    created because a notification could not be sent — so a broken audit object
    returns ok=False instead of propagating."""
    out = await fanout.notify_audit_scheduled(None, audit=object(), actor_id="u1")
    assert out["ok"] is False
    assert out["email"] == 0
