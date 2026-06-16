"""Pydantic schemas for the agent platform API surface.

Naming convention mirrors the rest of app/schemas/: <Resource><Verb>
for inputs, <Resource>Out for responses, <Resource>ListResponse for
collections.

These schemas describe the wire format only — internal Python helpers
inside agent_service may pass richer dataclasses around.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ─── Invocation: request side ──────────────────────────────────────────


class InvokeAgentRequest(BaseModel):
    """Body for POST /api/agents/{agent_code}/invoke.

    sourceModule + sourceRecordId tell the agent which record to analyse.
    The frontend never picks the agent code — it's fixed by the page the
    user is on (RCA_ASSISTANT from the Cause Analysis tab, etc.).
    """

    model_config = ConfigDict(extra="ignore")

    sourceModule: str = Field(
        ...,
        description='Module the record belongs to ("INCIDENT" | "OBSERVATION" | ...)',
    )
    sourceRecordId: str = Field(..., description="Primary key of the record to analyse")

    # Optional: forces escalation model on this call. When omitted, the
    # service uses primaryModelId. UI surfaces this as a "Deep analysis"
    # toggle behind an authority gate.
    forceEscalationModel: bool = False


class HumanDecisionRequest(BaseModel):
    """Body for POST /api/agent-invocations/{id}/decision.

    decision is one of:
      • ACCEPT_AS_IS — agent's suggestion is taken verbatim
      • ACCEPT_WITH_MODIFICATION — humanModifications holds the final state
      • REJECT — discard the suggestion, rejectionReason explains why
    """

    model_config = ConfigDict(extra="ignore")

    decision: str = Field(..., description="ACCEPT_AS_IS | ACCEPT_WITH_MODIFICATION | REJECT")
    humanModifications: dict[str, Any] | None = None
    rejectionReason: str | None = None
    rating: int | None = Field(None, ge=1, le=5)
    feedback: str | None = None


# ─── Invocation: response side ─────────────────────────────────────────


class AgentToolCallOut(BaseModel):
    """One tool call inside an invocation. Surfaced to the UI for the
    transparency drawer."""

    id: str
    toolName: str
    toolInput: dict[str, Any]
    toolOutput: Any | None
    executionMs: int | None
    hadError: bool
    errorDetails: str | None
    sequence: int
    invokedAt: datetime

    model_config = {"from_attributes": True}


class AgentInvocationOut(BaseModel):
    """Full invocation record returned to the UI. Contains the agent's
    structured suggestion, reasoning, tool calls, and (when set) the
    human decision."""

    id: str
    invocationNumber: str

    agentId: str
    invocationTrigger: str
    invokedAt: datetime
    invokedById: str | None

    sourceModule: str
    sourceRecordId: str
    sourceRecordType: str
    sourcePlantId: str | None

    authorityLevelUsed: str
    promptVersionId: str
    modelUsed: str

    inputTokens: int | None
    outputTokens: int | None
    totalCostUsd: float | None
    latencyMs: int | None

    # Input context is intentionally NOT in the default response — it can
    # be large and the UI doesn't need it for the standard render. The
    # transparency drawer fetches it via a separate richer endpoint when
    # the user clicks "View raw context".

    agentReasoning: str | None
    agentSuggestion: dict[str, Any] | None
    agentConfidence: float | None

    status: str
    humanDecisionAt: datetime | None
    humanDecisionById: str | None
    humanDecision: str | None
    humanModifications: dict[str, Any] | None
    rejectionReason: str | None

    ratingByHuman: int | None
    detailedFeedback: str | None

    hadError: bool
    errorType: str | None
    errorDetails: str | None

    hallucinationFlagged: bool
    hallucinationDetails: list[dict[str, Any]] | None

    toolCalls: list[AgentToolCallOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class AgentInvocationDetailOut(AgentInvocationOut):
    """Richer view returned by the transparency drawer endpoint. Adds the
    full input context that was fed to the agent and the raw API response
    for the final turn. Restricted to AGENT.AUDIT_VIEW callers."""

    inputContext: dict[str, Any]
    rawApiResponse: dict[str, Any] | None

    model_config = {"from_attributes": True}


class InvocationStartedResponse(BaseModel):
    """Returned by POST /invoke immediately. The actual tool-use loop
    runs in a FastAPI BackgroundTask; clients poll GET /agent-invocations/{id}
    until status leaves "RUNNING" (transitions to PENDING_REVIEW on success
    or ERRORED on failure). agentSuggestion is null until then."""

    invocationId: str
    invocationNumber: str
    status: str  # always "RUNNING" at this point
    pollUrl: str


# ─── Agent config: read side ───────────────────────────────────────────


class AgentOut(BaseModel):
    """Agent configuration surfaced to the Configuration → Agents UI and
    used by the invocation card to render cost / rate-limit hints."""

    id: str
    code: str
    name: str
    description: str
    module: str
    capabilities: dict[str, Any]

    primaryModelId: str
    escalationModelId: str | None

    activePromptId: str | None
    currentAuthorityLevel: str
    maxAuthorityLevel: str
    authorityRationale: str | None

    availableTools: list[str]
    estimatedTokensPerInvocation: int
    estimatedCostPerInvocation: float

    isActive: bool
    isInPilot: bool
    rateLimit: int

    totalInvocations: int
    totalAcceptances: int
    totalModifications: int
    totalRejections: int
    averageLatencyMs: int | None
    averageCostUsd: float | None
    calibrationScore: float | None
    lastCalibrationAt: datetime | None

    createdAt: datetime
    updatedAt: datetime

    model_config = {"from_attributes": True}


# ─── Agent config: write side (Configuration → Agents) ─────────────────


class AgentUpdateRequest(BaseModel):
    """PATCH body for /api/agents/{code}. All fields optional; only
    provided keys are applied. Authority promotion is clamped server-side
    to Agent.maxAuthorityLevel — sending a value past the ceiling is a
    400, not silently downgraded."""

    model_config = ConfigDict(extra="ignore")

    currentAuthorityLevel: str | None = Field(
        None, description='"L0" | "L1" | "L2"'
    )
    authorityRationale: str | None = None
    rateLimit: int | None = Field(None, ge=1, le=10_000)
    isActive: bool | None = None
    isInPilot: bool | None = None
    primaryModelId: str | None = None
    escalationModelId: str | None = None


# ─── Operational metrics (dashboard widgets) ───────────────────────────


class AgentMetricsPoint(BaseModel):
    """One bucket in a daily-series chart."""

    date: str  # ISO date (YYYY-MM-DD)
    invocations: int
    accepted: int
    modified: int
    rejected: int
    errored: int
    totalCostUsd: float


class AgentMetricsResponse(BaseModel):
    """Operational metrics returned by GET /api/agents/{code}/metrics?days=N.

    Today: a rolling window for the agent. Counts come from
    AgentInvocation aggregations — these are NOT the Agent.* rolling
    columns (which are all-time, populated by the calibration job).
    """

    agentCode: str
    windowDays: int
    totalInvocations: int
    decidedInvocations: int
    accepted: int
    modified: int
    rejected: int
    errored: int
    hallucinationFlagged: int
    averageRating: float | None
    averageLatencyMs: int | None
    averageCostUsd: float | None
    totalCostUsd: float
    daily: list[AgentMetricsPoint]


class AgentInvocationListItem(BaseModel):
    """Single row for the drill-down table on the agent detail page."""

    id: str
    invocationNumber: str
    invokedAt: datetime
    invokedById: str | None
    sourceModule: str
    sourceRecordId: str
    sourcePlantId: str | None
    modelUsed: str
    status: str
    humanDecision: str | None
    ratingByHuman: int | None
    totalCostUsd: float | None
    latencyMs: int | None
    hallucinationFlagged: bool
    hadError: bool

    model_config = {"from_attributes": True}


class AgentInvocationListResponse(BaseModel):
    items: list[AgentInvocationListItem]
    total: int


# ─── Prompt versioning ─────────────────────────────────────────────────


class AgentPromptOut(BaseModel):
    id: str
    agentId: str
    version: int
    promptDescription: str
    variantLabel: str | None
    invocationCount: int
    acceptanceRate: float | None
    modificationRate: float | None
    rejectionRate: float | None
    createdById: str
    approvedById: str | None
    approvedAt: datetime | None
    createdAt: datetime
    isActive: bool  # derived: this version is currently pointed to by Agent.activePromptId

    model_config = {"from_attributes": True}


class AgentPromptDetailOut(AgentPromptOut):
    """Includes the full prompt body. Returned only by the per-version
    detail endpoint — too heavy for the list view."""

    systemPrompt: str


class CalibrationRunResultItem(BaseModel):
    """One agent's calibration summary, returned by the manual-trigger
    endpoint."""

    agentId: str
    agentCode: str
    totalInvocations: int
    decidedTotal: int
    totalAcceptances: int
    totalModifications: int
    totalRejections: int
    totalExpired: int
    calibrationScore: float | None
    averageLatencyMs: int | None
    averageCostUsd: float | None
    promptVersionsUpdated: int


class CalibrationRunResponse(BaseModel):
    ranAt: datetime
    durationMs: int
    results: list[CalibrationRunResultItem]


# ─── Cost summary (cross-agent / cross-plant) ──────────────────────────


class CostBreakdownRow(BaseModel):
    """One row in a grouped-cost summary. The `group` field is the
    grouping value (agent code, plant id, or YYYY-MM string)."""

    group: str
    label: str
    totalInvocations: int
    totalCostUsd: float
    averageCostUsd: float | None


class AgentCostSummaryResponse(BaseModel):
    windowDays: int
    totalCostUsd: float
    totalInvocations: int
    byAgent: list[CostBreakdownRow]
    byPlant: list[CostBreakdownRow]
