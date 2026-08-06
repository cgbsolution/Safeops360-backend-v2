"""Post-closure rules engine for Safety Observation (Dimension 4 of the
brief). Ports the Node `src/lib/observation/post-closure-rules.ts` shape
to Python.

Reliability comes from the shared `app.services.trigger_engine`, not from
per-runner try/except. Before that engine existed this file caught a rule
crash with `print(..., file=sys.stderr)` and — worse — wrapped the
*persistence of the audit itself* in a bare try/except that printed and moved
on. A failed write therefore left no trace that the run had happened at all,
which is indistinguishable from a trigger that never fired. Both paths now go
through `run_trigger_rules`: stack traces are logged, failures become explicit
FAILED audit entries, an HSE Manager is notified, and a sink failure is
reported to the caller instead of printed.

Currently wired:
  • LessonsDistributionAgent (Anthropic) — generates a sharable lesson
    + audience + follow-up actions on every closure.

Stubs for the remaining rules from the brief (focused inspection on
repeats, contractor score, PPE trend, permit flag, behavioural coaching,
systemic CAPA, analytics refresh, anomaly feed) live in the Node file
for now and can be ported here as the modules they touch land in Python.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.observation import Observation
from app.services.ai.agents.lessons import run_lessons_distribution
from app.services.trigger_engine import (
    TriggerOutcome,
    TriggerResult,
    json_column_sink,
    run_trigger_rules,
)


async def _rule_lessons_distribution(db: AsyncSession, obs: Observation) -> TriggerResult:
    """AI lesson generation. Raising is fine — the engine converts it to a
    FAILED entry with the stack trace logged and the HSE Manager told, which is
    what this rule's old self-catching `except` could not do."""
    lesson = await run_lessons_distribution(db, observation_id=obs.id)
    if lesson is None:
        return TriggerResult(
            rule_name="Lessons Distribution (AI)",
        rule_id="rule_lessons_distribution",
            outcome=TriggerOutcome.SKIPPED,
            reason="Agent produced no lesson.",
        )
    if lesson.get("skipped"):
        return TriggerResult(
            rule_name="Lessons Distribution (AI)",
        rule_id="rule_lessons_distribution",
            outcome=TriggerOutcome.SKIPPED,
            reason=str(lesson.get("reason") or "skipped"),
            data=lesson,
        )
    return TriggerResult(
        rule_name="Lessons Distribution (AI)",
        rule_id="rule_lessons_distribution",
        outcome=TriggerOutcome.FIRED,
        reason=(
            f"Lesson generated, {len(lesson.get('audience') or [])} audience, "
            f"{len(lesson.get('actions') or [])} actions"
        ),
        spawned_record_type="AI_LESSON",
        data=lesson,
    )


_RULES = (_rule_lessons_distribution,)


async def run_post_closure_rules(
    db: AsyncSession, *, observation_id: str
) -> list[dict[str, Any]]:
    """Run all post-closure rules and persist the audit. Returns the list
    of TriggerEvents so callers (e.g. tests, manual repair tools) can
    inspect what fired."""
    obs = await db.get(Observation, observation_id)
    if obs is None:
        return []

    run = await run_trigger_rules(
        db,
        _RULES,
        obs,
        source_kind="Observation",
        source_id=observation_id,
        sink=json_column_sink("closureTriggers"),
        site_id=getattr(obs, "plantId", None),
    )
    return run.audit_entries()
