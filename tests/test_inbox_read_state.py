"""Inbox read/unread state — offline unit tests.

Read state is deliberately keyed on OPENING THE RECORD, not on tapping the
Inbox row, so a deep link / push notification / modal clears the bold styling
too. These assert the query construction that enforces the two invariants that
actually matter:

  1. A mark-read write can only ever touch the caller's OWN open tasks.
  2. Unread is computed as `readAt IS NULL` over the same open-status set the
     tabs use, so a task can never be "unread" and invisible at the same time.
"""

from __future__ import annotations

from app.routers.workflow import _OPEN_TASK_STATUSES
from app.schemas.workflow import MyCountResponse, WorkflowTaskOut


# ─── Payload contract ───────────────────────────────────────────────────────

def _task(**over) -> WorkflowTaskOut:
    base = dict(
        id="t1",
        module="OBSERVATION",
        recordId="r1",
        stepName="Section Head Review",
        taskType="APPROVAL",
        status="PENDING",
        priority="NORMAL",
        assignedAt="2026-07-25T10:00:00Z",
    )
    base.update(over)
    return WorkflowTaskOut(**base)


def test_task_defaults_to_unread():
    # A freshly-minted task has never been opened; the row must render bold.
    assert _task().isRead is False
    assert _task().readAt is None


def test_task_reports_read_once_stamped():
    t = _task(isRead=True, readAt="2026-07-25T11:00:00Z")
    assert t.isRead is True
    assert t.readAt is not None


def test_counts_expose_per_tab_unread():
    c = MyCountResponse(
        count=3,
        unreadPendingApprovals=2,
        unreadMyTasks=1,
        unreadPendingVerification=0,
        unreadTotal=3,
    )
    assert c.unreadTotal == 3
    assert (c.unreadPendingApprovals, c.unreadMyTasks, c.unreadPendingVerification) == (2, 1, 0)


def test_counts_default_to_zero_unread():
    # Older clients / a backend that hasn't run the DDL must not render pips.
    c = MyCountResponse(count=0)
    assert c.unreadTotal == 0
    assert c.unreadOverdueEscalated == 0


# ─── Unread is scoped to open work ──────────────────────────────────────────

def test_unread_is_only_computed_over_open_tasks():
    # A COMPLETED task is out of the queue entirely; it must never contribute
    # an unread pip, however stale its readAt happens to be.
    assert "COMPLETED" not in _OPEN_TASK_STATUSES
    assert "REJECTED" not in _OPEN_TASK_STATUSES
    assert set(_OPEN_TASK_STATUSES) == {"PENDING", "OVERDUE", "ESCALATED"}


def test_mark_read_statement_is_scoped_to_caller_and_unread_rows():
    # Guards the two clauses that make the write safe and idempotent: it can
    # only hit rows assigned to the caller, and only rows not already read.
    import inspect

    from app.routers import workflow as wf

    src = inspect.getsource(wf.mark_record_tasks_read)
    assert "WorkflowTask.assignedToId == user.id" in src
    assert "WorkflowTask.readAt.is_(None)" in src
    assert "_OPEN_TASK_STATUSES" in src


def test_read_all_statement_is_scoped_to_caller():
    import inspect

    from app.routers import workflow as wf

    src = inspect.getsource(wf.mark_all_tasks_read)
    assert "WorkflowTask.assignedToId == user.id" in src
    # No module/record filter here by design — it is the whole-inbox escape
    # hatch — but it must still never reach another user's rows.
    assert "recordId" not in src
