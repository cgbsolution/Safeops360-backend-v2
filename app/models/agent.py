"""SQLAlchemy mirror of the Agent / AgentPrompt / AgentInvocation /
AgentToolCall Prisma models. See prisma/schema.prisma "User-initiated AI
Agent infrastructure" section for the design intent and the two-pattern
distinction (workflow-rule agents vs. user-initiated agents-with-tools).

Conventions kept consistent with the rest of app/models/:
  • Table + column names are camelCase to match the Prisma DB columns
  • String[] (Postgres text array) maps to ARRAY(String) not JSON
  • createdAt uses server_default=func.now(); updatedAt uses default=func.now()
    + onupdate (Prisma @updatedAt is client-managed, no DB default)
  • Enum-like fields are plain String columns with allowed values documented
    inline — matches the rest of the codebase (status / authority levels are
    String, not Enum)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


class Agent(Base, IdMixin):
    """A configured AI agent. One row per agent code (RCA_ASSISTANT,
    CAPA_SUGGESTION, etc.). The active prompt is referenced by
    activePromptId; prompt history lives on AgentPrompt rows.
    """

    __tablename__ = "Agent"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # Functional module the agent serves. Values mirror the existing
    # permission module taxonomy (INCIDENT / OBSERVATION / NEAR_MISS /
    # PTW / FLRA / INSPECTION / TRAINING / MANHOURS / CONFIGURATION).
    module: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # Opaque capability map { "capability_key": "human description" }.
    # Used by the Configuration → Agents UI; the runtime treats this as
    # documentation only.
    capabilities: Mapped[dict] = mapped_column(JSON, nullable=False)

    # ── Model selection ────────────────────────────────────────────────
    # primaryModelId is the default. escalationModelId is consulted when
    # agent_service decides to retry with a stronger model (low confidence
    # or explicit user request). Both hold full Anthropic model IDs.
    primaryModelId: Mapped[str] = mapped_column(String, nullable=False)
    escalationModelId: Mapped[str | None] = mapped_column(String)

    # Pointer to the currently active AgentPrompt. Defined as a plain FK
    # here; the back-reference is on AgentPrompt.activeForAgent.
    activePromptId: Mapped[str | None] = mapped_column(
        ForeignKey("AgentPrompt.id"), unique=True
    )

    # ── Authority ──────────────────────────────────────────────────────
    currentAuthorityLevel: Mapped[str] = mapped_column(
        String, nullable=False, default="L0", server_default="L0"
    )
    maxAuthorityLevel: Mapped[str] = mapped_column(
        String, nullable=False, default="L0", server_default="L0"
    )
    authorityRationale: Mapped[str | None] = mapped_column(Text)

    # ── Tools (names only; definitions in app/services/agents/tools/) ──
    availableTools: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default="{}"
    )

    # ── Economic estimates surfaced in the UI before invocation ────────
    estimatedTokensPerInvocation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=8000, server_default="8000"
    )
    estimatedCostPerInvocation: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.012, server_default="0.012"
    )

    # ── Operational state ──────────────────────────────────────────────
    isActive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    isInPilot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    rateLimit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=50, server_default="50"
    )

    # ── Rolling metrics (calibration job populates these) ──────────────
    totalInvocations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    totalAcceptances: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    totalModifications: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    totalRejections: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    averageLatencyMs: Mapped[int | None] = mapped_column(Integer)
    averageCostUsd: Mapped[float | None] = mapped_column(Float)
    calibrationScore: Mapped[float | None] = mapped_column(Float)
    lastCalibrationAt: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )

    # Relationships. activePrompt FK is unique, so this is a one-to-one
    # the SQL side. We declare it with foreign_keys to disambiguate from
    # the AgentPrompt.agentId → Agent.id link.
    activePrompt: Mapped["AgentPrompt | None"] = relationship(
        "AgentPrompt",
        foreign_keys=[activePromptId],
        post_update=True,  # avoids circular insert ordering with AgentPrompt.agentId
    )
    prompts: Mapped[list["AgentPrompt"]] = relationship(
        "AgentPrompt",
        back_populates="agent",
        foreign_keys="AgentPrompt.agentId",
        cascade="all, delete-orphan",
    )
    invocations: Mapped[list["AgentInvocation"]] = relationship(
        "AgentInvocation", back_populates="agent"
    )


class AgentPrompt(Base, IdMixin):
    """One row per prompt version. Active prompt is the one referenced by
    Agent.activePromptId. Edits create a new row — never mutate in place,
    that's the audit-trail contract.
    """

    __tablename__ = "AgentPrompt"

    agentId: Mapped[str] = mapped_column(
        ForeignKey("Agent.id", ondelete="CASCADE"), nullable=False, index=True
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False)
    systemPrompt: Mapped[str] = mapped_column(Text, nullable=False)
    promptDescription: Mapped[str] = mapped_column(Text, nullable=False)

    # A/B test labels. Null = mainline.
    variantLabel: Mapped[str | None] = mapped_column(String)

    # Per-prompt-version metrics, updated by calibration job.
    invocationCount: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    acceptanceRate: Mapped[float | None] = mapped_column(Float)
    modificationRate: Mapped[float | None] = mapped_column(Float)
    rejectionRate: Mapped[float | None] = mapped_column(Float)

    createdById: Mapped[str] = mapped_column(
        ForeignKey("User.id"), nullable=False
    )
    approvedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    agent: Mapped[Agent] = relationship(
        "Agent", back_populates="prompts", foreign_keys=[agentId]
    )
    invocations: Mapped[list["AgentInvocation"]] = relationship(
        "AgentInvocation", back_populates="promptVersion"
    )


class AgentInvocation(Base, IdMixin):
    """One row per agent invocation. Doubles as the audit log for the
    agent platform — there is no separate AuditLog table. The AUDIT.VIEW
    permission grants read access to these rows plus AgentToolCall.
    """

    __tablename__ = "AgentInvocation"

    invocationNumber: Mapped[str] = mapped_column(
        String, unique=True, nullable=False, index=True
    )

    agentId: Mapped[str] = mapped_column(
        ForeignKey("Agent.id"), nullable=False, index=True
    )

    invokedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    invokedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    invocationTrigger: Mapped[str] = mapped_column(
        String, nullable=False, default="USER_INITIATED", server_default="USER_INITIATED"
    )

    sourceModule: Mapped[str] = mapped_column(String, nullable=False)
    sourceRecordId: Mapped[str] = mapped_column(String, nullable=False)
    sourceRecordType: Mapped[str] = mapped_column(String, nullable=False)
    sourcePlantId: Mapped[str | None] = mapped_column(String)

    authorityLevelUsed: Mapped[str] = mapped_column(String, nullable=False)
    promptVersionId: Mapped[str] = mapped_column(
        ForeignKey("AgentPrompt.id"), nullable=False
    )

    modelUsed: Mapped[str] = mapped_column(String, nullable=False)
    inputTokens: Mapped[int | None] = mapped_column(Integer)
    outputTokens: Mapped[int | None] = mapped_column(Integer)
    totalCostUsd: Mapped[float | None] = mapped_column(Float)
    latencyMs: Mapped[int | None] = mapped_column(Integer)

    inputContext: Mapped[dict] = mapped_column(JSON, nullable=False)
    rawApiResponse: Mapped[dict | None] = mapped_column(JSON)
    agentReasoning: Mapped[str | None] = mapped_column(Text)
    agentSuggestion: Mapped[dict | None] = mapped_column(JSON)
    agentConfidence: Mapped[float | None] = mapped_column(Float)

    # Lifecycle: RUNNING → PENDING_REVIEW → ACCEPTED | MODIFIED | REJECTED | EXPIRED
    # (ERRORED is a terminal state from RUNNING when the tool loop fails)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="RUNNING", server_default="RUNNING"
    )

    humanDecisionAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    humanDecisionById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    humanDecision: Mapped[str | None] = mapped_column(String)
    humanModifications: Mapped[dict | None] = mapped_column(JSON)
    rejectionReason: Mapped[str | None] = mapped_column(Text)

    ratingByHuman: Mapped[int | None] = mapped_column(Integer)
    detailedFeedback: Mapped[str | None] = mapped_column(Text)

    hadError: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    errorType: Mapped[str | None] = mapped_column(String)
    errorDetails: Mapped[str | None] = mapped_column(Text)

    hallucinationFlagged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    hallucinationDetails: Mapped[list | None] = mapped_column(JSON)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    agent: Mapped[Agent] = relationship("Agent", back_populates="invocations")
    promptVersion: Mapped[AgentPrompt] = relationship(
        "AgentPrompt", back_populates="invocations"
    )
    toolCalls: Mapped[list["AgentToolCall"]] = relationship(
        "AgentToolCall",
        back_populates="invocation",
        cascade="all, delete-orphan",
        order_by="AgentToolCall.sequence",
    )


class AgentToolCall(Base, IdMixin):
    """One row per tool call inside an invocation. Ordered by sequence so
    the transparency drawer renders the loop in the order it ran.
    """

    __tablename__ = "AgentToolCall"

    invocationId: Mapped[str] = mapped_column(
        ForeignKey("AgentInvocation.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    toolName: Mapped[str] = mapped_column(String, nullable=False)
    toolInput: Mapped[dict] = mapped_column(JSON, nullable=False)
    toolOutput: Mapped[dict | list | None] = mapped_column(JSON)
    executionMs: Mapped[int | None] = mapped_column(Integer)

    hadError: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    errorDetails: Mapped[str | None] = mapped_column(Text)

    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    invokedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    invocation: Mapped[AgentInvocation] = relationship(
        "AgentInvocation", back_populates="toolCalls"
    )


__all__ = [
    "Agent",
    "AgentPrompt",
    "AgentInvocation",
    "AgentToolCall",
]
