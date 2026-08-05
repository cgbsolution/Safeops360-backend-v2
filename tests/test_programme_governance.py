"""Annual Audit Programme — the governance path a human actually walks.

Design: [docs/cams/08-audit-programme.md](../../docs/cams/08-audit-programme.md) §3.

The engine, the coverage states and the pure approval guard are covered by
`test_programme_coverage.py`. What was untested — because it had no caller — is
the LIFECYCLE as a sequence: create → submit → approve → activate → close, and
the guards that stop each step happening in the wrong order or by the wrong
person.

Two of these assert things that were previously impossible to violate only
because nothing could reach them:

  * **four eyes on the pair that matters.** Owner ≠ approver was guarded from the
    start. Submitter ≠ approver was not — the submitter was anonymous, so a
    delegate could prepare and submit a cycle they did not own and then approve
    their own submission.
  * **APPROVED → ACTIVE exists.** `CYCLE_TRANSITIONS` only permits closure from
    ACTIVE, so without an activation step an approved cycle could never close.

The async paths run against an in-memory stand-in rather than a database — the
house style, and the same reason the rest of the suite is pure: there is no
async-DB harness in the repo. What is tested here is the decision logic, which
is where every one of these guards lives.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.programme.lifecycle import (
    CYCLE_TRANSITIONS,
    activate_cycle,
    approval_blockers,
    approval_report,
    cycle_transition_allowed,
    return_cycle_to_draft,
    submit_cycle_for_review,
)


def _unit(**over):
    base = dict(
        id="u1",
        dimension="DISCIPLINE",
        siteId="site-1",
        dimensionKey="FS",
        dimensionLabel="Fire Safety",
        requiredPerCycle=2,
        waiverReason=None,
        waivedByUserId=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


# ── The lifecycle as a sequence ──────────────────────────────────────


def test_the_whole_cycle_path_is_traversable():
    """DRAFT → UNDER_REVIEW → APPROVED → ACTIVE → CLOSED, one legal step at a time.

    Asserted as a walk rather than per-edge because the defect this catches is
    a missing step in the middle, which every per-edge test still passes.
    """
    path = ["DRAFT", "UNDER_REVIEW", "APPROVED", "ACTIVE", "CLOSED"]
    for current, nxt in zip(path, path[1:]):
        assert cycle_transition_allowed(current, nxt), f"{current} → {nxt} is not permitted"
    assert CYCLE_TRANSITIONS["CLOSED"] == ()


def test_closure_is_only_reachable_from_active():
    """The reason activation has to exist as its own step.

    An APPROVED cycle cannot close. Before `activate_cycle` there was no way to
    leave APPROVED at all, so nothing approved could ever be closed.
    """
    for state in ("DRAFT", "UNDER_REVIEW", "APPROVED", "CLOSED"):
        assert not cycle_transition_allowed(state, "CLOSED")
    assert cycle_transition_allowed("ACTIVE", "CLOSED")


def test_a_rejected_review_can_go_back_to_draft():
    assert cycle_transition_allowed("UNDER_REVIEW", "DRAFT")


# ── Four eyes ────────────────────────────────────────────────────────


def test_submitter_cannot_approve_their_own_submission():
    """The gap the owner guard does not close.

    A delegate submits a cycle owned by someone else, then approves it: owner ≠
    approver passes, and one person has walked the whole thing through.
    """
    b = approval_blockers(
        objectives="x" * 30,
        scope_units=[_unit()],
        slots_per_unit={"u1": 2},
        approver_id="u-delegate",
        owner_id="u-owner",
        submitter_id="u-delegate",
    )
    assert any("second pair of eyes" in x for x in b)


def test_a_different_approver_from_the_submitter_is_fine():
    assert approval_blockers(
        objectives="x" * 30,
        scope_units=[_unit()],
        slots_per_unit={"u1": 2},
        approver_id="u-approver",
        owner_id="u-owner",
        submitter_id="u-delegate",
    ) == []


def test_an_unsubmitted_cycle_does_not_trip_the_submitter_guard():
    """`submitter_id=None` is a legacy cycle, not a self-approval."""
    assert approval_blockers(
        objectives="x" * 30,
        scope_units=[_unit()],
        slots_per_unit={"u1": 2},
        approver_id="u-approver",
        owner_id="u-owner",
        submitter_id=None,
    ) == []


# ── Structured blockers ──────────────────────────────────────────────


def test_blockers_carry_the_scope_unit_that_caused_them():
    """So the approver reads the problem on the row, not in a wall of text."""
    rows = approval_report(
        objectives="x" * 30,
        scope_units=[
            _unit(id="u1", dimensionLabel="Fire Safety", requiredPerCycle=None),
            _unit(id="u2", dimensionLabel="Electrical", requiredPerCycle=2),
        ],
        slots_per_unit={},
        approver_id="a",
        owner_id="o",
    )
    by_unit = {r["scopeUnitId"]: r for r in rows}
    assert by_unit["u1"]["code"] == "FREQUENCY_MISSING"
    assert by_unit["u1"]["scopeUnitLabel"] == "Fire Safety"
    assert by_unit["u1"]["siteId"] == "site-1"
    assert by_unit["u2"]["code"] == "NO_SLOT"


def test_cycle_level_blockers_carry_no_scope_unit():
    rows = approval_report(
        objectives="",
        scope_units=[_unit()],
        slots_per_unit={"u1": 1},
        approver_id="a",
        owner_id="o",
    )
    obj = [r for r in rows if r["code"] == "OBJECTIVES_MISSING"]
    assert len(obj) == 1
    assert obj[0]["scopeUnitId"] is None


def test_the_flat_and_structured_guards_cannot_disagree():
    """One implementation, two shapes — the preview must equal the enforcement."""
    kwargs = dict(
        objectives="",
        scope_units=[_unit(requiredPerCycle=None), _unit(id="u2", waiverReason="mothballed")],
        slots_per_unit={},
        approver_id="same",
        owner_id="same",
        submitter_id="same",
    )
    assert approval_blockers(**kwargs) == [r["message"] for r in approval_report(**kwargs)]


def test_every_blocker_carries_a_stable_code():
    """The UI groups and links on the code, so a blank one is a silent break."""
    rows = approval_report(
        objectives="",
        scope_units=[_unit(requiredPerCycle=None)],
        slots_per_unit={},
        approver_id=None,
        owner_id="o",
    )
    assert rows
    assert all(r["code"] and r["message"] for r in rows)


# ── Async guards, against an in-memory stand-in ──────────────────────


class _FakeDb:
    """Enough of AsyncSession for the state-machine functions.

    They call `get`, `execute` and `flush`; nothing here needs a query planner.
    """

    def __init__(self, obj):
        self._obj = obj
        self.flushed = 0

    async def get(self, _model, _id):
        return self._obj

    async def flush(self):
        self.flushed += 1


def _cycle(**over):
    base = dict(
        id="c1",
        programmeId="p1",
        cycleLabel="FY27",
        status="DRAFT",
        periodStart=date(2026, 4, 1),
        periodEnd=date(2027, 3, 31),
        periodsPerCycle=4,
        submittedForReviewAt=None,
        submittedByUserId=None,
        approvedByUserId=None,
        approvedAt=None,
        activatedAt=None,
        closedAt=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_activation_stamps_the_cycle_and_moves_it():
    cycle = _cycle(status="APPROVED")
    db = _FakeDb(cycle)
    out = asyncio.run(activate_cycle(db, cycle_id="c1", user=SimpleNamespace(id="u1")))
    assert out["status"] == "ACTIVE"
    assert cycle.status == "ACTIVE"
    assert isinstance(cycle.activatedAt, datetime)
    assert cycle.activatedAt.tzinfo is timezone.utc


def test_a_draft_cycle_cannot_be_activated():
    db = _FakeDb(_cycle(status="DRAFT"))
    with pytest.raises(ValueError, match="cannot be activated"):
        asyncio.run(activate_cycle(db, cycle_id="c1", user=SimpleNamespace(id="u1")))


def test_returning_to_draft_clears_the_submitter():
    """The next submission is a new act by whoever makes it.

    Leaving the old stamp would keep blocking whoever submitted last time from
    approving a cycle they no longer had anything to do with.
    """
    cycle = _cycle(
        status="UNDER_REVIEW",
        submittedByUserId="u-delegate",
        submittedForReviewAt=datetime.now(timezone.utc),
    )
    db = _FakeDb(cycle)
    out = asyncio.run(return_cycle_to_draft(db, cycle_id="c1", user=SimpleNamespace(id="u2")))
    assert out["status"] == "DRAFT"
    assert cycle.submittedByUserId is None
    assert cycle.submittedForReviewAt is None


def test_an_approved_cycle_cannot_be_returned_to_draft():
    """Past approval the plan is frozen; changes are amendments, not edits."""
    db = _FakeDb(_cycle(status="APPROVED"))
    with pytest.raises(ValueError, match="cannot be returned to draft"):
        asyncio.run(return_cycle_to_draft(db, cycle_id="c1", user=SimpleNamespace(id="u1")))


class _UnitsDb(_FakeDb):
    """Adds the one `execute` the submit guard makes."""

    def __init__(self, cycle, units):
        super().__init__(cycle)
        self._units = units

    async def execute(self, _stmt):
        units = self._units
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: units))


def test_submitting_records_who_submitted():
    """The stamp the four-eyes guard reads. Without it, the guard is unenforceable."""
    cycle = _cycle(status="DRAFT")
    db = _UnitsDb(cycle, [_unit()])
    out = asyncio.run(
        submit_cycle_for_review(db, cycle_id="c1", user=SimpleNamespace(id="u-delegate"))
    )
    assert out["status"] == "UNDER_REVIEW"
    assert cycle.submittedByUserId == "u-delegate"
    assert cycle.submittedForReviewAt is not None


def test_a_cycle_with_no_scope_units_cannot_be_submitted():
    db = _UnitsDb(_cycle(status="DRAFT"), [])
    with pytest.raises(ValueError, match="at least one scope unit"):
        asyncio.run(submit_cycle_for_review(db, cycle_id="c1", user=SimpleNamespace(id="u1")))


def test_a_unit_without_frequency_or_waiver_cannot_be_submitted():
    db = _UnitsDb(_cycle(status="DRAFT"), [_unit(requiredPerCycle=None)])
    with pytest.raises(ValueError, match="required frequency"):
        asyncio.run(submit_cycle_for_review(db, cycle_id="c1", user=SimpleNamespace(id="u1")))
