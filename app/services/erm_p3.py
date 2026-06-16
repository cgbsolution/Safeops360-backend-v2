"""ERM Phase 3 engines — BIA criticality/SPOF, plan coverage/health, crisis
activation/log, exercise gates, scenario readiness + stressed heat map.

Pure helpers + DB helpers. Jobs are on-demand (no scheduler), per prior phases.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.erm import EnterpriseRisk
from app.models.erm_p3 import (
    BcExercise,
    BusinessProcess,
    ContinuityPlan,
    CrisisEvent,
    ExerciseFinding,
    ProcessDependency,
    RecoveryTask,
    Scenario,
)
from app.models.user import User

CRITICAL = ("VITAL", "ESSENTIAL")
_OPEN_CAPA = ("DRAFT", "SUBMITTED", "UNDER_RCA", "ACTIONS_PLANNED", "ACTIONS_IN_PROGRESS", "PENDING_VERIFICATION")
_BAND_HEX = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


# ── BIA ──────────────────────────────────────────────────────────────────────
def criticality_from_rto(rto_hours: int) -> str:
    if rto_hours <= 4:
        return "VITAL"
    if rto_hours <= 24:
        return "ESSENTIAL"
    if rto_hours <= 168:  # 7 days
        return "IMPORTANT"
    return "DEFERRABLE"


def validate_impact_profile(profile: list[dict]) -> str | None:
    """Return an error message if a dimension's impact decreases over time."""
    for row in profile or []:
        seq = [row.get("at4h", 0), row.get("at24h", 0), row.get("at7d", 0), row.get("at30d", 0)]
        if any(seq[i] > seq[i + 1] for i in range(3)):
            return f"Impact profile for {row.get('dimension')} must be non-decreasing over time (impact at a later time cannot be lower than earlier)."
    return None


def band_for_score(score: int) -> str:
    if score <= 4:
        return "LOW"
    if score <= 9:
        return "MEDIUM"
    if score <= 15:
        return "HIGH"
    return "CRITICAL"


async def spof_count(db: AsyncSession, process_id: str) -> int:
    """Unmitigated SPOFs = SPOF deps with no workaround."""
    deps = (await db.execute(select(ProcessDependency).where(ProcessDependency.processId == process_id))).scalars().all()
    return sum(1 for d in deps if d.isSinglePointOfFailure and not (d.workaround and d.workaround.strip()))


async def covering_plans(db: AsyncSession, process_id: str, approved_only: bool = True) -> list[ContinuityPlan]:
    plans = (await db.execute(select(ContinuityPlan).where(ContinuityPlan.isDeleted.is_(False)))).scalars().all()
    out = []
    for p in plans:
        if process_id in (p.coveredProcessIds or []) and (not approved_only or p.status == "APPROVED"):
            out.append(p)
    return out


# ── Plans ────────────────────────────────────────────────────────────────────
async def plan_open_exercise_capas(db: AsyncSession, plan_id: str) -> int:
    """Open CAPAs from exercise findings on exercises that tested this plan."""
    exs = (await db.execute(select(BcExercise).where(BcExercise.isDeleted.is_(False)))).scalars().all()
    rel = [e for e in exs if plan_id in (e.testedPlanIds or [])]
    if not rel:
        return 0
    findings = (
        await db.execute(select(ExerciseFinding).where(ExerciseFinding.exerciseId.in_([e.id for e in rel])))
    ).scalars().all()
    capa_ids = [f.capaId for f in findings if f.capaId]
    if not capa_ids:
        return 0
    from app.models.capa import Capa
    return (
        await db.execute(select(func.count()).select_from(Capa).where(Capa.id.in_(capa_ids)).where(Capa.state.in_(_OPEN_CAPA)))
    ).scalar() or 0


def plan_health(plan: ContinuityPlan, open_exercise_capas: int, now: datetime | None = None) -> str:
    now = now or _now()
    if plan.status != "APPROVED":
        return "DRAFT" if plan.status in ("DRAFT", "IN_REVIEW") else plan.status
    review_ok = plan.nextReviewDate is None or _aware(plan.nextReviewDate) >= now
    exercised_ok = plan.lastExercisedAt is not None and _aware(plan.lastExercisedAt) >= now - timedelta(days=365)
    if review_ok and exercised_ok and open_exercise_capas == 0:
        return "HEALTHY"
    if not exercised_ok:
        return "STALE"
    return "AT_RISK"


def exercise_overdue(plan: ContinuityPlan, now: datetime | None = None) -> bool:
    now = now or _now()
    if plan.status != "APPROVED":
        return False
    return plan.lastExercisedAt is None or _aware(plan.lastExercisedAt) < now - timedelta(days=365)


async def recompute_plan_last_exercised(db: AsyncSession, plan_id: str) -> None:
    exs = (await db.execute(select(BcExercise).where(BcExercise.status == "COMPLETED").where(BcExercise.isDeleted.is_(False)))).scalars().all()
    dates = [_aware(e.conductedDate or e.scheduledDate) for e in exs if plan_id in (e.testedPlanIds or [])]
    plan = await db.get(ContinuityPlan, plan_id)
    if plan and dates:
        plan.lastExercisedAt = max(dates)


# ── Scenario readiness + stressed heat map ─────────────────────────────────────
async def scenario_readiness(db: AsyncSession, scenario: Scenario) -> str:
    """NO_PLAN / PLAN_EXISTS / PLAN_TESTED from coverage + exercise recency of the
    scenario's affected critical processes."""
    pids = scenario.affectedProcessIds or []
    if not pids:
        return "NO_PLAN"
    procs = (await db.execute(select(BusinessProcess).where(BusinessProcess.id.in_(pids)))).scalars().all()
    critical = [p for p in procs if p.criticality in CRITICAL]
    target = critical or procs
    if not target:
        return "NO_PLAN"
    now = _now()
    all_covered, all_tested = True, True
    for p in target:
        plans = await covering_plans(db, p.id, approved_only=True)
        if not plans:
            all_covered = False
            all_tested = False
            break
        if not any(pl.lastExercisedAt and _aware(pl.lastExercisedAt) >= now - timedelta(days=365) for pl in plans):
            all_tested = False
    if all_covered and all_tested:
        return "PLAN_TESTED"
    if all_covered:
        return "PLAN_EXISTS"
    return "NO_PLAN"


async def stressed_heatmap(db: AsyncSession, scenario: Scenario) -> dict[str, Any]:
    """Presentational stress: baseline vs whatIf-adjusted positions. Writes nothing."""
    adj = {a["riskId"]: a for a in (scenario.whatIfAdjustments or [])}
    risk_ids = list({*(scenario.affectedRiskIds or []), *adj.keys()})
    risks = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.id.in_(risk_ids or ["__none__"])))).scalars().all()

    def empty_grid():
        return {(l, i): {"likelihood": l, "impact": i, "count": 0, "band": band_for_score(l * i), "riskIds": []} for l in range(1, 6) for i in range(1, 6)}

    base, stress, movements = empty_grid(), empty_grid(), []
    for r in risks:
        bl, bi = r.residualLikelihood or 0, r.residualImpact or 0
        if bl and bi:
            c = base[(bl, bi)]; c["count"] += 1; c["riskIds"].append(r.id)
        a = adj.get(r.id)
        sl, si = (a["stressedLikelihood"], a["stressedImpact"]) if a else (bl, bi)
        if sl and si:
            c = stress[(sl, si)]; c["count"] += 1; c["riskIds"].append(r.id)
        if a and (sl, si) != (bl, bi):
            movements.append({"riskId": r.id, "riskCode": r.riskCode, "title": r.title, "fromL": bl, "fromI": bi, "toL": sl, "toI": si})
    return {
        "scenarioId": scenario.id, "scenarioTitle": scenario.title,
        "baseline": list(base.values()), "stressed": list(stress.values()), "movements": movements,
    }


# ── Crisis ───────────────────────────────────────────────────────────────────
async def snapshot_plans_for_crisis(db: AsyncSession, plan_ids: list[str]) -> list[dict[str, Any]]:
    """Cache activated plan content (sections + recovery tasks) for offline read."""
    out = []
    for pid in plan_ids:
        p = await db.get(ContinuityPlan, pid)
        if not p:
            continue
        tasks = (await db.execute(select(RecoveryTask).where(RecoveryTask.planId == pid).order_by(RecoveryTask.orderIndex))).scalars().all()
        out.append({
            "planId": p.id, "planCode": p.planCode, "title": p.title, "version": p.version,
            "sections": p.sections or [], "activationCriteria": p.activationCriteria or [],
            "strategySummary": p.strategySummary,
            "recoveryTasks": [{"id": t.id, "orderIndex": t.orderIndex, "title": t.title, "detail": t.detail, "responsibleRoleName": t.responsibleRoleName, "targetHoursFromActivation": t.targetHoursFromActivation} for t in tasks],
        })
    return out


async def user_name_map(db: AsyncSession, ids) -> dict[str, str]:
    ids = [i for i in set(ids) if i and i != "SYSTEM"]
    if not ids:
        return {}
    rows = (await db.execute(select(User.id, User.name).where(User.id.in_(ids)))).all()
    return {r[0]: r[1] for r in rows}


async def notify_crisis_activation(db: AsyncSession, crisis: CrisisEvent, escalate_corporate: bool) -> None:
    """Best-effort call-tree + escalation notification. Never raises."""
    try:
        from app.models.user import Role, UserRole
        from app.services.notifications import send_email

        emails: set[str] = set()
        if escalate_corporate:
            rows = (
                await db.execute(
                    select(User.email).join(UserRole, UserRole.userId == User.id).join(Role, Role.id == UserRole.roleId)
                    .where(Role.code.in_(("CRO", "BCM_COORDINATOR")))
                )
            ).scalars().all()
            emails.update(e for e in rows if e)
        if emails:
            await send_email(list(emails), subject=f"[CRISIS] {crisis.crisisCode} activated (sev {crisis.severityLevel}): {crisis.title}",
                             body=f"Crisis {crisis.crisisCode} activated. Severity {crisis.severityLevel}. Plans: {crisis.activatedPlanIds}.")
    except Exception:
        return
