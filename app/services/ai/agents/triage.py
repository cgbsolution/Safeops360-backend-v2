"""TriageAgent — runs once per observation submission.

Reads the just-created observation and asks Claude to suggest:
  • severity (LOW | MEDIUM | HIGH | CRITICAL) — sanity check on the
    observer's pick
  • category — sanity check
  • priority (NORMAL | HIGH | URGENT)
  • two short "first-response" prompts the responsible person should do
    immediately

Fail-open: never blocks creation.
"""

from __future__ import annotations

from typing import Any

from app.services.ai.anthropic_client import complete_json, is_configured

AGENT_ID = "TriageAgent"

_SYSTEM_PROMPT = """You are SafeOps360's TriageAgent. You receive a freshly submitted safety observation and produce a quick triage in JSON.

Output JSON ONLY with EXACTLY these keys:

{
  "suggestedSeverity": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
  "suggestedCategory": "<one of: PPE, HOUSEKEEPING, WORK_AT_HEIGHT, HOT_WORK, MOBILE_EQUIPMENT, ELECTRICAL, MATERIAL_HANDLING, CONFINED_SPACE, CHEMICAL_HANDLING, EMERGENCY, OTHER>",
  "priority": "NORMAL" | "HIGH" | "URGENT",
  "rationale": "<single sentence explaining why>",
  "firstResponse": ["<short imperative>", "<short imperative>"]   // exactly 2 items
}

Rules:
  • Be conservative on severity — only call CRITICAL when fatality / serious-injury potential is clear from the description.
  • Output JSON only. No prose around it."""


async def run_triage(*, observation: dict[str, Any]) -> dict[str, Any] | None:
    """Caller passes the freshly-created Observation as a dict (the
    fields needed are: type, category, severity, description,
    immediateAction). Returns a triage dict or None on failure."""
    if not is_configured():
        return {"agentId": AGENT_ID, "skipped": True, "reason": "ANTHROPIC_API_KEY not set"}

    import json

    settings_model = None
    try:
        from app.core.config import get_settings

        settings_model = get_settings().anthropic_model
    except Exception:
        pass

    result = await complete_json(
        system=_SYSTEM_PROMPT,
        user=json.dumps(observation, default=str, indent=2),
        max_tokens=400,
        temperature=0.1,
    )
    if result is None:
        return {"agentId": AGENT_ID, "skipped": True, "reason": "model call failed"}

    cleaned = {
        "agentId": AGENT_ID,
        "model": settings_model,
        "suggestedSeverity": str(result.get("suggestedSeverity") or "").upper() or None,
        "suggestedCategory": str(result.get("suggestedCategory") or "").upper() or None,
        "priority": str(result.get("priority") or "NORMAL").upper(),
        "rationale": str(result.get("rationale") or "").strip()[:400],
        "firstResponse": [str(a) for a in (result.get("firstResponse") or [])][:3],
    }
    return cleaned
