"""Agent platform router.

Endpoints:
  POST /api/agents/{agent_code}/invoke
        Start an invocation. Returns 202 with the invocation ID; the
        actual tool-use loop runs in a BackgroundTask. Clients poll
        the GET endpoint until status leaves RUNNING.

  GET  /api/agent-invocations/{invocation_id}
        Fetch invocation state + structured result. Polled by the
        invocation card while status == RUNNING; rendered once
        status transitions to PENDING_REVIEW / ERRORED.

  GET  /api/agent-invocations/{invocation_id}/detail
        Richer view including the full input context fed to the
        agent and the raw API response. Used by the transparency
        drawer; gated on AGENT.AUDIT_VIEW.

  POST /api/agent-invocations/{invocation_id}/decision
        Record the human accept/modify/reject decision.

The background task wrapper creates its own AsyncSession from the
factory; the request-scoped session yielded by get_db() closes when
the HTTP response is sent.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import AsyncSessionLocal, get_db
from app.core.deps import get_current_user
from app.models.agent import Agent, AgentInvocation
from app.models.user import User
from app.schemas.agent import (
    AgentInvocationDetailOut,
    AgentInvocationOut,
    HumanDecisionRequest,
    InvocationStartedResponse,
    InvokeAgentRequest,
)
from app.services.agents import agent_service
from app.services.permissions import PermissionContext, can

router = APIRouter(tags=["agents"])


# ─── Invoke ──────────────────────────────────────────────────────────


@router.post(
    "/api/agents/{agent_code}/invoke",
    response_model=InvocationStartedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def invoke_agent(
    agent_code: str,
    payload: InvokeAgentRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InvocationStartedResponse:
    """Start an agent invocation. The HTTP response returns immediately
    with the invocation ID; clients poll the GET endpoint."""
    permission_code = agent_service.get_invoke_permission_code(agent_code)
    permission = await can(db, user.id, permission_code, PermissionContext())
    if not permission.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            permission.reason or f"Missing permission {permission_code}",
        )

    invocation = await agent_service.start_invocation(
        db=db,
        agent_code=agent_code,
        source_module=payload.sourceModule,
        source_record_id=payload.sourceRecordId,
        user_id=user.id,
        force_escalation_model=payload.forceEscalationModel,
    )

    # Hand off to the background task. It opens its own session so the
    # request-scoped one can close cleanly when this response returns.
    background_tasks.add_task(_run_invocation_background, invocation.id)

    return InvocationStartedResponse(
        invocationId=invocation.id,
        invocationNumber=invocation.invocationNumber,
        status=invocation.status,
        pollUrl=f"/api/agent-invocations/{invocation.id}",
    )


async def _run_invocation_background(invocation_id: str) -> None:
    """BackgroundTask entry point. Owns its session. Catches all
    exceptions so the FastAPI task runner never sees an unhandled
    error (it has no observer)."""
    async with AsyncSessionLocal() as session:
        try:
            await agent_service.run_invocation(db=session, invocation_id=invocation_id)
        except Exception:  # noqa: BLE001
            # agent_service.run_invocation is supposed to land the row in
            # ERRORED on any failure. This catch is the belt over the
            # braces — if a bug leaks an exception, we still don't crash
            # the worker.
            pass


# ─── Poll / fetch ────────────────────────────────────────────────────


@router.get(
    "/api/agent-invocations/{invocation_id}",
    response_model=AgentInvocationOut,
)
async def get_invocation(
    invocation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentInvocationOut:
    """Fetch an invocation and its tool calls. The caller is authorised
    if they have the agent's invoke permission (they triggered it) OR
    AGENT.AUDIT_VIEW (they're reviewing someone else's run)."""
    invocation = await _load_invocation_with_tool_calls(db, invocation_id)
    await _authorise_view(db, user, invocation)
    return AgentInvocationOut.model_validate(invocation)


@router.get(
    "/api/agent-invocations/{invocation_id}/detail",
    response_model=AgentInvocationDetailOut,
)
async def get_invocation_detail(
    invocation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentInvocationDetailOut:
    """Audit-drawer view. Returns the full input context + raw API
    response in addition to the standard fields. Restricted to
    AGENT.AUDIT_VIEW."""
    permission = await can(db, user.id, "AGENT.AUDIT_VIEW", PermissionContext())
    if not permission.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            permission.reason or "AGENT.AUDIT_VIEW required",
        )
    invocation = await _load_invocation_with_tool_calls(db, invocation_id)
    return AgentInvocationDetailOut.model_validate(invocation)


@router.get(
    "/api/agents/latest-invocation",
    response_model=AgentInvocationOut | None,
)
async def latest_invocation(
    sourceModule: str,
    sourceRecordId: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentInvocationOut | None:
    """The most recent invocation for a source record. Lets the UI hydrate the
    agent card on page load so a finished result shows immediately — instead of
    being lost when the client-side poll state goes away."""
    inv = (
        await db.execute(
            select(AgentInvocation)
            .where(AgentInvocation.sourceModule == sourceModule)
            .where(AgentInvocation.sourceRecordId == sourceRecordId)
            .order_by(AgentInvocation.createdAt.desc())
            .limit(1)
            .options(selectinload(AgentInvocation.toolCalls))
        )
    ).scalar_one_or_none()
    if inv is None:
        return None
    await _authorise_view(db, user, inv)
    return AgentInvocationOut.model_validate(inv)


# ─── Human decision ──────────────────────────────────────────────────


@router.post(
    "/api/agent-invocations/{invocation_id}/decision",
    response_model=AgentInvocationOut,
)
async def record_decision(
    invocation_id: str,
    payload: HumanDecisionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentInvocationOut:
    """Record the investigator's decision on a PENDING_REVIEW invocation.
    Caller must be the user who triggered the invocation OR have the
    agent's invoke permission for the relevant scope."""
    invocation = await _load_invocation_with_tool_calls(db, invocation_id)

    # Find the agent code so we can reuse the invoke permission as the
    # gate. The invoker is implicit-OK; other users need the permission.
    if invocation.invokedById != user.id:
        agent = await db.get(Agent, invocation.agentId)
        if agent is not None:
            permission_code = agent_service.get_invoke_permission_code(agent.code)
            permission = await can(db, user.id, permission_code, PermissionContext())
            if not permission.allowed:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    permission.reason
                    or f"{permission_code} required to act on this invocation",
                )

    updated = await agent_service.record_human_decision(
        db=db,
        invocation_id=invocation_id,
        decision=payload.decision,
        user_id=user.id,
        human_modifications=payload.humanModifications,
        rejection_reason=payload.rejectionReason,
        rating=payload.rating,
        feedback=payload.feedback,
    )
    # Re-load tool calls for the response (record_human_decision returns
    # the row but with a fresh expire_on_commit=False, the toolCalls
    # collection may not be populated).
    refreshed = await _load_invocation_with_tool_calls(db, updated.id)
    return AgentInvocationOut.model_validate(refreshed)


# ─── Helpers ─────────────────────────────────────────────────────────


async def _load_invocation_with_tool_calls(
    db: AsyncSession, invocation_id: str
) -> AgentInvocation:
    """Load an invocation with its tool calls eager-loaded. Required by
    the response serialiser — touching .toolCalls lazily inside an
    async session triggers MissingGreenlet."""
    stmt = (
        select(AgentInvocation)
        .where(AgentInvocation.id == invocation_id)
        .options(selectinload(AgentInvocation.toolCalls))
    )
    invocation = (await db.execute(stmt)).scalar_one_or_none()
    if invocation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invocation not found")
    return invocation


async def _authorise_view(
    db: AsyncSession, user: User, invocation: AgentInvocation
) -> None:
    """A caller can view an invocation if they triggered it OR they
    have AGENT.AUDIT_VIEW. Anyone with the agent's invoke permission
    can also view (e.g. a co-investigator looking at the same record)."""
    if invocation.invokedById == user.id:
        return

    audit_view = await can(db, user.id, "AGENT.AUDIT_VIEW", PermissionContext())
    if audit_view.allowed:
        return

    # Fall back to the agent-specific invoke permission. Pulls the agent
    # so we can resolve its permission code.
    agent = await db.get(Agent, invocation.agentId)
    if agent is not None:
        try:
            permission_code = agent_service.get_invoke_permission_code(agent.code)
        except HTTPException:
            permission_code = None
        if permission_code:
            check = await can(db, user.id, permission_code, PermissionContext())
            if check.allowed:
                return

    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "You do not have permission to view this invocation",
    )
