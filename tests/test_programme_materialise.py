"""Materialising a slot, and the closure gate.

Two things that had guarded, tested service code and no reachable caller:

  * **Materialise.** The UI's version was a text box asking for an engagement
    id, hard-coded to `engagementKind: "AUDIT"`. It could not link an inspection
    despite the pointer being polymorphic, and a mistyped id silently produced a
    slot whose coverage and variance were computed against a stranger's audit.
  * **Closure.** `close_cycle` refuses a cycle with no ISO 19011 §5.6 review.
    Nothing in the product could create a review, so the guard had never fired
    in anger — and nothing could ever be closed.

Both are asserted against in-memory stand-ins, the house style: the decision
logic is the thing worth pinning, and there is no async-DB harness in the repo.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.programme.lifecycle import close_cycle
from app.services.programme.materialise import _planned_datetime, _suggested_title


# ── The window → date decision ───────────────────────────────────────


def test_scheduling_defaults_to_the_window_opening():
    """A plan that starts at the top of its window has room to slip inside it.

    Defaulting to the midpoint or the close would guarantee that any delay
    became drift, which is the one number the variance report exists to measure.
    """
    dt = _planned_datetime(date(2026, 7, 1), date(2026, 9, 30), on=None)
    assert dt.date() == date(2026, 7, 1)
    assert dt.tzinfo is timezone.utc


def test_a_chosen_date_inside_the_window_is_kept():
    dt = _planned_datetime(date(2026, 7, 1), date(2026, 9, 30), on=date(2026, 8, 12))
    assert dt.date() == date(2026, 8, 12)


def test_a_date_outside_the_window_is_clamped_into_it():
    """A window is the commitment. Scheduling outside it is a real slip, and it
    belongs in an amendment — not in a silently-accepted date that makes the
    drift disappear."""
    early = _planned_datetime(date(2026, 7, 1), date(2026, 9, 30), on=date(2026, 1, 5))
    late = _planned_datetime(date(2026, 7, 1), date(2026, 9, 30), on=date(2027, 1, 5))
    assert early.date() == date(2026, 7, 1)
    assert late.date() == date(2026, 9, 30)


def test_a_single_day_window_is_not_broken_by_clamping():
    dt = _planned_datetime(date(2026, 7, 1), date(2026, 7, 1), on=date(2026, 8, 1))
    assert dt.date() == date(2026, 7, 1)


# ── The suggested title ──────────────────────────────────────────────


def _unit(key, label, dimension="DISCIPLINE"):
    return SimpleNamespace(dimension=dimension, dimensionKey=key, dimensionLabel=label)


def test_the_suggested_title_names_the_cycle_period_and_scope():
    title = _suggested_title(
        SimpleNamespace(name="Group OH&S Programme"),
        SimpleNamespace(cycleLabel="FY27"),
        SimpleNamespace(periodIndex=1),
        [_unit("FS", "Fire Safety"), _unit("EL", "Electrical")],
    )
    assert "FY27" in title
    assert "P2" in title
    assert "Fire Safety" in title


def test_a_long_scope_is_summarised_rather_than_dumped():
    units = [_unit(f"D{i}", f"Discipline {i}") for i in range(6)]
    title = _suggested_title(
        SimpleNamespace(name="P"), SimpleNamespace(cycleLabel="FY27"),
        SimpleNamespace(periodIndex=0), units,
    )
    assert "+3" in title


def test_a_slot_with_no_discipline_units_still_gets_a_title():
    """Inspection-only slots carry STANDARD units; the title must not come out blank."""
    title = _suggested_title(
        SimpleNamespace(name="SA8000 Programme"),
        SimpleNamespace(cycleLabel="FY27"),
        SimpleNamespace(periodIndex=2),
        [_unit("SA8000", "SA8000", dimension="STANDARD")],
    )
    assert title.strip()
    assert "SA8000 Programme" in title


# ── The closure gate ─────────────────────────────────────────────────


class _CloseDb:
    """Answers the two queries `close_cycle` makes: review count, then open slots."""

    def __init__(self, cycle, *, review_count: int, open_slots: list):
        self._cycle = cycle
        self._answers = [review_count, open_slots]
        self.flushed = 0

    async def get(self, _model, _id):
        return self._cycle

    async def execute(self, _stmt):
        nxt = self._answers.pop(0)
        if isinstance(nxt, int):
            return SimpleNamespace(scalar_one=lambda: nxt)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: nxt))

    async def flush(self):
        self.flushed += 1


def _cycle(status="ACTIVE"):
    return SimpleNamespace(id="c1", status=status, closedAt=None)


def test_a_cycle_cannot_close_without_a_programme_review():
    """The ISO 19011 §5.6 gate — the whole reason the review screen had to exist."""
    db = _CloseDb(_cycle(), review_count=0, open_slots=[])
    with pytest.raises(ValueError, match="at least one programme review"):
        asyncio.run(close_cycle(db, cycle_id="c1", user=SimpleNamespace(id="u1")))


def test_one_review_unlocks_closure():
    cycle = _cycle()
    db = _CloseDb(cycle, review_count=1, open_slots=[])
    out = asyncio.run(close_cycle(db, cycle_id="c1", user=SimpleNamespace(id="u1")))
    assert out["status"] == "CLOSED"
    assert cycle.status == "CLOSED"
    assert isinstance(cycle.closedAt, datetime)


def test_open_slots_still_block_closure_even_with_a_review():
    """A review does not paper over slots that were never resolved — each one
    still needs to be completed, deferred, cancelled or waived, with a reason."""
    db = _CloseDb(
        _cycle(),
        review_count=1,
        open_slots=[SimpleNamespace(slotCode="S001"), SimpleNamespace(slotCode="S002")],
    )
    with pytest.raises(ValueError, match="S001"):
        asyncio.run(close_cycle(db, cycle_id="c1", user=SimpleNamespace(id="u1")))


def test_an_approved_cycle_cannot_skip_activation_to_close():
    db = _CloseDb(_cycle(status="APPROVED"), review_count=1, open_slots=[])
    with pytest.raises(ValueError, match="cannot be closed"):
        asyncio.run(close_cycle(db, cycle_id="c1", user=SimpleNamespace(id="u1")))
