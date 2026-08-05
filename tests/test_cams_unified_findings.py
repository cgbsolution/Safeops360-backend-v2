"""`audit_findings()` clock handling — the crash that took /cams down.

The unified register mixes two row shapes, and they need two different clocks:

  * promoted `AuditFinding.dueDate` is a **DATE** column   -> compare to a date
  * legacy checkpoint `createdAt` is an aware **DATETIME** -> subtract a datetime

The function bound only one name, `today = now().date()`, and bound it INSIDE
`if promoted:`. That produced two distinct 500s on the same line:

  * promoted rows present -> `date - datetime` -> TypeError
  * promoted rows absent  -> `today` unbound   -> NameError

Both surface as the Command Centre's "Internal server error", because /cams
fetches this endpoint on page load.

House style: no async-DB harness, so the session is a scripted stand-in. The
stand-ins carry `siteId`/`plantId`/`ownerId = None` on purpose — the name-map
helpers short-circuit on an empty id set, so the only queries that run are the
two this function issues itself.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.cams import audit_findings

AWARE = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
NAIVE = datetime(2026, 7, 1, 9, 0)  # Prisma writes timestamps without tz


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def first(self):
        return None


class _ScriptedSession:
    """Returns queued result sets in order, one per execute()."""

    def __init__(self, *result_sets):
        self._queue = list(result_sets)
        self.calls = 0

    async def execute(self, statement):  # noqa: ARG002
        self.calls += 1
        return _Result(self._queue.pop(0) if self._queue else [])


def _audit(aid="aud-1"):
    return SimpleNamespace(
        id=aid, auditNumber="AUD-GT-2026-NW-0007", title="Integrated audit",
        plantId=None, scheduledDate=AWARE,
    )


def _promoted_finding(due=None):
    return SimpleNamespace(
        id="f-1", findingCode="AFN-001", checkpointCode="DISC-14-001",
        title="Extinguisher overdue", description="", severity="MAJOR_NC",
        clauseRef="8.2", standard="ISO 45001", siteId=None, ownerId=None,
        capaId=None, status="OPEN", isRepeatFinding=False, repeatOfFindingId=None,
        dueDate=due, observationOnly=False, closedById=None, closedAt=None,
        createdAt=AWARE, isDeleted=False,
    )


def _legacy_checkpoint(created):
    return SimpleNamespace(
        id="r-1", checkpointCode="CP-1", checkpointQuestion="Is the extinguisher in date?",
        observation="", criticality="major", standard="ISO 45001",
        assignedOwnerId=None, routedToUserId=None, capa={}, workflowState="OPEN",
        auditorEvidenceIds=[], createdAt=created, updatedAt=created, finalizedAt=None,
    )


def _run(promoted, legacy):
    db = _ScriptedSession(promoted, legacy)
    return asyncio.run(audit_findings(db))


# ── The two 500s ─────────────────────────────────────────────────────────────


def test_promoted_and_legacy_together_does_not_raise():
    """The live crash: `date - datetime` on the legacy branch."""
    out = _run([(_promoted_finding(), _audit())], [(_legacy_checkpoint(AWARE), _audit("aud-2"))])
    assert len(out) == 2


def test_legacy_only_does_not_raise():
    """The latent one: `today` was unbound when no promoted rows existed."""
    out = _run([], [(_legacy_checkpoint(AWARE), _audit())])
    assert len(out) == 1


# ── Age arithmetic ───────────────────────────────────────────────────────────


def test_age_days_is_a_non_negative_int():
    out = _run([], [(_legacy_checkpoint(AWARE), _audit())])
    age = out[0]["ageDays"]
    assert isinstance(age, int)
    assert age >= 0


def test_age_days_handles_naive_timestamps():
    """Prisma stores DateTime without tz, so SQLAlchemy reads it back naive.
    `_as_aware` exists for exactly this; the subtraction must not blow up."""
    out = _run([], [(_legacy_checkpoint(NAIVE), _audit())])
    assert out[0]["ageDays"] >= 0


def test_age_days_is_zero_when_created_at_is_missing():
    row = _legacy_checkpoint(AWARE)
    row.createdAt = None
    out = _run([], [(row, _audit())])
    assert out[0]["ageDays"] == 0


# ── Due-date comparison on the promoted branch ───────────────────────────────


def test_past_due_date_marks_overdue():
    past = date.today() - timedelta(days=5)
    out = _run([(_promoted_finding(due=past), _audit())], [])
    assert out[0]["isOverdue"] is True


def test_future_due_date_is_not_overdue():
    future = date.today() + timedelta(days=5)
    out = _run([(_promoted_finding(due=future), _audit())], [])
    assert out[0]["isOverdue"] is False


def test_closed_finding_is_never_overdue():
    past = date.today() - timedelta(days=5)
    f = _promoted_finding(due=past)
    f.status = "CLOSED"
    out = _run([(f, _audit())], [])
    assert out[0]["isOverdue"] is False


def test_missing_due_date_is_not_overdue():
    out = _run([(_promoted_finding(due=None), _audit())], [])
    assert out[0]["isOverdue"] is False


def test_empty_everywhere_returns_empty():
    assert _run([], []) == []
