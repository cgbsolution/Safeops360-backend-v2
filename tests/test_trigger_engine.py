"""Shared trigger engine — offline unit tests.

These exist because the platform previously had zero tests over any of its three
post-closure rule runners, which is the mechanical reason a trigger could fire 0
times across 22 production closures without anyone noticing. Every guarantee the
engine advertises is asserted here, with a fake session and no database — the
house style (see test_alert_rules.py).

The guarantees under test:
  1. one rule's exception does not stop the others
  2. a FAILED result ALWAYS carries a non-empty failure reason
  3. someone is notified when a rule fails
  4. a sink failure is reported to the caller, not swallowed
  5. SKIPPED and FAILED are never conflated
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import trigger_engine as te
from app.services.trigger_engine import (
    TriggerOutcome,
    TriggerResult,
    adapt_dict_rule,
    json_column_sink,
    run_trigger_rules,
)


# ── fakes ─────────────────────────────────────────────────────────────────────
class _Nested:
    def __init__(self, session): self.session = session
    async def __aenter__(self): self.session.savepoints += 1; return self
    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.session.rollbacks += 1
        return False  # never suppress; the engine's own except must see it


class FakeSession:
    """Minimal AsyncSession stand-in: only what the engine touches."""

    def __init__(self):
        self.savepoints = 0
        self.rollbacks = 0
        self.added = []
        self.flushes = 0

    def begin_nested(self): return _Nested(self)
    def add(self, obj): self.added.append(obj)
    async def flush(self): self.flushes += 1
    async def get(self, model, pk): return None


@pytest.fixture(autouse=True)
def _stub_platform(monkeypatch):
    """Stub the notification + event + role-lookup seams so the engine can be
    exercised without a database. Recorded calls are the assertions."""
    sent: list[dict] = []
    events: list[dict] = []

    async def _fake_create_notification(db, **kw):
        sent.append(kw)
        return SimpleNamespace(id="n1")

    async def _fake_users_with_role(db, role, plant_id=None):
        return [SimpleNamespace(id="hse-1", plantId=plant_id)]

    def _fake_emit(db, **kw):
        events.append(kw)
        return SimpleNamespace(id="e1")

    import app.services.erm_notifications as notif
    import app.services.events as ev

    monkeypatch.setattr(notif, "create_notification", _fake_create_notification)
    monkeypatch.setattr(notif, "_users_with_role", _fake_users_with_role)
    monkeypatch.setattr(ev, "emit", _fake_emit)
    return SimpleNamespace(sent=sent, events=events)


def _run(coro):
    return asyncio.run(coro)


# ── rules used by the tests ───────────────────────────────────────────────────
async def rule_ok(db, subject):
    return TriggerResult("Good rule", outcome=TriggerOutcome.FIRED, reason="did the thing",
                         spawned_record_type="MOC", spawned_record_id="moc-1")


async def rule_skips(db, subject):
    return TriggerResult("Quiet rule", outcome=TriggerOutcome.SKIPPED, reason="not applicable")


async def rule_explodes(db, subject):
    raise KeyError("plantId")


async def rule_explodes_silently(db, subject):
    # The nastiest case: an exception whose str() is empty. A naive
    # `str(exc)` failure reason would persist "" and the audit row would say a
    # failure happened with no indication of what.
    raise ValueError()


async def rule_returns_none(db, subject):
    return None


# ── tests ─────────────────────────────────────────────────────────────────────
def test_one_rule_failing_does_not_stop_the_others(_stub_platform):
    db = FakeSession()
    run = _run(run_trigger_rules(
        db, [rule_explodes, rule_ok, rule_skips], subject=None,
        source_kind="Incident", source_id="i-1",
    ))
    assert [r.outcome for r in run.results] == [
        TriggerOutcome.FAILED, TriggerOutcome.FIRED, TriggerOutcome.SKIPPED
    ]
    # Every rule got its own savepoint — that is what the isolation is made of.
    assert db.savepoints >= 3


def test_failed_result_always_carries_a_non_empty_reason(_stub_platform):
    """The spec calls this out for MocTriggerLog.failureReason: it must never be
    silently empty on a failure. `str(ValueError())` is '' — the engine has to
    fall back to the class name."""
    db = FakeSession()
    run = _run(run_trigger_rules(
        db, [rule_explodes_silently, rule_explodes], subject=None,
        source_kind="ChemicalThresholdBreach", source_id="plant-1",
    ))
    assert len(run.failed) == 2
    for r in run.failed:
        assert r.failure_reason
        assert r.failure_reason.strip()
    assert "ValueError" in run.failed[0].failure_reason
    assert "KeyError" in run.failed[1].failure_reason
    # Stack traces are kept for the 3am page, out of the short reason field.
    assert all(r.stack for r in run.failed)


def test_failed_result_keeps_the_rule_id_declared_on_the_callable(_stub_platform):
    """Regression: a rule that RAISES cannot attach its own rule_id, so the
    engine must take it from the callable.

    Caught by CHEM-T12 against a real database — the MocTriggerLog row was
    written with ruleId NULL, so a failed trigger recorded that *something* had
    failed but not WHICH statutory obligation went unraised. That is only
    marginally better than the silent failure this engine exists to replace,
    and it is invisible unless something asserts on it."""
    async def exploding(db, subject):
        raise RuntimeError("downstream 500")

    exploding.trigger_id = "rule_threshold_abc123"
    exploding.trigger_name = "Threshold breach — MSIHC Schedule 2"

    db = FakeSession()
    run = _run(run_trigger_rules(
        db, [exploding], subject=None,
        source_kind="ChemicalThresholdBreach", source_id="plant-1",
    ))
    assert run.failed[0].rule_id == "rule_threshold_abc123"
    assert run.failed[0].rule_name == "Threshold breach — MSIHC Schedule 2"
    assert run.failed[0].to_audit_entry()["ruleId"] == "rule_threshold_abc123"


def test_rule_result_without_an_id_inherits_the_callables(_stub_platform):
    async def quiet(db, subject):
        return TriggerResult("X", outcome=TriggerOutcome.SKIPPED, reason="n/a")

    quiet.trigger_id = "rule_threshold_zzz"
    db = FakeSession()
    run = _run(run_trigger_rules(
        db, [quiet], subject=None, source_kind="X", source_id="1",
    ))
    assert run.results[0].rule_id == "rule_threshold_zzz"


def test_a_human_is_notified_when_a_rule_fails(_stub_platform):
    db = FakeSession()
    _run(run_trigger_rules(
        db, [rule_explodes], subject=None,
        source_kind="ChemicalThresholdBreach", source_id="plant-1", site_id="plant-1",
    ))
    assert len(_stub_platform.sent) == 1
    msg = _stub_platform.sent[0]
    assert msg["type"] == "TRIGGER_RULE_FAILED"
    assert msg["severity"] == "CRITICAL"
    # The body must say what did NOT happen, not just that something errored.
    assert "was NOT created" in msg["body"]
    assert "KeyError" in msg["body"]


def test_success_notifies_nobody(_stub_platform):
    db = FakeSession()
    _run(run_trigger_rules(
        db, [rule_ok, rule_skips], subject=None, source_kind="Incident", source_id="i-2",
    ))
    assert _stub_platform.sent == []


def test_fired_and_failed_emit_events_but_skipped_does_not(_stub_platform):
    db = FakeSession()
    _run(run_trigger_rules(
        db, [rule_ok, rule_skips, rule_explodes], subject=None,
        source_kind="Incident", source_id="i-3", site_id="plant-9",
    ))
    kinds = [e["event_type"] for e in _stub_platform.events]
    assert kinds == [te.TRIGGER_FIRED, te.TRIGGER_FAILED]


def test_sink_failure_is_reported_not_swallowed(_stub_platform):
    """The observation runner used to `print()` a persistence failure and carry
    on, so a failed write left no trace the run had happened at all."""
    db = FakeSession()

    async def exploding_sink(db_, subject, results):
        raise RuntimeError("audit table is read-only")

    run = _run(run_trigger_rules(
        db, [rule_ok], subject=None, source_kind="Incident", source_id="i-4",
        sink=exploding_sink,
    ))
    assert run.sink_failed is True
    assert "audit table is read-only" in run.sink_error
    # And a human hears about it even though no RULE failed.
    assert len(_stub_platform.sent) == 1
    assert "Audit trail could not be written" in _stub_platform.sent[0]["body"]


def test_rule_returning_none_is_skipped_not_failed(_stub_platform):
    db = FakeSession()
    run = _run(run_trigger_rules(
        db, [rule_returns_none], subject=None, source_kind="Incident", source_id="i-5",
    ))
    assert run.results[0].outcome is TriggerOutcome.SKIPPED
    assert run.failed == []


def test_rule_self_reporting_failed_without_a_reason_still_gets_one(_stub_platform):
    """A rule may return FAILED without raising (a downstream 500, say). The
    invariant is about the OUTCOME, not about how it arose."""
    async def sloppy(db, subject):
        return TriggerResult("Sloppy", outcome=TriggerOutcome.FAILED)

    db = FakeSession()
    run = _run(run_trigger_rules(
        db, [sloppy], subject=None, source_kind="Incident", source_id="i-6",
    ))
    assert run.failed[0].failure_reason.strip()


# ── audit entry shape ─────────────────────────────────────────────────────────
def test_audit_entry_keeps_the_legacy_shape_the_ui_reads():
    """The observation AI panel finds entries by `ruleId` and the near-miss page
    filters on `fired`. Both must survive the migration onto the engine."""
    r = TriggerResult(
        "Lessons Distribution (AI)", rule_id="rule_lessons_distribution",
        outcome=TriggerOutcome.FIRED, reason="ok", data={"lesson": "x"},
    )
    e = r.to_audit_entry()
    assert e["ruleId"] == "rule_lessons_distribution"
    assert e["fired"] is True
    assert e["status"] == "FIRED"
    assert e["data"] == {"lesson": "x"}


def test_audit_entry_distinguishes_failed_from_skipped():
    skipped = TriggerResult("A", outcome=TriggerOutcome.SKIPPED, reason="n/a").to_audit_entry()
    failed = TriggerResult(
        "B", outcome=TriggerOutcome.FAILED, failure_reason="boom"
    ).to_audit_entry()
    # Both are `fired: False` in the legacy shape — which is exactly why the
    # legacy shape could not tell "didn't apply" from "crashed".
    assert skipped["fired"] is False and failed["fired"] is False
    assert skipped["status"] == "SKIPPED" and failed["status"] == "FAILED"
    assert "error" not in skipped
    assert failed["error"] is True and failed["failureReason"] == "boom"


# ── legacy adapter ────────────────────────────────────────────────────────────
def test_dict_rule_adapter_maps_self_caught_errors_to_failed(_stub_platform):
    """Legacy rules caught their own exceptions and returned
    `{fired: False, error: "..."}` — indistinguishable from a clean no-op in the
    old runners. The adapter promotes those to FAILED."""
    async def legacy_error(db, subject):
        return {"ruleId": "rule_x", "ruleName": "Legacy", "fired": False, "error": "boom"}

    async def legacy_noop(db, subject):
        return {"ruleId": "rule_y", "ruleName": "Legacy quiet", "fired": False, "reason": "n/a"}

    db = FakeSession()
    run = _run(run_trigger_rules(
        db, [adapt_dict_rule(legacy_error), adapt_dict_rule(legacy_noop)], subject=None,
        source_kind="NearMiss", source_id="nm-1",
    ))
    assert run.results[0].outcome is TriggerOutcome.FAILED
    assert run.results[0].failure_reason == "boom"
    assert run.results[1].outcome is TriggerOutcome.SKIPPED


def test_json_column_sink_reassigns_rather_than_mutating():
    """SQLAlchemy does not track in-place mutation of a plain JSON column, so an
    `existing.append(...)` would never be persisted."""
    subject = SimpleNamespace(closureTriggers=[{"ruleName": "old"}])
    original = subject.closureTriggers
    sink = json_column_sink("closureTriggers")
    db = FakeSession()
    _run(sink(db, subject, [TriggerResult("New", outcome=TriggerOutcome.FIRED)]))
    assert subject.closureTriggers is not original
    assert len(subject.closureTriggers) == 2
    assert db.flushes == 1


def test_json_column_sink_tolerates_a_non_list_legacy_value():
    subject = SimpleNamespace(closureTriggers={"legacy": "dict"})
    _run(json_column_sink("closureTriggers")(
        FakeSession(), subject, [TriggerResult("New", outcome=TriggerOutcome.FIRED)]
    ))
    assert isinstance(subject.closureTriggers, list)
    assert len(subject.closureTriggers) == 1
