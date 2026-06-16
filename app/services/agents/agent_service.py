"""Agent runtime for the user-initiated agent platform.

Responsibilities:
  • Load the agent config and active prompt
  • Authorize the caller against the agent's invoke permission code
  • Enforce per-hour rate limit per plant
  • Create the AgentInvocation row (status=RUNNING)
  • Build the input context for the agent (Commit 1 ships a generic
    record-fetch; Commit 3 swaps this for the RCA-specific context
    builder)
  • Run the tool-use loop via complete_with_tools()
  • Parse <reasoning> / <suggestion> / <confidence> from the final text
  • Detect hallucinations (invented record IDs)
  • Persist the result (RUNNING → PENDING_REVIEW / ERRORED)
  • Record human accept/modify/reject decisions
  • Update agent rolling metrics on each decision

Concurrency note: agent invocations are kicked off via FastAPI's
BackgroundTasks. The route returns the invocation ID immediately; the
loop runs in the background and the frontend polls. The background
task gets its own AsyncSession (passed explicitly here, not via
Depends) so it survives the request lifecycle.

Pricing note (cost computation): Anthropic prices vary by model. We
keep a static price table here for cost tracking. Real production
pricing should be loaded from a config table — punt that to Commit 5
when the operations dashboard lands.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentInvocation, AgentPrompt, AgentToolCall
from app.models.capa import Capa
from app.models.hira import HiraEntry, HiraStudy
from app.models.incident import Incident
from app.models.near_miss import NearMiss
from app.models.observation import Observation
from app.models.permit import Permit
from app.services.agents.context_builders import CONTEXT_BUILDERS
from app.services.agents.tools import get_tool_definitions, get_tool_handler
from app.services.ai.anthropic_client import (
    AnthropicApiError,
    AnthropicNotConfiguredError,
    ToolLoopResult,
    complete_with_tools,
)

# ─── Pricing table (USD per million tokens) ────────────────────────────
# Approximate Anthropic public pricing at time of writing. Update as
# Anthropic publishes new tiers. Used only for cost tracking; never
# affects routing. Real numbers come from billing.
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_per_million, output_per_million)
    "claude-opus-4-7": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
_DEFAULT_PRICE = (3.0, 15.0)  # fallback when model not in table


# ─── Agent code → permission code mapping ──────────────────────────────
# Each user-initiated agent gets its own AGENT.<MODULE>.INVOKE permission
# so RBAC can restrict the pilot to specific plants/roles. Adding a new
# agent means adding a row here AND seeding the permission in seed-rbac.ts.
_INVOKE_PERMISSIONS: dict[str, str] = {
    "RCA_ASSISTANT": "AGENT.RCA_INVOKE",
    "PERMIT_RISK_REVIEWER": "AGENT.PERMIT_REVIEW_INVOKE",
    "TRIAGE_AGENT": "AGENT.TRIAGE_INVOKE",
    "HIRA_ASSISTANT": "AGENT.HIRA_INVOKE",
    "CAPA_ASSISTANT": "AGENT.CAPA_INVOKE",
    # Future agents register here.
}


# ─── Hallucination patterns ────────────────────────────────────────────
# Regex patterns matching the SafeOps360 record number conventions. The
# detector greps the agent's final output for these patterns and
# verifies each match against the DB. Unverified matches are flagged.
#
# Patterns observed in seed data:
#   INC-2025-LUM-0042   (Incident)
#   OBS-2025-LUM-0001   (Observation)
#   NM-2025-LUM-0007    (Near Miss)
#   PTW-LUM-0042        (Permit)
_RECORD_ID_PATTERNS: list[tuple[str, str, type[Any]]] = [
    (r"\bINC-\d{4}-[A-Z]{2,5}-\d{3,6}\b", "Incident", Incident),
    (r"\bOBS-\d{4}-[A-Z]{2,5}-\d{3,6}\b", "Observation", Observation),
    (r"\bNM-\d{4}-[A-Z]{2,5}-\d{3,6}\b", "NearMiss", NearMiss),
    (r"\bPTW-[A-Z]{2,5}-\d{3,6}\b", "Permit", Permit),
]


@dataclass
class InvocationResult:
    """Returned by invoke_agent() so callers / tests can introspect the
    full outcome without re-querying the DB."""

    invocation_id: str
    invocation_number: str
    status: str  # "PENDING_REVIEW" or "ERRORED"
    suggestion: dict[str, Any] | None
    reasoning: str | None
    confidence: float | None
    tool_call_count: int
    cost_usd: float | None
    latency_ms: int | None
    hallucination_flagged: bool


# ─────────────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────────────


async def start_invocation(
    *,
    db: AsyncSession,
    agent_code: str,
    source_module: str,
    source_record_id: str,
    user_id: str,
    force_escalation_model: bool = False,
) -> AgentInvocation:
    """Create the AgentInvocation row (status=RUNNING) and return it.

    Caller is expected to:
      1. Have already verified the AGENT.*.INVOKE permission via the
         standard permissions service.
      2. Schedule run_invocation() in a BackgroundTask so the HTTP
         response can return immediately.

    Side effects:
      • Enforces rate limit (raises HTTPException 429 on breach).
      • Resolves plant scope from the source record (used by rate limit
         and for filtering in the dashboard).
      • Generates the invocation number.

    Does NOT call Anthropic. That happens in run_invocation().
    """
    agent = await _load_agent(db, agent_code)
    prompt = await _load_active_prompt(db, agent)

    plant_id = await _resolve_source_plant_id(db, source_module, source_record_id)
    await _enforce_rate_limit(db, agent, plant_id)

    invocation_number = await _generate_invocation_number(db)
    model_to_use = (
        agent.escalationModelId
        if force_escalation_model and agent.escalationModelId
        else agent.primaryModelId
    )

    invocation = AgentInvocation(
        invocationNumber=invocation_number,
        agentId=agent.id,
        invokedById=user_id,
        invocationTrigger="USER_INITIATED",
        sourceModule=source_module,
        sourceRecordId=source_record_id,
        sourceRecordType=source_module,
        sourcePlantId=plant_id,
        authorityLevelUsed=agent.currentAuthorityLevel,
        promptVersionId=prompt.id,
        modelUsed=model_to_use,
        inputContext={},  # filled in run_invocation when context is built
        status="RUNNING",
    )
    db.add(invocation)
    await db.flush()
    await db.refresh(invocation)
    await db.commit()
    return invocation


async def run_invocation(
    *,
    db: AsyncSession,
    invocation_id: str,
) -> InvocationResult:
    """Execute the tool-use loop for an invocation already in RUNNING
    state. This is the function the BackgroundTask runs.

    Catches all errors and lands the invocation in ERRORED state with a
    populated errorDetails — the background task wrapper should not let
    exceptions propagate (no one is awaiting them).
    """
    invocation = await _load_invocation(db, invocation_id)
    if invocation.status != "RUNNING":
        raise ValueError(
            f"Invocation {invocation.invocationNumber} is in status {invocation.status}, "
            "expected RUNNING"
        )

    agent = await db.get(Agent, invocation.agentId)
    prompt = await db.get(AgentPrompt, invocation.promptVersionId)
    if agent is None or prompt is None:
        await _mark_errored(db, invocation, "INTERNAL", "Agent or prompt vanished mid-flight")
        return _result_from(invocation)

    started_at = datetime.now(timezone.utc)
    try:
        context = await _build_context(
            db,
            source_module=invocation.sourceModule,
            source_record_id=invocation.sourceRecordId,
        )
        invocation.inputContext = context
        await db.flush()
    except Exception as e:  # noqa: BLE001
        await _mark_errored(db, invocation, "INTERNAL", f"Context build failed: {e}")
        return _result_from(invocation)

    tool_definitions = get_tool_definitions(list(agent.availableTools))

    async def dispatch_tool(tool_name: str, tool_input: dict[str, Any]) -> Any:
        handler = get_tool_handler(tool_name)
        return await handler(
            tool_input,
            db=db,
            source_record_id=invocation.sourceRecordId,
            source_module=invocation.sourceModule,
        )

    try:
        loop_result: ToolLoopResult = await complete_with_tools(
            system=prompt.systemPrompt,
            initial_user_message=json.dumps(context, default=str, indent=2),
            tools=tool_definitions,
            dispatch_tool=dispatch_tool,
            model=invocation.modelUsed,
        )
    except AnthropicNotConfiguredError as e:
        await _mark_errored(db, invocation, "INTERNAL", str(e))
        return _result_from(invocation)
    except AnthropicApiError as e:
        await _mark_errored(db, invocation, "API_ERROR", str(e))
        return _result_from(invocation)
    except Exception as e:  # noqa: BLE001
        await _mark_errored(db, invocation, "INTERNAL", f"Unexpected runtime error: {e}")
        return _result_from(invocation)

    # Persist tool calls
    for record in loop_result.tool_calls:
        db.add(
            AgentToolCall(
                invocationId=invocation.id,
                toolName=record.name,
                toolInput=record.input,
                toolOutput=record.output,
                executionMs=record.execution_ms,
                hadError=record.had_error,
                errorDetails=record.error_details,
                sequence=record.sequence,
            )
        )

    # Parse the final response into reasoning / suggestion / confidence
    parsed = _parse_final_response(loop_result.final_text)
    invocation.agentReasoning = parsed["reasoning"]
    invocation.agentSuggestion = parsed["suggestion"]
    invocation.agentConfidence = parsed["confidence"]
    invocation.rawApiResponse = loop_result.raw_last_response

    # Token / cost accounting
    invocation.inputTokens = loop_result.input_tokens_total
    invocation.outputTokens = loop_result.output_tokens_total
    invocation.totalCostUsd = _compute_cost(
        invocation.modelUsed,
        loop_result.input_tokens_total,
        loop_result.output_tokens_total,
    )
    invocation.latencyMs = int(
        (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
    )

    # Hallucination detection — scan the agent's text for plausible
    # record IDs and verify each. The full text includes reasoning +
    # suggestion JSON; cast a wide net.
    hallucinations = await _detect_hallucinations(db, loop_result.final_text)
    if hallucinations:
        invocation.hallucinationFlagged = True
        invocation.hallucinationDetails = hallucinations

    # Soft-fail if the loop hit its iteration cap. Still PENDING_REVIEW
    # so the human can decide whether to accept what we have.
    if loop_result.hit_iteration_cap:
        invocation.errorType = "ITERATION_CAP"
        invocation.errorDetails = "Agent loop hit max_iterations without end_turn"

    invocation.status = "PENDING_REVIEW"
    await db.flush()

    # Rolling metric: total invocations only (decision-dependent counters
    # update in record_human_decision()).
    agent.totalInvocations += 1
    await db.commit()

    return _result_from(invocation)


async def record_human_decision(
    *,
    db: AsyncSession,
    invocation_id: str,
    decision: str,
    user_id: str,
    human_modifications: dict[str, Any] | None = None,
    rejection_reason: str | None = None,
    rating: int | None = None,
    feedback: str | None = None,
) -> AgentInvocation:
    """Record the investigator's accept/modify/reject decision for an
    invocation. Updates the rolling acceptance/modification/rejection
    counters on the Agent row.

    Raises HTTPException 404 if the invocation doesn't exist, 409 if
    it's not in PENDING_REVIEW, 400 if decision is invalid.
    """
    if decision not in ("ACCEPT_AS_IS", "ACCEPT_WITH_MODIFICATION", "REJECT"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Invalid decision: {decision!r}. Must be one of "
            "ACCEPT_AS_IS, ACCEPT_WITH_MODIFICATION, REJECT.",
        )
    if decision == "ACCEPT_WITH_MODIFICATION" and human_modifications is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "ACCEPT_WITH_MODIFICATION requires humanModifications payload",
        )
    if decision == "REJECT" and not rejection_reason:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "REJECT requires rejectionReason",
        )

    invocation = await _load_invocation(db, invocation_id)
    if invocation.status != "PENDING_REVIEW":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Invocation is in status {invocation.status!r}, cannot record decision",
        )

    invocation.humanDecisionAt = datetime.now(timezone.utc)
    invocation.humanDecisionById = user_id
    invocation.humanDecision = decision
    invocation.humanModifications = human_modifications
    invocation.rejectionReason = rejection_reason
    invocation.ratingByHuman = rating
    invocation.detailedFeedback = feedback

    if decision == "ACCEPT_AS_IS":
        invocation.status = "ACCEPTED"
    elif decision == "ACCEPT_WITH_MODIFICATION":
        invocation.status = "MODIFIED"
    else:
        invocation.status = "REJECTED"

    # Rolling counters on the Agent row. The calibration job will
    # recompute rates from these later.
    agent = await db.get(Agent, invocation.agentId)
    if agent is not None:
        if invocation.status == "ACCEPTED":
            agent.totalAcceptances += 1
        elif invocation.status == "MODIFIED":
            agent.totalModifications += 1
        elif invocation.status == "REJECTED":
            agent.totalRejections += 1

    await db.commit()
    await db.refresh(invocation)
    return invocation


def get_invoke_permission_code(agent_code: str) -> str:
    """Lookup helper used by the router. Raises if the agent code has no
    registered invoke permission — agents must be registered in
    _INVOKE_PERMISSIONS before they can be invoked."""
    try:
        return _INVOKE_PERMISSIONS[agent_code]
    except KeyError as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown agent code {agent_code!r} (no invoke permission registered)",
        ) from e


# ─────────────────────────────────────────────────────────────────────
#  Internal helpers
# ─────────────────────────────────────────────────────────────────────


async def _load_agent(db: AsyncSession, agent_code: str) -> Agent:
    stmt = select(Agent).where(Agent.code == agent_code)
    agent = (await db.execute(stmt)).scalar_one_or_none()
    if agent is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Agent {agent_code!r} is not configured",
        )
    if not agent.isActive:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Agent {agent_code!r} is currently disabled",
        )
    return agent


async def _load_active_prompt(db: AsyncSession, agent: Agent) -> AgentPrompt:
    if agent.activePromptId is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Agent {agent.code!r} has no active prompt — cannot invoke",
        )
    prompt = await db.get(AgentPrompt, agent.activePromptId)
    if prompt is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Agent {agent.code!r} active prompt pointer is stale",
        )
    return prompt


async def _load_invocation(db: AsyncSession, invocation_id: str) -> AgentInvocation:
    invocation = await db.get(AgentInvocation, invocation_id)
    if invocation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invocation not found")
    return invocation


async def _resolve_source_plant_id(
    db: AsyncSession, source_module: str, source_record_id: str
) -> str | None:
    """Look up the plantId on the source record so per-plant rate limits
    and dashboard filters work. Returns None when the module doesn't
    have a plant (shouldn't happen for any operational module today)."""
    # HIRA's source record is a HiraEntry, which carries no plantId of its
    # own — the plant lives on the parent study. Resolve via the study.
    if source_module == "HIRA":
        entry = await db.get(HiraEntry, source_record_id)
        if entry is None:
            return None
        study = await db.get(HiraStudy, entry.studyId)
        return study.plantId if study is not None else None

    table_map: dict[str, type[Any]] = {
        "INCIDENT": Incident,
        "OBSERVATION": Observation,
        "NEAR_MISS": NearMiss,
        "PTW": Permit,
        "CAPA": Capa,
    }
    model = table_map.get(source_module)
    if model is None:
        return None
    row = await db.get(model, source_record_id)
    if row is None:
        # Don't fail invocation start over a missing source — the tool
        # dispatcher will surface the real error.
        return None
    return getattr(row, "plantId", None)


async def _enforce_rate_limit(
    db: AsyncSession, agent: Agent, plant_id: str | None
) -> None:
    """Block invocation when the configured rate ceiling is reached.
    Rate is per-agent per-plant per hour. A null plant counts against a
    null bucket, so global / cross-plant agents have their own quota."""
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    stmt = (
        select(func.count())
        .select_from(AgentInvocation)
        .where(AgentInvocation.agentId == agent.id)
        .where(AgentInvocation.invokedAt > one_hour_ago)
    )
    if plant_id is None:
        stmt = stmt.where(AgentInvocation.sourcePlantId.is_(None))
    else:
        stmt = stmt.where(AgentInvocation.sourcePlantId == plant_id)
    count = (await db.execute(stmt)).scalar_one()
    if count >= agent.rateLimit:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Rate limit reached for agent {agent.code} "
            f"({agent.rateLimit}/hour). Try again later.",
        )


async def _generate_invocation_number(db: AsyncSession) -> str:
    """Generate "AINV-{year}-{6-digit}" by finding the MAX numeric suffix
    across this year's rows. Tolerates concurrency via the unique
    constraint on invocationNumber catching the rare race."""
    year = datetime.now(timezone.utc).year
    prefix = f"AINV-{year}-"
    existing = (
        await db.execute(
            select(AgentInvocation.invocationNumber).where(
                AgentInvocation.invocationNumber.like(f"{prefix}%")
            )
        )
    ).scalars().all()
    max_suffix = 0
    for n in existing:
        try:
            suffix = int(n.rsplit("-", 1)[-1])
            if suffix > max_suffix:
                max_suffix = suffix
        except ValueError:
            continue
    return f"{prefix}{max_suffix + 1:06d}"


async def _build_context(
    db: AsyncSession, *, source_module: str, source_record_id: str
) -> dict[str, Any]:
    """Build the input context dict fed to the agent.

    Dispatch by module to a registered builder under
    app/services/agents/context_builders/. INCIDENT has the rich
    builder (Commit 3); other modules fall back to a minimal stub
    until their builders are written. Adding a new builder is a
    one-line entry in CONTEXT_BUILDERS, no edit needed here.
    """
    builder = CONTEXT_BUILDERS.get(source_module)
    if builder is not None:
        return await builder(db, source_record_id)

    # Fallback for unregistered modules: enough for the loop to run,
    # not enough for the agent to do anything useful. The system prompt
    # will surface this thinness in its `caveats`.
    return {
        "sourceModule": source_module,
        "sourceRecordId": source_record_id,
        "_note": (
            f"No rich context builder registered for module {source_module!r}; "
            "agent has only the record ID. Register a builder in "
            "app/services/agents/context_builders/."
        ),
    }


def _parse_final_response(text: str) -> dict[str, Any]:
    """Extract <reasoning>, <suggestion>, and <confidence> blocks from
    the agent's final assistant turn. Missing blocks return None — the
    caller decides whether that's an error."""
    out: dict[str, Any] = {"reasoning": None, "suggestion": None, "confidence": None}

    reasoning_match = re.search(r"<reasoning>([\s\S]*?)</reasoning>", text)
    if reasoning_match:
        out["reasoning"] = reasoning_match.group(1).strip()

    suggestion_match = re.search(r"<suggestion>([\s\S]*?)</suggestion>", text)
    if suggestion_match:
        raw = suggestion_match.group(1).strip()
        try:
            out["suggestion"] = json.loads(raw)
        except json.JSONDecodeError:
            # The agent emitted a suggestion block that wasn't valid JSON.
            # Store the raw text under an "_unparsed" key so the UI can
            # still surface it and the prompt engineer can investigate.
            out["suggestion"] = {"_unparsed": raw}

    confidence_match = re.search(r"<confidence>([\d.]+)</confidence>", text)
    if confidence_match:
        try:
            value = float(confidence_match.group(1))
            if 0.0 <= value <= 1.0:
                out["confidence"] = value
        except ValueError:
            pass

    return out


async def _detect_hallucinations(
    db: AsyncSession, text: str
) -> list[dict[str, Any]] | None:
    """Scan the agent's final output for record-ID patterns and verify
    each against the DB. Returns a list of unverified references, or
    None if everything checks out.

    This is a coarse defence — the agent could still hallucinate facts
    that don't involve record IDs. But inventing INC-FAKE-0001-style
    references is the failure mode most damaging to trust, so we
    catch it explicitly.
    """
    findings: list[dict[str, Any]] = []
    for pattern, type_label, model in _RECORD_ID_PATTERNS:
        for match in re.finditer(pattern, text):
            value = match.group(0)
            stmt = select(model.id).where(getattr(model, "number") == value).limit(1)
            existing = (await db.execute(stmt)).scalar_one_or_none()
            if existing is None:
                findings.append(
                    {
                        "type": "INVENTED_RECORD_ID",
                        "recordType": type_label,
                        "value": value,
                        "context": "Pattern matched but no row with this number exists",
                    }
                )
    return findings or None


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Approximate USD cost from token counts using the static price
    table. Returns 0.0 when the model isn't priced (don't block on
    missing pricing)."""
    in_price, out_price = _MODEL_PRICING.get(model, _DEFAULT_PRICE)
    return round(
        (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price,
        6,
    )


async def _mark_errored(
    db: AsyncSession,
    invocation: AgentInvocation,
    error_type: str,
    detail: str,
) -> None:
    """Land an invocation in ERRORED status with the failure cause."""
    invocation.status = "ERRORED"
    invocation.hadError = True
    invocation.errorType = error_type
    invocation.errorDetails = detail
    await db.commit()


def _result_from(invocation: AgentInvocation) -> InvocationResult:
    """Build the InvocationResult dataclass from the persisted row.
    Used by both successful and errored returns.

    Note: tool_call_count is left at 0 here. Touching invocation.toolCalls
    on an async session triggers a sync greenlet load (MissingGreenlet)
    unless the relationship was explicitly eager-loaded. Callers who
    need the count should query AgentToolCall directly.
    """
    return InvocationResult(
        invocation_id=invocation.id,
        invocation_number=invocation.invocationNumber,
        status=invocation.status,
        suggestion=invocation.agentSuggestion,
        reasoning=invocation.agentReasoning,
        confidence=invocation.agentConfidence,
        tool_call_count=0,
        cost_usd=invocation.totalCostUsd,
        latency_ms=invocation.latencyMs,
        hallucination_flagged=invocation.hallucinationFlagged,
    )
