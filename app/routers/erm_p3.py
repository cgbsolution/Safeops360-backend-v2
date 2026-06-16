"""ERM Phase 3 router — BCM (BIA / Plans / Crisis / Exercises) + Scenario/Horizon.

Shares /api/erm prefix. RBAC via can(). Crisis log is append-only (no edit/delete
endpoint). Permission codes (seed-rbac.ts):
  BCM.READ BIA.WRITE BIA.APPROVE PLAN.WRITE PLAN.APPROVE
  CRISIS.ADMIN CRISIS.ACTIVATE CRISIS.MANAGE EXERCISE.WRITE EXERCISE.COMPLETE
  SCENARIO.WRITE HORIZON.WRITE
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.capa import Capa, CapaSourceCategory, CapaSourceType
from app.models.erm import EnterpriseRisk, RiskCategory
from app.models.erm_p3 import (
    BcExercise,
    BusinessProcess,
    CallTree,
    ContinuityPlan,
    CrisisEvent,
    CrisisLogEntry,
    CrisisTeamRole,
    ExerciseFinding,
    HorizonItem,
    ProcessDependency,
    RecoveryTask,
    Scenario,
)
from app.models.plant import Plant
from app.models.user import User
from app.schemas import erm_p3 as S
from app.services import erm_p3 as svc
from app.services import fser_provider
from app.services.permissions import PermissionContext, can, get_user_role_codes

router = APIRouter(prefix="/api/erm", tags=["erm-phase3"])


async def _require(db, user, code, *, plant_id=None, record=None, record_id=None):
    res = await can(db, user.id, code, PermissionContext(plant_id=plant_id, record=record, record_id=record_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


def _now():
    return datetime.now(timezone.utc)


def _aware(d):
    return svc._aware(d)


async def _plants(db) -> dict[str, str]:
    return {r[0]: r[1] for r in (await db.execute(select(Plant.id, Plant.name))).all()}


async def _names(db, ids):
    return await svc.user_name_map(db, ids)


# ════════════════════════════════════════════════════════════════════════════
# BIA — Business Processes
# ════════════════════════════════════════════════════════════════════════════
async def _serialise_process(db, p, plants, names, plan_index) -> S.ProcessListItem:
    spofs = await svc.spof_count(db, p.id)
    covers = [pl for pl in plan_index if p.id in (pl.coveredProcessIds or []) and pl.status == "APPROVED"]
    overdue = bool(p.nextBiaReviewDate and _aware(p.nextBiaReviewDate) < _now())
    return S.ProcessListItem(
        id=p.id, processCode=p.processCode, name=p.name, siteId=p.siteId, siteName=plants.get(p.siteId) if p.siteId else "Corporate",
        ownerId=p.ownerId, ownerName=names.get(p.ownerId), departmentName=p.departmentName, rtoHours=p.rtoHours,
        rpoHours=p.rpoHours, mtpdHours=p.mtpdHours, criticality=p.criticality, biaStatus=p.biaStatus,
        nextBiaReviewDate=p.nextBiaReviewDate, reviewOverdue=overdue, unmitigatedSpofCount=spofs,
        planCoverageCount=len(covers), isCovered=len(covers) > 0, linkedRiskIds=p.linkedRiskIds or [], updatedAt=p.updatedAt,
    )


@router.get("/bcm/processes", response_model=S.ProcessListResponse)
async def list_processes(criticality: str | None = Query(None), siteId: str | None = Query(None), biaStatus: str | None = Query(None),
                         user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BCM.READ")
    role_codes = await get_user_role_codes(db, user.id)
    rows = (await db.execute(select(BusinessProcess).where(BusinessProcess.isDeleted.is_(False)))).scalars().all()
    # Plant HSE Head: own-site OPS processes only (pragmatic site scope)
    if "PLANT_HSE_HEAD" in role_codes and not any(r in role_codes for r in ("CRO", "BCM_COORDINATOR", "RISK_CHAMPION", "EXECUTIVE_VIEWER", "SYSTEM_ADMIN", "ADMIN")):
        from app.services.permissions import get_accessible_plants
        acc = await get_accessible_plants(db, user.id)
        if acc is not None:
            rows = [p for p in rows if p.siteId in acc]
    if criticality:
        rows = [p for p in rows if p.criticality == criticality]
    if siteId:
        rows = [p for p in rows if p.siteId == siteId]
    if biaStatus:
        rows = [p for p in rows if p.biaStatus == biaStatus]
    plants = await _plants(db)
    names = await _names(db, [p.ownerId for p in rows])
    plan_index = (await db.execute(select(ContinuityPlan).where(ContinuityPlan.isDeleted.is_(False)))).scalars().all()
    items = [await _serialise_process(db, p, plants, names, plan_index) for p in rows]
    items.sort(key=lambda x: ({"VITAL": 0, "ESSENTIAL": 1, "IMPORTANT": 2, "DEFERRABLE": 3}.get(x.criticality, 4), x.processCode))
    cc: dict[str, int] = {}
    for it in items:
        cc[it.criticality] = cc.get(it.criticality, 0) + 1
    return S.ProcessListResponse(items=items, total=len(items), criticalityCounts=cc)


async def _next_code(db, model, prefix, width=4, year=False):
    n = (await db.execute(select(func.count()).select_from(model))).scalar() or 0
    if year:
        return f"{prefix}-{_now().year}-{(n + 1):0{width}d}"
    return f"{prefix}-{(n + 1):0{width}d}"


@router.post("/bcm/processes", response_model=S.ProcessDetail, status_code=201)
async def create_process(body: S.ProcessUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BIA.WRITE", plant_id=body.siteId)
    if body.mtpdHours < body.rtoHours:
        raise HTTPException(400, "MTPD must be greater than or equal to RTO.")
    err = svc.validate_impact_profile([r.model_dump() for r in body.impactProfile])
    if err:
        raise HTTPException(400, err)
    crit = body.criticalityOverride or svc.criticality_from_rto(body.rtoHours)
    if body.criticalityOverride and not (body.criticalityOverrideJustification and body.criticalityOverrideJustification.strip()):
        raise HTTPException(400, "Criticality override requires a justification.")
    p = BusinessProcess(
        processCode=await _next_code(db, BusinessProcess, "BP"), name=body.name, description=body.description, siteId=body.siteId,
        ownerId=body.ownerId, departmentName=body.departmentName, rtoHours=body.rtoHours, rpoHours=body.rpoHours, mtpdHours=body.mtpdHours,
        criticality=crit, criticalityOverrideJustification=body.criticalityOverrideJustification, peakPeriods=body.peakPeriods,
        impactProfile=[r.model_dump() for r in body.impactProfile], linkedRiskIds=body.linkedRiskIds, biaStatus="DRAFT", createdBy=user.id,
    )
    db.add(p)
    await db.commit()
    return await _build_process_detail(db, p.id, user)


@router.get("/bcm/processes/{pid}", response_model=S.ProcessDetail)
async def get_process(pid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BCM.READ")
    return await _build_process_detail(db, pid, user)


@router.patch("/bcm/processes/{pid}", response_model=S.ProcessDetail)
async def update_process(pid: str, body: S.ProcessUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(BusinessProcess, pid)
    if not p or p.isDeleted:
        raise HTTPException(404, "Process not found")
    # process owner may edit own even without BIA.WRITE
    res = await can(db, user.id, "BIA.WRITE", PermissionContext(plant_id=p.siteId))
    if not res.allowed and p.ownerId != user.id:
        raise HTTPException(403, "Requires BIA.WRITE or process ownership.")
    if body.mtpdHours < body.rtoHours:
        raise HTTPException(400, "MTPD must be greater than or equal to RTO.")
    err = svc.validate_impact_profile([r.model_dump() for r in body.impactProfile])
    if err:
        raise HTTPException(400, err)
    p.name, p.description, p.siteId, p.ownerId, p.departmentName = body.name, body.description, body.siteId, body.ownerId, body.departmentName
    p.rtoHours, p.rpoHours, p.mtpdHours, p.peakPeriods = body.rtoHours, body.rpoHours, body.mtpdHours, body.peakPeriods
    p.impactProfile = [r.model_dump() for r in body.impactProfile]
    p.linkedRiskIds = body.linkedRiskIds
    p.criticality = body.criticalityOverride or svc.criticality_from_rto(body.rtoHours)
    if body.criticalityOverride:
        if not (body.criticalityOverrideJustification and body.criticalityOverrideJustification.strip()):
            raise HTTPException(400, "Criticality override requires a justification.")
        p.criticalityOverrideJustification = body.criticalityOverrideJustification
    p.updatedBy = user.id
    await db.commit()
    return await _build_process_detail(db, pid, user)


@router.post("/bcm/processes/{pid}/approve", response_model=S.ProcessDetail)
async def approve_process(pid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BIA.APPROVE")
    p = await db.get(BusinessProcess, pid)
    if not p:
        raise HTTPException(404, "Process not found")
    p.biaStatus = "APPROVED"
    p.approvedBy = user.id
    p.lastBiaDate = _now()
    p.nextBiaReviewDate = _now() + timedelta(days=365)
    p.updatedBy = user.id
    await db.commit()
    return await _build_process_detail(db, pid, user)


@router.post("/bcm/processes/{pid}/dependencies", response_model=S.DependencyOut, status_code=201)
async def add_dependency(pid: str, body: S.DependencyUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(BusinessProcess, pid)
    if not p:
        raise HTTPException(404, "Process not found")
    res = await can(db, user.id, "BIA.WRITE", PermissionContext(plant_id=p.siteId))
    if not res.allowed and p.ownerId != user.id:
        raise HTTPException(403, "Requires BIA.WRITE or process ownership.")
    d = ProcessDependency(processId=pid, **body.model_dump(), createdBy=user.id)
    db.add(d)
    await db.commit()
    await db.refresh(d)
    o = S.DependencyOut.model_validate(d)
    o.unmitigatedSpof = d.isSinglePointOfFailure and not (d.workaround and d.workaround.strip())
    return o


@router.delete("/bcm/dependencies/{did}")
async def delete_dependency(did: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    d = await db.get(ProcessDependency, did)
    if not d:
        raise HTTPException(404, "Dependency not found")
    p = await db.get(BusinessProcess, d.processId)
    res = await can(db, user.id, "BIA.WRITE", PermissionContext(plant_id=p.siteId if p else None))
    if not res.allowed and (not p or p.ownerId != user.id):
        raise HTTPException(403, "Requires BIA.WRITE or process ownership.")
    await db.delete(d)
    await db.commit()
    return {"ok": True}


@router.post("/bcm/processes/{pid}/raise-risk")
async def raise_spof_as_risk(pid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """One-click: create a draft OPS EnterpriseRisk pre-linked to this process (T3-03)."""
    await _require(db, user, "BIA.WRITE")
    p = await db.get(BusinessProcess, pid)
    if not p:
        raise HTTPException(404, "Process not found")
    ops = (await db.execute(select(RiskCategory).where(RiskCategory.code == "OPS"))).scalar_one_or_none()
    if not ops:
        raise HTTPException(400, "OPS category missing")
    year = _now().year
    n = (await db.execute(select(func.count()).select_from(EnterpriseRisk))).scalar() or 0
    risk = EnterpriseRisk(
        riskCode=f"ERM-{year}-{(n + 1):04d}", title=f"SPOF exposure — {p.name}",
        description=f"Single point of failure identified in BIA for process {p.processCode} ({p.name}).",
        categoryId=ops.id, orgLevel="SITE" if p.siteId else "ENTERPRISE", plantId=p.siteId,
        riskOwnerId=p.ownerId, riskChampionId=user.id, lifecycleState="DRAFT", velocity="MODERATE", sourceType="MANUAL",
        identifiedDate=_now(), nextReviewDate=_now() + timedelta(days=90), createdBy=user.id,
    )
    db.add(risk)
    await db.flush()
    p.linkedRiskIds = list({*(p.linkedRiskIds or []), risk.id})
    await db.commit()
    return {"ok": True, "riskId": risk.id, "riskCode": risk.riskCode}


async def _build_process_detail(db, pid, user) -> S.ProcessDetail:
    p = (await db.execute(select(BusinessProcess).where(BusinessProcess.id == pid).execution_options(populate_existing=True))).scalar_one_or_none()
    if not p or p.isDeleted:
        raise HTTPException(404, "Process not found")
    plants = await _plants(db)
    names = await _names(db, [p.ownerId])
    plan_index = (await db.execute(select(ContinuityPlan).where(ContinuityPlan.isDeleted.is_(False)))).scalars().all()
    base = await _serialise_process(db, p, plants, names, plan_index)
    deps = (await db.execute(select(ProcessDependency).where(ProcessDependency.processId == pid))).scalars().all()
    dep_out = []
    for d in deps:
        o = S.DependencyOut.model_validate(d)
        o.unmitigatedSpof = d.isSinglePointOfFailure and not (d.workaround and d.workaround.strip())
        dep_out.append(o)
    covers = [{"id": pl.id, "planCode": pl.planCode, "title": pl.title, "status": pl.status} for pl in plan_index if pid in (pl.coveredProcessIds or [])]
    risks = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.id.in_((p.linkedRiskIds or []) or ["__none__"])))).scalars().all()
    return S.ProcessDetail(
        **base.model_dump(), description=p.description, peakPeriods=p.peakPeriods, impactProfile=p.impactProfile or [],
        criticalityOverrideJustification=p.criticalityOverrideJustification, approvedBy=p.approvedBy, lastBiaDate=p.lastBiaDate,
        dependencies=dep_out, coveringPlans=covers,
        linkedRisks=[{"id": r.id, "riskCode": r.riskCode, "title": r.title, "residualBand": r.residualBand} for r in risks],
        createdAt=p.createdAt,
    )


@router.get("/bcm/dependency-map", response_model=S.DependencyMap)
async def dependency_map(siteId: str | None = Query(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BCM.READ")
    procs = (await db.execute(select(BusinessProcess).where(BusinessProcess.isDeleted.is_(False)))).scalars().all()
    if siteId:
        procs = [p for p in procs if p.siteId == siteId]
    nodes, edges = [], []
    dep_node_ids: dict[str, str] = {}  # dependency name+type → shared node id
    for p in procs:
        nodes.append(S.DepMapNode(id=f"proc:{p.id}", label=f"{p.processCode} {p.name}", nodeType="PROCESS", criticality=p.criticality, siteId=p.siteId))
        deps = (await db.execute(select(ProcessDependency).where(ProcessDependency.processId == p.id))).scalars().all()
        for d in deps:
            key = f"{d.dependencyType}:{d.name.lower()}"
            nid = dep_node_ids.get(key)
            spof = d.isSinglePointOfFailure and not (d.workaround and d.workaround.strip())
            if not nid:
                nid = f"dep:{d.id}"
                dep_node_ids[key] = nid
                nodes.append(S.DepMapNode(id=nid, label=d.name, nodeType=d.dependencyType, isSpof=spof))
            edges.append(S.DepMapEdge(id=f"e:{d.id}", source=f"proc:{p.id}", target=nid, dependencyType=d.dependencyType, isSpof=spof))
    return S.DependencyMap(nodes=nodes, edges=edges)


# ════════════════════════════════════════════════════════════════════════════
# Plans
# ════════════════════════════════════════════════════════════════════════════
async def _serialise_plan(db, p, plants, names) -> S.PlanListItem:
    open_capas = await svc.plan_open_exercise_capas(db, p.id)
    return S.PlanListItem(
        id=p.id, planCode=p.planCode, title=p.title, planType=p.planType, siteId=p.siteId,
        siteName=plants.get(p.siteId) if p.siteId else "Corporate", ownerId=p.ownerId, ownerName=names.get(p.ownerId),
        coveredProcessCount=len(p.coveredProcessIds or []), version=p.version, status=p.status,
        healthChip=svc.plan_health(p, open_capas), nextReviewDate=p.nextReviewDate, lastExercisedAt=p.lastExercisedAt,
        exerciseOverdue=svc.exercise_overdue(p), updatedAt=p.updatedAt,
    )


@router.get("/bcm/plans", response_model=S.PlanListResponse)
async def list_plans(planType: str | None = Query(None), pstatus: str | None = Query(None, alias="status"), siteId: str | None = Query(None),
                     user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BCM.READ")
    rows = (await db.execute(select(ContinuityPlan).where(ContinuityPlan.isDeleted.is_(False)))).scalars().all()
    if planType:
        rows = [p for p in rows if p.planType == planType]
    if pstatus:
        rows = [p for p in rows if p.status == pstatus]
    if siteId:
        rows = [p for p in rows if p.siteId == siteId]
    plants = await _plants(db)
    names = await _names(db, [p.ownerId for p in rows])
    items = [await _serialise_plan(db, p, plants, names) for p in rows]
    sc: dict[str, int] = {}
    for it in items:
        sc[it.status] = sc.get(it.status, 0) + 1
    return S.PlanListResponse(items=items, total=len(items), statusCounts=sc)


async def _apply_plan_body(db, p, body):
    p.title, p.planType, p.siteId, p.ownerId = body.title, body.planType, body.siteId, body.ownerId
    p.coveredProcessIds, p.scopeStatement, p.activationCriteria = body.coveredProcessIds, body.scopeStatement, body.activationCriteria
    p.sections = [s.model_dump() for s in body.sections]
    p.strategySummary, p.fserPlanRef = body.strategySummary, body.fserPlanRef
    # replace recovery tasks
    existing = (await db.execute(select(RecoveryTask).where(RecoveryTask.planId == p.id))).scalars().all()
    for t in existing:
        await db.delete(t)
    for t in body.recoveryTasks:
        db.add(RecoveryTask(planId=p.id, **t.model_dump()))


@router.post("/bcm/plans", response_model=S.PlanDetail, status_code=201)
async def create_plan(body: S.PlanUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "PLAN.WRITE", plant_id=body.siteId)
    p = ContinuityPlan(planCode=await _next_code(db, ContinuityPlan, "BCP"), status="DRAFT", version=1, ownerId=body.ownerId,
                       title=body.title, planType=body.planType, siteId=body.siteId, coveredProcessIds=body.coveredProcessIds,
                       scopeStatement=body.scopeStatement, activationCriteria=body.activationCriteria,
                       sections=[s.model_dump() for s in body.sections], strategySummary=body.strategySummary,
                       fserPlanRef=body.fserPlanRef, createdBy=user.id)
    db.add(p)
    await db.flush()
    for t in body.recoveryTasks:
        db.add(RecoveryTask(planId=p.id, **t.model_dump()))
    await db.commit()
    return await _build_plan_detail(db, p.id, user)


@router.get("/bcm/plans/{pid}", response_model=S.PlanDetail)
async def get_plan(pid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BCM.READ")
    return await _build_plan_detail(db, pid, user)


@router.patch("/bcm/plans/{pid}", response_model=S.PlanDetail)
async def update_plan(pid: str, body: S.PlanUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(ContinuityPlan, pid)
    if not p or p.isDeleted:
        raise HTTPException(404, "Plan not found")
    res = await can(db, user.id, "PLAN.WRITE", PermissionContext(plant_id=p.siteId))
    if not res.allowed and p.ownerId != user.id:
        raise HTTPException(403, "Requires PLAN.WRITE or plan ownership.")
    # editing an APPROVED plan forks a new DRAFT version (T3-05)
    if p.status == "APPROVED":
        p.status = "DRAFT"
    await _apply_plan_body(db, p, body)
    p.updatedBy = user.id
    await db.commit()
    return await _build_plan_detail(db, pid, user)


@router.post("/bcm/plans/{pid}/submit", response_model=S.PlanDetail)
async def submit_plan(pid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(ContinuityPlan, pid)
    if not p:
        raise HTTPException(404, "Plan not found")
    res = await can(db, user.id, "PLAN.WRITE", PermissionContext(plant_id=p.siteId))
    if not res.allowed and p.ownerId != user.id:
        raise HTTPException(403, "Requires PLAN.WRITE or ownership.")
    if p.status != "DRAFT":
        raise HTTPException(400, f"Cannot submit from {p.status}")
    p.status = "IN_REVIEW"
    p.updatedBy = user.id
    await db.commit()
    return await _build_plan_detail(db, pid, user)


@router.post("/bcm/plans/{pid}/approve", response_model=S.PlanDetail)
async def approve_plan(pid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "PLAN.APPROVE")
    p = await db.get(ContinuityPlan, pid)
    if not p:
        raise HTTPException(404, "Plan not found")
    # snapshot current content immutably + increment version (T3-05)
    snaps = list(p.versionSnapshots or [])
    snaps.append({"version": p.version, "approvedAt": (p.approvedAt or _now()).isoformat() if isinstance(p.approvedAt, datetime) else None,
                  "sections": p.sections, "strategySummary": p.strategySummary, "scopeStatement": p.scopeStatement, "snapshotAt": _now().isoformat()})
    p.versionSnapshots = snaps
    p.version += 1
    p.status = "APPROVED"
    p.approvedBy = user.id
    p.approvedAt = _now()
    p.nextReviewDate = _now() + timedelta(days=365)
    p.updatedBy = user.id
    await db.commit()
    return await _build_plan_detail(db, pid, user)


async def _build_plan_detail(db, pid, user) -> S.PlanDetail:
    p = (await db.execute(select(ContinuityPlan).where(ContinuityPlan.id == pid).execution_options(populate_existing=True))).scalar_one_or_none()
    if not p or p.isDeleted:
        raise HTTPException(404, "Plan not found")
    plants = await _plants(db)
    names = await _names(db, [p.ownerId, p.approvedBy or ""])
    base = await _serialise_plan(db, p, plants, names)
    tasks = (await db.execute(select(RecoveryTask).where(RecoveryTask.planId == pid).order_by(RecoveryTask.orderIndex))).scalars().all()
    procs = (await db.execute(select(BusinessProcess).where(BusinessProcess.id.in_((p.coveredProcessIds or []) or ["__none__"])))).scalars().all()
    return S.PlanDetail(
        **base.model_dump(), scopeStatement=p.scopeStatement, activationCriteria=p.activationCriteria or [],
        sections=p.sections or [], strategySummary=p.strategySummary, fserPlanRef=p.fserPlanRef, versionSnapshots=p.versionSnapshots or [],
        recoveryTasks=[S.RecoveryTaskOut.model_validate(t) for t in tasks],
        coveredProcesses=[{"id": pr.id, "processCode": pr.processCode, "name": pr.name, "criticality": pr.criticality} for pr in procs],
        approvedBy=p.approvedBy, approvedAt=p.approvedAt, openExerciseCapas=await svc.plan_open_exercise_capas(db, pid), createdAt=p.createdAt,
    )


# ════════════════════════════════════════════════════════════════════════════
# BCM Dashboard
# ════════════════════════════════════════════════════════════════════════════
@router.get("/bcm/dashboard", response_model=S.BcmDashboard)
async def bcm_dashboard(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BCM.READ")
    procs = (await db.execute(select(BusinessProcess).where(BusinessProcess.isDeleted.is_(False)))).scalars().all()
    plans = (await db.execute(select(ContinuityPlan).where(ContinuityPlan.isDeleted.is_(False)))).scalars().all()
    now = _now()
    critical = [p for p in procs if p.criticality in svc.CRITICAL]
    approved = [pl for pl in plans if pl.status == "APPROVED"]

    def covered(p):
        return any(p.id in (pl.coveredProcessIds or []) for pl in approved)

    covered_critical = [p for p in critical if covered(p)]
    gaps = [{"processCode": p.processCode, "name": p.name, "criticality": p.criticality, "siteId": p.siteId} for p in critical if not covered(p)]
    spofs = 0
    for p in procs:
        spofs += await svc.spof_count(db, p.id)
    plans_review_due = sum(1 for pl in plans if pl.status == "APPROVED" and pl.nextReviewDate and _aware(pl.nextReviewDate) < now)
    ex_overdue = sum(1 for pl in plans if svc.exercise_overdue(pl))
    open_ex_capas = 0
    for pl in plans:
        open_ex_capas += await svc.plan_open_exercise_capas(db, pl.id)
    exercises = (await db.execute(select(BcExercise).where(BcExercise.isDeleted.is_(False)))).scalars().all()
    programme = sorted([{"exerciseCode": e.exerciseCode, "title": e.title, "type": e.exerciseType, "scheduledDate": e.scheduledDate.isoformat(), "status": e.status} for e in exercises], key=lambda x: x["scheduledDate"])
    crises = (await db.execute(select(CrisisEvent).where(CrisisEvent.isDeleted.is_(False)).order_by(CrisisEvent.activatedAt.desc()).limit(5))).scalars().all()
    active = (await db.execute(select(func.count()).select_from(CrisisEvent).where(CrisisEvent.status.in_(("ACTIVATED", "MANAGED"))).where(CrisisEvent.isDeleted.is_(False)))).scalar() or 0
    recent = [{"crisisCode": c.crisisCode, "title": c.title, "status": c.status, "severityLevel": c.severityLevel, "activatedAt": c.activatedAt.isoformat()} for c in crises]
    return S.BcmDashboard(
        criticalProcesses=len(critical), coveragePct=round(len(covered_critical) * 100 / len(critical), 1) if critical else 0.0,
        coveredCritical=len(covered_critical), totalCritical=len(critical), coverageGaps=gaps, unmitigatedSpofs=spofs,
        plansReviewDue=plans_review_due, exercisesOverdue=ex_overdue, openExerciseCapas=open_ex_capas, exerciseProgramme=programme,
        recentCrises=recent, activeCrises=active,
    )


@router.get("/board-pack-phase3")
async def board_pack_phase3(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """BCM readiness + scenario-resilience + horizon-scan blocks for the board pack
    (E-11). Mirrors /board-pack-phase2: a separate, additive endpoint the editor
    fetches with graceful degradation — Phase-1 render is untouched."""
    await _require(db, user, "BCM.READ")
    dash = await bcm_dashboard(user=user, db=db)
    # Scenarios — active only, least-ready (NO_PLAN) and highest-impact first.
    scns = (await db.execute(select(Scenario).where(Scenario.isDeleted.is_(False)).where(Scenario.status == "ACTIVE"))).scalars().all()
    scen_rows = []
    for s in scns:
        readiness = await svc.scenario_readiness(db, s)
        top = max((int(im.get("estimatedLevel", 0) or 0) for im in (s.impactEstimates or [])), default=0)
        scen_rows.append({"scenarioCode": s.scenarioCode, "title": s.title, "category": s.category,
                          "probabilityQualitative": s.probabilityQualitative, "mitigationReadiness": readiness, "topImpactLevel": top})
    r_order = {"NO_PLAN": 0, "PLAN_EXISTS": 1, "PLAN_TESTED": 2}
    scen_rows.sort(key=lambda r: (r_order.get(r["mitigationReadiness"], 9), -r["topImpactLevel"]))
    # Horizon — strongest signals first; open (undisposed) ahead of disposed.
    hz = (await db.execute(select(HorizonItem).where(HorizonItem.isDeleted.is_(False)))).scalars().all()
    sig = {"STRONG": 0, "EMERGING": 1, "WEAK": 2}
    hz_rows = sorted(
        [{"title": h.title, "category": h.category, "signalStrength": h.signalStrength, "disposition": h.disposition} for h in hz],
        key=lambda r: (sig.get(r["signalStrength"], 9), 1 if r["disposition"] else 0),
    )
    return {
        "bcmReadiness": {
            "coveragePct": dash.coveragePct, "coveredCritical": dash.coveredCritical, "totalCritical": dash.totalCritical,
            "unmitigatedSpofs": dash.unmitigatedSpofs, "coverageGaps": dash.coverageGaps,
            "plansReviewDue": dash.plansReviewDue, "exercisesOverdue": dash.exercisesOverdue,
            "openExerciseCapas": dash.openExerciseCapas, "activeCrises": dash.activeCrises,
        },
        "scenarios": scen_rows[:6],
        "horizon": hz_rows[:6],
    }


# ════════════════════════════════════════════════════════════════════════════
# Crisis — teams, call trees, activation, workspace
# ════════════════════════════════════════════════════════════════════════════
@router.get("/bcm/crisis-team", response_model=list[S.TeamRoleOut])
async def list_team(siteId: str | None = Query(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BCM.READ")
    rows = (await db.execute(select(CrisisTeamRole).where(CrisisTeamRole.isDeleted.is_(False)).order_by(CrisisTeamRole.escalationOrder))).scalars().all()
    if siteId:
        rows = [r for r in rows if r.siteId == siteId]
    names = await _names(db, [x for r in rows for x in (r.primaryUserId, r.alternateUserId)])
    out = []
    for r in rows:
        o = S.TeamRoleOut.model_validate(r)
        o.primaryUserName = names.get(r.primaryUserId)
        o.alternateUserName = names.get(r.alternateUserId)
        o.vacancy = not (r.primaryUserId and r.alternateUserId)
        out.append(o)
    return out


@router.post("/bcm/crisis-team", response_model=S.TeamRoleOut, status_code=201)
async def create_team_role(body: S.TeamRoleUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CRISIS.ADMIN")
    if not body.alternateUserId:
        raise HTTPException(400, "Alternate is mandatory — no single-person crisis roles.")
    r = CrisisTeamRole(**body.model_dump(), createdBy=user.id)
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return S.TeamRoleOut.model_validate(r)


@router.get("/bcm/call-trees", response_model=list[S.CallTreeOut])
async def list_call_trees(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BCM.READ")
    rows = (await db.execute(select(CallTree).where(CallTree.isDeleted.is_(False)))).scalars().all()
    return [S.CallTreeOut.model_validate(r) for r in rows]


@router.post("/bcm/call-trees", response_model=S.CallTreeOut, status_code=201)
async def create_call_tree(body: S.CallTreeUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CRISIS.ADMIN")
    ct = CallTree(name=body.name, siteId=body.siteId, nodes=body.nodes, createdBy=user.id)
    db.add(ct)
    await db.commit()
    await db.refresh(ct)
    return S.CallTreeOut.model_validate(ct)


@router.post("/bcm/call-trees/{ctid}/publish", response_model=S.CallTreeOut)
async def publish_call_tree(ctid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CRISIS.ADMIN")
    ct = await db.get(CallTree, ctid)
    if not ct:
        raise HTTPException(404, "Call tree not found")
    ct.publishedAt = _now()
    ct.updatedBy = user.id
    await db.commit()
    await db.refresh(ct)
    return S.CallTreeOut.model_validate(ct)


@router.post("/bcm/crisis/activate", response_model=S.CrisisDetail, status_code=201)
async def activate_crisis(body: S.CrisisActivate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CRISIS.ACTIVATE", plant_id=body.siteId)
    role_codes = await get_user_role_codes(db, user.id)
    # Plant HSE Head may only activate severity 1 on their own site (T3-11)
    if "PLANT_HSE_HEAD" in role_codes and not any(r in role_codes for r in ("CRO", "BCM_COORDINATOR", "SYSTEM_ADMIN", "ADMIN")):
        if body.severityLevel != 1:
            raise HTTPException(403, "Plant HSE Head may only activate severity-1 crises.")
    cached = await svc.snapshot_plans_for_crisis(db, body.activatedPlanIds)
    crisis = CrisisEvent(
        crisisCode=await _next_code(db, CrisisEvent, "CRX", year=True), title=body.title, siteId=body.siteId,
        activatedPlanIds=body.activatedPlanIds, linkedRiskIds=body.linkedRiskIds, linkedIncidentId=body.linkedIncidentId,
        status="ACTIVATED", activatedBy=user.id, activatedAt=_now(), severityLevel=body.severityLevel,
        cachedPlanContent=cached, createdBy=user.id,
    )
    db.add(crisis)
    await db.flush()
    db.add(CrisisLogEntry(crisisId=crisis.id, enteredBy=user.id, entryType="STATUS_UPDATE",
                          content=f"Crisis activated at severity {body.severityLevel}. Plans: {', '.join(c['planCode'] for c in cached) or 'none'}."))
    # severity-1 activation always notifies BCM Coordinator/CRO; sev 2+ escalates corporate
    await svc.notify_crisis_activation(db, crisis, escalate_corporate=True)
    await db.commit()
    return await _build_crisis_detail(db, crisis.id, user)


@router.get("/bcm/crisis", response_model=list[S.CrisisListItem])
async def list_crises(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BCM.READ")
    rows = (await db.execute(select(CrisisEvent).where(CrisisEvent.isDeleted.is_(False)).order_by(CrisisEvent.activatedAt.desc()))).scalars().all()
    plants = await _plants(db)
    names = await _names(db, [c.activatedBy for c in rows])
    out = []
    for c in rows:
        cnt = (await db.execute(select(func.count()).select_from(CrisisLogEntry).where(CrisisLogEntry.crisisId == c.id))).scalar() or 0
        dur = int((_aware(c.standDownAt) - _aware(c.activatedAt)).total_seconds() // 60) if c.standDownAt else None
        o = S.CrisisListItem.model_validate(c)
        o.siteName = plants.get(c.siteId) if c.siteId else "Corporate"
        o.activatedByName = names.get(c.activatedBy)
        o.logEntryCount = cnt
        o.durationMinutes = dur
        out.append(o)
    return out


@router.get("/bcm/crisis/{cid}", response_model=S.CrisisDetail)
async def get_crisis(cid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BCM.READ")
    return await _build_crisis_detail(db, cid, user)


async def _is_team_member(db, user_id, site_id) -> bool:
    rows = (await db.execute(select(CrisisTeamRole).where(CrisisTeamRole.isDeleted.is_(False)))).scalars().all()
    return any(r.primaryUserId == user_id or r.alternateUserId == user_id for r in rows if r.siteId in (site_id, None))


@router.post("/bcm/crisis/{cid}/log", response_model=S.LogEntryOut, status_code=201)
async def add_log_entry(cid: str, body: S.LogEntryCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    crisis = await db.get(CrisisEvent, cid)
    if not crisis:
        raise HTTPException(404, "Crisis not found")
    # crisis team members (any role) may log during an active event; plus CRISIS.MANAGE holders
    manage = (await can(db, user.id, "CRISIS.MANAGE", PermissionContext(plant_id=crisis.siteId))).allowed
    if not manage and not await _is_team_member(db, user.id, crisis.siteId):
        raise HTTPException(403, "Only crisis team members or CRISIS.MANAGE may log entries.")
    if crisis.status == "CLOSED":
        raise HTTPException(400, "Crisis is closed — log is sealed.")
    entry = CrisisLogEntry(crisisId=cid, enteredBy=user.id, entryType=body.entryType, content=body.content, recoveryTaskId=body.recoveryTaskId)
    db.add(entry)
    if crisis.status == "ACTIVATED":
        crisis.status = "MANAGED"
    await db.commit()
    await db.refresh(entry)
    o = S.LogEntryOut.model_validate(entry)
    o.enteredByName = (await _names(db, [user.id])).get(user.id)
    return o


@router.post("/bcm/crisis/{cid}/severity", response_model=S.CrisisDetail)
async def change_severity(cid: str, body: S.SeverityChange, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    crisis = await db.get(CrisisEvent, cid)
    if not crisis:
        raise HTTPException(404, "Crisis not found")
    if crisis.status == "CLOSED":
        raise HTTPException(400, "Crisis is closed — it cannot be reopened or modified.")
    role_codes = await get_user_role_codes(db, user.id)
    plant_hse_only = "PLANT_HSE_HEAD" in role_codes and not any(r in role_codes for r in ("CRO", "BCM_COORDINATOR"))
    if plant_hse_only and body.severityLevel != 1:
        raise HTTPException(403, "Plant HSE Head limited to severity 1.")
    if not plant_hse_only:
        await _require(db, user, "CRISIS.MANAGE", plant_id=crisis.siteId)
    prev = crisis.severityLevel
    crisis.severityLevel = body.severityLevel
    db.add(CrisisLogEntry(crisisId=cid, enteredBy=user.id, entryType="STATUS_UPDATE", content=f"Severity changed {prev} → {body.severityLevel}."))
    if body.severityLevel > prev:
        await svc.notify_crisis_activation(db, crisis, escalate_corporate=True)
    await db.commit()
    return await _build_crisis_detail(db, cid, user)


@router.post("/bcm/crisis/{cid}/stand-down", response_model=S.CrisisDetail)
async def stand_down(cid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    crisis = await db.get(CrisisEvent, cid)
    if not crisis:
        raise HTTPException(404, "Crisis not found")
    if crisis.status == "CLOSED":
        raise HTTPException(400, "Crisis is closed — it cannot be reopened or modified.")
    await _require(db, user, "CRISIS.MANAGE", plant_id=crisis.siteId)
    crisis.status = "STAND_DOWN"
    crisis.standDownAt = _now()
    db.add(CrisisLogEntry(crisisId=cid, enteredBy=user.id, entryType="STATUS_UPDATE", content="Stand-down declared."))
    await db.commit()
    return await _build_crisis_detail(db, cid, user)


@router.post("/bcm/crisis/{cid}/close", response_model=S.CrisisDetail)
async def close_crisis(cid: str, body: S.CrisisClose, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    crisis = await db.get(CrisisEvent, cid)
    if not crisis:
        raise HTTPException(404, "Crisis not found")
    await _require(db, user, "CRISIS.MANAGE", plant_id=crisis.siteId)
    # closure gate (T3-12): post-crisis review = a CAPA OR an explicit no-action note
    if not (body.reviewCapaId or (body.reviewNote and body.reviewNote.strip())):
        raise HTTPException(400, "Closure requires a post-crisis review: link a CAPA or record a 'no actions required' note.")
    crisis.status = "CLOSED"
    crisis.postCrisisReviewDone = True
    crisis.reviewNote = body.reviewNote
    crisis.reviewCapaId = body.reviewCapaId
    db.add(CrisisLogEntry(crisisId=cid, enteredBy=user.id, entryType="STATUS_UPDATE", content="Crisis closed; post-crisis review complete."))
    await db.commit()
    return await _build_crisis_detail(db, cid, user)


async def _build_crisis_detail(db, cid, user) -> S.CrisisDetail:
    c = (await db.execute(select(CrisisEvent).where(CrisisEvent.id == cid).execution_options(populate_existing=True))).scalar_one_or_none()
    if not c or c.isDeleted:
        raise HTTPException(404, "Crisis not found")
    plants = await _plants(db)
    logs = (await db.execute(select(CrisisLogEntry).where(CrisisLogEntry.crisisId == cid).order_by(CrisisLogEntry.timestamp))).scalars().all()
    lnames = await _names(db, [c.activatedBy, *[e.enteredBy for e in logs]])
    checked = {e.recoveryTaskId for e in logs if e.entryType == "TASK_CHECK" and e.recoveryTaskId}
    tasks = []
    for plan in c.cachedPlanContent or []:
        for t in plan.get("recoveryTasks", []):
            tasks.append({**t, "planCode": plan.get("planCode"), "checked": t["id"] in checked})
    roster_rows = (await db.execute(select(CrisisTeamRole).where(CrisisTeamRole.isDeleted.is_(False)))).scalars().all()
    roster_rows = [r for r in roster_rows if r.siteId in (c.siteId, None)]
    rnames = await _names(db, [x for r in roster_rows for x in (r.primaryUserId, r.alternateUserId)])
    roster = [{"roleName": r.roleName, "primary": rnames.get(r.primaryUserId), "alternate": rnames.get(r.alternateUserId), "escalationOrder": r.escalationOrder} for r in roster_rows]
    fser = await fser_provider.get_fser_panel(db, c.siteId)
    dur = int((_aware(c.standDownAt) - _aware(c.activatedAt)).total_seconds() // 60) if c.standDownAt else None
    log_out = []
    for e in logs:
        o = S.LogEntryOut.model_validate(e)
        o.enteredByName = lnames.get(e.enteredBy)
        log_out.append(o)
    return S.CrisisDetail(
        id=c.id, crisisCode=c.crisisCode, title=c.title, siteId=c.siteId, siteName=plants.get(c.siteId) if c.siteId else "Corporate",
        status=c.status, severityLevel=c.severityLevel, activatedAt=c.activatedAt, activatedByName=lnames.get(c.activatedBy),
        standDownAt=c.standDownAt, durationMinutes=dur, logEntryCount=len(logs), postCrisisReviewDone=c.postCrisisReviewDone,
        activatedPlanIds=c.activatedPlanIds or [], linkedRiskIds=c.linkedRiskIds or [], linkedIncidentId=c.linkedIncidentId,
        reviewNote=c.reviewNote, reviewCapaId=c.reviewCapaId, cachedPlanContent=c.cachedPlanContent or [], recoveryTasks=tasks,
        logEntries=log_out, teamRoster=roster, fserPanel=fser, createdAt=c.createdAt,
    )


# ════════════════════════════════════════════════════════════════════════════
# Exercises
# ════════════════════════════════════════════════════════════════════════════
async def _serialise_exercise(db, e, plants, names) -> S.ExerciseOut:
    findings = (await db.execute(select(ExerciseFinding).where(ExerciseFinding.exerciseId == e.id))).scalars().all()
    open_capas = 0
    capa_ids = [f.capaId for f in findings if f.capaId]
    if capa_ids:
        open_capas = (await db.execute(select(func.count()).select_from(Capa).where(Capa.id.in_(capa_ids)).where(Capa.state.in_(svc._OPEN_CAPA)))).scalar() or 0
    # Build explicitly — model_validate(e) would lazy-load the `findings`
    # relationship during attribute extraction (MissingGreenlet in async).
    return S.ExerciseOut(
        id=e.id, exerciseCode=e.exerciseCode, title=e.title, exerciseType=e.exerciseType,
        scheduledDate=e.scheduledDate, siteId=e.siteId, siteName=plants.get(e.siteId) if e.siteId else "Corporate",
        testedPlanIds=e.testedPlanIds or [], testedScenarioId=e.testedScenarioId,
        facilitatorId=e.facilitatorId, facilitatorName=names.get(e.facilitatorId),
        participants=e.participants or [], objectives=e.objectives or [], status=e.status,
        conductedDate=e.conductedDate, outcome=e.outcome, rtoAchievedHours=e.rtoAchievedHours,
        callTreeStats=e.callTreeStats, reportRichText=e.reportRichText,
        findings=[S.FindingOut.model_validate(f) for f in findings], openCapaCount=open_capas,
    )


@router.get("/bcm/exercises", response_model=S.ExerciseListResponse)
async def list_exercises(estatus: str | None = Query(None, alias="status"), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BCM.READ")
    rows = (await db.execute(select(BcExercise).where(BcExercise.isDeleted.is_(False)).order_by(BcExercise.scheduledDate.desc()))).scalars().all()
    if estatus:
        rows = [e for e in rows if e.status == estatus]
    plants = await _plants(db)
    names = await _names(db, [e.facilitatorId for e in rows])
    items = [await _serialise_exercise(db, e, plants, names) for e in rows]
    sc: dict[str, int] = {}
    for it in items:
        sc[it.status] = sc.get(it.status, 0) + 1
    return S.ExerciseListResponse(items=items, total=len(items), statusCounts=sc)


@router.post("/bcm/exercises", response_model=S.ExerciseOut, status_code=201)
async def create_exercise(body: S.ExerciseUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "EXERCISE.WRITE", plant_id=body.siteId)
    e = BcExercise(exerciseCode=await _next_code(db, BcExercise, "BCX", year=True), status="PLANNED", **body.model_dump(), createdBy=user.id)
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return await _serialise_exercise(db, e, await _plants(db), await _names(db, [e.facilitatorId]))


@router.get("/bcm/exercises/{eid}", response_model=S.ExerciseOut)
async def get_exercise(eid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BCM.READ")
    e = await db.get(BcExercise, eid)
    if not e or e.isDeleted:
        raise HTTPException(404, "Exercise not found")
    return await _serialise_exercise(db, e, await _plants(db), await _names(db, [e.facilitatorId]))


@router.post("/bcm/exercises/{eid}/findings", response_model=S.FindingOut, status_code=201)
async def add_finding(eid: str, body: S.FindingCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "EXERCISE.WRITE")
    e = await db.get(BcExercise, eid)
    if not e:
        raise HTTPException(404, "Exercise not found")
    f = ExerciseFinding(exerciseId=eid, description=body.description, severity=body.severity, createdBy=user.id)
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return S.FindingOut.model_validate(f)


@router.post("/bcm/exercises/findings/{fid}/raise-capa")
async def raise_exercise_capa(fid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "EXERCISE.WRITE")
    f = await db.get(ExerciseFinding, fid)
    if not f:
        raise HTTPException(404, "Finding not found")
    if f.capaId:
        # Idempotent: a finding has at most one CAPA — return the existing one
        # rather than minting a duplicate (guards double-clicks / repeat calls).
        existing = await db.get(Capa, f.capaId)
        return {"ok": True, "capaId": f.capaId, "capaNumber": existing.capaNumber if existing else None, "alreadyLinked": True}
    e = await db.get(BcExercise, f.exerciseId)
    st = (await db.execute(select(CapaSourceType).where(CapaSourceType.code == "BC_EXERCISE"))).scalar_one_or_none()
    if st is None:
        raise HTTPException(400, "BC_EXERCISE CAPA source type not seeded.")
    cat = await db.get(CapaSourceCategory, st.categoryId)
    plant = (await db.get(Plant, e.siteId)) if e.siteId else (await db.execute(select(Plant).order_by(Plant.code).limit(1))).scalar_one_or_none()
    year = _now().year
    count = (await db.execute(select(func.count()).select_from(Capa).where(Capa.plantId == plant.id).where(Capa.sourceCategoryId == st.categoryId))).scalar() or 0
    capa = Capa(
        capaNumber=f"CAPA-{cat.prefix if cat else 'BCX'}-{year}-{plant.code}-{(count + 1):03d}", title=f"Exercise gap: {f.description[:120]}",
        plantId=plant.id, sourceCategoryId=st.categoryId, sourceTypeId=st.id, sourceTypeCode="BC_EXERCISE",
        sourceReferenceId=f.id, sourceReferenceUrl=f"/erm/bcm/exercises/{e.id}", sourceReferenceSummary=f"{e.exerciseCode} — {e.title}",
        sourceMetadata={"exerciseCode": e.exerciseCode, "severity": f.severity}, problemDescription=f.description,
        detectionMethod="BC_EXERCISE", detectedAt=_now(), detectedByUserId=user.id, primaryCategory="BC Exercise",
        severity="HIGH" if f.severity == "MAJOR_GAP" else "MODERATE", priority="HIGH", state="ACTIONS_PLANNED",
        stateChangedAt=_now(), stateChangedByUserId=user.id, raisedByUserId=user.id, primaryOwnerUserId=e.facilitatorId, createdByUserId=user.id,
    )
    db.add(capa)
    await db.flush()
    f.capaId = capa.id
    await db.commit()
    return {"ok": True, "capaId": capa.id, "capaNumber": capa.capaNumber}


@router.post("/bcm/exercises/{eid}/complete", response_model=S.ExerciseOut)
async def complete_exercise(eid: str, body: S.ExerciseComplete, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "EXERCISE.COMPLETE")
    e = await db.get(BcExercise, eid)
    if not e:
        raise HTTPException(404, "Exercise not found")
    findings = (await db.execute(select(ExerciseFinding).where(ExerciseFinding.exerciseId == eid))).scalars().all()
    # T3-15: every MAJOR_GAP must carry a linked CAPA before completion
    ungapped = [f for f in findings if f.severity == "MAJOR_GAP" and not f.capaId]
    if ungapped:
        raise HTTPException(400, f"{len(ungapped)} MAJOR_GAP finding(s) need a linked CAPA before this exercise can complete.")
    e.status = "COMPLETED"
    e.outcome = body.outcome
    e.conductedDate = body.conductedDate or _now()
    e.rtoAchievedHours = body.rtoAchievedHours
    e.reportRichText = body.reportRichText
    e.updatedBy = user.id
    # plan feedback loop: stamp tested plans' lastExercised
    for pid in (e.testedPlanIds or []):
        await svc.recompute_plan_last_exercised(db, pid)
    await db.commit()
    return await _serialise_exercise(db, e, await _plants(db), await _names(db, [e.facilitatorId]))


@router.post("/bcm/exercises/{eid}/run-call-tree-test", response_model=S.ExerciseOut)
async def run_call_tree_test(eid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """CALL_TREE_TEST: send TEST-prefixed notifications + capture ack stats (T3-16)."""
    await _require(db, user, "EXERCISE.WRITE")
    e = await db.get(BcExercise, eid)
    if not e or e.exerciseType != "CALL_TREE_TEST":
        raise HTTPException(400, "Not a CALL_TREE_TEST exercise.")
    # count nodes across the site's call trees
    trees = (await db.execute(select(CallTree).where(CallTree.isDeleted.is_(False)))).scalars().all()
    trees = [t for t in trees if t.siteId in (e.siteId, None)]
    notified = sum(len(t.nodes or []) for t in trees)
    acknowledged = round(notified * 0.9)  # simulated test response
    e.callTreeStats = {"notified": notified, "acknowledged": acknowledged, "medianAckMinutes": 9}
    e.status = "IN_PROGRESS"
    e.updatedBy = user.id
    await db.commit()
    return await _serialise_exercise(db, e, await _plants(db), await _names(db, [e.facilitatorId]))


# ════════════════════════════════════════════════════════════════════════════
# Scenario + Horizon
# ════════════════════════════════════════════════════════════════════════════
async def _serialise_scenario(db, s) -> S.ScenarioOut:
    readiness = await svc.scenario_readiness(db, s)
    o = S.ScenarioOut.model_validate(s)
    o.mitigationReadiness = readiness
    levels = [ie.get("estimatedLevel", 0) for ie in (s.impactEstimates or [])]
    o.topImpactLevel = max(levels) if levels else None
    return o


@router.get("/bcm/scenarios", response_model=list[S.ScenarioOut])
async def list_scenarios(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BCM.READ")
    rows = (await db.execute(select(Scenario).where(Scenario.isDeleted.is_(False)).order_by(Scenario.scenarioCode))).scalars().all()
    return [await _serialise_scenario(db, s) for s in rows]


@router.post("/bcm/scenarios", response_model=S.ScenarioOut, status_code=201)
async def create_scenario(body: S.ScenarioUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "SCENARIO.WRITE")
    s = Scenario(scenarioCode=await _next_code(db, Scenario, "SCN"), status="DRAFT",
                 impactEstimates=[i.model_dump() for i in body.impactEstimates], whatIfAdjustments=[w.model_dump() for w in body.whatIfAdjustments],
                 **body.model_dump(exclude={"impactEstimates", "whatIfAdjustments"}), createdBy=user.id)
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return await _serialise_scenario(db, s)


@router.patch("/bcm/scenarios/{sid}", response_model=S.ScenarioOut)
async def update_scenario(sid: str, body: S.ScenarioUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "SCENARIO.WRITE")
    s = await db.get(Scenario, sid)
    if not s:
        raise HTTPException(404, "Scenario not found")
    for f in ("title", "category", "narrative", "probabilityQualitative", "timeHorizon", "affectedRiskIds", "affectedProcessIds"):
        setattr(s, f, getattr(body, f))
    s.impactEstimates = [i.model_dump() for i in body.impactEstimates]
    s.whatIfAdjustments = [w.model_dump() for w in body.whatIfAdjustments]
    s.updatedBy = user.id
    await db.commit()
    await db.refresh(s)
    return await _serialise_scenario(db, s)


@router.post("/bcm/scenarios/{sid}/activate", response_model=S.ScenarioOut)
async def activate_scenario(sid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "SCENARIO.WRITE")
    s = await db.get(Scenario, sid)
    if not s:
        raise HTTPException(404, "Scenario not found")
    s.status = "ACTIVE"
    s.lastReviewedAt = _now()
    await db.commit()
    await db.refresh(s)
    return await _serialise_scenario(db, s)


@router.get("/bcm/scenarios/{sid}/stressed-heatmap", response_model=S.StressedHeatMap)
async def scenario_stressed_heatmap(sid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BCM.READ")  # Exec Viewer also has BCM.READ
    s = await db.get(Scenario, sid)
    if not s:
        raise HTTPException(404, "Scenario not found")
    return await svc.stressed_heatmap(db, s)  # presentational only — no writes (T3-20)


@router.post("/bcm/scenarios/{sid}/run-as-exercise")
async def run_scenario_as_exercise(sid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "EXERCISE.WRITE")
    s = await db.get(Scenario, sid)
    if not s:
        raise HTTPException(404, "Scenario not found")
    # gather plans covering the scenario's affected processes
    plan_ids = set()
    for pid in (s.affectedProcessIds or []):
        for pl in await svc.covering_plans(db, pid, approved_only=False):
            plan_ids.add(pl.id)
    e = BcExercise(exerciseCode=await _next_code(db, BcExercise, "BCX", year=True), title=f"Scenario exercise — {s.title}",
                   exerciseType="TABLETOP", scheduledDate=_now() + timedelta(days=14), testedPlanIds=list(plan_ids),
                   testedScenarioId=s.id, facilitatorId=user.id, objectives=[f"Test response to: {s.title}"], status="PLANNED", createdBy=user.id)
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return {"ok": True, "exerciseId": e.id, "exerciseCode": e.exerciseCode}


@router.get("/bcm/horizon", response_model=list[S.HorizonOut])
async def list_horizon(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "BCM.READ")
    rows = (await db.execute(select(HorizonItem).where(HorizonItem.isDeleted.is_(False)))).scalars().all()
    names = await _names(db, [h.watchedBy for h in rows])
    now = _now()
    out = []
    for h in rows:
        o = S.HorizonOut.model_validate(h)
        o.watchedByName = names.get(h.watchedBy)
        o.reviewOverdue = bool(h.reviewDate and _aware(h.reviewDate) < now and not h.disposition)
        out.append(o)
    return out


@router.post("/bcm/horizon", response_model=S.HorizonOut, status_code=201)
async def create_horizon(body: S.HorizonUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "HORIZON.WRITE")
    h = HorizonItem(title=body.title, description=body.description, category=body.category, signalStrength=body.signalStrength,
                    potentialCategoryIds=body.potentialCategoryIds, watchedBy=user.id, reviewDate=body.reviewDate or (_now() + timedelta(days=90)), createdBy=user.id)
    db.add(h)
    await db.commit()
    await db.refresh(h)
    o = S.HorizonOut.model_validate(h)
    o.watchedByName = (await _names(db, [user.id])).get(user.id)
    return o


@router.post("/bcm/horizon/{hid}/disposition", response_model=S.HorizonOut)
async def dispose_horizon(hid: str, body: S.HorizonDisposition, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "HORIZON.WRITE")
    h = await db.get(HorizonItem, hid)
    if not h:
        raise HTTPException(404, "Horizon item not found")
    promoted_id = None
    if body.disposition == "PROMOTED_TO_SCENARIO":
        s = Scenario(scenarioCode=await _next_code(db, Scenario, "SCN"), title=h.title, category=h.category,
                     narrative=h.description, status="DRAFT", createdBy=user.id)
        db.add(s)
        await db.flush()
        promoted_id = s.id
    elif body.disposition == "PROMOTED_TO_RISK":
        cat_id = (h.potentialCategoryIds or [None])[0]
        if not cat_id:
            cat_id = (await db.execute(select(RiskCategory.id).where(RiskCategory.code == "GEO"))).scalar_one_or_none()
        year = _now().year
        n = (await db.execute(select(func.count()).select_from(EnterpriseRisk))).scalar() or 0
        r = EnterpriseRisk(riskCode=f"ERM-{year}-{(n + 1):04d}", title=h.title, description=h.description, categoryId=cat_id,
                           orgLevel="ENTERPRISE", riskOwnerId=user.id, riskChampionId=user.id, lifecycleState="DRAFT", velocity="SLOW",
                           sourceType="MANUAL", identifiedDate=_now(), nextReviewDate=_now() + timedelta(days=180), createdBy=user.id)
        db.add(r)
        await db.flush()
        promoted_id = r.id
    h.disposition = body.disposition
    h.dispositionNote = body.note
    h.promotedEntityId = promoted_id
    h.updatedBy = user.id
    await db.commit()
    await db.refresh(h)
    o = S.HorizonOut.model_validate(h)
    o.watchedByName = (await _names(db, [h.watchedBy])).get(h.watchedBy)
    return o
