"""Safety-roster gate — the single check every "assign this person to work"
path calls.

A worker under an open deroster flag (`pending_safety_review`) or a confirmed
one (`derostered`) must not be put on new work. The spec was explicit that this
is not a one-call-site change, so the rule lives here rather than being
re-expressed at each site, and the call sites are enumerated below so the next
person adding an assignment screen knows the list to join.

Call sites wired to this module:
  • routers/ptw_active.py       — adding a User to a permit crew (hard block)
  • routers/epc_gate.py         — gate clearance check (i) (fails the check)
  • routers/epc_mobilization.py — pre-mobilisation check (fails the check)
  • routers/epc_workers.py      — /{id}/gate-status preview (fails the check)
  • routers/epc_mobilization.py — /site/{id}/roster listing (annotates rows)
  • routers/workforce.py        — Worker Involved picker (filters by default)

Deliberately NOT gated: naming a worker on a NEW observation, incident or near
miss. The hold is on giving someone work, not on reporting about them — the
opposite would make a flagged worker invisible to the safety system precisely
when they are under review.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.epc import ContractorWorker
from app.models.observation_sla import (
    ROSTER_ACTIVE,
    ROSTER_BLOCKED,
    ROSTER_DEROSTERED,
    ROSTER_PENDING_REVIEW,
)
from app.models.user import User

_LABELS = {
    ROSTER_ACTIVE: "Active",
    ROSTER_PENDING_REVIEW: "Under safety review",
    ROSTER_DEROSTERED: "Derostered",
}

# Wording used where the person themself, or a general report, may see it.
# `pending_safety_review` never reads as a sanction — nobody has decided yet.
_DETAIL = {
    ROSTER_PENDING_REVIEW: (
        "Worker is under safety review following a high-severity unsafe act. "
        "New work assignment is on hold until a Section Head or HSE Manager decides."
    ),
    ROSTER_DEROSTERED: (
        "Worker is derostered pending completion of the assigned corrective action."
    ),
}


@dataclass
class RosterGate:
    allowed: bool
    status: str
    label: str
    detail: str
    derosterRef: str | None = None

    def as_check(self) -> dict:
        """EPC gate/mobilisation check shape."""
        return {
            "result": "pass" if self.allowed else "fail",
            "detail": self.detail,
            "rosterStatus": self.status,
            "derosterRef": self.derosterRef,
        }


def _evaluate(status: str | None, deroster_ref: str | None) -> RosterGate:
    s = status or ROSTER_ACTIVE
    if s not in ROSTER_BLOCKED:
        return RosterGate(
            allowed=True, status=ROSTER_ACTIVE, label=_LABELS[ROSTER_ACTIVE],
            detail="No safety hold on this worker",
        )
    return RosterGate(
        allowed=False,
        status=s,
        label=_LABELS.get(s, s),
        detail=_DETAIL.get(s, "Worker is not available for new assignment."),
        derosterRef=deroster_ref,
    )


def for_person(person: User | ContractorWorker | None) -> RosterGate:
    """Evaluate an already-loaded person row. `getattr` with a default keeps
    this safe on rows read before the column existed."""
    if person is None:
        return RosterGate(
            allowed=False, status="unknown", label="Unknown",
            detail="Worker record not found.",
        )
    return _evaluate(
        getattr(person, "rosterStatus", ROSTER_ACTIVE),
        getattr(person, "currentDerosterRef", None),
    )


async def for_user(db: AsyncSession, user_id: str | None) -> RosterGate:
    if not user_id:
        return _evaluate(ROSTER_ACTIVE, None)
    return for_person(await db.get(User, user_id))


async def for_contractor_worker(db: AsyncSession, worker_id: str | None) -> RosterGate:
    if not worker_id:
        return _evaluate(ROSTER_ACTIVE, None)
    return for_person(await db.get(ContractorWorker, worker_id))


__all__ = ["RosterGate", "for_person", "for_user", "for_contractor_worker"]
