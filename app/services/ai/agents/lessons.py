"""LessonsDistributionAgent — runs once per observation closure.

Reads the closed observation and its workflow audit trail, asks Claude for
a sharable "lesson learned" plus a target audience and follow-up actions.
Output is persisted onto Observation.closureTriggers (existing JSON column)
under a fixed key so the UI can render it in the Related Items section.

Fail-open: when no API key is set OR the call fails for any reason, we
skip and don't block closure.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai.anthropic_client import complete_json, is_configured

AGENT_ID = "LessonsDistributionAgent"

_SYSTEM_PROMPT = """You are SafeOps360's LessonsDistributionAgent. Your job is to turn a single closed safety observation into a concise, sharable lesson that helps prevent the same issue elsewhere in the organisation.

You receive a JSON record describing the observation and its workflow history. You output a JSON object with EXACTLY these keys (and no others):

{
  "lesson": "<1-2 sentence plain-English lesson, written for an operations crew, not a manager. Avoid jargon.>",
  "audience": ["<role or department>", ...],   // who should be told about this — pick from: ALL_PLANT_OPERATIONS, AREA_SUPERVISORS, HSE_MANAGER, CONTRACTOR_CREWS, MAINTENANCE, SPECIFIC_DEPARTMENT
  "actions": ["<short imperative action, e.g. 'Review confined-space entry checklist with all permit issuers'>", ...],  // up to 3 follow-up actions, each a single line
  "tags": ["<short tag>", ...],                 // 2-4 themes, e.g. PPE_COMPLIANCE, HOUSEKEEPING_DRIFT, CONTRACTOR_BEHAVIOUR
  "confidence": "LOW" | "MEDIUM" | "HIGH"        // your confidence the lesson is generalisable
}

Rules:
  1. Output JSON only. No prose around it.
  2. If the observation is too thin to draw a useful lesson (e.g. trivial PPE miss with no context), set confidence="LOW" and keep `actions` short.
  3. Keep `lesson` under 280 characters.
  4. Never invent facts not present in the input."""


def _summarise_observation_for_prompt(obs: dict[str, Any]) -> str:
    """Build a compact JSON-y prompt body. Keeping it small keeps cost low."""
    import json

    return json.dumps(obs, default=str, indent=2)


async def run_lessons_distribution(
    db: AsyncSession, *, observation_id: str
) -> dict[str, Any] | None:
    """Returns a structured lesson dict or None when the agent is disabled
    or the call failed. Caller is responsible for persisting it.

    Shape: { lesson, audience[], actions[], tags[], confidence }, plus
    `agentId` and `model` injected by this function for audit."""
    if not is_configured():
        return {"agentId": AGENT_ID, "skipped": True, "reason": "ANTHROPIC_API_KEY not set"}

    from app.models.observation import Observation
    from app.models.workflow import WorkflowHistory, WorkflowInstance
    from sqlalchemy import select

    obs = await db.get(Observation, observation_id)
    if obs is None:
        return None

    # Pull workflow history if any so the agent can read what was actually
    # done, not just the static observation fields.
    inst = (
        await db.execute(
            select(WorkflowInstance).where(
                WorkflowInstance.module == "OBSERVATION",
                WorkflowInstance.recordId == observation_id,
            )
        )
    ).scalar_one_or_none()
    history_rows: list[WorkflowHistory] = []
    if inst is not None:
        history_rows = list(
            (
                await db.execute(
                    select(WorkflowHistory)
                    .where(WorkflowHistory.instanceId == inst.id)
                    .order_by(WorkflowHistory.performedAt)
                )
            )
            .scalars()
            .all()
        )

    payload = {
        "number": obs.number,
        "type": obs.type.value if hasattr(obs.type, "value") else obs.type,
        "category": obs.category.value if hasattr(obs.category, "value") else obs.category,
        "severity": obs.severity.value if hasattr(obs.severity, "value") else obs.severity,
        "description": obs.description,
        "immediateAction": obs.immediateAction,
        "closingRemark": obs.closingRemark,
        "history": [
            {
                "step": h.stepName,
                "action": h.action.value if hasattr(h.action, "value") else h.action,
                "comments": h.comments,
            }
            for h in history_rows
        ],
    }

    settings_model = None
    try:
        from app.core.config import get_settings

        settings_model = get_settings().anthropic_model
    except Exception:
        pass

    result = await complete_json(
        system=_SYSTEM_PROMPT,
        user=_summarise_observation_for_prompt(payload),
        max_tokens=600,
        temperature=0.2,
    )
    if result is None:
        return {"agentId": AGENT_ID, "skipped": True, "reason": "model call failed"}

    # Light shape check — don't trust the model blindly
    cleaned = {
        "agentId": AGENT_ID,
        "model": settings_model,
        "lesson": str(result.get("lesson") or "").strip()[:600],
        "audience": [str(a) for a in (result.get("audience") or [])][:8],
        "actions": [str(a) for a in (result.get("actions") or [])][:5],
        "tags": [str(t) for t in (result.get("tags") or [])][:6],
        "confidence": str(result.get("confidence") or "MEDIUM").upper(),
    }
    return cleaned
