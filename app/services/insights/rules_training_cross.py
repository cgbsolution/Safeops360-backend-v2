"""Training-driven cross-links for the Incident / Near Miss / Observation
insight bars (spec: "training-driven signals should appear on Incident
Investigation, Near Miss, and Observation list screens").

Deterministic: reads TrainingAssignments the engine auto-created FROM records of
that module and surfaces the follow-up + any overdue slippage as bar insights.
Slot-filled templates only; no model calls. Guarded by the engine so a failure
here can never break a list screen.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competency_matrix import Competency
from app.models.training_engine import TrainingAssignment
from app.schemas.insights import Insight
from app.services.insights.common import confidence_for
from app.services.insights.templates import fill

# insights module key → the sourceModule stamped on TrainingAssignment.
_MODULE_MAP = {"incident": "INCIDENT", "nearmiss": "NEAR_MISS", "observation": "OBSERVATION"}
_OPEN = ("assigned", "in_progress", "overdue", "escalated")


async def training_cross_bar(db: AsyncSession, *, module_key: str, plant: str | None) -> list[Insight]:
    source_module = _MODULE_MAP.get(module_key)
    if source_module is None:
        return []

    stmt = (
        select(TrainingAssignment)
        .where(TrainingAssignment.sourceModule == source_module)
        .where(TrainingAssignment.status.in_(_OPEN))
        .where(TrainingAssignment.isDeleted.is_(False))
    )
    if plant:
        stmt = stmt.where(TrainingAssignment.plantId == plant)
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return []

    workers = len({r.personUserId for r in rows})
    overdue = sum(1 for r in rows if r.status == "overdue")
    by_comp: dict[str, int] = {}
    for r in rows:
        by_comp[r.competencyId] = by_comp.get(r.competencyId, 0) + 1
    top_comp_id = max(by_comp, key=by_comp.get)
    comp = await db.get(Competency, top_comp_id)
    comp_name = comp.name if comp else top_comp_id

    refs = [r.sourceRecordRef for r in rows if r.sourceRecordRef]
    out: list[Insight] = [
        Insight(
            id=f"training:followup:{source_module.lower()}:{plant or 'all'}",
            kind="next_best_action",
            severity="watch",
            headline=fill("training.followup.open", workers=workers, competency=comp_name),
            evidence=fill(
                "training.followup.open.evidence",
                count=len(rows),
                module=source_module.replace("_", " ").title(),
                competency=comp_name,
            ),
            recordRefs=list(dict.fromkeys(refs)),
            suggestedAction="Track completion in the Training assignment queue — the competency gap stays open until then.",
            confidence=confidence_for(len(rows)),
        )
    ]
    if overdue:
        out.append(
            Insight(
                id=f"training:followup-overdue:{source_module.lower()}:{plant or 'all'}",
                kind="overdue_escalation",
                severity="high",
                headline=fill("training.followup.overdue", overdue=overdue),
                evidence=fill("training.followup.overdue.evidence", overdue=overdue, count=len(rows)),
                recordRefs=list(dict.fromkeys(refs)),
                suggestedAction="Escalate the overdue event-driven trainings — recurrence risk persists until they are done.",
                confidence=confidence_for(len(rows)),
                overdueDays=None,
            )
        )
    return out


__all__ = ["training_cross_bar"]
