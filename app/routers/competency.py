"""Skill Matrix router. Mounts at /api/skill-matrix.

Read-only endpoints for the Skill Matrix (competency-state) module — Phase 1
of the IMS expansion. The competency *catalog* + role definitions are loaded
by prisma/seed-competency-library.ts; the per-person CompetencyRecord cells by
prisma/seed-competency-records.ts.

Endpoints:
  GET /api/skill-matrix/competencies      — competency library (filterable)
  GET /api/skill-matrix/role-definitions  — job-role definitions + requirements
  GET /api/skill-matrix/matrix            — person × competency grid for a plant

The matrix endpoint returns the grid the frontend renders: one row per person
that has any competency record in the plant, one column per competency that is
tracked there, and a cell carrying the §3.2 lifecycle state + validity.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import HTTPException, status

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.competency_matrix import (
    Competency,
    CompetencyRecord,
    PersonRoleAssignment,
    RoleCompetencyRequirement,
    RoleDefinition,
)
from app.models.training_engine import TrainingAssignment
from app.models.user import User
from app.services.access_scope import build_query_scope
from app.services.competency_state import sync_plant_from_training

# Lifecycle-state buckets for roll-up / gap maths (mirrors the 11-state machine's
# validity semantics; kept local so the router doesn't couple to the state svc).
_MET_STATES = {"validated_active", "expiring_soon"}
_EXPIRED_STATES = {"expired", "expired_in_grace", "lapsed", "revoked", "suspended", "superseded"}

router = APIRouter(prefix="/api/skill-matrix", tags=["skill-matrix"])


async def _require_skill_matrix_read(db: AsyncSession, user: User, plant_id: str | None = None) -> None:
    """Authorise a Skill-Matrix read. The matrix carries employee competency
    PII, so this is the P0 gate. Uses the permission-SPECIFIC plant scope
    (build_query_scope), which fails CLOSED for OWN_DEPARTMENT / OWN_RECORDS
    grants — unlike can(plant_id=…), which only enforces plant for OWN_PLANT
    and would let a WORKER read any plant's matrix."""
    scope = await build_query_scope(db, user.id, "SKILL_MATRIX.READ")
    if plant_id is None:
        # Catalog read (no plant axis): require holding SKILL_MATRIX.READ at all.
        if scope.all_plants or scope.plant_ids:
            return
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    if not scope.allows_plant(plant_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied for this plant")


@router.post("/sync-from-training")
async def sync_from_training(
    plantId: str = Query(..., description="Plant whose matrix to recompute from training"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Recompute every competency cell in the plant from current training
    evidence ("training feeds competency", D1). Idempotent — running it twice
    with unchanged training is a no-op. Each cell that moves writes an audit
    version row.
    """
    await _require_skill_matrix_read(db, user, plantId)
    stats = await sync_plant_from_training(db, plant_id=plantId, actor_user_id=user.id)
    return {"plantId": plantId, **stats}


@router.get("/competencies")
async def list_competencies(
    category: str | None = Query(None, description="Filter by competency category"),
    q: str | None = Query(None, description="Free-text search on name/code"),
    limit: int = Query(300, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    await _require_skill_matrix_read(db, user)
    stmt = select(Competency).where(Competency.isActive.is_(True))
    if category:
        stmt = stmt.where(Competency.category == category)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Competency.name).like(like),
                func.lower(Competency.code).like(like),
            )
        )
    stmt = stmt.order_by(Competency.category.asc(), Competency.code.asc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": c.id,
            "code": c.code,
            "name": c.name,
            "category": c.category,
            "subcategory": c.subcategory,
            "defaultValidityMonths": c.defaultValidityMonths,
            "isGlobal": c.isGlobal,
        }
        for c in rows
    ]


@router.get("/role-definitions")
async def list_role_definitions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    await _require_skill_matrix_read(db, user)
    rows = (
        await db.execute(
            select(RoleDefinition)
            .where(RoleDefinition.isActive.is_(True))
            .order_by(RoleDefinition.roleName.asc())
        )
    ).scalars().all()
    out: list[dict] = []
    for rd in rows:
        reqs = (
            await db.execute(
                select(RoleCompetencyRequirement).where(
                    RoleCompetencyRequirement.roleDefinitionId == rd.id
                )
            )
        ).scalars().all()
        out.append(
            {
                "id": rd.id,
                "roleName": rd.roleName,
                "appliesToDepartments": rd.appliesToDepartments or [],
                "appliesToPlants": rd.appliesToPlants or [],
                "requirementCount": len(reqs),
                "requirements": [
                    {
                        "competencyId": r.competencyId,
                        "requirementType": r.requirementType,
                    }
                    for r in reqs
                ],
            }
        )
    return out


@router.get("/matrix")
async def get_matrix(
    plantId: str = Query(..., description="Plant to scope the matrix to"),
    category: str | None = Query(None, description="Restrict columns to one category"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Person × competency grid for a plant.

    Columns are the competencies that actually have records in this plant
    (optionally filtered to one category); rows are the people who hold any
    record. Each cell carries the lifecycle state so the UI can RAG-colour it.
    """
    await _require_skill_matrix_read(db, user, plantId)
    records = (
        await db.execute(
            select(CompetencyRecord).where(CompetencyRecord.plantId == plantId)
        )
    ).scalars().all()

    empty_summary = {
        "byState": {},
        "totalCells": 0,
        "personCount": 0,
        "competencyCount": 0,
    }
    if not records:
        return {"plantId": plantId, "competencies": [], "persons": [], "summary": empty_summary}

    comp_ids = {r.competencyId for r in records}
    person_ids = {r.personUserId for r in records}

    comp_stmt = select(Competency).where(Competency.id.in_(comp_ids))
    if category:
        comp_stmt = comp_stmt.where(Competency.category == category)
    comps = (await db.execute(comp_stmt)).scalars().all()
    comps = sorted(comps, key=lambda c: (c.category, c.code))
    allowed_comp_ids = {c.id for c in comps}

    users = (
        await db.execute(select(User).where(User.id.in_(person_ids)))
    ).scalars().all()
    users = sorted(users, key=lambda u: (u.name or "").lower())

    # (personUserId, competencyId) -> record, restricted to the chosen columns.
    cell: dict[tuple[str, str], CompetencyRecord] = {}
    by_state: dict[str, int] = {}
    for r in records:
        if r.competencyId not in allowed_comp_ids:
            continue
        cell[(r.personUserId, r.competencyId)] = r
        by_state[r.state] = by_state.get(r.state, 0) + 1

    persons: list[dict] = []
    for u in users:
        cells: dict[str, dict] = {}
        for c in comps:
            r = cell.get((u.id, c.id))
            if r is None:
                continue
            cells[c.id] = {
                "state": r.state,
                "validUntil": r.validUntil.isoformat() if r.validUntil else None,
                "currentScore": r.currentScore,
            }
        persons.append(
            {
                "userId": u.id,
                "name": u.name,
                "role": u.role,
                "department": u.department,
                "designation": u.designation,
                "cells": cells,
            }
        )

    return {
        "plantId": plantId,
        "competencies": [
            {
                "id": c.id,
                "code": c.code,
                "name": c.name,
                "category": c.category,
                "subcategory": c.subcategory,
            }
            for c in comps
        ],
        "persons": persons,
        "summary": {
            "byState": by_state,
            "totalCells": sum(by_state.values()),
            "personCount": len(persons),
            "competencyCount": len(comps),
        },
    }


@router.get("/rollup")
async def get_rollup(
    plantId: str = Query(..., description="Plant to roll up"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Site/department roll-up: % workforce compliant per competency (feeds the
    executive Daily Brief). A competency cell is 'met' when validated_active or
    expiring_soon; 'expired' when lapsed/expired/suspended; else 'in progress'."""
    await _require_skill_matrix_read(db, user, plantId)
    records = (
        await db.execute(select(CompetencyRecord).where(CompetencyRecord.plantId == plantId))
    ).scalars().all()
    if not records:
        return {"plantId": plantId, "competencies": [], "summary": {"workforceCompliancePct": None, "recordCount": 0}}

    comp_ids = {r.competencyId for r in records}
    comps = (await db.execute(select(Competency).where(Competency.id.in_(comp_ids)))).scalars().all()
    comp_by_id = {c.id: c for c in comps}

    agg: dict[str, dict] = {}
    for r in records:
        a = agg.setdefault(r.competencyId, {"total": 0, "met": 0, "expired": 0, "inProgress": 0})
        a["total"] += 1
        if r.state in _MET_STATES:
            a["met"] += 1
        elif r.state in _EXPIRED_STATES:
            a["expired"] += 1
        else:
            a["inProgress"] += 1

    rows = []
    for cid, a in agg.items():
        c = comp_by_id.get(cid)
        pct = round(a["met"] / a["total"] * 100, 1) if a["total"] else 0.0
        rows.append(
            {
                "competencyId": cid,
                "code": c.code if c else None,
                "name": c.name if c else cid,
                "category": c.category if c else None,
                "total": a["total"],
                "met": a["met"],
                "expired": a["expired"],
                "inProgress": a["inProgress"],
                "compliancePct": pct,
            }
        )
    rows.sort(key=lambda x: x["compliancePct"])
    total_cells = sum(a["total"] for a in agg.values())
    total_met = sum(a["met"] for a in agg.values())
    return {
        "plantId": plantId,
        "competencies": rows,
        "summary": {
            "workforceCompliancePct": round(total_met / total_cells * 100, 1) if total_cells else None,
            "recordCount": total_cells,
            "competencyCount": len(rows),
            "atRiskCount": sum(1 for r in rows if r["compliancePct"] < 80),
        },
    }


@router.get("/profile/{user_id}")
async def get_worker_profile(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """A single worker's competency profile: held competencies (with state +
    validity), role-required gaps, and open training assignments."""
    person = await db.get(User, user_id)
    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user_id != user.id:
        await _require_skill_matrix_read(db, user, person.plantId)

    records = (
        await db.execute(select(CompetencyRecord).where(CompetencyRecord.personUserId == user_id))
    ).scalars().all()
    held_ids = {r.competencyId for r in records}

    # Role-required competencies via PersonRoleAssignment → RoleDefinition reqs.
    role_def_ids = (
        await db.execute(
            select(PersonRoleAssignment.roleDefinitionId)
            .where(PersonRoleAssignment.personUserId == user_id)
            .where(PersonRoleAssignment.status == "active")
        )
    ).scalars().all()
    required = []
    if role_def_ids:
        required = (
            await db.execute(
                select(RoleCompetencyRequirement).where(
                    RoleCompetencyRequirement.roleDefinitionId.in_(list(role_def_ids))
                )
            )
        ).scalars().all()
    required_ids = {r.competencyId for r in required}

    all_comp_ids = held_ids | required_ids
    comps = {}
    if all_comp_ids:
        rows = (await db.execute(select(Competency).where(Competency.id.in_(all_comp_ids)))).scalars().all()
        comps = {c.id: c for c in rows}

    record_out = []
    for r in records:
        c = comps.get(r.competencyId)
        record_out.append(
            {
                "competencyId": r.competencyId,
                "name": c.name if c else r.competencyId,
                "category": c.category if c else None,
                "state": r.state,
                "currentProficiency": getattr(r, "currentProficiency", None),
                "validFrom": r.validFrom.isoformat() if r.validFrom else None,
                "validUntil": r.validUntil.isoformat() if r.validUntil else None,
                "nextRevalidationDue": r.nextRevalidationDue.isoformat() if r.nextRevalidationDue else None,
            }
        )

    # A gap = required competency not held, or held but expired/suspended.
    held_state = {r.competencyId: r.state for r in records}
    gaps = []
    for req in required:
        st = held_state.get(req.competencyId)
        if st is None or st in _EXPIRED_STATES or st not in _MET_STATES:
            c = comps.get(req.competencyId)
            gaps.append(
                {
                    "competencyId": req.competencyId,
                    "name": c.name if c else req.competencyId,
                    "requirementType": req.requirementType,
                    "requiredProficiency": getattr(req, "requiredProficiency", None),
                    "currentState": st or "not_yet_attempted",
                }
            )

    assignments = (
        await db.execute(
            select(TrainingAssignment)
            .where(TrainingAssignment.personUserId == user_id)
            .where(TrainingAssignment.isDeleted.is_(False))
            .order_by(TrainingAssignment.assignedAt.desc())
        )
    ).scalars().all()
    assignment_out = [
        {
            "id": a.id,
            "competencyId": a.competencyId,
            "competencyName": comps.get(a.competencyId).name if comps.get(a.competencyId) else a.competencyId,
            "source": a.source,
            "status": a.status,
            "isMandatory": a.isMandatory,
            "dueDate": a.dueDate.isoformat() if a.dueDate else None,
            "sourceRecordRef": a.sourceRecordRef,
        }
        for a in assignments
    ]

    return {
        "user": {"id": person.id, "name": person.name, "role": person.role,
                 "department": person.department, "plantId": person.plantId, "designation": person.designation},
        "records": record_out,
        "gaps": gaps,
        "assignments": assignment_out,
        "summary": {
            "held": len(records),
            "met": sum(1 for r in records if r.state in _MET_STATES),
            "gaps": len(gaps),
            "openAssignments": sum(1 for a in assignments if a.status in ("assigned", "in_progress", "overdue", "escalated")),
        },
    }
