"""CAMS — Compliance & Audit Management System router.

One engine for audits AND inspections (they differ only by engagementType +
AuditType config). All endpoints tenant(=plant-set)-scoped + RBAC-enforced.

Permission codes (seeded in seed-rbac.ts):
  CAMS.READ              view engagements / templates / findings / dashboards
  CAMS.TYPE_CONFIG       audit-type & recurrence configuration
  CAMS.TEMPLATE_AUTHOR   author / edit / clone templates
  CAMS.TEMPLATE_APPROVE  approve a template version
  CAMS.SCHEDULE          plan / schedule / reschedule engagements
  CAMS.EXECUTE           run the checklist / record + disposition findings
  CAMS.CLOSE             close an engagement (gates enforced)
  CAMS.FINDING_MANAGE    raise / disposition findings + raise CAPA (AUDIT source)
  CAMS.ANALYTICS         analytics & benchmarking
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.capa import Capa
from app.models.cams import (
    CamsAuditType,
    CamsComplianceLink,
    CamsEngagement,
    CamsFinding,
    CamsRecurrence,
    CamsResponse,
    CamsTemplate,
    CamsTemplateQuestion,
    CamsTemplateSection,
)
from app.models.user import User
from app.schemas import cams as S
from app.services import cams as svc
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/cams", tags=["cams"])


async def _require(db: AsyncSession, user: User, code: str, *, plant_id=None, record=None, record_id=None) -> None:
    res = await can(db, user.id, code, PermissionContext(plant_id=plant_id, record=record, record_id=record_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_days(dt: datetime | None) -> int:
    if not dt:
        return 0
    ref = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return max(0, (_now() - ref).days)


# ════════════════════════════════════════════════════════════════════════════
# Audit Types  (C-09)
# ════════════════════════════════════════════════════════════════════════════
async def _serialise_audit_type(db: AsyncSession, t: CamsAuditType, names: dict[str, str]) -> S.AuditTypeOut:
    count = (
        await db.execute(select(func.count()).select_from(CamsEngagement).where(CamsEngagement.auditTypeId == t.id).where(CamsEngagement.isDeleted.is_(False)))
    ).scalar() or 0
    tpl_name = None
    if t.defaultTemplateId:
        tpl = await db.get(CamsTemplate, t.defaultTemplateId)
        tpl_name = tpl.name if tpl else None
    o = S.AuditTypeOut.model_validate(t)
    o.defaultTemplateName = tpl_name
    o.engagementCount = count
    return o


@router.get("/audit-types", response_model=list[S.AuditTypeOut])
async def list_audit_types(
    engagementType: str | None = Query(None),
    activeOnly: bool = Query(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "CAMS.READ")
    stmt = select(CamsAuditType).where(CamsAuditType.isDeleted.is_(False))
    if engagementType:
        stmt = stmt.where(CamsAuditType.engagementType == engagementType)
    if activeOnly:
        stmt = stmt.where(CamsAuditType.isActive.is_(True))
    rows = (await db.execute(stmt.order_by(CamsAuditType.name))).scalars().all()
    return [await _serialise_audit_type(db, t, {}) for t in rows]


@router.post("/audit-types", response_model=S.AuditTypeOut, status_code=201)
async def create_audit_type(body: S.AuditTypeUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.TYPE_CONFIG")
    t = CamsAuditType(
        typeCode=await svc.next_audit_type_code(db),
        name=body.name, engagementType=body.engagementType,
        defaultTemplateId=body.defaultTemplateId, defaultRecurrence=body.defaultRecurrence,
        requiresAssetRef=body.requiresAssetRef, requiresAuditorCompetency=body.requiresAuditorCompetency,
        standardRefs=body.standardRefs, isActive=body.isActive, createdBy=user.id,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return await _serialise_audit_type(db, t, {})


@router.patch("/audit-types/{type_id}", response_model=S.AuditTypeOut)
async def update_audit_type(type_id: str, body: S.AuditTypeUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.TYPE_CONFIG")
    t = await db.get(CamsAuditType, type_id)
    if not t or t.isDeleted:
        raise HTTPException(404, "Audit type not found")
    for k, v in body.model_dump().items():
        setattr(t, k, v)
    t.updatedBy = user.id
    await db.commit()
    await db.refresh(t)
    return await _serialise_audit_type(db, t, {})


@router.delete("/audit-types/{type_id}", status_code=204)
async def delete_audit_type(type_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.TYPE_CONFIG")
    t = await db.get(CamsAuditType, type_id)
    if not t or t.isDeleted:
        raise HTTPException(404, "Audit type not found")
    t.isDeleted = True
    t.isActive = False
    t.updatedBy = user.id
    await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# Templates / Checklist Engine  (C-07 / C-08)
# ════════════════════════════════════════════════════════════════════════════
async def _template_with_sections(db: AsyncSession, template_id: str) -> CamsTemplate | None:
    # populate_existing=True: the session uses expire_on_commit=False, so after a
    # mutate→commit the identity-map instance keeps its stale (pre-edit) sections
    # collection. Without this, a builder save/clone would re-serialise the OLD
    # structure. populate_existing forces the query to refresh loaded relationships.
    stmt = (
        select(CamsTemplate)
        .where(CamsTemplate.id == template_id)
        .options(selectinload(CamsTemplate.sections).selectinload(CamsTemplateSection.questions))
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _serialise_template(t: CamsTemplate, names: dict[str, str], *, with_sections: bool) -> S.TemplateOut:
    sections = sorted(t.sections, key=lambda s: s.orderIndex)
    qs = [q for s in sections for q in s.questions]
    clauses = {q.standardClauseRef for q in qs if q.standardClauseRef}
    base = S.TemplateDetail if with_sections else S.TemplateOut
    o = base.model_validate(t)
    o.approvedByName = names.get(t.approvedBy) if t.approvedBy else None
    o.ownerName = names.get(t.ownerId)
    o.sectionCount = len(sections)
    o.questionCount = len(qs)
    o.clauseCount = len(clauses)
    if with_sections:
        o.sections = [
            S.SectionOut(
                id=s.id, orderIndex=s.orderIndex, title=s.title, weightPct=s.weightPct,
                questions=[S.QuestionOut.model_validate(q) for q in sorted(s.questions, key=lambda q: q.orderIndex)],
            )
            for s in sections
        ]
    return o


@router.get("/templates", response_model=S.TemplateListResponse)
async def list_templates(
    tstatus: str | None = Query(None, alias="status"),
    engagementType: str | None = Query(None),
    standard: str | None = Query(None),
    q: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "CAMS.READ")
    stmt = (
        select(CamsTemplate)
        .where(CamsTemplate.isDeleted.is_(False))
        .options(selectinload(CamsTemplate.sections).selectinload(CamsTemplateSection.questions))
    )
    rows = (await db.execute(stmt.order_by(CamsTemplate.templateCode))).scalars().all()

    def keep(t: CamsTemplate) -> bool:
        if tstatus and t.status != tstatus:
            return False
        if engagementType and engagementType not in (t.applicableEngagementTypes or []):
            return False
        if standard and standard not in (t.standardRefs or []):
            return False
        if q and q.lower() not in f"{t.templateCode} {t.name}".lower():
            return False
        return True

    rows = [t for t in rows if keep(t)]
    names = await svc.user_name_map(db, [t.ownerId for t in rows] + [t.approvedBy for t in rows])
    items = [_serialise_template(t, names, with_sections=False) for t in rows]
    status_counts: dict[str, int] = {}
    for t in rows:
        status_counts[t.status] = status_counts.get(t.status, 0) + 1
    return S.TemplateListResponse(items=items, total=len(items), statusCounts=status_counts)


@router.get("/templates/{template_id}", response_model=S.TemplateDetail)
async def get_template(template_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.READ")
    t = await _template_with_sections(db, template_id)
    if not t or t.isDeleted:
        raise HTTPException(404, "Template not found")
    names = await svc.user_name_map(db, [t.ownerId, t.approvedBy])
    return _serialise_template(t, names, with_sections=True)


@router.post("/templates", response_model=S.TemplateDetail, status_code=201)
async def create_template(body: S.TemplateCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.TEMPLATE_AUTHOR")
    t = CamsTemplate(
        templateCode=await svc.next_template_code(db),
        name=body.name, description=body.description,
        applicableEngagementTypes=body.applicableEngagementTypes, standardRefs=body.standardRefs,
        version=1, status="DRAFT", scoringConfig=body.scoringConfig.model_dump(),
        ownerId=body.ownerId or user.id, isGlobal=body.isGlobal, siteId=body.siteId, createdBy=user.id,
    )
    db.add(t)
    await db.commit()
    t = await _template_with_sections(db, t.id)
    names = await svc.user_name_map(db, [t.ownerId])
    return _serialise_template(t, names, with_sections=True)


@router.put("/templates/{template_id}", response_model=S.TemplateDetail)
async def save_template(template_id: str, body: S.TemplateSave, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Builder save. Only a DRAFT may be structurally edited (immutability of
    approved versions). Editing an APPROVED template ⇒ clone first."""
    await _require(db, user, "CAMS.TEMPLATE_AUTHOR")
    t = await _template_with_sections(db, template_id)
    if not t or t.isDeleted:
        raise HTTPException(404, "Template not found")
    if t.status not in ("DRAFT", "IN_REVIEW"):
        raise HTTPException(409, f"Template is {t.status}; clone it to make edits (approved versions are immutable).")

    if body.name is not None:
        t.name = body.name
    if body.description is not None:
        t.description = body.description
    if body.applicableEngagementTypes is not None:
        t.applicableEngagementTypes = body.applicableEngagementTypes
    if body.standardRefs is not None:
        t.standardRefs = body.standardRefs
    if body.scoringConfig is not None:
        t.scoringConfig = body.scoringConfig.model_dump()
    if body.isGlobal is not None:
        t.isGlobal = body.isGlobal
    if body.siteId is not None:
        t.siteId = body.siteId
    t.updatedBy = user.id

    if body.sections is not None:
        # Replace structure wholesale (cascade removes old sections + questions).
        for s in list(t.sections):
            await db.delete(s)
        await db.flush()
        for si, sec in enumerate(body.sections):
            section = CamsTemplateSection(templateId=t.id, orderIndex=sec.orderIndex or si, title=sec.title, weightPct=sec.weightPct)
            db.add(section)
            await db.flush()
            for qi, q in enumerate(sec.questions):
                db.add(CamsTemplateQuestion(
                    sectionId=section.id, orderIndex=q.orderIndex or qi, text=q.text,
                    questionType=q.questionType, isMandatory=q.isMandatory, standardClauseRef=q.standardClauseRef,
                    guidance=q.guidance, weight=q.weight, ncTriggersFinding=q.ncTriggersFinding,
                    evidenceRequiredOnNc=q.evidenceRequiredOnNc, options=q.options,
                ))
    await db.commit()
    t = await _template_with_sections(db, t.id)
    names = await svc.user_name_map(db, [t.ownerId, t.approvedBy])
    return _serialise_template(t, names, with_sections=True)


@router.post("/templates/{template_id}/submit", response_model=S.TemplateDetail)
async def submit_template(template_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.TEMPLATE_AUTHOR")
    t = await _template_with_sections(db, template_id)
    if not t or t.isDeleted:
        raise HTTPException(404, "Template not found")
    if t.status != "DRAFT":
        raise HTTPException(409, f"Only a DRAFT can be submitted for review (is {t.status}).")
    if not t.sections:
        raise HTTPException(400, "Add at least one section with a question before submitting.")
    t.status = "IN_REVIEW"
    t.updatedBy = user.id
    await db.commit()
    t = await _template_with_sections(db, t.id)
    return _serialise_template(t, await svc.user_name_map(db, [t.ownerId, t.approvedBy]), with_sections=True)


@router.post("/templates/{template_id}/approve", response_model=S.TemplateDetail)
async def approve_template(template_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.TEMPLATE_APPROVE")
    t = await _template_with_sections(db, template_id)
    if not t or t.isDeleted:
        raise HTTPException(404, "Template not found")
    if t.status not in ("DRAFT", "IN_REVIEW"):
        raise HTTPException(409, f"Template is already {t.status}.")
    t.status = "APPROVED"
    t.approvedBy = user.id
    t.approvedAt = _now()
    t.updatedBy = user.id
    await db.commit()
    t = await _template_with_sections(db, t.id)
    return _serialise_template(t, await svc.user_name_map(db, [t.ownerId, t.approvedBy]), with_sections=True)


@router.post("/templates/{template_id}/retire", response_model=S.TemplateDetail)
async def retire_template(template_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.TEMPLATE_APPROVE")
    t = await _template_with_sections(db, template_id)
    if not t or t.isDeleted:
        raise HTTPException(404, "Template not found")
    t.status = "RETIRED"
    t.updatedBy = user.id
    await db.commit()
    t = await _template_with_sections(db, t.id)
    return _serialise_template(t, await svc.user_name_map(db, [t.ownerId, t.approvedBy]), with_sections=True)


@router.post("/templates/{template_id}/clone", response_model=S.TemplateDetail, status_code=201)
async def clone_template(template_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Fork a new DRAFT version (version+1) from an existing template, copying
    its structure. This is how an APPROVED template is 'edited'."""
    await _require(db, user, "CAMS.TEMPLATE_AUTHOR")
    src = await _template_with_sections(db, template_id)
    if not src or src.isDeleted:
        raise HTTPException(404, "Template not found")
    clone = CamsTemplate(
        templateCode=await svc.next_template_code(db),
        name=src.name, description=src.description,
        applicableEngagementTypes=list(src.applicableEngagementTypes or []),
        standardRefs=list(src.standardRefs or []),
        version=src.version + 1, status="DRAFT", parentTemplateId=src.id,
        scoringConfig=dict(src.scoringConfig or {}), ownerId=user.id,
        isGlobal=src.isGlobal, siteId=src.siteId, createdBy=user.id,
    )
    db.add(clone)
    await db.flush()
    for s in sorted(src.sections, key=lambda x: x.orderIndex):
        ns = CamsTemplateSection(templateId=clone.id, orderIndex=s.orderIndex, title=s.title, weightPct=s.weightPct)
        db.add(ns)
        await db.flush()
        for q in sorted(s.questions, key=lambda x: x.orderIndex):
            db.add(CamsTemplateQuestion(
                sectionId=ns.id, orderIndex=q.orderIndex, text=q.text, questionType=q.questionType,
                isMandatory=q.isMandatory, standardClauseRef=q.standardClauseRef, guidance=q.guidance,
                weight=q.weight, ncTriggersFinding=q.ncTriggersFinding, evidenceRequiredOnNc=q.evidenceRequiredOnNc,
                options=q.options,
            ))
    await db.commit()
    clone = await _template_with_sections(db, clone.id)
    return _serialise_template(clone, await svc.user_name_map(db, [clone.ownerId]), with_sections=True)


@router.get("/clause-catalogue", response_model=list[S.ClauseRef])
async def clause_catalogue(standard: str | None = Query(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.READ")
    rows = svc.CLAUSE_CATALOGUE
    if standard:
        rows = [r for r in rows if r["standard"] == standard]
    return [S.ClauseRef(**r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
# Engagements  (C-04 / C-05)
# ════════════════════════════════════════════════════════════════════════════
async def _finding_rollup(db: AsyncSession, engagement_ids: list[str]) -> dict[str, dict[str, int]]:
    if not engagement_ids:
        return {}
    rows = (
        await db.execute(
            select(CamsFinding).where(CamsFinding.engagementId.in_(engagement_ids)).where(CamsFinding.isDeleted.is_(False))
        )
    ).scalars().all()
    agg: dict[str, dict[str, int]] = {}
    for f in rows:
        d = agg.setdefault(f.engagementId, {"findingCount": 0, "openFindingCount": 0, "ncCount": 0})
        d["findingCount"] += 1
        if f.status not in ("CLOSED", "ACCEPTED_RISK"):
            d["openFindingCount"] += 1
        if f.severity in ("MINOR_NC", "MAJOR_NC", "CRITICAL_NC"):
            d["ncCount"] += 1
    return agg


def _serialise_engagement(e: CamsEngagement, names: dict[str, str], plants: dict[str, str], types: dict[str, str], templates: dict[str, str], roll: dict[str, int]) -> S.EngagementOut:
    o = S.EngagementOut.model_validate(e)
    o.leadAuditorName = names.get(e.leadAuditorId)
    o.auditeeOwnerName = names.get(e.auditeeOwnerId) if e.auditeeOwnerId else None
    o.siteName = plants.get(e.siteId) if e.siteId else None
    o.auditTypeName = types.get(e.auditTypeId) if e.auditTypeId else None
    o.templateName = templates.get(e.templateId) if e.templateId else None
    o.findingCount = roll.get("findingCount", 0)
    o.openFindingCount = roll.get("openFindingCount", 0)
    o.ncCount = roll.get("ncCount", 0)
    return o


@router.get("/engagements", response_model=S.EngagementListResponse)
async def list_engagements(
    estatus: str | None = Query(None, alias="status"),
    engagementType: str | None = Query(None),
    siteId: str | None = Query(None),
    leadAuditorId: str | None = Query(None),
    sourceModule: str | None = Query(None),
    fromDate: datetime | None = Query(None),
    toDate: datetime | None = Query(None),
    q: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "CAMS.READ")
    # P1-2: scope to the actor's plants (fail-closed). Without this a user could
    # omit siteId and read every plant's engagements.
    from app.services.access_scope import build_query_scope

    scope = await build_query_scope(db, user.id, "CAMS.READ")
    stmt = scope.apply(select(CamsEngagement).where(CamsEngagement.isDeleted.is_(False)), CamsEngagement)
    if estatus:
        stmt = stmt.where(CamsEngagement.status == estatus)
    if engagementType:
        stmt = stmt.where(CamsEngagement.engagementType == engagementType)
    if siteId:
        stmt = stmt.where(CamsEngagement.siteId == siteId)
    if leadAuditorId:
        stmt = stmt.where(CamsEngagement.leadAuditorId == leadAuditorId)
    if sourceModule:
        stmt = stmt.where(CamsEngagement.sourceModule == sourceModule)
    if fromDate:
        stmt = stmt.where(CamsEngagement.plannedDate >= fromDate)
    if toDate:
        stmt = stmt.where(CamsEngagement.plannedDate <= toDate)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(CamsEngagement.engagementCode.ilike(like), CamsEngagement.title.ilike(like)))
    # Newest-created first — platform-wide register convention. `plannedDate`
    # is a schedule field, so a freshly-scheduled engagement for an earlier date
    # used to land mid-list instead of on top.
    rows = (
        await db.execute(
            stmt.order_by(CamsEngagement.createdAt.desc(), CamsEngagement.id.desc())
        )
    ).scalars().all()

    roll = await _finding_rollup(db, [e.id for e in rows])
    names = await svc.user_name_map(db, [e.leadAuditorId for e in rows] + [e.auditeeOwnerId for e in rows])
    plants = await svc.plant_name_map(db, [e.siteId for e in rows])
    type_ids = {e.auditTypeId for e in rows if e.auditTypeId}
    tpl_ids = {e.templateId for e in rows if e.templateId}
    types = {t.id: t.name for t in (await db.execute(select(CamsAuditType).where(CamsAuditType.id.in_(type_ids)))).scalars().all()} if type_ids else {}
    templates = {t.id: t.name for t in (await db.execute(select(CamsTemplate).where(CamsTemplate.id.in_(tpl_ids)))).scalars().all()} if tpl_ids else {}

    items = [_serialise_engagement(e, names, plants, types, templates, roll.get(e.id, {})) for e in rows]
    status_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for e in rows:
        status_counts[e.status] = status_counts.get(e.status, 0) + 1
        type_counts[e.engagementType] = type_counts.get(e.engagementType, 0) + 1
    return S.EngagementListResponse(items=items, total=len(items), statusCounts=status_counts, typeCounts=type_counts)


@router.get("/unified-engagements")
async def unified_engagements(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Centralized feed: Cams inspections/engagements UNION ComplianceAudit
    audits (projected to the engagement shape, status vocab translated). Drives
    the CAMS command centre + calendar. Each item carries `href` so audit rows
    route to /cams/audits and inspection rows to /cams/engagements."""
    await _require(db, user, "CAMS.READ")
    rows = (
        await db.execute(
            select(CamsEngagement)
            .where(CamsEngagement.isDeleted.is_(False))
            # Newest-created first — platform-wide register convention.
            .order_by(CamsEngagement.createdAt.desc(), CamsEngagement.id.desc())
        )
    ).scalars().all()
    roll = await _finding_rollup(db, [e.id for e in rows])
    names = await svc.user_name_map(db, [e.leadAuditorId for e in rows] + [e.auditeeOwnerId for e in rows])
    plants = await svc.plant_name_map(db, [e.siteId for e in rows])
    type_ids = {e.auditTypeId for e in rows if e.auditTypeId}
    tpl_ids = {e.templateId for e in rows if e.templateId}
    types = {t.id: t.name for t in (await db.execute(select(CamsAuditType).where(CamsAuditType.id.in_(type_ids)))).scalars().all()} if type_ids else {}
    templates = {t.id: t.name for t in (await db.execute(select(CamsTemplate).where(CamsTemplate.id.in_(tpl_ids)))).scalars().all()} if tpl_ids else {}

    cams_items: list[dict[str, Any]] = []
    for e in rows:
        d = _serialise_engagement(e, names, plants, types, templates, roll.get(e.id, {})).model_dump()
        d["href"] = f"/cams/engagements/{e.id}"
        cams_items.append(d)
    items = (await svc.audit_engagements(db)) + cams_items
    # The two sources type `plannedDate` differently — audit_engagements emits an
    # ISO string (`.isoformat()`), while the Cams `model_dump()` keeps a datetime.
    # Normalise to an ISO string in the key so a mixed list sorts without a
    # str-vs-datetime TypeError (ISO 8601 sorts lexically == chronologically).
    def _planned_key(x: dict[str, Any]) -> str:
        v = x.get("plannedDate")
        if isinstance(v, datetime):
            return v.isoformat()
        return v or ""

    items.sort(key=_planned_key, reverse=True)
    status_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for it in items:
        status_counts[it["status"]] = status_counts.get(it["status"], 0) + 1
        type_counts[it["engagementType"]] = type_counts.get(it["engagementType"], 0) + 1
    return {"items": items, "total": len(items), "statusCounts": status_counts, "typeCounts": type_counts}


async def _engagement_out(db: AsyncSession, e: CamsEngagement) -> S.EngagementOut:
    roll = (await _finding_rollup(db, [e.id])).get(e.id, {})
    names = await svc.user_name_map(db, [e.leadAuditorId, e.auditeeOwnerId])
    plants = await svc.plant_name_map(db, [e.siteId])
    types = {}
    if e.auditTypeId:
        t = await db.get(CamsAuditType, e.auditTypeId)
        types = {t.id: t.name} if t else {}
    templates = {}
    if e.templateId:
        tpl = await db.get(CamsTemplate, e.templateId)
        templates = {tpl.id: tpl.name} if tpl else {}
    return _serialise_engagement(e, names, plants, types, templates, roll)


@router.post("/engagements", response_model=S.EngagementOut, status_code=201)
async def create_engagement(body: S.EngagementCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.SCHEDULE", plant_id=body.siteId)
    e = CamsEngagement(
        engagementCode=await svc.next_engagement_code(db, body.engagementType),
        title=body.title, engagementType=body.engagementType, auditTypeId=body.auditTypeId,
        standardRefs=body.standardRefs, siteId=body.siteId, areaOrAssetRef=body.areaOrAssetRef,
        scopeStatement=body.scopeStatement, leadAuditorId=body.leadAuditorId, auditTeamIds=body.auditTeamIds,
        auditeeOwnerId=body.auditeeOwnerId, plannedDate=body.plannedDate, scheduledStart=body.scheduledStart,
        scheduledEnd=body.scheduledEnd, templateId=body.templateId, riskBasis=body.riskBasis,
        triggeringRiskId=body.triggeringRiskId, sourceModule=body.sourceModule,
        status="SCHEDULED" if body.scheduledStart else "PLANNED", createdBy=user.id,
    )
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return await _engagement_out(db, e)


@router.get("/engagements/{engagement_id}", response_model=S.EngagementOut)
async def get_engagement(engagement_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.READ")
    e = await db.get(CamsEngagement, engagement_id)
    if not e or e.isDeleted:
        raise HTTPException(404, "Engagement not found")
    return await _engagement_out(db, e)


@router.patch("/engagements/{engagement_id}", response_model=S.EngagementOut)
async def update_engagement(engagement_id: str, body: S.EngagementUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    e = await db.get(CamsEngagement, engagement_id)
    if not e or e.isDeleted:
        raise HTTPException(404, "Engagement not found")
    # Scope the permission to the engagement's plant so an OWN_PLANT lead auditor
    # can't edit (or relocate) engagements outside their site.
    await _require(db, user, "CAMS.SCHEDULE", plant_id=e.siteId)
    if body.siteId is not None and body.siteId != e.siteId:
        await _require(db, user, "CAMS.SCHEDULE", plant_id=body.siteId)
    if e.status in ("CLOSED", "CANCELLED"):
        raise HTTPException(409, f"Engagement is {e.status} and cannot be edited.")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(e, k, v)
    e.updatedBy = user.id
    await db.commit()
    await db.refresh(e)
    return await _engagement_out(db, e)


# Allowed status transitions + the permission each requires.
_TRANSITIONS: dict[str, dict[str, str]] = {
    "PLANNED": {"SCHEDULED": "CAMS.SCHEDULE", "CANCELLED": "CAMS.SCHEDULE"},
    "SCHEDULED": {"IN_PROGRESS": "CAMS.EXECUTE", "CANCELLED": "CAMS.SCHEDULE"},
    "IN_PROGRESS": {"FIELDWORK_COMPLETE": "CAMS.EXECUTE", "CANCELLED": "CAMS.SCHEDULE"},
    "FIELDWORK_COMPLETE": {"FINDINGS_REVIEW": "CAMS.CLOSE", "IN_PROGRESS": "CAMS.EXECUTE"},
    "FINDINGS_REVIEW": {"REPORT_ISSUED": "CAMS.CLOSE"},
    "REPORT_ISSUED": {"CLOSED": "CAMS.CLOSE"},
}


@router.post("/engagements/{engagement_id}/transition", response_model=S.EngagementOut)
async def transition_engagement(engagement_id: str, body: S.EngagementTransition, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    e = await db.get(CamsEngagement, engagement_id)
    if not e or e.isDeleted:
        raise HTTPException(404, "Engagement not found")
    allowed = _TRANSITIONS.get(e.status, {})
    if body.toStatus not in allowed:
        raise HTTPException(409, f"Cannot move {e.status} → {body.toStatus}.")
    await _require(db, user, allowed[body.toStatus], plant_id=e.siteId)

    if body.toStatus == "IN_PROGRESS":
        # Snapshot the template version + ensure a response container exists.
        if e.templateId:
            tpl = await db.get(CamsTemplate, e.templateId)
            if tpl is None:
                raise HTTPException(400, "Selected template no longer exists.")
            if tpl.status != "APPROVED":
                raise HTTPException(400, "Engagement template must be APPROVED before fieldwork starts.")
            e.templateVersionUsed = tpl.version
            existing = (await db.execute(select(CamsResponse).where(CamsResponse.engagementId == e.id))).scalar_one_or_none()
            if existing is None:
                db.add(CamsResponse(engagementId=e.id, templateVersionUsed=tpl.version, answers=[], sectionScores=[]))
    if body.toStatus == "CLOSED":
        blockers = await svc.engagement_close_blockers(db, e.id)
        if blockers:
            raise HTTPException(400, "Cannot close: " + " ".join(blockers))

    e.status = body.toStatus
    e.updatedBy = user.id
    await db.commit()
    await db.refresh(e)
    return await _engagement_out(db, e)


# ════════════════════════════════════════════════════════════════════════════
# Checklist runner  (C-06)
# ════════════════════════════════════════════════════════════════════════════
async def _build_runner(db: AsyncSession, e: CamsEngagement) -> S.ChecklistRunner:
    """Serialise the checklist runner for an engagement. No permission check —
    callers gate first (so an EXECUTE-only principal isn't blocked by a READ
    re-check after a successful save)."""
    if not e.templateId:
        raise HTTPException(400, "No template assigned to this engagement.")
    tpl = await _template_with_sections(db, e.templateId)
    if not tpl:
        raise HTTPException(400, "Engagement template not found.")
    resp = (await db.execute(select(CamsResponse).where(CamsResponse.engagementId == e.id))).scalar_one_or_none()
    answers_by_q = {a["questionId"]: a for a in (resp.answers if resp else [])}

    sections_out: list[dict[str, Any]] = []
    for s in sorted(tpl.sections, key=lambda x: x.orderIndex):
        qs = []
        for q in sorted(s.questions, key=lambda x: x.orderIndex):
            ans = answers_by_q.get(q.id, {})
            qs.append({
                "id": q.id, "orderIndex": q.orderIndex, "text": q.text, "questionType": q.questionType,
                "isMandatory": q.isMandatory, "standardClauseRef": q.standardClauseRef, "guidance": q.guidance,
                "weight": q.weight, "ncTriggersFinding": q.ncTriggersFinding, "evidenceRequiredOnNc": q.evidenceRequiredOnNc,
                "options": q.options, "sectionId": s.id, "sectionTitle": s.title,
                "value": ans.get("value"), "conformance": ans.get("conformance"), "note": ans.get("note", ""),
                "evidenceAttachmentIds": ans.get("evidenceAttachmentIds", []), "findingId": ans.get("findingId"),
            })
        sections_out.append({"id": s.id, "title": s.title, "weightPct": s.weightPct, "questions": qs})

    return S.ChecklistRunner(
        engagementId=e.id, engagementCode=e.engagementCode, engagementTitle=e.title, status=e.status,
        templateId=tpl.id, templateName=tpl.name, templateVersionUsed=e.templateVersionUsed,
        scoringConfig=tpl.scoringConfig or {}, sections=sections_out,
        completedBy=resp.completedBy if resp else None, completedAt=resp.completedAt if resp else None,
        scorePercent=e.scorePercent, overallResult=e.overallResult,
    )


@router.get("/engagements/{engagement_id}/checklist", response_model=S.ChecklistRunner)
async def get_checklist(engagement_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.READ")
    e = await db.get(CamsEngagement, engagement_id)
    if not e or e.isDeleted:
        raise HTTPException(404, "Engagement not found")
    return await _build_runner(db, e)


@router.put("/engagements/{engagement_id}/checklist", response_model=S.ChecklistRunner)
async def save_checklist(engagement_id: str, body: S.ChecklistSave, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    e = await db.get(CamsEngagement, engagement_id)
    if not e or e.isDeleted:
        raise HTTPException(404, "Engagement not found")
    await _require(db, user, "CAMS.EXECUTE", plant_id=e.siteId)
    if e.status not in ("IN_PROGRESS",):
        raise HTTPException(409, f"Checklist can only be saved while IN_PROGRESS (is {e.status}). Move the engagement to fieldwork first.")
    if not e.templateId:
        raise HTTPException(400, "No template assigned.")
    tpl = await _template_with_sections(db, e.templateId)
    if not tpl:
        raise HTTPException(400, "Template not found.")

    resp = (await db.execute(select(CamsResponse).where(CamsResponse.engagementId == e.id))).scalar_one_or_none()
    if resp is None:
        resp = CamsResponse(engagementId=e.id, templateVersionUsed=e.templateVersionUsed or tpl.version, answers=[], sectionScores=[])
        db.add(resp)
        await db.flush()

    # Merge incoming answers over any existing ones (preserve findingId links).
    existing = {a["questionId"]: a for a in (resp.answers or [])}
    valid_qids = {q.id for s in tpl.sections for q in s.questions}
    for a in body.answers:
        if a.questionId not in valid_qids:
            continue
        prev = existing.get(a.questionId, {})
        existing[a.questionId] = {
            "questionId": a.questionId, "value": a.value, "conformance": a.conformance,
            "evidenceAttachmentIds": a.evidenceAttachmentIds, "note": a.note,
            "ncSeverity": a.ncSeverity, "findingId": prev.get("findingId"),
        }
    answers_by_q = existing

    created = 0
    if body.complete:
        created = await svc.sync_findings_from_answers(db, e, tpl.sections, answers_by_q, actor_id=user.id)
        score = svc.compute_score(tpl.sections, answers_by_q, tpl.scoringConfig)
        resp.sectionScores = score["sectionScores"]
        resp.completedBy = user.id
        resp.completedAt = _now()
        e.scorePercent = score["scorePercent"]
        e.overallResult = score["overallResult"]
        e.conductedDate = _now()
        e.status = "FIELDWORK_COMPLETE"
        e.updatedBy = user.id

    resp.answers = list(answers_by_q.values())
    await db.commit()
    await db.refresh(e)
    return await _build_runner(db, e)


# ════════════════════════════════════════════════════════════════════════════
# Findings  (C-10)
# ════════════════════════════════════════════════════════════════════════════
async def _serialise_finding(db: AsyncSession, f: CamsFinding, names: dict[str, str], plants: dict[str, str], engagements: dict[str, CamsEngagement]) -> S.FindingOut:
    o = S.FindingOut.model_validate(f)
    eng = engagements.get(f.engagementId)
    o.engagementCode = eng.engagementCode if eng else None
    o.engagementTitle = eng.title if eng else None
    o.ownerName = names.get(f.ownerId) if f.ownerId else None
    o.siteName = plants.get(f.siteId) if f.siteId else None
    o.ageDays = _age_days(f.createdAt)
    o.capaRequired = f.severity in ("MAJOR_NC", "CRITICAL_NC")
    if f.capaId:
        capa = await db.get(Capa, f.capaId)
        if capa:
            o.capaNumber = capa.capaNumber
            o.capaState = capa.state
    return o


@router.get("/findings", response_model=S.FindingListResponse)
async def list_findings(
    severity: str | None = Query(None),
    fstatus: str | None = Query(None, alias="status"),
    standardClauseRef: str | None = Query(None),
    siteId: str | None = Query(None),
    engagementId: str | None = Query(None),
    repeatOnly: bool = Query(False),
    overdueOnly: bool = Query(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "CAMS.READ")
    stmt = select(CamsFinding).where(CamsFinding.isDeleted.is_(False))
    if severity:
        stmt = stmt.where(CamsFinding.severity == severity)
    if fstatus:
        stmt = stmt.where(CamsFinding.status == fstatus)
    if standardClauseRef:
        stmt = stmt.where(CamsFinding.standardClauseRef == standardClauseRef)
    if siteId:
        stmt = stmt.where(CamsFinding.siteId == siteId)
    if engagementId:
        stmt = stmt.where(CamsFinding.engagementId == engagementId)
    if repeatOnly:
        stmt = stmt.where(CamsFinding.isRepeatFinding.is_(True))
    rows = (await db.execute(stmt.order_by(CamsFinding.createdAt.desc()))).scalars().all()
    if overdueOnly:
        rows = [f for f in rows if f.dueDate and f.dueDate.replace(tzinfo=timezone.utc) < _now() and f.status not in ("CLOSED", "ACCEPTED_RISK")]

    eng_ids = {f.engagementId for f in rows}
    engagements = {e.id: e for e in (await db.execute(select(CamsEngagement).where(CamsEngagement.id.in_(eng_ids)))).scalars().all()} if eng_ids else {}
    names = await svc.user_name_map(db, [f.ownerId for f in rows])
    plants = await svc.plant_name_map(db, [f.siteId for f in rows])

    items = [await _serialise_finding(db, f, names, plants, engagements) for f in rows]
    sev_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    repeat = 0
    for f in rows:
        sev_counts[f.severity] = sev_counts.get(f.severity, 0) + 1
        status_counts[f.status] = status_counts.get(f.status, 0) + 1
        if f.isRepeatFinding:
            repeat += 1
    return S.FindingListResponse(items=items, total=len(items), severityCounts=sev_counts, statusCounts=status_counts, repeatCount=repeat)


@router.get("/unified-findings")
async def unified_findings(
    severity: str | None = Query(None),
    fstatus: str | None = Query(None, alias="status"),
    standardClauseRef: str | None = Query(None),
    siteId: str | None = Query(None),
    repeatOnly: bool = Query(False),
    overdueOnly: bool = Query(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Centralized findings register: Cams inspection findings UNION
    ComplianceAudit fail/partial checkpoints (projected to the finding shape).
    Filters apply to both sources; audit findings are never repeats."""
    await _require(db, user, "CAMS.READ")
    rows = (
        await db.execute(
            select(CamsFinding).where(CamsFinding.isDeleted.is_(False)).order_by(CamsFinding.createdAt.desc())
        )
    ).scalars().all()
    eng_ids = {f.engagementId for f in rows}
    engagements = {e.id: e for e in (await db.execute(select(CamsEngagement).where(CamsEngagement.id.in_(eng_ids)))).scalars().all()} if eng_ids else {}
    names = await svc.user_name_map(db, [f.ownerId for f in rows])
    plants = await svc.plant_name_map(db, [f.siteId for f in rows])
    cams_items: list[dict[str, Any]] = []
    for f in rows:
        d = (await _serialise_finding(db, f, names, plants, engagements)).model_dump()
        d["href"] = f"/cams/findings/{f.id}"
        cams_items.append(d)
    items = (await svc.audit_findings(db)) + cams_items

    def _keep(it: dict[str, Any]) -> bool:
        if severity and it["severity"] != severity:
            return False
        if fstatus and it["status"] != fstatus:
            return False
        if standardClauseRef and (it.get("standardClauseRef") or "") != standardClauseRef:
            return False
        if siteId and it.get("siteId") != siteId:
            return False
        if repeatOnly and not it.get("isRepeatFinding"):
            return False
        if overdueOnly and not (it.get("ageDays") and it["status"] not in ("CLOSED", "ACCEPTED_RISK")):
            return False
        return True

    items = [it for it in items if _keep(it)]
    sev_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    repeat = 0
    for it in items:
        sev_counts[it["severity"]] = sev_counts.get(it["severity"], 0) + 1
        status_counts[it["status"]] = status_counts.get(it["status"], 0) + 1
        if it.get("isRepeatFinding"):
            repeat += 1
    return {"items": items, "total": len(items), "severityCounts": sev_counts, "statusCounts": status_counts, "repeatCount": repeat}


@router.post("/findings", response_model=S.FindingOut, status_code=201)
async def create_finding(body: S.FindingCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.FINDING_MANAGE")
    e = await db.get(CamsEngagement, body.engagementId)
    if not e or e.isDeleted:
        raise HTTPException(404, "Engagement not found")
    f = CamsFinding(
        findingCode=await svc.next_finding_code(db),
        engagementId=e.id, sourceQuestionId=body.sourceQuestionId, title=body.title, description=body.description,
        severity=body.severity, standardClauseRef=body.standardClauseRef, siteId=e.siteId,
        areaOrAssetRef=body.areaOrAssetRef or e.areaOrAssetRef, ownerId=body.ownerId or e.auditeeOwnerId,
        dueDate=body.dueDate, status="OPEN", createdBy=user.id,
    )
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return await _serialise_finding(db, f, await svc.user_name_map(db, [f.ownerId]), await svc.plant_name_map(db, [f.siteId]), {e.id: e})


@router.get("/findings/{finding_id}", response_model=S.FindingOut)
async def get_finding(finding_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.READ")
    f = await db.get(CamsFinding, finding_id)
    if not f or f.isDeleted:
        raise HTTPException(404, "Finding not found")
    e = await db.get(CamsEngagement, f.engagementId)
    return await _serialise_finding(db, f, await svc.user_name_map(db, [f.ownerId]), await svc.plant_name_map(db, [f.siteId]), {e.id: e} if e else {})


@router.patch("/findings/{finding_id}", response_model=S.FindingOut)
async def update_finding(finding_id: str, body: S.FindingUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.FINDING_MANAGE")
    f = await db.get(CamsFinding, finding_id)
    if not f or f.isDeleted:
        raise HTTPException(404, "Finding not found")
    data = body.model_dump(exclude_unset=True)
    target_status = data.get("status")
    if target_status in ("CLOSED", "ACCEPTED_RISK"):
        if f.severity in ("MAJOR_NC", "CRITICAL_NC") and not f.capaId and target_status == "CLOSED":
            raise HTTPException(400, f"{f.findingCode} is {f.severity}: a CAPA must be raised before it can be closed.")
        f.closedBy = user.id
        f.closedAt = _now()
    for k, v in data.items():
        setattr(f, k, v)
    f.updatedBy = user.id
    await db.commit()
    await db.refresh(f)
    e = await db.get(CamsEngagement, f.engagementId)
    return await _serialise_finding(db, f, await svc.user_name_map(db, [f.ownerId]), await svc.plant_name_map(db, [f.siteId]), {e.id: e} if e else {})


@router.post("/findings/{finding_id}/raise-capa", response_model=S.FindingOut)
async def raise_capa(finding_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.FINDING_MANAGE")
    f = await db.get(CamsFinding, finding_id)
    if not f or f.isDeleted:
        raise HTTPException(404, "Finding not found")
    if f.capaId:
        raise HTTPException(409, "A CAPA has already been raised for this finding.")
    e = await db.get(CamsEngagement, f.engagementId)
    if not e:
        raise HTTPException(404, "Engagement not found")
    try:
        await svc.raise_capa_for_finding(db, f, e, user.id)
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from ex
    await db.commit()
    await db.refresh(f)
    return await _serialise_finding(db, f, await svc.user_name_map(db, [f.ownerId]), await svc.plant_name_map(db, [f.siteId]), {e.id: e})


# ════════════════════════════════════════════════════════════════════════════
# Recurrence
# ════════════════════════════════════════════════════════════════════════════
@router.get("/recurrences", response_model=list[S.RecurrenceOut])
async def list_recurrences(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.READ")
    rows = (await db.execute(select(CamsRecurrence).where(CamsRecurrence.isDeleted.is_(False)))).scalars().all()
    type_ids = {r.auditTypeId for r in rows if r.auditTypeId}
    types = {t.id: t.name for t in (await db.execute(select(CamsAuditType).where(CamsAuditType.id.in_(type_ids)))).scalars().all()} if type_ids else {}
    out = []
    for r in rows:
        o = S.RecurrenceOut.model_validate(r)
        o.auditTypeName = types.get(r.auditTypeId) if r.auditTypeId else None
        out.append(o)
    return out


@router.post("/recurrences", response_model=S.RecurrenceOut, status_code=201)
async def create_recurrence(body: S.RecurrenceUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.TYPE_CONFIG")
    r = CamsRecurrence(**body.model_dump(), createdBy=user.id)
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return S.RecurrenceOut.model_validate(r)


@router.patch("/recurrences/{rec_id}", response_model=S.RecurrenceOut)
async def update_recurrence(rec_id: str, body: S.RecurrenceUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.TYPE_CONFIG")
    r = await db.get(CamsRecurrence, rec_id)
    if not r or r.isDeleted:
        raise HTTPException(404, "Recurrence rule not found")
    for k, v in body.model_dump().items():
        setattr(r, k, v)
    r.updatedBy = user.id
    await db.commit()
    await db.refresh(r)
    return S.RecurrenceOut.model_validate(r)


@router.post("/recurrences/run")
async def run_recurrences(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.SCHEDULE")
    res = await svc.generate_due_engagements(db, actor_id=user.id)
    await db.commit()
    return res


# ════════════════════════════════════════════════════════════════════════════
# Analytics & Benchmarking  (C-13)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/analytics", response_model=S.AnalyticsOut)
async def analytics(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.ANALYTICS")
    return S.AnalyticsOut(**await svc.compute_analytics(db))


# ════════════════════════════════════════════════════════════════════════════
# Compliance Tracker  (C-12)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/compliance", response_model=S.ComplianceTrackerOut)
async def compliance_tracker(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.READ")
    return S.ComplianceTrackerOut(**await svc.compute_compliance(db))


@router.post("/compliance/links", response_model=S.ComplianceLinkOut, status_code=201)
async def create_compliance_link(body: S.ComplianceLinkCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.FINDING_MANAGE")
    if not body.engagementId and not body.findingId:
        raise HTTPException(400, "A compliance link needs an engagement or a finding.")
    link = CamsComplianceLink(
        engagementId=body.engagementId, findingId=body.findingId, obligationId=body.obligationId,
        linkType=body.linkType, notes=body.notes, createdBy=user.id,
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)
    eng = await db.get(CamsEngagement, link.engagementId) if link.engagementId else None
    fnd = await db.get(CamsFinding, link.findingId) if link.findingId else None
    o = S.ComplianceLinkOut.model_validate(link)
    o.engagementCode = eng.engagementCode if eng else None
    o.findingCode = fnd.findingCode if fnd else None
    return o


@router.delete("/compliance/links/{link_id}", status_code=204)
async def delete_compliance_link(link_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CAMS.FINDING_MANAGE")
    link = await db.get(CamsComplianceLink, link_id)
    if not link or link.isDeleted:
        raise HTTPException(404, "Link not found")
    link.isDeleted = True
    await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# CAPA — surfaced AUDIT-source view  (C-14)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/capa", response_model=S.AuditCapaListResponse)
async def audit_capas(
    cstate: str | None = Query(None, alias="state"),
    overdueOnly: bool = Query(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "CAMS.READ")
    audit_codes = ("AUDIT_INTERNAL", "AUDIT_EXTERNAL", "AUDIT_REGULATORY")
    rows = (await db.execute(select(Capa).where(Capa.sourceTypeCode.in_(audit_codes)))).scalars().all()
    names = await svc.user_name_map(db, [c.primaryOwnerUserId for c in rows])
    fids = [c.sourceReferenceId for c in rows if c.sourceReferenceId]
    finds = {f.id: f for f in (await db.execute(select(CamsFinding).where(CamsFinding.id.in_(fids)))).scalars().all()} if fids else {}
    eng_ids = {f.engagementId for f in finds.values()}
    engs = {e.id: e for e in (await db.execute(select(CamsEngagement).where(CamsEngagement.id.in_(eng_ids)))).scalars().all()} if eng_ids else {}

    # A CAPA from the ComplianceAudit engine points sourceReferenceId at an
    # AuditCheckpointResponse (not a CamsFinding). Resolve those too so audit
    # CAPAs show their checkpoint code + audit number (not blank). The remaining
    # unresolved ids are the audit-checkpoint ones.
    acr_ids = [fid for fid in fids if fid not in finds]
    acr = {r.id: r for r in (await db.execute(
        select(AuditCheckpointResponse).where(AuditCheckpointResponse.id.in_(acr_ids)))).scalars().all()} if acr_ids else {}
    acr_audit_ids = {r.auditId for r in acr.values()}
    acr_audits = {a.id: a for a in (await db.execute(
        select(ComplianceAudit).where(ComplianceAudit.id.in_(acr_audit_ids)))).scalars().all()} if acr_audit_ids else {}

    items = []
    state_counts: dict[str, int] = {}
    overdue_n = open_n = 0
    closed_states = ("CLOSED", "CLOSED_RECURRED", "CANCELLED", "REJECTED")
    for c in rows:
        o = S.AuditCapaOut.model_validate(c)
        o.primaryOwnerName = names.get(c.primaryOwnerUserId)
        fnd = finds.get(c.sourceReferenceId) if c.sourceReferenceId else None
        cp = acr.get(c.sourceReferenceId) if c.sourceReferenceId else None
        if fnd:
            o.findingCode = fnd.findingCode
            eng = engs.get(fnd.engagementId)
            o.engagementCode = eng.engagementCode if eng else None
        elif cp:
            o.findingCode = cp.checkpointCode
            a = acr_audits.get(cp.auditId)
            o.engagementCode = a.auditNumber if a else None
        is_open = c.state not in closed_states
        if is_open:
            open_n += 1
        if is_open and c.closureTargetDate:
            ct = c.closureTargetDate if c.closureTargetDate.tzinfo else c.closureTargetDate.replace(tzinfo=timezone.utc)
            if ct < _now():
                o.overdueDays = (_now() - ct).days
                overdue_n += 1
        state_counts[c.state] = state_counts.get(c.state, 0) + 1
        items.append(o)
    if cstate:
        items = [i for i in items if i.state == cstate]
    if overdueOnly:
        items = [i for i in items if i.overdueDays > 0]
    items.sort(key=lambda i: (0 if i.overdueDays else 1, i.capaNumber))
    return S.AuditCapaListResponse(items=items, total=len(items), stateCounts=state_counts, overdueCount=overdue_n, openCount=open_n)


# ═════════════════════════════════════════════════════════════════════
# Analytics & Benchmarking (P2-4) — computed, not seed commentary
# ═════════════════════════════════════════════════════════════════════
@router.post("/analytics/detect-repeats")
async def analytics_detect_repeats(
    windowDays: int = Query(365, ge=30, le=1825),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """Recompute is_repeat_finding across all findings (auto-detection engine)."""
    await _require(db, user, "CAMS.READ")
    from app.services import cams_analytics as ca
    res = await ca.detect_repeat_findings(db, window_days=windowDays)
    await db.commit()
    return res


@router.get("/analytics/pareto")
async def analytics_pareto(
    dimension: str = Query("clause"), days: int = Query(365, ge=30, le=1825),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "CAMS.READ")
    from app.services import cams_analytics as ca
    from app.services.access_scope import build_query_scope
    scope = await build_query_scope(db, user.id, "CAMS.READ")
    pids = None if scope.all_plants else scope.plant_ids
    return await ca.findings_pareto(db, pids, dimension=dimension, days=days)


@router.get("/analytics/benchmarks")
async def analytics_benchmarks(
    days: int = Query(365, ge=30, le=1825),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "CAMS.READ")
    from app.services import cams_analytics as ca
    from app.services.access_scope import build_query_scope
    scope = await build_query_scope(db, user.id, "CAMS.READ")
    pids = None if scope.all_plants else scope.plant_ids
    return await ca.site_benchmarks(db, pids, days=days)


@router.get("/analytics/clause-conformance")
async def analytics_clause(
    standardRef: str | None = Query(None), days: int = Query(365, ge=30, le=1825),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "CAMS.READ")
    from app.services import cams_analytics as ca
    from app.services.access_scope import build_query_scope
    scope = await build_query_scope(db, user.id, "CAMS.READ")
    pids = None if scope.all_plants else scope.plant_ids
    return await ca.clause_analysis(db, pids, standard_ref=standardRef, days=days)
