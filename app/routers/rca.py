"""ERM Cross-Domain Root Cause Analysis (RCA) & Causal Intelligence router.

Mounted at /api/erm/rca, gated on the ERM module (router_map). RBAC-enforced via
the shared can() service.

Permission codes (seeded in seed-rbac.ts):
  RCA.READ RCA.CREATE RCA.TAG RCA.APPROVE RCA.TAXONOMY_ADMIN

A single first-class RootCauseAnalysis store: event-derived RCAs are exposed from
incidents (system-of-record stays the incident); risk- and loss-event RCAs are
authored directly. ERM holds the causal linkage + analytics layer on top.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.soft_delete import soft_delete
from app.models.capa import Capa
from app.models.erm import EnterpriseRisk
from app.models.erm_p2 import LossEvent
from app.models.incident import Incident
from app.models.rca import (
    RcaIdentifiedCause,
    RcaRiskLink,
    RootCauseAnalysis,
    RootCauseCategory,
    RootCauseSubCause,
)
from app.models.user import User
from app.schemas import rca as S
from app.services import rca_analytics, rca_core, rca_taxonomy
from app.services.access_scope import build_query_scope
from app.services.capa_spawn import spawn_capa
from app.services.permissions import PermissionContext, can, get_user_role_codes

router = APIRouter(prefix="/api/erm/rca", tags=["erm-rca"])

# Roles that see every risk domain in analytics; everyone else defaults to all
# domains on READ, EXCEPT a compliance-scoped role which is restricted to its
# own domain (RCA-T16). Plant scope is enforced separately via QueryScope.
_ALL_DOMAIN_ROLES = {
    "CRO", "ADMIN", "SYSTEM_ADMIN", "RISK_CHAMPION", "RISK_OWNER", "EXECUTIVE_VIEWER", "HSE_MANAGER",
}
_DOMAIN_SCOPED_ROLES = {
    "COMPLIANCE_OFFICER": {"COMPLIANCE"},
    "COMPLIANCE_LEAD": {"COMPLIANCE"},
    "COMPLIANCE_MANAGER": {"COMPLIANCE"},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _require(db: AsyncSession, user: User, code: str, *, plant_id: str | None = None,
                   record: dict | None = None, record_id: str | None = None) -> None:
    res = await can(db, user.id, code, PermissionContext(plant_id=plant_id, record=record, record_id=record_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


async def _effective_domains(db: AsyncSession, user: User) -> set[str] | None:
    """The risk domains this actor may view in analytics. None == all domains."""
    roles = set(await get_user_role_codes(db, user.id))
    if roles & _ALL_DOMAIN_ROLES:
        return None
    scoped: set[str] = set()
    restricted = False
    for r in roles:
        if r in _DOMAIN_SCOPED_ROLES:
            scoped |= _DOMAIN_SCOPED_ROLES[r]
            restricted = True
    return scoped if restricted else None


# ─────────────────────────────────────────────────────────────────────
# Taxonomy masters
# ─────────────────────────────────────────────────────────────────────
@router.get("/categories", response_model=list[S.CategoryOut])
async def list_categories(active_only: bool = False, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.READ")
    return await rca_taxonomy.list_categories(db, active_only=active_only)


@router.get("/sub-causes", response_model=list[S.SubCauseOut])
async def list_sub_causes(domain: str | None = Query(default=None),
                          user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Domain-scoped picker: a FINANCIAL RCA offers only financial-applicable
    sub-causes; an OPERATIONAL RCA offers operational ones (RCA-T06)."""
    await _require(db, user, "RCA.READ")
    return await rca_taxonomy.subcauses_for_domain(db, domain)


async def _category_out(db: AsyncSession, category_id: str) -> S.CategoryOut:
    cat = (
        await db.execute(
            select(RootCauseCategory)
            .where(RootCauseCategory.id == category_id)
            .options(selectinload(RootCauseCategory.subCauses))
        )
    ).scalar_one()
    return S.CategoryOut.model_validate(cat)


@router.post("/categories", response_model=S.CategoryOut, status_code=status.HTTP_201_CREATED)
async def create_category(body: S.CategoryUpsert, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.TAXONOMY_ADMIN")
    cat = RootCauseCategory(**body.model_dump(), createdBy=user.id, updatedBy=user.id)
    db.add(cat)
    await db.flush()
    return await _category_out(db, cat.id)


@router.patch("/categories/{category_id}", response_model=S.CategoryOut)
async def update_category(category_id: str, body: S.CategoryUpdate, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.TAXONOMY_ADMIN")
    cat = await db.get(RootCauseCategory, category_id)
    if cat is None or cat.isDeleted:
        raise HTTPException(404, "Category not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(cat, k, v)
    cat.updatedBy = user.id
    await db.flush()
    return await _category_out(db, cat.id)


@router.post("/sub-causes", response_model=S.SubCauseOut, status_code=status.HTTP_201_CREATED)
async def create_sub_cause(body: S.SubCauseUpsert, user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.TAXONOMY_ADMIN")
    try:
        await rca_taxonomy.validate_subcause_parent(db, body.categoryId)  # RCA-T05
    except ValueError as e:
        raise HTTPException(400, str(e))
    sub = RootCauseSubCause(**body.model_dump(), createdBy=user.id, updatedBy=user.id)
    db.add(sub)
    await db.flush()
    await db.refresh(sub)
    return sub


@router.patch("/sub-causes/{sub_cause_id}", response_model=S.SubCauseOut)
async def update_sub_cause(sub_cause_id: str, body: S.SubCauseUpdate, user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.TAXONOMY_ADMIN")
    sub = await db.get(RootCauseSubCause, sub_cause_id)
    if sub is None or sub.isDeleted:
        raise HTTPException(404, "Sub-cause not found")
    data = body.model_dump(exclude_unset=True)
    if "categoryId" in data:
        try:
            await rca_taxonomy.validate_subcause_parent(db, data["categoryId"])
        except ValueError as e:
            raise HTTPException(400, str(e))
    for k, v in data.items():
        setattr(sub, k, v)
    sub.updatedBy = user.id
    await db.flush()
    await db.refresh(sub)
    return sub


# ─────────────────────────────────────────────────────────────────────
# Serialisation helpers
# ─────────────────────────────────────────────────────────────────────
async def _detail(db: AsyncSession, rca: RootCauseAnalysis) -> S.RcaDetail:
    cats = {c.id: c for c in (await db.execute(select(RootCauseCategory))).scalars().all()}
    subs = {s.id: s for s in (await db.execute(select(RootCauseSubCause))).scalars().all()}

    causes_out = []
    for c in sorted(rca.identifiedCauses, key=lambda x: x.sortOrder):
        sub = subs.get(c.subCauseId)
        cat = cats.get(c.enterpriseCategoryId)
        causes_out.append(
            S.IdentifiedCauseOut(
                id=c.id, subCauseId=c.subCauseId, enterpriseCategoryId=c.enterpriseCategoryId,
                causalRole=c.causalRole, description=c.description, confidence=c.confidence, sortOrder=c.sortOrder,
                subCauseName=sub.name if sub else None, subCauseCode=sub.code if sub else None,
                categoryName=cat.name if cat else None, categoryCode=cat.code if cat else None,
            )
        )

    risk_ids = [link.riskId for link in rca.riskLinks]
    risks = {}
    if risk_ids:
        rows = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.id.in_(risk_ids)))).scalars().all()
        risks = {r.id: r for r in rows}
    links_out = []
    for link in rca.riskLinks:
        r = risks.get(link.riskId)
        links_out.append(
            S.RiskLinkOut(
                id=link.id, riskId=link.riskId, contributionType=link.contributionType,
                weight=link.weight, note=link.note,
                riskCode=r.riskCode if r else None, riskTitle=r.title if r else None,
                riskResidualBand=r.residualBand if r else None,
            )
        )

    capa_ids = list(
        (
            await db.execute(
                select(Capa.id).where(Capa.sourceTypeCode == "ENTERPRISE_RCA").where(Capa.sourceReferenceId == rca.id)
            )
        ).scalars().all()
    )

    source_label = None
    if rca.originType == "RISK" and rca.sourceRiskId:
        r = await db.get(EnterpriseRisk, rca.sourceRiskId)
        source_label = f"{r.riskCode} · {r.title}" if r else None
    elif rca.originType == "LOSS_EVENT" and rca.sourceLossEventId:
        le = await db.get(LossEvent, rca.sourceLossEventId)
        source_label = f"{le.eventCode} · {le.title}" if le else None
    elif rca.originType == "EVENT" and rca.sourceEventId:
        inc = await db.get(Incident, rca.sourceEventId)
        source_label = f"Incident {inc.number}" if inc else None

    return S.RcaDetail(
        id=rca.id, rcaCode=rca.rcaCode, title=rca.title, originType=rca.originType,
        sourceEventId=rca.sourceEventId, sourceRiskId=rca.sourceRiskId, sourceLossEventId=rca.sourceLossEventId,
        primaryDomain=rca.primaryDomain, methodology=rca.methodology, status=rca.status,
        analysisPayload=rca.analysisPayload or {}, narrative=rca.narrative, analystId=rca.analystId,
        approverId=rca.approverId, approvedAt=rca.approvedAt, occurrenceDate=rca.occurrenceDate,
        plantId=rca.plantId, createdAt=rca.createdAt, updatedAt=rca.updatedAt,
        identifiedCauses=causes_out, riskLinks=links_out, capaIds=capa_ids, sourceLabel=source_label,
    )


async def _load_full(db: AsyncSession, rca_id: str) -> RootCauseAnalysis:
    rca = (
        await db.execute(
            select(RootCauseAnalysis)
            .where(RootCauseAnalysis.id == rca_id)
            .options(
                selectinload(RootCauseAnalysis.identifiedCauses),
                selectinload(RootCauseAnalysis.riskLinks),
            )
        )
    ).scalar_one_or_none()
    if rca is None:
        raise HTTPException(404, "RCA not found")
    return rca


# ─────────────────────────────────────────────────────────────────────
# RCA register + detail
# ─────────────────────────────────────────────────────────────────────
@router.get("", response_model=S.RcaListResponse)
async def list_rcas(
    domain: str | None = None, originType: str | None = None, rcaStatus: str | None = Query(default=None, alias="status"),
    enterpriseCategoryId: str | None = None, plantId: str | None = None,
    sourceRiskId: str | None = None, sourceLossEventId: str | None = None,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "RCA.READ")
    scope = await build_query_scope(db, user.id, "RCA.READ")
    stmt = select(RootCauseAnalysis)
    stmt = scope.apply(stmt, RootCauseAnalysis)
    if domain:
        stmt = stmt.where(RootCauseAnalysis.primaryDomain == domain)
    if originType:
        stmt = stmt.where(RootCauseAnalysis.originType == originType)
    if rcaStatus:
        stmt = stmt.where(RootCauseAnalysis.status == rcaStatus)
    if plantId:
        stmt = stmt.where(RootCauseAnalysis.plantId == plantId)
    if sourceRiskId:
        stmt = stmt.where(RootCauseAnalysis.sourceRiskId == sourceRiskId)
    if sourceLossEventId:
        stmt = stmt.where(RootCauseAnalysis.sourceLossEventId == sourceLossEventId)
    # domain scoping (compliance-only role, RCA-T16)
    allowed = await _effective_domains(db, user)
    if allowed is not None:
        stmt = stmt.where(RootCauseAnalysis.primaryDomain.in_(allowed))
    stmt = stmt.options(
        selectinload(RootCauseAnalysis.identifiedCauses), selectinload(RootCauseAnalysis.riskLinks)
    ).order_by(RootCauseAnalysis.createdAt.desc())
    rows = list((await db.execute(stmt)).scalars().all())

    if enterpriseCategoryId:
        rows = [r for r in rows if any(c.enterpriseCategoryId == enterpriseCategoryId for c in r.identifiedCauses)]

    # Batch-resolve traceability: every risk this RCA touches (source + links)
    # and the originating record's code, so the register can show a Source
    # column and a linked-risks popover that click through to the risk.
    risk_ids: set[str] = set()
    loss_ids: set[str] = set()
    for r in rows:
        if r.sourceRiskId:
            risk_ids.add(r.sourceRiskId)
        if r.sourceLossEventId:
            loss_ids.add(r.sourceLossEventId)
        for lk in r.riskLinks:
            if lk.riskId:
                risk_ids.add(lk.riskId)
    risk_map: dict[str, EnterpriseRisk] = {}
    if risk_ids:
        risk_map = {
            x.id: x for x in (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.id.in_(risk_ids)))).scalars().all()
        }
    loss_map: dict[str, LossEvent] = {}
    if loss_ids:
        loss_map = {
            x.id: x for x in (await db.execute(select(LossEvent).where(LossEvent.id.in_(loss_ids)))).scalars().all()
        }

    def _source(r: RootCauseAnalysis) -> tuple[str | None, str | None]:
        if r.originType == "RISK" and r.sourceRiskId:
            rk = risk_map.get(r.sourceRiskId)
            return (rk.riskCode if rk else None), (f"/erm/register/{r.sourceRiskId}" if rk else None)
        if r.originType == "LOSS_EVENT" and r.sourceLossEventId:
            le = loss_map.get(r.sourceLossEventId)
            return (le.eventCode if le else None), (f"/erm/loss?focus={r.sourceLossEventId}" if le else None)
        if r.originType == "EVENT" and r.sourceEventId:
            return "Incident", f"/incidents/{r.sourceEventId}"
        return None, None

    items = []
    for r in rows:
        src_code, src_href = _source(r)
        linked = []
        seen_ids: set[str] = set()
        for lk in r.riskLinks:
            if lk.riskId and lk.riskId not in seen_ids:
                seen_ids.add(lk.riskId)
                rk = risk_map.get(lk.riskId)
                linked.append(S.LinkedRiskRef(riskId=lk.riskId, riskCode=rk.riskCode if rk else None,
                                              riskTitle=rk.title if rk else None))
        items.append(S.RcaListItem(
            id=r.id, rcaCode=r.rcaCode, title=r.title, originType=r.originType, primaryDomain=r.primaryDomain,
            methodology=r.methodology, status=r.status, analystId=r.analystId, plantId=r.plantId,
            occurrenceDate=r.occurrenceDate, createdAt=r.createdAt,
            causeCount=len(r.identifiedCauses), linkedRiskCount=len(r.riskLinks),
            sourceEventId=r.sourceEventId, sourceRiskId=r.sourceRiskId, sourceLossEventId=r.sourceLossEventId,
            sourceCode=src_code, sourceHref=src_href, linkedRisks=linked,
        ))
    return S.RcaListResponse(items=items, total=len(items))


@router.get("/{rca_id}", response_model=S.RcaDetail)
async def get_rca(rca_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.READ")
    rca = await _load_full(db, rca_id)
    return await _detail(db, rca)


# ── Origination Paths A / B / C ──
@router.post("/risk-rcas", response_model=S.RcaDetail, status_code=status.HTTP_201_CREATED)
async def create_risk_rca(body: S.RcaCreateRisk, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.CREATE")
    try:
        rca = await rca_core.create_risk_rca(
            db, source_risk_id=body.sourceRiskId, title=body.title, methodology=body.methodology,
            narrative=body.narrative, occurrence_date=body.occurrenceDate, actor_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.flush()
    return await _detail(db, await _load_full(db, rca.id))


@router.post("/loss-rcas", response_model=S.RcaDetail, status_code=status.HTTP_201_CREATED)
async def create_loss_rca(body: S.RcaCreateLoss, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.CREATE")
    try:
        rca = await rca_core.create_loss_rca(
            db, source_loss_event_id=body.sourceLossEventId, title=body.title, methodology=body.methodology,
            narrative=body.narrative, occurrence_date=body.occurrenceDate, actor_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.flush()
    return await _detail(db, await _load_full(db, rca.id))


@router.post("/event-rcas", response_model=S.RcaDetail, status_code=status.HTTP_201_CREATED)
async def expose_event_rca(body: S.RcaCreateEvent, user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    """Path A — expose an incident's RCA into ERM (idempotent; incident stays SoR)."""
    await _require(db, user, "RCA.CREATE")
    inc = await db.get(Incident, body.sourceEventId)
    if inc is None:
        raise HTTPException(404, "Source incident not found")
    rca = await rca_core.expose_incident_rca(db, inc, actor_id=user.id)
    await db.flush()
    return await _detail(db, await _load_full(db, rca.id))


@router.patch("/{rca_id}", response_model=S.RcaDetail)
async def update_rca(rca_id: str, body: S.RcaUpdate, user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.CREATE")
    rca = await _load_full(db, rca_id)
    data = body.model_dump(exclude_unset=True)
    if "methodology" in data and data["methodology"]:
        from app.services.rca import normalise_rca_method
        data["methodology"] = normalise_rca_method(data["methodology"]) or rca.methodology
    for k, v in data.items():
        setattr(rca, k, v)
    if rca.status == "DRAFT" and ("analysisPayload" in data or "narrative" in data):
        rca.status = "IN_ANALYSIS"
    rca.updatedBy = user.id
    await db.flush()
    return await _detail(db, await _load_full(db, rca.id))


@router.post("/{rca_id}/submit", response_model=S.RcaDetail)
async def submit_rca(rca_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.CREATE")
    rca = await _load_full(db, rca_id)
    if rca.status in ("APPROVED", "SUPERSEDED"):
        raise HTTPException(400, f"Cannot submit an RCA in status {rca.status}")
    rca.status = "PEER_REVIEW"
    rca.updatedBy = user.id
    await db.flush()
    return await _detail(db, await _load_full(db, rca.id))


@router.post("/{rca_id}/approve", response_model=S.RcaDetail)
async def approve_rca(rca_id: str, body: S.ApproveIn, user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.APPROVE")
    rca = await _load_full(db, rca_id)
    rca.status = "APPROVED"
    rca.approverId = user.id
    rca.approvedAt = _now()
    rca.updatedBy = user.id
    # Daily Brief outbox: rca.completed → "n corrective actions now active"
    from app.services import events as domain_events
    domain_events.emit(
        db,
        event_type=domain_events.RCA_COMPLETED,
        entity_type="RootCauseAnalysis",
        entity_id=rca.id,
        entity_ref=rca.rcaCode,
        site_id=rca.plantId,
        actor_id=user.id,
        payload={"from": "PEER_REVIEW", "to": "APPROVED", "title": rca.title},
    )
    await db.flush()
    return await _detail(db, await _load_full(db, rca.id))


@router.post("/{rca_id}/reopen", response_model=S.RcaDetail)
async def reopen_rca(rca_id: str, reason: str = Query(..., min_length=5),
                     user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Reopen an APPROVED analysis (new evidence / recurrence). New endpoint —
    no reopen path existed (DECISIONS.md D8); fires the CRITICAL rca.reopened
    impact rule since live permits may rest on its validated controls."""
    await _require(db, user, "RCA.APPROVE")
    rca = await _load_full(db, rca_id)
    if rca.status != "APPROVED":
        raise HTTPException(400, f"Only an APPROVED RCA can be reopened (status is {rca.status})")
    rca.status = "IN_ANALYSIS"
    rca.approverId = None
    rca.approvedAt = None
    rca.updatedBy = user.id
    from app.services import events as domain_events
    domain_events.emit(
        db,
        event_type=domain_events.RCA_REOPENED,
        entity_type="RootCauseAnalysis",
        entity_id=rca.id,
        entity_ref=rca.rcaCode,
        site_id=rca.plantId,
        actor_id=user.id,
        payload={"from": "APPROVED", "to": "IN_ANALYSIS", "reason": reason, "title": rca.title},
    )
    await db.flush()
    return await _detail(db, await _load_full(db, rca.id))


@router.delete("/{rca_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rca(rca_id: str, reason: str = Query(..., min_length=10),
                     user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.CREATE")
    rca = await db.get(RootCauseAnalysis, rca_id)
    if rca is None or rca.isDeleted:
        raise HTTPException(404, "RCA not found")
    soft_delete(rca, user.id, reason)  # hard-delete is ORM-blocked (governed)
    rca.updatedBy = user.id
    await db.flush()


# ─────────────────────────────────────────────────────────────────────
# Cause tagging + risk linking
# ─────────────────────────────────────────────────────────────────────
@router.post("/{rca_id}/causes", response_model=S.RcaDetail, status_code=status.HTTP_201_CREATED)
async def add_cause(rca_id: str, body: S.CauseTagIn, user: User = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.TAG")
    rca = await _load_full(db, rca_id)
    sub = await db.get(RootCauseSubCause, body.subCauseId)
    if sub is None or sub.isDeleted:
        raise HTTPException(404, "Sub-cause not found")
    cause = RcaIdentifiedCause(
        rcaId=rca.id, subCauseId=sub.id, enterpriseCategoryId=sub.categoryId,
        causalRole=body.causalRole, description=body.description, confidence=body.confidence,
        sortOrder=body.sortOrder, createdBy=user.id,
    )
    db.add(cause)
    await db.flush()
    return await _detail(db, await _load_full(db, rca.id))


@router.delete("/{rca_id}/causes/{cause_id}", response_model=S.RcaDetail)
async def remove_cause(rca_id: str, cause_id: str, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.TAG")
    cause = await db.get(RcaIdentifiedCause, cause_id)
    if cause is None or cause.rcaId != rca_id:
        raise HTTPException(404, "Cause not found")
    await db.delete(cause)  # child row, not a governed entity
    await db.flush()
    return await _detail(db, await _load_full(db, rca_id))


@router.post("/{rca_id}/links", response_model=S.RcaDetail, status_code=status.HTTP_201_CREATED)
async def add_risk_link(rca_id: str, body: S.RiskLinkIn, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.TAG")
    rca = await _load_full(db, rca_id)
    risk = await db.get(EnterpriseRisk, body.riskId)
    if risk is None or risk.isDeleted:
        raise HTTPException(404, "Risk not found")
    link = RcaRiskLink(
        rcaId=rca.id, riskId=body.riskId, contributionType=body.contributionType,
        weight=body.weight, note=body.note, createdBy=user.id,
    )
    db.add(link)
    await db.flush()
    return await _detail(db, await _load_full(db, rca.id))


@router.delete("/{rca_id}/links/{link_id}", response_model=S.RcaDetail)
async def remove_risk_link(rca_id: str, link_id: str, user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.TAG")
    link = await db.get(RcaRiskLink, link_id)
    if link is None or link.rcaId != rca_id:
        raise HTTPException(404, "Link not found")
    await db.delete(link)
    await db.flush()
    return await _detail(db, await _load_full(db, rca_id))


@router.post("/{rca_id}/capas", status_code=status.HTTP_201_CREATED)
async def raise_capa(rca_id: str, body: S.RaiseCapaIn, user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    """Raise a corrective action on the universal CAPA engine (sourceType=ENTERPRISE_RCA)."""
    await _require(db, user, "RCA.TAG")
    rca = await db.get(RootCauseAnalysis, rca_id)
    if rca is None or rca.isDeleted:
        raise HTTPException(404, "RCA not found")
    try:
        capa = await spawn_capa(
            db, source_code="ENTERPRISE_RCA", plant_id=rca.plantId, title=body.title, problem=body.problem,
            ref_id=rca.id, ref_url=f"/erm/rca/{rca.id}", ref_summary=f"{rca.rcaCode} — {rca.title}",
            metadata={"rcaCode": rca.rcaCode, "primaryDomain": rca.primaryDomain, "originType": rca.originType},
            severity=body.severity, priority=body.priority, detected_method="RCA",
            # Capa.primaryOwnerUserId is NOT NULL — default to the analyst when no owner is named.
            owner_id=body.ownerId or rca.analystId or user.id, actor_id=user.id, due_days=body.dueDays,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.flush()
    return {"capaId": capa.id, "capaNumber": capa.capaNumber}


# ─────────────────────────────────────────────────────────────────────
# Causal analytics (computed from RCA records)
# ─────────────────────────────────────────────────────────────────────
@router.get("/analytics/causes", response_model=S.CauseAnalyticsResponse)
async def cause_analytics(domain: str | None = None, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.READ")
    scope = await build_query_scope(db, user.id, "RCA.READ")
    allowed = await _effective_domains(db, user)
    if allowed is not None:
        if domain and domain not in allowed:
            raise HTTPException(403, "Domain outside your analytics scope")
        if not domain:
            # restrict to the single allowed domain (compliance lead → compliance only)
            if len(allowed) == 1:
                domain = next(iter(allowed))
    result = await rca_analytics.compute_cause_analytics(db, scope, domain_filter=domain)
    return result


@router.get("/analytics/cause/{sub_cause_id}", response_model=S.CauseDetailResponse)
async def cause_detail(sub_cause_id: str, domain: str | None = None,
                       user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Drill-through: the risks + RCAs behind one root-cause row (analytics drawer)."""
    await _require(db, user, "RCA.READ")
    scope = await build_query_scope(db, user.id, "RCA.READ")
    allowed = await _effective_domains(db, user)
    if allowed is not None and domain and domain not in allowed:
        raise HTTPException(403, "Domain outside your analytics scope")
    return await rca_analytics.compute_cause_detail(db, scope, sub_cause_id, domain_filter=domain)


@router.get("/analytics/recurring-drivers", response_model=list[S.CauseAnalytic])
async def recurring_drivers(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.READ")
    scope = await build_query_scope(db, user.id, "RCA.READ")
    return await rca_analytics.detect_recurring_drivers(db, scope)


@router.get("/analytics/risk/{risk_id}/contributing-causes", response_model=S.ContributingCausesResponse)
async def contributing_causes(risk_id: str, user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.READ")
    scope = await build_query_scope(db, user.id, "RCA.READ")
    return await rca_analytics.compute_contributing_causes_for_risk(db, risk_id, scope)


@router.get("/analytics/cause-to-risk-graph", response_model=S.CauseRiskGraph)
async def cause_to_risk_graph(subCauseId: str | None = None, includeChains: bool = True,
                              user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "RCA.READ")
    scope = await build_query_scope(db, user.id, "RCA.READ")
    return await rca_analytics.build_cause_to_risk_graph(
        db, scope, sub_cause_id=subCauseId, include_chains=includeChains
    )
