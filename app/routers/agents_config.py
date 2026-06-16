"""Configuration → Agents router.

Separate from app/routers/agents.py (which handles per-invocation
runtime — invoke / poll / decide) because the surface here is purely
admin / operations:

  GET  /api/agents                                 — list with metrics
  GET  /api/agents/{code}                          — single agent config
  GET  /api/agents/{code}/metrics?days=N           — rolling metrics + trend
  GET  /api/agents/{code}/invocations              — drill-down list
  PATCH /api/agents/{code}                         — authority / rate / model / active
  GET  /api/agents/{code}/prompts                  — version history
  GET  /api/agents/{code}/prompts/{version}        — full prompt body
  POST /api/agents/{code}/prompts/{version}/promote — make active
  GET  /api/agent-cost-summary?days=N              — cross-agent / cross-plant
  POST /api/agents/calibration/run                  — manual calibration trigger

Permission model:
  • GET endpoints require AGENT.RCA_INVOKE (someone who can use the
    agent can see its config).
  • PATCH + prompt-promote require AGENT.RCA_CONFIGURE (Corporate HSE
    or SYSTEM_ADMIN per seed).
  • The manual calibration trigger requires AGENT.RCA_CONFIGURE — it's
    an operational action.
  • The cost summary requires AGENT.AUDIT_VIEW (cost is sensitive info
    in some orgs; gate it the same as audit data).
  • Prompt body view requires AGENT.PROMPT_EDIT (the body is the IP).

Field-name reality reminder: this router renders FROM the actual
schema (Agent.code, AgentInvocation.invokedAt, etc.). We don't paper
over the brief's incidentNumber/rcaData examples here — those mapping
decisions live in agent_service.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.agent import Agent, AgentInvocation, AgentPrompt
from app.models.plant import Plant
from app.models.user import User
from app.schemas.agent import (
    AgentCostSummaryResponse,
    AgentInvocationListItem,
    AgentInvocationListResponse,
    AgentMetricsPoint,
    AgentMetricsResponse,
    AgentOut,
    AgentPromptDetailOut,
    AgentPromptOut,
    AgentUpdateRequest,
    CalibrationRunResponse,
    CalibrationRunResultItem,
    CostBreakdownRow,
)
from app.services.agents import agent_service
from app.services.agents.calibration import run_calibration
from app.services.permissions import PermissionContext, can

router = APIRouter(tags=["agents-config"])


# ─── Permission helpers ───────────────────────────────────────────────


async def _require(
    db: AsyncSession, user_id: str, permission_code: str
) -> None:
    """Shorthand: raise 403 unless the user holds the permission. Uses
    a no-record-id PermissionContext — every check in this router is
    operational, not per-record."""
    check = await can(db, user_id, permission_code, PermissionContext())
    if not check.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            check.reason or f"{permission_code} required",
        )


# ─── List + detail ────────────────────────────────────────────────────


@router.get("/api/agents", response_model=list[AgentOut])
async def list_agents(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AgentOut]:
    """List every configured agent. Open to any authenticated user so the
    AI Agents landing page is universally discoverable — invocation on a
    specific record is still gated by AGENT.RCA_INVOKE at the runtime
    router (app/routers/agents.py)."""
    rows = (
        (await db.execute(select(Agent).order_by(Agent.code)))
        .scalars()
        .all()
    )
    return [AgentOut.model_validate(a) for a in rows]


@router.get("/api/agents/{code}", response_model=AgentOut)
async def get_agent(
    code: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentOut:
    # Open to any authenticated user — the AI Agents detail page is a
    # read-only operations dashboard; mutations are gated separately.
    agent = await _load_agent(db, code)
    return AgentOut.model_validate(agent)


@router.patch("/api/agents/{code}", response_model=AgentOut)
async def update_agent(
    code: str,
    payload: AgentUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentOut:
    """Change agent runtime config. Authority promotion is clamped to
    Agent.maxAuthorityLevel — sending L2 when ceiling is L1 returns 400."""
    await _require(db, user.id, "AGENT.RCA_CONFIGURE")
    agent = await _load_agent(db, code)

    levels = {"L0": 0, "L1": 1, "L2": 2}
    if payload.currentAuthorityLevel is not None:
        requested = payload.currentAuthorityLevel
        if requested not in levels:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Invalid authority level {requested!r}. Use L0 | L1 | L2.",
            )
        if levels[requested] > levels[agent.maxAuthorityLevel]:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Authority {requested!r} exceeds ceiling {agent.maxAuthorityLevel!r}. "
                f"Raise maxAuthorityLevel first (a separate, governed action).",
            )
        agent.currentAuthorityLevel = requested

    if payload.authorityRationale is not None:
        agent.authorityRationale = payload.authorityRationale
    if payload.rateLimit is not None:
        agent.rateLimit = payload.rateLimit
    if payload.isActive is not None:
        agent.isActive = payload.isActive
    if payload.isInPilot is not None:
        agent.isInPilot = payload.isInPilot
    if payload.primaryModelId is not None:
        agent.primaryModelId = payload.primaryModelId
    if payload.escalationModelId is not None:
        agent.escalationModelId = payload.escalationModelId

    await db.commit()
    await db.refresh(agent)
    return AgentOut.model_validate(agent)


# ─── Metrics (rolling) ────────────────────────────────────────────────


@router.get("/api/agents/{code}/metrics", response_model=AgentMetricsResponse)
async def get_agent_metrics(
    code: str,
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentMetricsResponse:
    """Returns aggregated metrics over the last `days` days, plus a
    daily series for the trend chart. Open to any authenticated user."""
    agent = await _load_agent(db, code)
    window_start = datetime.now(timezone.utc) - timedelta(days=days)

    # Top-line aggregates
    agg = (
        await db.execute(
            select(
                func.count(AgentInvocation.id).label("total"),
                func.sum(
                    case((AgentInvocation.status == "ACCEPTED", 1), else_=0)
                ).label("acc"),
                func.sum(
                    case((AgentInvocation.status == "MODIFIED", 1), else_=0)
                ).label("mod"),
                func.sum(
                    case((AgentInvocation.status == "REJECTED", 1), else_=0)
                ).label("rej"),
                func.sum(
                    case((AgentInvocation.status == "ERRORED", 1), else_=0)
                ).label("err"),
                func.sum(
                    case((AgentInvocation.hallucinationFlagged.is_(True), 1), else_=0)
                ).label("hallu"),
                func.avg(AgentInvocation.ratingByHuman).label("avg_rating"),
                func.avg(AgentInvocation.latencyMs).label("avg_latency"),
                func.avg(AgentInvocation.totalCostUsd).label("avg_cost"),
                func.sum(AgentInvocation.totalCostUsd).label("total_cost"),
            )
            .where(AgentInvocation.agentId == agent.id)
            .where(AgentInvocation.invokedAt >= window_start)
        )
    ).one()

    total = int(agg.total or 0)
    acc = int(agg.acc or 0)
    mod = int(agg.mod or 0)
    rej = int(agg.rej or 0)
    err = int(agg.err or 0)

    # Daily series. Use date_trunc('day') so the buckets snap cleanly.
    series_stmt = (
        select(
            func.date_trunc("day", AgentInvocation.invokedAt).label("day"),
            func.count(AgentInvocation.id).label("total"),
            func.sum(
                case((AgentInvocation.status == "ACCEPTED", 1), else_=0)
            ).label("acc"),
            func.sum(
                case((AgentInvocation.status == "MODIFIED", 1), else_=0)
            ).label("mod"),
            func.sum(
                case((AgentInvocation.status == "REJECTED", 1), else_=0)
            ).label("rej"),
            func.sum(
                case((AgentInvocation.status == "ERRORED", 1), else_=0)
            ).label("err"),
            func.coalesce(func.sum(AgentInvocation.totalCostUsd), 0.0).label(
                "cost"
            ),
        )
        .where(AgentInvocation.agentId == agent.id)
        .where(AgentInvocation.invokedAt >= window_start)
        .group_by("day")
        .order_by("day")
    )
    series_rows = (await db.execute(series_stmt)).all()
    daily = [
        AgentMetricsPoint(
            date=row.day.date().isoformat(),
            invocations=int(row.total or 0),
            accepted=int(row.acc or 0),
            modified=int(row.mod or 0),
            rejected=int(row.rej or 0),
            errored=int(row.err or 0),
            totalCostUsd=float(row.cost or 0.0),
        )
        for row in series_rows
    ]

    return AgentMetricsResponse(
        agentCode=agent.code,
        windowDays=days,
        totalInvocations=total,
        decidedInvocations=acc + mod + rej,
        accepted=acc,
        modified=mod,
        rejected=rej,
        errored=err,
        hallucinationFlagged=int(agg.hallu or 0),
        averageRating=(float(agg.avg_rating) if agg.avg_rating is not None else None),
        averageLatencyMs=(int(agg.avg_latency) if agg.avg_latency is not None else None),
        averageCostUsd=(float(agg.avg_cost) if agg.avg_cost is not None else None),
        totalCostUsd=float(agg.total_cost or 0.0),
        daily=daily,
    )


# ─── Invocation drill-down ────────────────────────────────────────────


@router.get(
    "/api/agents/{code}/invocations", response_model=AgentInvocationListResponse
)
async def list_agent_invocations(
    code: str,
    status_filter: str | None = Query(None, description="Filter by status code"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentInvocationListResponse:
    """Drill-down: list invocations for one agent. Hits the dashboard
    table when the user clicks a metric.

    Restricted to AGENT.AUDIT_VIEW callers because the row list is a
    cross-record view (the per-invocation /api/agent-invocations/{id}
    endpoint in agents.py covers the invoker's own rows)."""
    await _require(db, user.id, "AGENT.AUDIT_VIEW")
    agent = await _load_agent(db, code)

    stmt = select(AgentInvocation).where(AgentInvocation.agentId == agent.id)
    count_stmt = (
        select(func.count(AgentInvocation.id))
        .select_from(AgentInvocation)
        .where(AgentInvocation.agentId == agent.id)
    )
    if status_filter:
        stmt = stmt.where(AgentInvocation.status == status_filter)
        count_stmt = count_stmt.where(AgentInvocation.status == status_filter)

    stmt = stmt.order_by(AgentInvocation.invokedAt.desc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    total = (await db.execute(count_stmt)).scalar_one()

    return AgentInvocationListResponse(
        items=[AgentInvocationListItem.model_validate(r) for r in rows],
        total=int(total),
    )


# ─── Prompts ──────────────────────────────────────────────────────────


@router.get("/api/agents/{code}/prompts", response_model=list[AgentPromptOut])
async def list_agent_prompts(
    code: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AgentPromptOut]:
    # Prompt metadata (version list, acceptance rates) is open. The full
    # prompt body still requires AGENT.PROMPT_EDIT (see get_agent_prompt).
    agent = await _load_agent(db, code)
    rows = (
        (
            await db.execute(
                select(AgentPrompt)
                .where(AgentPrompt.agentId == agent.id)
                .order_by(AgentPrompt.version.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        AgentPromptOut(
            id=p.id,
            agentId=p.agentId,
            version=p.version,
            promptDescription=p.promptDescription,
            variantLabel=p.variantLabel,
            invocationCount=p.invocationCount,
            acceptanceRate=p.acceptanceRate,
            modificationRate=p.modificationRate,
            rejectionRate=p.rejectionRate,
            createdById=p.createdById,
            approvedById=p.approvedById,
            approvedAt=p.approvedAt,
            createdAt=p.createdAt,
            isActive=p.id == agent.activePromptId,
        )
        for p in rows
    ]


@router.get(
    "/api/agents/{code}/prompts/{version}", response_model=AgentPromptDetailOut
)
async def get_agent_prompt(
    code: str,
    version: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentPromptDetailOut:
    """Full prompt body. AGENT.PROMPT_EDIT-gated because the body is IP."""
    await _require(db, user.id, "AGENT.PROMPT_EDIT")
    agent = await _load_agent(db, code)
    prompt = (
        await db.execute(
            select(AgentPrompt)
            .where(AgentPrompt.agentId == agent.id)
            .where(AgentPrompt.version == version)
        )
    ).scalar_one_or_none()
    if prompt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt version not found")

    return AgentPromptDetailOut(
        id=prompt.id,
        agentId=prompt.agentId,
        version=prompt.version,
        promptDescription=prompt.promptDescription,
        variantLabel=prompt.variantLabel,
        invocationCount=prompt.invocationCount,
        acceptanceRate=prompt.acceptanceRate,
        modificationRate=prompt.modificationRate,
        rejectionRate=prompt.rejectionRate,
        createdById=prompt.createdById,
        approvedById=prompt.approvedById,
        approvedAt=prompt.approvedAt,
        createdAt=prompt.createdAt,
        isActive=prompt.id == agent.activePromptId,
        systemPrompt=prompt.systemPrompt,
    )


@router.post(
    "/api/agents/{code}/prompts/{version}/promote", response_model=AgentOut
)
async def promote_agent_prompt(
    code: str,
    version: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentOut:
    """Make a specific prompt version the active one. Used both for
    promoting a new version and for rolling back to a previous one
    after a failed experiment."""
    await _require(db, user.id, "AGENT.RCA_CONFIGURE")
    agent = await _load_agent(db, code)
    prompt = (
        await db.execute(
            select(AgentPrompt)
            .where(AgentPrompt.agentId == agent.id)
            .where(AgentPrompt.version == version)
        )
    ).scalar_one_or_none()
    if prompt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt version not found")
    agent.activePromptId = prompt.id
    await db.commit()
    await db.refresh(agent)
    return AgentOut.model_validate(agent)


# ─── Calibration trigger ──────────────────────────────────────────────


@router.post(
    "/api/agents/calibration/run", response_model=CalibrationRunResponse
)
async def trigger_calibration(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CalibrationRunResponse:
    """Manual calibration trigger. Used while the cron isn't yet wired
    in production, and as a 'just rerun it now' control on the dashboard.
    The cron entry point is `python -m app.services.agents.calibration`."""
    await _require(db, user.id, "AGENT.RCA_CONFIGURE")
    started = datetime.now(timezone.utc)
    results = await run_calibration(db)
    duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return CalibrationRunResponse(
        ranAt=started,
        durationMs=duration_ms,
        results=[
            CalibrationRunResultItem(
                agentId=r.agentId,
                agentCode=r.agentCode,
                totalInvocations=r.totalInvocations,
                decidedTotal=r.decidedTotal,
                totalAcceptances=r.totalAcceptances,
                totalModifications=r.totalModifications,
                totalRejections=r.totalRejections,
                totalExpired=r.totalExpired,
                calibrationScore=r.calibrationScore,
                averageLatencyMs=r.averageLatencyMs,
                averageCostUsd=r.averageCostUsd,
                promptVersionsUpdated=r.prompt_versions_updated,
            )
            for r in results
        ],
    )


# ─── Cost summary (cross-agent, cross-plant) ──────────────────────────


@router.get("/api/agent-cost-summary", response_model=AgentCostSummaryResponse)
async def agent_cost_summary(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentCostSummaryResponse:
    """Cross-agent + cross-plant cost breakdown for the last N days.

    Surfaces:
      • total cost in window
      • per-agent breakdown (which agent is burning the budget)
      • per-plant breakdown (which plant is most active)
    """
    await _require(db, user.id, "AGENT.AUDIT_VIEW")
    window_start = datetime.now(timezone.utc) - timedelta(days=days)

    # Top line + by-agent.
    by_agent_stmt = (
        select(
            Agent.code,
            Agent.name,
            func.count(AgentInvocation.id).label("total"),
            func.coalesce(func.sum(AgentInvocation.totalCostUsd), 0.0).label(
                "cost"
            ),
        )
        .join(AgentInvocation, AgentInvocation.agentId == Agent.id)
        .where(AgentInvocation.invokedAt >= window_start)
        .group_by(Agent.code, Agent.name)
        .order_by(func.sum(AgentInvocation.totalCostUsd).desc())
    )
    by_agent_rows = (await db.execute(by_agent_stmt)).all()
    by_agent: list[CostBreakdownRow] = []
    total_invocations = 0
    total_cost = 0.0
    for row in by_agent_rows:
        invocations = int(row.total or 0)
        cost = float(row.cost or 0.0)
        total_invocations += invocations
        total_cost += cost
        by_agent.append(
            CostBreakdownRow(
                group=row.code,
                label=row.name,
                totalInvocations=invocations,
                totalCostUsd=round(cost, 4),
                averageCostUsd=(
                    round(cost / invocations, 6) if invocations > 0 else None
                ),
            )
        )

    # By-plant. We join on Plant for the readable label; rows with a
    # null sourcePlantId are bucketed under "(unscoped)".
    by_plant_stmt = (
        select(
            AgentInvocation.sourcePlantId,
            Plant.name,
            func.count(AgentInvocation.id).label("total"),
            func.coalesce(func.sum(AgentInvocation.totalCostUsd), 0.0).label(
                "cost"
            ),
        )
        .outerjoin(Plant, Plant.id == AgentInvocation.sourcePlantId)
        .where(AgentInvocation.invokedAt >= window_start)
        .group_by(AgentInvocation.sourcePlantId, Plant.name)
        .order_by(func.sum(AgentInvocation.totalCostUsd).desc())
    )
    by_plant_rows = (await db.execute(by_plant_stmt)).all()
    by_plant: list[CostBreakdownRow] = []
    for row in by_plant_rows:
        invocations = int(row.total or 0)
        cost = float(row.cost or 0.0)
        plant_id = row.sourcePlantId or "_unscoped"
        plant_label = row.name or "(unscoped)"
        by_plant.append(
            CostBreakdownRow(
                group=plant_id,
                label=plant_label,
                totalInvocations=invocations,
                totalCostUsd=round(cost, 4),
                averageCostUsd=(
                    round(cost / invocations, 6) if invocations > 0 else None
                ),
            )
        )

    return AgentCostSummaryResponse(
        windowDays=days,
        totalCostUsd=round(total_cost, 4),
        totalInvocations=total_invocations,
        byAgent=by_agent,
        byPlant=by_plant,
    )


# ─── Helpers ──────────────────────────────────────────────────────────


async def _load_agent(db: AsyncSession, code: str) -> Agent:
    agent = (
        await db.execute(select(Agent).where(Agent.code == code))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Agent {code!r} not found")
    return agent
