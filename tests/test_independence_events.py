"""The enforcement log — what gets recorded, and what deliberately does not.

The guard was correct and left no trace. `create_audit` raises `ValueError`, the
router's transaction rolls back, and anything written inside it goes with the
audit that was never created; preflight wrote nothing at all. So the module's
strongest claim — "we block conflicted auditors" — had zero evidence behind it,
while `IndependenceWaiver` (evidence of the guard being *overridden*) was the
only durable record and had zero rows.

These tests pin the decisions that make the log readable rather than merely
present: which verdict maps to which outcome, and what is left out on purpose.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from app.services.independence import Conflict, IndependenceVerdict
from app.services.independence_events import (
    OUTCOMES,
    event_fields_for,
    outcome_for,
    record_event,
    record_verdicts,
)


def _block(source="DECLARED_AUDITEE") -> Conflict:
    return Conflict(
        rule="OWN_WORK", severity="BLOCK", source=source,
        reason="They are a declared auditee on AUD-0002.", detail={"engagementId": "aud-2"},
    )


def _warn(source="ROLE_SCOPE") -> Conflict:
    return Conflict(
        rule="OWN_WORK", severity="WARN", source=source,
        reason="They hold a site-scoped role at the site being audited.",
    )


# ── Outcome mapping ──────────────────────────────────────────────────


def test_a_block_records_as_blocked():
    assert outcome_for(IndependenceVerdict(allowed=False, conflicts=[_block()])) == "BLOCKED"


def test_a_warning_only_verdict_records_as_warned():
    """A WARN is not a block. A register that merged them would claim the
    product refused an assignment it actually allowed."""
    assert outcome_for(IndependenceVerdict(allowed=True, conflicts=[_warn()])) == "WARNED"


def test_a_waived_block_records_as_waived_not_blocked():
    """The governance decision is the headline; the underlying conflict stays on
    the row in `rule` / `source` / `conflictDetail`."""
    v = IndependenceVerdict(allowed=True, conflicts=[_block()], waived=True, waiverId="w1")
    assert outcome_for(v) == "WAIVED"
    assert event_fields_for(v)["rule"] == "OWN_WORK"
    assert event_fields_for(v)["waiver_id"] == "w1"


def test_a_clean_verdict_records_as_cleared():
    assert outcome_for(IndependenceVerdict(allowed=True)) == "CLEARED"


def test_every_outcome_is_in_the_closed_vocabulary():
    for v in (
        IndependenceVerdict(allowed=False, conflicts=[_block()]),
        IndependenceVerdict(allowed=True, conflicts=[_warn()]),
        IndependenceVerdict(allowed=True, conflicts=[_block()], waived=True),
        IndependenceVerdict(allowed=True),
    ):
        assert outcome_for(v) in OUTCOMES


# ── The flattened row ────────────────────────────────────────────────


def test_the_lead_conflict_supplies_rule_source_and_reason():
    """The register groups on `source`, so a blank one is a silent break."""
    f = event_fields_for(IndependenceVerdict(allowed=False, conflicts=[_block("AREA_OWNER")]))
    assert f["source"] == "AREA_OWNER"
    assert f["rule"] == "OWN_WORK"
    assert f["reason"]


def test_a_block_outranks_a_warning_for_the_headline():
    v = IndependenceVerdict(allowed=False, conflicts=[_warn(), _block("DISCIPLINE_OWNER")])
    f = event_fields_for(v)
    assert f["source"] == "DISCIPLINE_OWNER"
    assert f["conflict_detail"]["blockingCount"] == 1
    assert f["conflict_detail"]["warningCount"] == 1


def test_all_conflicts_are_kept_not_just_the_headline():
    """The message frozen on the row is one line; the full reasoning is still
    needed when someone asks a year later."""
    v = IndependenceVerdict(allowed=False, conflicts=[_block(), _warn()])
    assert len(event_fields_for(v)["conflict_detail"]["conflicts"]) == 2


def test_the_flattened_keys_are_record_event_arguments():
    """They are spread straight into `record_event(**fields)` — returning column
    names instead would look right and fail at the call site."""
    f = event_fields_for(IndependenceVerdict(allowed=False, conflicts=[_block()]))
    assert set(f) == {"outcome", "rule", "source", "reason", "conflict_detail", "waiver_id"}


# ── What is written, against a stand-in session ──────────────────────


@dataclass
class _FakeSession:
    """Enough of AsyncSession to observe what would be persisted."""

    added: list = field(default_factory=list)
    duplicate: bool = False

    async def execute(self, _stmt):
        class _R:
            def __init__(self, dup):
                self._dup = dup

            def first(self_inner):
                return ("existing",) if self_inner._dup else None

        return _R(self.duplicate)

    async def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = f"ev-{len(self.added)}"

    def add(self, row):
        self.added.append(row)


def test_an_event_is_written_with_the_frozen_reason():
    s = _FakeSession()
    rid = asyncio.run(
        record_event(
            subject_user_id="u1", engagement_kind="AUDIT", outcome="BLOCKED",
            origin="CREATE_AUDIT", reason="because", rule="OWN_WORK",
            source="DECLARED_AUDITEE", session=s,
        )
    )
    assert rid and len(s.added) == 1
    assert s.added[0].reason == "because"
    assert s.added[0].origin == "CREATE_AUDIT"


def test_an_unknown_outcome_is_refused_rather_than_stored():
    """The vocabulary is closed at the DB too. A typo'd outcome would vanish
    from every filtered view rather than showing up wrong."""
    s = _FakeSession()
    assert asyncio.run(
        record_event(subject_user_id="u1", engagement_kind="AUDIT",
                     outcome="NOPE", origin="PREFLIGHT", session=s)
    ) is None
    assert s.added == []


def test_a_repeat_verdict_inside_the_window_is_deduplicated():
    """Preflight fires on every render of the team-assignment step. Without
    this, tabbing through a form writes a hundred identical rows and the
    register becomes unreadable — a log nobody can read is not evidence."""
    s = _FakeSession(duplicate=True)
    assert asyncio.run(
        record_event(subject_user_id="u1", engagement_kind="AUDIT", outcome="BLOCKED",
                     origin="PREFLIGHT", session=s)
    ) is None
    assert s.added == []


def test_dedupe_can_be_turned_off_for_governance_acts():
    """Granting and revoking a waiver are deliberate acts, not form re-renders.
    Both must land even if they repeat."""
    s = _FakeSession(duplicate=True)
    rid = asyncio.run(
        record_event(subject_user_id="u1", engagement_kind="AUDIT", outcome="WAIVED",
                     origin="WAIVER_GRANT", dedupe=False, session=s)
    )
    assert rid and len(s.added) == 1


def test_clean_verdicts_are_not_recorded_by_default():
    """The register's claim is "every time the guard had something to say", not
    "every question anyone asked it". Recording every clean candidate would bury
    the blocks that are the actual evidence."""
    s = _FakeSession()
    n = asyncio.run(
        record_verdicts(
            verdicts={
                "clean": IndependenceVerdict(allowed=True),
                "blocked": IndependenceVerdict(allowed=False, conflicts=[_block()]),
            },
            engagement_kind="AUDIT", origin="PREFLIGHT", attempted_by_user_id="actor",
            session=s,
        )
    )
    assert n == 1
    assert [r.subjectUserId for r in s.added] == ["blocked"]


def test_clean_verdicts_can_be_recorded_when_asked_for():
    s = _FakeSession()
    n = asyncio.run(
        record_verdicts(
            verdicts={"clean": IndependenceVerdict(allowed=True)},
            engagement_kind="AUDIT", origin="PREFLIGHT", attempted_by_user_id="actor",
            include_cleared=True, session=s,
        )
    )
    assert n == 1 and s.added[0].outcome == "CLEARED"


def test_an_attempt_with_no_engagement_still_records():
    """The schedule wizard pre-flights a team before the audit row exists, and
    that attempt is exactly the one worth keeping: it is the one that never
    became an engagement."""
    s = _FakeSession()
    rid = asyncio.run(
        record_event(subject_user_id="u1", engagement_kind="AUDIT", outcome="BLOCKED",
                     origin="PREFLIGHT", engagement_id=None, session=s)
    )
    assert rid and s.added[0].engagementId is None
