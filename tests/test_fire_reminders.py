"""Overdue fire-checklist reminders — cadence, recipients, escalation rules.

The failure modes worth testing here are all silent ones:

  * a reminder that fires again every night (no idempotency);
  * an escalation that REPLACES the technician's notice instead of adding to it,
    leaving the only person who can fill the sheet with nothing outstanding;
  * a technician silently guessed when none is assigned, which looks handled;
  * a period reported overdue while it is still open, or missed once closed.

The period maths and the recipient resolution are unit-testable offline; the
sweep itself is exercised live against a deliberately overdue entry.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.services import fire_checklists as svc
from app.services import fire_reminders as rem


# ── cadence: when does a period become overdue ───────────────────────────────
def test_period_end_is_the_last_day_of_the_period():
    assert svc.period_end("DAILY", "2026-08-27") == date(2026, 8, 27)
    assert svc.period_end("MONTHLY", "2026-02") == date(2026, 2, 28)
    assert svc.period_end("MONTHLY", "2024-02") == date(2024, 2, 29)  # leap year
    assert svc.period_end("QUARTERLY", "2026-Q1") == date(2026, 3, 31)
    assert svc.period_end("QUARTERLY", "2026-Q4") == date(2026, 12, 31)
    assert svc.period_end("ANNUAL", "2026") == date(2026, 12, 31)


def test_a_period_still_open_is_never_overdue():
    # THE off-by-one that matters: a monthly sheet is not late on the 3rd of the
    # month it covers, and a daily sheet is not late on the day it covers.
    today = date(2026, 8, 3)
    assert "2026-08" not in svc.overdue_periods("MONTHLY", today, lookback_days=45)
    assert today.isoformat() not in svc.overdue_periods("DAILY", today, lookback_days=5)


def test_the_period_that_just_closed_is_overdue():
    assert svc.overdue_periods("MONTHLY", date(2026, 8, 1), lookback_days=45)[0] == "2026-07"
    assert svc.overdue_periods("DAILY", date(2026, 8, 3), lookback_days=3)[0] == "2026-08-02"


def test_overdue_periods_are_newest_first_and_deduplicated():
    got = svc.overdue_periods("MONTHLY", date(2026, 8, 28), lookback_days=120)
    assert got == sorted(set(got), reverse=True)


def test_the_lookback_window_bounds_the_first_run():
    # Unbounded, the first sweep would mint a row for every month since the
    # register was created, burying the periods anyone can still act on.
    assert svc.overdue_periods("DAILY", date(2026, 8, 28), lookback_days=0) == []
    assert len(svc.overdue_periods("DAILY", date(2026, 8, 28), lookback_days=10)) == 10


def test_lookback_is_per_cadence_so_daily_does_not_swamp_the_first_run(monkeypatch):
    # A flat 45-day window gave a daily checklist 45 overdue periods per asset
    # per template — 329 rows on 37 assets, burying anything still actionable.
    monkeypatch.delenv("FIRE_REMINDER_LOOKBACK_DAYS", raising=False)
    assert rem.lookback_days("DAILY") == 14
    assert rem.lookback_days("MONTHLY") == 62
    assert rem.lookback_days("QUARTERLY") == 200
    assert rem.lookback_days("ANNUAL") == 400


def test_an_explicit_lookback_override_applies_to_every_cadence(monkeypatch):
    monkeypatch.setenv("FIRE_REMINDER_LOOKBACK_DAYS", "30")
    assert rem.lookback_days("DAILY") == 30
    assert rem.lookback_days("ANNUAL") == 30


def test_an_unknown_cadence_does_not_take_the_sweep_down():
    with pytest.raises(svc.ChecklistError):
        svc.period_end("FORTNIGHTLY", "2026-08")


# ── configuration ────────────────────────────────────────────────────────────
def test_escalation_window_defaults_to_three_days(monkeypatch):
    monkeypatch.delenv("FIRE_REMINDER_ESCALATE_DAYS", raising=False)
    assert rem.escalate_after_days() == 3


def test_escalation_window_is_client_configurable(monkeypatch):
    monkeypatch.setenv("FIRE_REMINDER_ESCALATE_DAYS", "7")
    assert rem.escalate_after_days() == 7


def test_a_nonsense_window_falls_back_rather_than_crashing_the_job(monkeypatch):
    monkeypatch.setenv("FIRE_REMINDER_ESCALATE_DAYS", "soon")
    assert rem.escalate_after_days() == 3


def test_unassigned_strategy_defaults_to_report(monkeypatch):
    # The whole point of item 1: with no assignment data, do NOT pick someone.
    monkeypatch.delenv("FIRE_REMINDER_UNASSIGNED_STRATEGY", raising=False)
    assert rem.unassigned_strategy() == "report"


def test_an_unrecognised_strategy_falls_back_to_report(monkeypatch):
    monkeypatch.setenv("FIRE_REMINDER_UNASSIGNED_STRATEGY", "whatever")
    assert rem.unassigned_strategy() == "report"


# ── recipient resolution ─────────────────────────────────────────────────────
class _Db:
    def __init__(self, users=None):
        self._users = {u.id: u for u in (users or [])}

    async def get(self, _model, key):
        return self._users.get(key)


def _asset(**kw):
    base = dict(id="a1", equipmentCode="FE-1", plantId="p1", assignedTechnicianId=None,
                location="Bay", assetSubtype=None, type="FIRE_EXTINGUISHER")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_an_assigned_technician_is_used(monkeypatch):
    monkeypatch.setenv("FIRE_REMINDER_UNASSIGNED_STRATEGY", "report")
    user = SimpleNamespace(id="u1", plantId="p1")
    got, unassigned = await rem.resolve_technician(_Db([user]), _asset(assignedTechnicianId="u1"))
    assert got == "u1" and unassigned is False


@pytest.mark.asyncio
async def test_report_strategy_never_invents_a_technician(monkeypatch):
    monkeypatch.setenv("FIRE_REMINDER_UNASSIGNED_STRATEGY", "report")
    got, unassigned = await rem.resolve_technician(_Db(), _asset())
    assert got is None and unassigned is True


@pytest.mark.asyncio
async def test_an_assignment_to_a_missing_user_reports_unassigned(monkeypatch):
    # Different from "never assigned", and it must not resolve to nobody
    # silently — the row records which so the follow-up differs.
    monkeypatch.setenv("FIRE_REMINDER_UNASSIGNED_STRATEGY", "report")
    got, unassigned = await rem.resolve_technician(_Db(), _asset(assignedTechnicianId="ghost"))
    assert got is None and unassigned is True


@pytest.mark.asyncio
async def test_location_default_strategy_resolves_by_role(monkeypatch):
    monkeypatch.setenv("FIRE_REMINDER_UNASSIGNED_STRATEGY", "location_default")
    tech = SimpleNamespace(id="m1", plantId="p1")

    async def fake_role(db, role, plant_id=None):
        return [tech] if role == "MAINTENANCE_HEAD" else []

    monkeypatch.setattr(rem, "_users_with_role", fake_role)
    got, unassigned = await rem.resolve_technician(_Db(), _asset())
    assert got == "m1" and unassigned is False


@pytest.mark.asyncio
async def test_ehs_lead_falls_through_the_role_chain(monkeypatch):
    lead = SimpleNamespace(id="h1", plantId="p1")

    async def fake_role(db, role, plant_id=None):
        # No PLANT_HSE_HEAD at this site — a real shape in this data.
        return [lead] if role == "HSE_MANAGER" else []

    monkeypatch.setattr(rem, "_users_with_role", fake_role)
    assert [u.id for u in await rem.resolve_ehs_leads(_Db(), "p1")] == ["h1"]


@pytest.mark.asyncio
async def test_escalation_recipients_are_capped(monkeypatch):
    # One role is held by 131 users at a real site in this data. That must not
    # become 131 emails a night.
    many = [SimpleNamespace(id=f"u{i}", plantId="p1") for i in range(131)]

    async def fake_role(db, role, plant_id=None):
        return many if role == "PLANT_HSE_HEAD" else []

    monkeypatch.setattr(rem, "_users_with_role", fake_role)
    assert len(await rem.resolve_ehs_leads(_Db(), "p1")) == 5


@pytest.mark.asyncio
async def test_no_ehs_lead_resolves_to_an_empty_list_not_an_error(monkeypatch):
    async def fake_role(db, role, plant_id=None):
        return []

    monkeypatch.setattr(rem, "_users_with_role", fake_role)
    assert await rem.resolve_ehs_leads(_Db(), "p-with-no-lead") == []


# ── the badge the UI reads ───────────────────────────────────────────────────
class _RemDb:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self._rows))


def _rem_row(**kw):
    base = dict(assetId="a1", state=rem.STATE_NOTIFIED, period="2026-07", templateCode="T",
                frequency="MONTHLY", dueDate=datetime(2026, 7, 31, tzinfo=timezone.utc),
                escalatedAt=None, unassigned=False)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_the_worst_open_state_wins_the_badge():
    # A register row has space for one badge, and "escalated" is the one that
    # needs acting on.
    rows = [_rem_row(period="2026-06", state=rem.STATE_ESCALATED), _rem_row(period="2026-07")]
    out = await rem.open_reminders_for_assets(_RemDb(rows), ["a1"])
    assert out["a1"]["state"] == rem.STATE_ESCALATED
    assert out["a1"]["openCount"] == 2


@pytest.mark.asyncio
async def test_assets_with_nothing_outstanding_get_no_badge():
    assert await rem.open_reminders_for_assets(_RemDb([]), ["a1"]) == {}
    assert await rem.open_reminders_for_assets(_RemDb([]), []) == {}
