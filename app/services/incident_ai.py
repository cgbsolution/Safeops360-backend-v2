"""Feature 2 — AI-assist for incidents (summary draft + root-cause suggestion).

The two highest investigator-time-cost points:

  1. draft_summary — turn the raw first-responder report into a single tight
     paragraph (equipment, location, what happened, injury status, cost if
     mentioned). No speculation, no cause attribution.

  2. suggest_root_cause — retrieve the most similar CLOSED incidents (via the
     rule-based matcher) and propose a root-cause statement, with a confidence
     score that is COMPUTED from how many retrieved matches share the current
     incident's causal category — never a number the model invents.

Both are fail-soft: `complete_json` returns None on any failure (unconfigured
key, API error, bad JSON) and these helpers propagate None so the UI degrades
to fully-manual entry. No AI output is ever written to a field as if it were
human-authored — the router marks provenance in `incident.aiAssist`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident
from app.services import incident_similarity
from app.services.ai.anthropic_client import complete_json, is_configured

_SUMMARY_SYSTEM = (
    "You are an EHS incident documentation assistant for an industrial safety "
    "platform. Given a raw first-responder report, extract into ONE tight "
    "paragraph: the equipment involved (with any ID), the location, what "
    "happened, injury status, and estimated cost if (and only if) it is "
    "mentioned. Rules: no speculation, no root-cause attribution, no blame, no "
    "recommendations. British/Indian English. Respond ONLY as JSON: "
    '{"summary": "<one paragraph>"}.'
)

_ROOT_CAUSE_SYSTEM = (
    "You are an EHS root-cause analysis assistant. You are given the current "
    "incident's immediate and underlying causes, plus the root causes of the "
    "most similar past incidents. Propose ONE concise, specific root-cause "
    "statement (a systemic/underlying cause, not a restatement of the event). "
    "Do NOT invent a confidence number — confidence is computed separately. "
    'Respond ONLY as JSON: {"rootCause": "<one sentence>"}.'
)


def available() -> bool:
    """Whether the AI provider is configured (drives UI feature gating)."""
    return is_configured()


def _report_blob(incident: Incident) -> str:
    parts = [
        f"Incident type: {incident.type.value if incident.type else 'unknown'}",
        f"Location: {incident.location or ''} {incident.specificLocation or ''}".strip(),
        f"Initial report: {incident.initialDescription or ''}",
        f"Description: {incident.description or ''}",
        f"Immediate action taken: {incident.immediateAction or ''}",
    ]
    return "\n".join(p for p in parts if p.strip())


async def draft_summary(db: AsyncSession, incident: Incident) -> dict[str, Any] | None:
    """Return {"summary": str} or None (fail-soft)."""
    if not is_configured():
        return None
    result = await complete_json(
        system=_SUMMARY_SYSTEM,
        user=_report_blob(incident),
        max_tokens=400,
        temperature=0.1,
    )
    if not result or not isinstance(result, dict):
        return None
    summary = (result.get("summary") or "").strip()
    if not summary:
        return None
    return {"summary": summary}


async def suggest_root_cause(db: AsyncSession, incident: Incident) -> dict[str, Any] | None:
    """Return {"text", "confidence", "basedOnIncidentIds"} or None (fail-soft).

    Confidence = (# retrieved matches sharing the current incident's causal
    category) / (# retrieved) × 100 — computed here, not by the model. The
    incident `type` is the v1 causal-category proxy; when Feature 3 adds
    structured causal categorisation, swap the category key without touching
    this contract.
    """
    if not is_configured():
        return None

    matches = await incident_similarity.similar_incidents(
        db, incident, only_closed=True, limit=10
    )
    retrieved = len(matches)
    based_on = [m["incidentId"] for m in matches]

    # Computed confidence — category overlap among the retrieved matches.
    cur_category = incident.type.value if incident.type else None
    matching = sum(1 for m in matches if m.get("type") == cur_category)
    confidence = round(matching / retrieved * 100) if retrieved else 0
    confidence = max(0, min(100, confidence))

    # Build the prompt: current causes + retrieved root causes.
    retrieved_causes = []
    for m in matches:
        for rc in m.get("rootCauses") or []:
            retrieved_causes.append(f"- ({m['number']}) {rc}")
    user = "\n".join(
        [
            f"Current immediate causes: {', '.join(incident.immediateCauses or []) or '—'}",
            f"Current underlying causes: {', '.join(incident.underlyingCauses or []) or '—'}",
            "",
            "Root causes of the most similar past incidents:",
            ("\n".join(retrieved_causes) if retrieved_causes else "- (none on record)"),
        ]
    )
    result = await complete_json(
        system=_ROOT_CAUSE_SYSTEM, user=user, max_tokens=300, temperature=0.2
    )
    if not result or not isinstance(result, dict):
        return None
    text = (result.get("rootCause") or "").strip()
    if not text:
        return None
    return {"text": text, "confidence": confidence, "basedOnIncidentIds": based_on}
