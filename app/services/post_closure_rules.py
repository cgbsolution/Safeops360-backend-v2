"""Post-closure rules engine for Safety Observation (Dimension 4 of the
brief). Ports the Node `src/lib/observation/post-closure-rules.ts` shape
to Python. Each rule is independent — failures are caught and logged, so
one bad rule can't block another from firing. Audit entries are appended
to Observation.closureTriggers (JSONB column).

Currently wired:
  • LessonsDistributionAgent (Anthropic) — generates a sharable lesson
    + audience + follow-up actions on every closure.

Stubs for the remaining rules from the brief (focused inspection on
repeats, contractor score, PPE trend, permit flag, behavioural coaching,
systemic CAPA, analytics refresh, anomaly feed) live in the Node file
for now and can be ported here as the modules they touch land in Python.
"""

from __future__ import annotations

import sys
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.observation import Observation
from app.services.ai.agents.lessons import run_lessons_distribution


async def run_post_closure_rules(
    db: AsyncSession, *, observation_id: str
) -> list[dict[str, Any]]:
    """Run all post-closure rules and persist the audit. Returns the list
    of TriggerEvents so callers (e.g. tests, manual repair tools) can
    inspect what fired."""
    obs = await db.get(Observation, observation_id)
    if obs is None:
        return []

    events: list[dict[str, Any]] = []

    # ── Rule: LessonsDistributionAgent ────────────────────────────────
    try:
        lesson = await run_lessons_distribution(db, observation_id=observation_id)
        if lesson is not None:
            events.append(
                {
                    "ruleId": "rule_lessons_distribution",
                    "ruleName": "Lessons Distribution (AI)",
                    "fired": not lesson.get("skipped", False),
                    "reason": (
                        f"Lesson generated, {len(lesson.get('audience') or [])} audience, "
                        f"{len(lesson.get('actions') or [])} actions"
                        if not lesson.get("skipped")
                        else lesson.get("reason") or "skipped"
                    ),
                    "spawnedRecordType": "AI_LESSON",
                    "data": lesson,
                }
            )
    except Exception as e:  # noqa: BLE001
        events.append(
            {
                "ruleId": "rule_lessons_distribution",
                "ruleName": "Lessons Distribution (AI)",
                "fired": False,
                "error": str(e),
            }
        )
        print(f"[post-closure] lessons agent crashed: {e}", file=sys.stderr)

    # Persist all events onto the observation. Append to existing
    # closureTriggers if any (so re-runs are visible side-by-side).
    if events:
        try:
            existing = obs.closureTriggers or []
            if not isinstance(existing, list):
                existing = []
            obs.closureTriggers = [*existing, *events]
            await db.flush()
        except Exception as e:  # noqa: BLE001
            print(f"[post-closure] persist failed: {e}", file=sys.stderr)

    return events
