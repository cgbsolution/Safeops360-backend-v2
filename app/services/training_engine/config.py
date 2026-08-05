"""Resolve the active TrainingRuleConfig into a pure RuleConfigView.

The rule engine (rules.py) never touches the ORM — it consumes RuleConfigView.
This is the one place that turns the DB config row into that view, honouring the
"all threshold/window values must be tenant-configurable, not hardcoded" business
rule: plant-specific config overrides the global (plantId NULL) row, which
overrides the code defaults.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.training_engine import TrainingRuleConfig
from app.services.training_engine.rules import RuleConfigView


def _view(row: TrainingRuleConfig) -> RuleConfigView:
    return RuleConfigView(
        thresholdCount=row.thresholdCount,
        thresholdWindowDays=row.thresholdWindowDays,
        severitySifImmediate=row.severitySifImmediate,
        severityThreshold=row.severityThreshold,
        recertWindowDays=row.recertWindowDays,
        assignmentDueDays=row.assignmentDueDays,
        correlationWindowDays=row.correlationWindowDays,
        personFlagThreshold=getattr(row, "personFlagThreshold", 2),
        personFlagWindowDays=getattr(row, "personFlagWindowDays", 365),
        personRiskElevated=getattr(row, "personRiskElevated", 3),
        personRiskHigh=getattr(row, "personRiskHigh", 6),
        personRiskCritical=getattr(row, "personRiskCritical", 10),
    )


async def resolve_config(db: AsyncSession, plant_id: str | None) -> RuleConfigView:
    rows = (
        await db.execute(select(TrainingRuleConfig).where(TrainingRuleConfig.isActive.is_(True)))
    ).scalars().all()
    if plant_id:
        specific = next((r for r in rows if r.plantId == plant_id), None)
        if specific is not None:
            return _view(specific)
    glob = next((r for r in rows if r.plantId is None), None)
    if glob is not None:
        return _view(glob)
    return RuleConfigView.defaults()
