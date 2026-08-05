"""Unified workforce search — the Worker Involved picker.

There is no single Workforce master in this platform. `User` is the employee
directory; `ContractorWorker` is the EPC workforce and is deliberately
self-contained (no `userAccountId` FK to User). Both can commit an unsafe act,
so this endpoint searches both and returns one normalised shape, tagged with
`partyType` so the caller knows which table a result came from.

Scoping differs by population because the schemas differ:
  • Users carry `plantId` directly → filtered to the observation's plant.
  • ContractorWorkers do not — they hang off a ContractorCompany, and
    ConstructionSite has no plantId either. They are scoped by
    `contractorCompanyId` instead, which is the more useful filter anyway:
    the observation form already asks which contractor is involved, so the
    picker narrows to that company's crew.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.epc import ContractorCompany, ContractorWorker
from app.models.observation_sla import PARTY_CONTRACTOR_WORKER, PARTY_USER, ROSTER_ACTIVE
from app.models.user import User
from app.schemas.observation_sla import WorkerSearchOut
from app.services.permissions import get_accessible_plants

router = APIRouter(prefix="/api/workforce", tags=["workforce"])

_MAX_RESULTS = 25


@router.get("/search", response_model=list[WorkerSearchOut])
async def search_workforce(
    plantId: str | None = Query(None),
    query: str = Query("", min_length=0),
    contractorCompanyId: str | None = Query(None),
    includeInactive: bool = Query(
        False,
        description=(
            "Include workers currently blocked from new work. Default False. "
            "The Worker Involved picker sets this True — a blocked worker can "
            "still be named on a NEW observation about something they did; the "
            "block is on assigning them work, not on reporting about them."
        ),
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[WorkerSearchOut]:
    term = (query or "").strip()
    like = f"%{term.lower()}%"
    results: list[WorkerSearchOut] = []

    # ── employees ──
    allowed = await get_accessible_plants(db, user.id)
    ustmt = select(User)
    if plantId:
        ustmt = ustmt.where(User.plantId == plantId)
    elif allowed is not None:
        ustmt = ustmt.where(User.plantId.in_(allowed))
    if term:
        ustmt = ustmt.where(
            or_(
                func.lower(User.name).like(like),
                func.lower(User.email).like(like),
                func.lower(func.coalesce(User.designation, "")).like(like),
            )
        )
    for u in (await db.execute(ustmt.order_by(User.name).limit(_MAX_RESULTS))).scalars().all():
        status = getattr(u, "rosterStatus", ROSTER_ACTIVE) or ROSTER_ACTIVE
        if not includeInactive and status != ROSTER_ACTIVE:
            continue
        results.append(
            WorkerSearchOut(
                partyType=PARTY_USER,
                id=u.id,
                name=u.name,
                role=u.designation,
                employer=u.department,
                plantId=u.plantId,
                rosterStatus=status,
            )
        )

    # ── contractor workers ──
    cstmt = select(ContractorWorker, ContractorCompany.name).join(
        ContractorCompany, ContractorCompany.id == ContractorWorker.contractorCompanyId
    )
    if contractorCompanyId:
        cstmt = cstmt.where(ContractorWorker.contractorCompanyId == contractorCompanyId)
    if term:
        cstmt = cstmt.where(
            or_(
                func.lower(ContractorWorker.fullName).like(like),
                func.lower(ContractorWorker.workerCode).like(like),
                func.lower(func.coalesce(ContractorWorker.primaryTrade, "")).like(like),
            )
        )
    rows = (await db.execute(cstmt.order_by(ContractorWorker.fullName).limit(_MAX_RESULTS))).all()
    for w, company_name in rows:
        status = getattr(w, "rosterStatus", ROSTER_ACTIVE) or ROSTER_ACTIVE
        if not includeInactive and status != ROSTER_ACTIVE:
            continue
        results.append(
            WorkerSearchOut(
                partyType=PARTY_CONTRACTOR_WORKER,
                id=w.id,
                name=w.fullName,
                role=w.primaryTrade,
                employer=company_name,
                code=w.workerCode,
                plantId=None,
                rosterStatus=status,
            )
        )

    return results
