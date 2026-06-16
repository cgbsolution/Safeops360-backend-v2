"""Thin wrapper around the Anthropic SDK.

Two entry points serve the two agent patterns in SafeOps360:

  • complete_json() — single-turn, JSON-only response. Used by the
    workflow-rule-triggered fire-and-forget agents (TriageAgent,
    LessonsDistributionAgent). Simpler, no tools, no streaming. These
    agents must never block the workflow; the wrapper returns None on
    any failure.

  • complete_with_tools() — multi-turn tool-use loop. Used by the
    user-initiated agent platform (RcaAssistantAgent and future agents
    with tools). Drives the conversation forward through repeated
    tool_use ↔ tool_result exchanges until the model emits end_turn or
    a configured iteration cap is hit. Raises on hard failures so the
    AgentInvocation row can record an ERRORED state.

Both functions share the same lazy-init Anthropic client and the same
"return None when no API key" graceful-degrade for the simple path.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings

_client = None


def _get_client():
    """Lazy-init the Anthropic client. Returns None when no API key is set."""
    global _client
    settings = get_settings()
    if not settings.anthropic_api_key:
        return None
    if _client is None:
        try:
            from anthropic import Anthropic  # noqa: PLC0415

            # Cap per-request time so a hung call fails fast (~90s) instead of
            # the SDK default (~10 min) — which is what left invocations
            # "Running" for minutes. One retry covers transient 5xx/timeouts.
            _client = Anthropic(api_key=settings.anthropic_api_key, timeout=90.0, max_retries=1)
        except Exception as e:  # noqa: BLE001
            print(f"[ai] Failed to init Anthropic client: {e}", file=sys.stderr)
            return None
    return _client


def is_configured() -> bool:
    return get_settings().anthropic_api_key is not None


async def complete_json(
    *,
    system: str,
    user: str,
    max_tokens: int = 1024,
    temperature: float = 0.2,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Synchronous Anthropic call wrapped in async-friendly shape.

    The Anthropic SDK is sync; for our low-volume agent calls (one per
    observation closure / submission) we're fine running it inline.
    Returns the parsed JSON dict, or None on any failure (key missing,
    API error, parse error). Callers MUST handle None.
    """
    client = _get_client()
    if client is None:
        return None
    settings = get_settings()
    used_model = model or settings.anthropic_model
    try:
        # Run the sync SDK call off the event loop so it never blocks other
        # requests (e.g. the frontend status-poll) while the model thinks.
        msg = await asyncio.to_thread(
            client.messages.create,
            model=used_model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:  # noqa: BLE001
        print(f"[ai] Anthropic call failed (model={used_model}): {e}", file=sys.stderr)
        return None

    # Extract the text content. SDK returns a list of content blocks.
    parts: list[str] = []
    for block in msg.content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    raw = "".join(parts).strip()
    if not raw:
        return None

    # Try direct parse first; fall back to extracting the first {...} block.
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        snippet = raw[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError as e:
            print(f"[ai] Failed to parse JSON from model output: {e}\n---\n{raw[:500]}", file=sys.stderr)
            return None
    return None


# ─────────────────────────────────────────────────────────────────────
#  Tool-use loop (used by the user-initiated agent platform)
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ToolCallRecord:
    """One tool invocation inside the loop. The agent_service persists
    these as AgentToolCall rows."""

    name: str
    input: dict[str, Any]
    output: Any | None = None
    execution_ms: int | None = None
    had_error: bool = False
    error_details: str | None = None
    sequence: int = 0  # 1-based; set by the caller


@dataclass
class ToolLoopResult:
    """Final result of a complete_with_tools() run."""

    # The text content of the final assistant turn. Callers parse out
    # <reasoning>, <suggestion>, <confidence> from this.
    final_text: str

    # Sum of input tokens across every turn in the loop. The Anthropic
    # API charges per-turn, so the total cost is sum(input_tokens) *
    # input_price + sum(output_tokens) * output_price.
    input_tokens_total: int
    output_tokens_total: int

    # The raw .model_dump() of the LAST API response. Stored for
    # debugging / audit; older turns are not preserved.
    raw_last_response: dict[str, Any]

    # Tool calls in execution order. Sequence is 1-based.
    tool_calls: list[ToolCallRecord] = field(default_factory=list)

    # Set when the loop terminated because max_iterations was reached
    # without an end_turn. Indicates the agent is "looping" — caller
    # should treat this as a soft failure (still has partial output).
    hit_iteration_cap: bool = False


class AnthropicNotConfiguredError(RuntimeError):
    """Raised by complete_with_tools() when ANTHROPIC_API_KEY is unset.
    The simpler complete_json() returns None in this case so workflows
    keep running, but agent invocations cannot proceed silently — the
    agent_service needs to record an ERRORED state with a clear cause."""


class AnthropicApiError(RuntimeError):
    """Raised when the SDK raises during messages.create() inside the
    tool loop. agent_service catches this and records the invocation as
    ERRORED with errorType=API_ERROR."""


# Type alias for a tool dispatcher. The agent_service supplies this to
# complete_with_tools(): given a tool name + input dict, run the handler
# and return its output as a JSON-serialisable value. Raising is fine;
# the loop catches the exception and feeds an error tool_result back to
# the model so it can recover or stop.
ToolDispatcher = Callable[[str, dict[str, Any]], Awaitable[Any]]


async def complete_with_tools(
    *,
    system: str,
    initial_user_message: str,
    tools: list[dict[str, Any]],
    dispatch_tool: ToolDispatcher,
    model: str,
    max_tokens: int = 4096,
    max_iterations: int = 8,
    temperature: float = 0.2,
) -> ToolLoopResult:
    """Run a tool-use loop against the Anthropic Messages API.

    Args:
      system: System prompt for the agent.
      initial_user_message: The first user turn — typically the
        serialised record context (incident facts as JSON).
      tools: List of tool definitions in Anthropic's input_schema
        format ({"name", "description", "input_schema"}). The
        tool framework owns this format; this wrapper just forwards it.
      dispatch_tool: Async callable that runs a named tool with the
        model's input and returns the result. Exceptions from this
        callable are caught and fed back as tool errors.
      model: Full Anthropic model ID (e.g. "claude-opus-4-7").
      max_tokens: Per-turn output cap. The same value is used for every
        turn in the loop.
      max_iterations: Safety cap on the loop. Each iteration is one
        messages.create() call. Most real agents finish in 2-5
        iterations; 8 is generous. Hitting the cap surfaces as
        hit_iteration_cap=True on the result.
      temperature: Forwarded to the API. Default 0.2 favours determinism
        for the structured output the platform expects.

    Returns:
      ToolLoopResult with the final assistant text, aggregated token
      usage, and every tool call made.

    Raises:
      AnthropicNotConfiguredError if no API key is set.
      AnthropicApiError on SDK failures.
    """
    client = _get_client()
    if client is None:
        raise AnthropicNotConfiguredError("ANTHROPIC_API_KEY is not configured")

    messages: list[dict[str, Any]] = [{"role": "user", "content": initial_user_message}]
    tool_calls: list[ToolCallRecord] = []
    input_tokens_total = 0
    output_tokens_total = 0
    last_response_dump: dict[str, Any] = {}
    hit_cap = False

    for iteration in range(max_iterations):
        # Anthropic's API rejects tools=[] with a validation error
        # ("tools must contain at least one item"). When the caller has
        # no tools (e.g. the TriageAgent which reasons purely over its
        # context), omit the parameter entirely so the call still works.
        # The model can never emit tool_use blocks in this mode, so the
        # loop naturally finishes in one iteration.
        create_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": messages,
        }
        if tools:
            create_kwargs["tools"] = tools

        try:
            response = await asyncio.to_thread(
                client.messages.create, **create_kwargs
            )
        except Exception as e:  # noqa: BLE001
            raise AnthropicApiError(f"messages.create failed at iteration {iteration + 1}: {e}") from e

        # Aggregate usage. The SDK's Usage object exposes .input_tokens
        # and .output_tokens. Defensive default to 0 in case the SDK
        # changes shape across minor versions.
        usage = getattr(response, "usage", None)
        if usage is not None:
            input_tokens_total += getattr(usage, "input_tokens", 0) or 0
            output_tokens_total += getattr(usage, "output_tokens", 0) or 0

        last_response_dump = _safe_model_dump(response)

        # If the model stopped because it wants tools, run them and
        # feed the results back. Otherwise we're done.
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason != "tool_use":
            return ToolLoopResult(
                final_text=_extract_text(response),
                input_tokens_total=input_tokens_total,
                output_tokens_total=output_tokens_total,
                raw_last_response=last_response_dump,
                tool_calls=tool_calls,
                hit_iteration_cap=False,
            )

        # Append the assistant turn verbatim — the API requires the
        # tool_use blocks to round-trip in the same content order.
        messages.append({"role": "assistant", "content": response.content})

        tool_result_blocks: list[dict[str, Any]] = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_use_id = block.id
            tool_name = block.name
            tool_input: dict[str, Any] = dict(block.input or {})

            sequence = len(tool_calls) + 1
            record = ToolCallRecord(name=tool_name, input=tool_input, sequence=sequence)
            tool_calls.append(record)

            start = time.monotonic()
            try:
                output = await dispatch_tool(tool_name, tool_input)
                record.output = output
                record.execution_ms = int((time.monotonic() - start) * 1000)
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": json.dumps(output, default=str),
                    }
                )
            except Exception as e:  # noqa: BLE001
                record.had_error = True
                record.error_details = str(e)
                record.execution_ms = int((time.monotonic() - start) * 1000)
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": f"Error executing tool: {e}",
                        "is_error": True,
                    }
                )

        messages.append({"role": "user", "content": tool_result_blocks})
    else:
        # for/else: we exhausted max_iterations without seeing end_turn.
        # Return whatever we have with the cap flag set so the caller
        # can surface this as a soft failure.
        hit_cap = True

    return ToolLoopResult(
        final_text=_extract_text_from_dump(last_response_dump),
        input_tokens_total=input_tokens_total,
        output_tokens_total=output_tokens_total,
        raw_last_response=last_response_dump,
        tool_calls=tool_calls,
        hit_iteration_cap=hit_cap,
    )


def _extract_text(response: Any) -> str:
    """Pull the concatenated text content from a Messages API response.
    Returns "" if the response had only tool_use blocks (shouldn't
    happen at end_turn but defensive)."""
    parts: list[str] = []
    for block in getattr(response, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts).strip()


def _extract_text_from_dump(dump: dict[str, Any]) -> str:
    """Same as _extract_text but operates on a .model_dump() dict —
    used when the cap is hit and we only have the serialised form."""
    parts: list[str] = []
    for block in dump.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts).strip()


def _safe_model_dump(response: Any) -> dict[str, Any]:
    """Serialise the SDK response to a plain dict for persistence.
    Pydantic models on the SDK expose model_dump; fall back to a manual
    walk for older SDK versions or non-Pydantic responses."""
    dump_fn = getattr(response, "model_dump", None)
    if callable(dump_fn):
        try:
            return dump_fn(mode="json")
        except Exception:  # noqa: BLE001
            try:
                return dump_fn()
            except Exception:  # noqa: BLE001
                pass
    # Fallback: best-effort attribute scrape. Good enough for audit.
    return {
        "id": getattr(response, "id", None),
        "model": getattr(response, "model", None),
        "stop_reason": getattr(response, "stop_reason", None),
        "content": [
            {
                "type": getattr(b, "type", None),
                "text": getattr(b, "text", None),
                "name": getattr(b, "name", None),
                "input": getattr(b, "input", None),
                "id": getattr(b, "id", None),
            }
            for b in getattr(response, "content", None) or []
        ],
    }
