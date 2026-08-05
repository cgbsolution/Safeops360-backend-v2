"""Inbox queue semantics — offline unit tests.

Two rules the platform now depends on, in the house no-DB style:

  1. A task is OPEN in every non-terminal state. The SLA sweep rewrites
     PENDING → OVERDUE → ESCALATED in place, so a PENDING-only filter drops a
     task from its owner's queue at the moment it becomes urgent.
  2. The working tabs are a feed (newest-assigned first); only the Overdue /
     Escalated tab ranks by lateness (oldest dueAt first, no-SLA last).

These assert the ORDER BY / status-set construction rather than driving
Postgres, matching how the rest of the suite covers router helpers.
"""

from __future__ import annotations

from app.models.workflow import TaskStatus
from app.routers.workflow import _OPEN_TASK_STATUSES, _tab_order_by


# ─── Open-status set ────────────────────────────────────────────────────────

def test_open_statuses_cover_the_whole_sla_ladder():
    # PENDING alone was the bug: the sweep's own output fell outside the filter.
    assert TaskStatus.PENDING.value in _OPEN_TASK_STATUSES
    assert "OVERDUE" in _OPEN_TASK_STATUSES
    assert "ESCALATED" in _OPEN_TASK_STATUSES


def test_open_statuses_exclude_terminal_states():
    for terminal in ("COMPLETED", "REJECTED", "SKIPPED"):
        assert terminal not in _OPEN_TASK_STATUSES


# ─── Per-tab ordering ───────────────────────────────────────────────────────

def _sql(clauses) -> list[str]:
    """Compile an ORDER BY tuple to lower-case SQL fragments for assertions."""
    return [" ".join(str(c).lower().split()) for c in clauses]


def test_work_tabs_are_newest_assigned_first():
    for tab in ("pending_approvals", "my_tasks", "pending_verification", "submitted_by_me", None):
        assert _sql(_tab_order_by(tab))[0] == '"workflowtask"."assignedat" desc', tab


def test_overdue_tab_ranks_by_lateness_not_recency():
    described = _sql(_tab_order_by("overdue_escalated"))
    # Oldest due date leads — that is "most time gone past due".
    assert described[0].startswith('"workflowtask"."dueat" asc')
    assert described[1] == '"workflowtask"."assignedat" asc'


def test_overdue_tab_is_case_insensitive():
    assert _sql(_tab_order_by("OVERDUE_ESCALATED"))[0].startswith('"workflowtask"."dueat" asc')


def test_overdue_tab_sorts_tasks_without_an_sla_last():
    # A task with no dueAt is not "infinitely overdue"; NULLS LAST keeps it from
    # squatting at the top of the lateness ranking.
    assert "nulls last" in _sql(_tab_order_by("overdue_escalated"))[0]


def test_work_tabs_order_is_deterministic():
    # assignedAt alone ties for tasks minted in the same transaction (a step
    # fanning out to several approvers); id breaks the tie so paging is stable.
    described = _sql(_tab_order_by("my_tasks"))
    assert described == ['"workflowtask"."assignedat" desc', '"workflowtask".id desc']
