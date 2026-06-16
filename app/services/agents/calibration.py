"""Calibration job for the user-initiated agent platform.

Reads AgentInvocation rows and recomputes the rolling metrics on each
Agent + AgentPrompt. Designed to be:

  • Idempotent — running it twice in a minute gives the same answers
  • Cheap — three aggregate queries per agent, no per-row Python work
  • Restartable — no partial-state writes; we commit at the end
  • Triggerable — runnable from a cron, an admin endpoint, or a one-off
    `python -m app.services.agents.calibration`

Metrics computed per Agent:
  • totalInvocations, totalAcceptances, totalModifications, totalRejections
  • averageLatencyMs (across PENDING_REVIEW + decision states; excludes
    RUNNING and ERRORED — those skew the average)
  • averageCostUsd (same exclusion)
  • calibrationScore: a single 0..1 number derived from the decision
    mix. Today's formula:
        score = (acceptances * 1.0 + modifications * 0.5) / decided_total
    Rationale: "accept as-is" is a strong positive; "modified" is a
    half-positive (the agent gave a useful starting point but missed
    something); "reject" is zero. Pure formula — no magic constants.
    The threshold for L0 → L1 promotion is policy: ≥0.65 sustained over
    50+ decided invocations.

Metrics computed per AgentPrompt:
  • invocationCount + acceptanceRate / modificationRate / rejectionRate
  • Used by the dashboard to surface "this prompt version has 40%+
    rejection — review" without anyone setting a flag manually.

Window: the brief says "rolling metrics", but doesn't specify the
window length. We use ALL-TIME counts for the Agent.total* columns
(matches the existing schema's semantics — the field names imply
totals) and ALL-TIME rates for AgentPrompt (the prompt's whole life is
relevant). The frontend dashboard can compute *windowed* metrics from
the AgentInvocation table directly when it wants a "last 30 days" view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentInvocation, AgentPrompt


# Statuses that count toward "decided" (i.e. the human ruled on the
# suggestion). EXPIRED is treated as a soft reject: the human ignored
# it long enough that the cron timed it out. ERRORED never reaches a
# human, so it's outside the decided pool.
_DECIDED_STATUSES = ("ACCEPTED", "MODIFIED", "REJECTED", "EXPIRED")


@dataclass
class AgentCalibrationResult:
    """One row per agent. Returned by run_calibration() so callers
    (admin endpoint, CLI) can render a summary without re-querying."""

    agentId: str  # noqa: N815 (matches DB column naming)
    agentCode: str  # noqa: N815
    totalInvocations: int  # noqa: N815
    totalAcceptances: int  # noqa: N815
    totalModifications: int  # noqa: N815
    totalRejections: int  # noqa: N815
    totalExpired: int  # noqa: N815
    decidedTotal: int  # noqa: N815
    calibrationScore: float | None  # noqa: N815
    averageLatencyMs: int | None  # noqa: N815
    averageCostUsd: float | None  # noqa: N815
    prompt_versions_updated: int


async def run_calibration(db: AsyncSession) -> list[AgentCalibrationResult]:
    """Recompute calibration metrics for every Agent and AgentPrompt.

    Caller is responsible for the session lifecycle. The cron / admin
    endpoint typically wraps this in a fresh AsyncSession and commits
    on success.
    """
    now = datetime.now(timezone.utc)
    agents = (await db.execute(select(Agent))).scalars().all()

    results: list[AgentCalibrationResult] = []

    for agent in agents:
        # Aggregate invocation counts by status, plus average latency
        # and cost across non-RUNNING / non-ERRORED rows. One query
        # per agent — Postgres counts via case-when expressions in a
        # single scan.
        decided_filter = AgentInvocation.status.in_(_DECIDED_STATUSES)
        agg_stmt = (
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
                    case((AgentInvocation.status == "EXPIRED", 1), else_=0)
                ).label("exp"),
                func.avg(
                    case(
                        (decided_filter, AgentInvocation.latencyMs),
                        else_=None,
                    )
                ).label("avg_latency"),
                func.avg(
                    case(
                        (decided_filter, AgentInvocation.totalCostUsd),
                        else_=None,
                    )
                ).label("avg_cost"),
            )
            .where(AgentInvocation.agentId == agent.id)
        )
        row = (await db.execute(agg_stmt)).one()

        total = int(row.total or 0)
        acc = int(row.acc or 0)
        mod = int(row.mod or 0)
        rej = int(row.rej or 0)
        exp = int(row.exp or 0)
        decided = acc + mod + rej + exp

        score = _compute_calibration_score(acc=acc, mod=mod, rej=rej, exp=exp)

        agent.totalInvocations = total
        agent.totalAcceptances = acc
        agent.totalModifications = mod
        agent.totalRejections = rej
        agent.averageLatencyMs = (
            int(row.avg_latency) if row.avg_latency is not None else None
        )
        agent.averageCostUsd = (
            float(row.avg_cost) if row.avg_cost is not None else None
        )
        agent.calibrationScore = score
        agent.lastCalibrationAt = now

        # Per-prompt-version rates. One aggregate per (agentId, promptVersionId).
        prompt_stmt = (
            select(
                AgentInvocation.promptVersionId,
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
                    case((AgentInvocation.status == "EXPIRED", 1), else_=0)
                ).label("exp"),
            )
            .where(AgentInvocation.agentId == agent.id)
            .group_by(AgentInvocation.promptVersionId)
        )
        prompt_rows = (await db.execute(prompt_stmt)).all()
        versions_updated = 0
        for pr in prompt_rows:
            prompt = await db.get(AgentPrompt, pr.promptVersionId)
            if prompt is None:
                continue
            p_total = int(pr.total or 0)
            p_acc = int(pr.acc or 0)
            p_mod = int(pr.mod or 0)
            p_rej = int(pr.rej or 0)
            p_exp = int(pr.exp or 0)
            p_decided = p_acc + p_mod + p_rej + p_exp
            prompt.invocationCount = p_total
            if p_decided > 0:
                prompt.acceptanceRate = round(p_acc / p_decided, 4)
                prompt.modificationRate = round(p_mod / p_decided, 4)
                prompt.rejectionRate = round((p_rej + p_exp) / p_decided, 4)
            else:
                prompt.acceptanceRate = None
                prompt.modificationRate = None
                prompt.rejectionRate = None
            versions_updated += 1

        results.append(
            AgentCalibrationResult(
                agentId=agent.id,
                agentCode=agent.code,
                totalInvocations=total,
                totalAcceptances=acc,
                totalModifications=mod,
                totalRejections=rej,
                totalExpired=exp,
                decidedTotal=decided,
                calibrationScore=score,
                averageLatencyMs=(
                    int(row.avg_latency) if row.avg_latency is not None else None
                ),
                averageCostUsd=(
                    float(row.avg_cost) if row.avg_cost is not None else None
                ),
                prompt_versions_updated=versions_updated,
            )
        )

    await db.commit()
    return results


def _compute_calibration_score(
    *, acc: int, mod: int, rej: int, exp: int
) -> float | None:
    """Single-formula calibration score, 0..1.

    Formula: (1.0 * accepted + 0.5 * modified) / (decided_total)
    where decided_total = accepted + modified + rejected + expired.

    Returns None when no decisions exist — the dashboard should show
    "not yet calibrated" rather than 0.0 (which would imply "very bad").
    """
    decided = acc + mod + rej + exp
    if decided == 0:
        return None
    return round((acc * 1.0 + mod * 0.5) / decided, 4)


# ─── CLI entry point ──────────────────────────────────────────────────


async def _run_via_cli() -> None:
    """`python -m app.services.agents.calibration` — runs the job once
    against a fresh session and prints a summary. Suitable for cron.

    Exit code 0 on success, 1 on failure (cron catches stderr).
    """
    import sys

    from app.core.db import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as session:
            results = await run_calibration(session)
    except Exception as e:  # noqa: BLE001
        print(f"[calibration] FAILED: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[calibration] OK — updated {len(results)} agent(s)")
    for r in results:
        score = f"{r.calibrationScore:.3f}" if r.calibrationScore is not None else "—"
        print(
            f"  {r.agentCode}: total={r.totalInvocations} "
            f"decided={r.decidedTotal} score={score} "
            f"versions={r.prompt_versions_updated}"
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(_run_via_cli())
