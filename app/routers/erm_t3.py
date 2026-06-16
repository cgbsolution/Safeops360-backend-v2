"""ERM Tier 3 router — Internal Controls · Vendor/Third-Party Risk · Insurance.

prefix /api/erm. RBAC via can(). On-demand endpoints; no scheduler. Two new CAPA
source extensions: CONTROL_DEFICIENCY, VENDOR_RISK (→ eight source types total).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.capa import Capa, CapaSourceCategory, CapaSourceType
from app.models.erm import EnterpriseRisk, RiskCategory
from app.models.erm_p2 import LegalObligation, LossEvent
from app.models.erm_p3 import BusinessProcess
from app.models.erm_t3 import (
    Control,
    ControlDeficiency,
    ControlTest,
    ControlTestPlan,
    CoverageGapAssessment,
    InsuranceClaim,
    InsurancePolicy,
    RiskControlMapping,
    VendorAssessment,
    VendorProfile,
    VendorScoringConfig,
)
from app.models.plant import Plant
from app.models.user import User
from app.schemas import erm_t3 as S
from app.services import erm_t3 as svc
from app.services import vendor_master_provider
from app.services.permissions import PermissionContext, can, get_user_role_codes

router = APIRouter(prefix="/api/erm", tags=["erm-tier3"])


def _now():
    return datetime.now(timezone.utc)


def _aware(d):
    return svc._aware(d)


async def _require(db, user, code, *, plant_id=None, record=None, record_id=None):
    res = await can(db, user.id, code, PermissionContext(plant_id=plant_id, record=record, record_id=record_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


async def _plants(db) -> dict[str, str]:
    return {r[0]: r[1] for r in (await db.execute(select(Plant.id, Plant.name))).all()}


async def _names(db, ids):
    return await svc.user_name_map(db, ids)


async def _next_code(db, model, prefix, width=4, year=False):
    n = (await db.execute(select(func.count()).select_from(model))).scalar() or 0
    return f"{prefix}-{_now().year}-{(n + 1):0{width}d}" if year else f"{prefix}-{(n + 1):0{width}d}"


async def _create_capa(db, *, source_code, plant_id, title, problem, ref_id, ref_url, ref_summary, metadata, severity, detected_method, detected_by, owner, user):
    st = (await db.execute(select(CapaSourceType).where(CapaSourceType.code == source_code))).scalar_one_or_none()
    if st is None:
        raise HTTPException(400, f"{source_code} CAPA source type not seeded.")
    cat = await db.get(CapaSourceCategory, st.categoryId)
    plant = (await db.get(Plant, plant_id)) if plant_id else (await db.execute(select(Plant).order_by(Plant.code).limit(1))).scalar_one_or_none()
    count = (await db.execute(select(func.count()).select_from(Capa).where(Capa.plantId == plant.id).where(Capa.sourceCategoryId == st.categoryId))).scalar() or 0
    capa = Capa(
        capaNumber=f"CAPA-{cat.prefix if cat else source_code[:3]}-{_now().year}-{plant.code}-{(count + 1):03d}",
        title=title[:200], plantId=plant.id, sourceCategoryId=st.categoryId, sourceTypeId=st.id, sourceTypeCode=source_code,
        sourceReferenceId=ref_id, sourceReferenceUrl=ref_url, sourceReferenceSummary=ref_summary, sourceMetadata=metadata,
        problemDescription=problem, detectionMethod=detected_method, detectedAt=_now(), detectedByUserId=detected_by,
        primaryCategory=cat.name if cat else source_code, severity=severity, priority="HIGH", state="ACTIONS_PLANNED",
        stateChangedAt=_now(), closureTargetDate=_now() + timedelta(days=90), raisedByUserId=user.id,
        primaryOwnerUserId=owner, createdByUserId=user.id,
    )
    db.add(capa)
    return capa


# ════════════════════════════════════════════════════════════════════════════
# INTERNAL CONTROLS
# ════════════════════════════════════════════════════════════════════════════
async def _serialise_control(db, c, plants, names) -> S.ControlListItem:
    open_def = await svc.open_deficiency_count(db, c.id)
    maps = (await db.execute(select(RiskControlMapping).where(RiskControlMapping.controlId == c.id))).scalars().all()
    return S.ControlListItem(
        id=c.id, controlCode=c.controlCode, name=c.name, controlType=c.controlType, nature=c.nature, frequency=c.frequency,
        category=c.category, controlOwnerId=c.controlOwnerId, controlOwnerName=names.get(c.controlOwnerId),
        siteId=c.siteId, siteName=plants.get(c.siteId) if c.siteId else "Corporate", isKeyControl=c.isKeyControl,
        currentDesignRating=c.currentDesignRating, currentOperatingRating=c.currentOperatingRating,
        lastTestDate=c.lastTestDate, nextTestDueDate=c.nextTestDueDate, testOverdue=svc.test_overdue(c),
        openDeficiencyCount=open_def, mappedRiskCount=sum(1 for m in maps if m.riskId), isActive=c.isActive, updatedAt=c.updatedAt,
    )


@router.get("/controls", response_model=S.ControlListResponse)
async def list_controls(category: str | None = Query(None), keyOnly: bool = Query(False), rating: str | None = Query(None),
                        siteId: str | None = Query(None), overdueOnly: bool = Query(False),
                        user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CONTROL.READ")
    rows = (await db.execute(select(Control).where(Control.isDeleted.is_(False)))).scalars().all()
    if category:
        rows = [c for c in rows if c.category == category]
    if keyOnly:
        rows = [c for c in rows if c.isKeyControl]
    if rating:
        rows = [c for c in rows if c.currentOperatingRating == rating]
    if siteId:
        rows = [c for c in rows if c.siteId == siteId]
    if overdueOnly:
        rows = [c for c in rows if svc.test_overdue(c)]
    plants = await _plants(db)
    names = await _names(db, [c.controlOwnerId for c in rows])
    items = [await _serialise_control(db, c, plants, names) for c in rows]
    cc: dict[str, int] = {}
    for it in items:
        cc[it.category] = cc.get(it.category, 0) + 1
    return S.ControlListResponse(items=items, total=len(items), categoryCounts=cc)


@router.post("/controls", response_model=S.ControlDetail, status_code=201)
async def create_control(body: S.ControlUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CONTROL.WRITE", plant_id=body.siteId)
    c = Control(
        controlCode=await _next_code(db, Control, "CTL"), name=body.name, description=body.description, controlType=body.controlType,
        nature=body.nature, frequency=body.frequency, category=body.category, controlOwnerId=body.controlOwnerId,
        processName=body.processName, siteId=body.siteId, isKeyControl=body.isKeyControl, assertions=body.assertions,
        controlDesignNotes=body.controlDesignNotes, currentDesignRating="NOT_ASSESSED", currentOperatingRating="NOT_ASSESSED",
        createdBy=user.id,
    )
    db.add(c)
    await db.commit()
    return await _build_control_detail(db, c.id, user)


@router.get("/controls/dashboard", response_model=S.ControlsDashboard)
async def controls_dashboard(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CONTROL.READ")
    controls = (await db.execute(select(Control).where(Control.isDeleted.is_(False)))).scalars().all()
    key = [c for c in controls if c.isKeyControl]
    now = _now()
    tested_cycle = [c for c in key if c.lastTestDate and _aware(c.lastTestDate) >= now - timedelta(days=365)]
    rating_dist = {"EFFECTIVE": 0, "DEFICIENT": 0, "NOT_ASSESSED": 0}
    for c in key:
        r = c.currentOperatingRating or "NOT_ASSESSED"
        rating_dist[r] = rating_dist.get(r, 0) + 1
    effective = rating_dist.get("EFFECTIVE", 0)
    assessed = effective + rating_dist.get("DEFICIENT", 0)
    defs = (await db.execute(select(ControlDeficiency).where(ControlDeficiency.isDeleted.is_(False)))).scalars().all()
    open_defs = [d for d in defs if d.status != "CLOSED"]
    def_by_sev: dict[str, int] = {}
    for d in open_defs:
        def_by_sev[d.severity] = def_by_sev.get(d.severity, 0) + 1
    mw = [d for d in defs if d.severity == "MATERIAL_WEAKNESS" and d.status != "CLOSED"]
    overdue = [c for c in key if svc.test_overdue(c, now)]
    cnames = await _names(db, [c.controlOwnerId for c in overdue])
    cmap = {c.id: c for c in controls}
    return S.ControlsDashboard(
        keyControls=len(key),
        testedThisCyclePct=round(len(tested_cycle) * 100 / len(key), 1) if key else 0.0,
        effectivePct=round(effective * 100 / assessed, 1) if assessed else 0.0,
        openDeficiencies=len(open_defs), materialWeaknesses=len(mw), overdueTests=len(overdue),
        ratingDistribution=rating_dist, deficiencyBySeverity=def_by_sev,
        overdueList=[{"controlCode": c.controlCode, "name": c.name, "owner": cnames.get(c.controlOwnerId), "nextTestDueDate": c.nextTestDueDate.isoformat() if c.nextTestDueDate else None} for c in overdue],
        unreportedMaterialWeaknesses=[{"deficiencyCode": d.deficiencyCode, "controlCode": cmap[d.controlId].controlCode if d.controlId in cmap else None, "description": d.description[:160]} for d in mw if not d.reportedToAuditCommittee],
    )


@router.get("/controls/matrix", response_model=S.RiskControlMatrix)
async def risk_control_matrix(category: str | None = Query(None), siteId: str | None = Query(None),
                              user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CONTROL.READ")
    risks = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.riskCode.startswith("ERM-")))).scalars().all()
    controls = {c.id: c for c in (await db.execute(select(Control).where(Control.isDeleted.is_(False)))).scalars().all()}
    maps = (await db.execute(select(RiskControlMapping))).scalars().all()
    by_risk: dict[str, list] = {}
    mapped_control_ids = set()
    for m in maps:
        if m.controlId in controls:
            mapped_control_ids.add(m.controlId)
        if m.riskId:
            by_risk.setdefault(m.riskId, []).append(m)
    rows = []
    for r in risks:
        rmaps = by_risk.get(r.id, [])
        cells = []
        has_primary = False
        primary_deficient = False
        for m in rmaps:
            c = controls.get(m.controlId)
            if not c:
                continue
            if m.mitigationStrength == "PRIMARY":
                has_primary = True
                if c.currentOperatingRating == "DEFICIENT":
                    primary_deficient = True
            cells.append(S.MatrixCell(controlId=c.id, controlCode=c.controlCode, name=c.name, mitigationStrength=m.mitigationStrength, operatingRating=c.currentOperatingRating))
        if siteId and r.plantId != siteId:
            continue
        rows.append(S.MatrixRow(riskId=r.id, riskCode=r.riskCode, title=r.title, residualBand=r.residualBand, controls=cells, hasPrimaryControl=has_primary, primaryControlDeficient=primary_deficient))
    orphans = [{"controlId": c.id, "controlCode": c.controlCode, "name": c.name} for cid, c in controls.items() if cid not in mapped_control_ids]
    return S.RiskControlMatrix(rows=rows, orphanControls=orphans)


@router.get("/controls/deficiencies", response_model=S.DeficiencyListResponse)
async def list_deficiencies(severity: str | None = Query(None), dstatus: str | None = Query(None, alias="status"),
                            user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CONTROL.READ")
    defs = (await db.execute(select(ControlDeficiency).where(ControlDeficiency.isDeleted.is_(False)))).scalars().all()
    if severity:
        defs = [d for d in defs if d.severity == severity]
    if dstatus:
        defs = [d for d in defs if d.status == dstatus]
    controls = {c.id: c for c in (await db.execute(select(Control))).scalars().all()}
    out = []
    sev_counts: dict[str, int] = {}
    for d in defs:
        c = controls.get(d.controlId)
        out.append(await _serialise_deficiency(db, d, c))
        sev_counts[d.severity] = sev_counts.get(d.severity, 0) + 1
    return S.DeficiencyListResponse(items=out, total=len(out), severityCounts=sev_counts)


async def _serialise_deficiency(db, d, control) -> S.DeficiencyOut:
    return S.DeficiencyOut(
        id=d.id, deficiencyCode=d.deficiencyCode, controlId=d.controlId,
        controlCode=control.controlCode if control else None, controlName=control.name if control else None,
        sourceTestId=d.sourceTestId, severity=d.severity, description=d.description, rootCause=d.rootCause,
        remediationCapaId=d.remediationCapaId, remediationCapaState=await svc.deficiency_capa_state(db, d.remediationCapaId),
        status=d.status, identifiedRiskImpact=d.identifiedRiskImpact, reportedToAuditCommittee=d.reportedToAuditCommittee,
        auditCommitteeReference=d.auditCommitteeReference,
        ageDays=(_now() - _aware(d.createdAt)).days if d.createdAt else 0, createdAt=d.createdAt,
    )


@router.get("/controls/{cid}", response_model=S.ControlDetail)
async def get_control(cid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CONTROL.READ")
    return await _build_control_detail(db, cid, user)


@router.patch("/controls/{cid}", response_model=S.ControlDetail)
async def update_control(cid: str, body: S.ControlUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    c = await db.get(Control, cid)
    if not c or c.isDeleted:
        raise HTTPException(404, "Control not found")
    await _require(db, user, "CONTROL.WRITE", plant_id=c.siteId)
    for f in ("name", "description", "controlType", "nature", "frequency", "category", "controlOwnerId", "processName", "siteId", "isKeyControl", "assertions", "controlDesignNotes"):
        setattr(c, f, getattr(body, f))
    c.updatedBy = user.id
    await db.commit()
    return await _build_control_detail(db, cid, user)


@router.post("/controls/{cid}/mappings", response_model=S.MappingOut, status_code=201)
async def add_mapping(cid: str, body: S.MappingUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    c = await db.get(Control, cid)
    if not c or c.isDeleted:
        raise HTTPException(404, "Control not found")
    await _require(db, user, "CONTROL.WRITE", plant_id=c.siteId)
    targets = [bool(body.riskId), bool(body.processId), bool(body.obligationId)]
    if sum(targets) != 1:
        raise HTTPException(400, "Map to exactly one target: a risk, a process, or an obligation.")
    m = RiskControlMapping(controlId=cid, riskId=body.riskId, processId=body.processId, obligationId=body.obligationId,
                           mitigationStrength=body.mitigationStrength, coverageNotes=body.coverageNotes, createdBy=user.id)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return await _serialise_mapping(db, m)


@router.delete("/controls/mappings/{mid}")
async def delete_mapping(mid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    m = await db.get(RiskControlMapping, mid)
    if not m:
        raise HTTPException(404, "Mapping not found")
    c = await db.get(Control, m.controlId)
    await _require(db, user, "CONTROL.WRITE", plant_id=c.siteId if c else None)
    await db.delete(m)
    await db.commit()
    return {"ok": True}


async def _serialise_mapping(db, m) -> S.MappingOut:
    target_type, code, label = "RISK", None, None
    if m.riskId:
        r = await db.get(EnterpriseRisk, m.riskId)
        target_type, code, label = "RISK", (r.riskCode if r else None), (r.title if r else None)
    elif m.processId:
        p = await db.get(BusinessProcess, m.processId)
        target_type, code, label = "PROCESS", (p.processCode if p else None), (p.name if p else None)
    elif m.obligationId:
        o = await db.get(LegalObligation, m.obligationId)
        target_type, code, label = "OBLIGATION", (o.obligationCode if o else None), (o.title if o else None)
    return S.MappingOut(id=m.id, controlId=m.controlId, riskId=m.riskId, processId=m.processId, obligationId=m.obligationId,
                        mitigationStrength=m.mitigationStrength, coverageNotes=m.coverageNotes, targetType=target_type, targetCode=code, targetLabel=label)


@router.post("/controls/{cid}/test-plans", response_model=S.TestPlanOut, status_code=201)
async def add_test_plan(cid: str, body: S.TestPlanUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CONTROL.TEST")
    c = await db.get(Control, cid)
    if not c or c.isDeleted:
        raise HTTPException(404, "Control not found")
    if not svc.segregation_ok(body.assignedTesterId, c.controlOwnerId):
        raise HTTPException(400, "A control cannot be tested by its owner — assign an independent tester.")
    tp = ControlTestPlan(controlId=cid, testCycleLabel=body.testCycleLabel, testMethod=body.testMethod,
                         sampleSizePlanned=body.sampleSizePlanned, testFrequencyPerYear=body.testFrequencyPerYear,
                         assignedTesterId=body.assignedTesterId, scheduledDate=body.scheduledDate, createdBy=user.id)
    db.add(tp)
    await svc.recompute_control_ratings(db, c)
    await db.commit()
    await db.refresh(tp)
    o = S.TestPlanOut.model_validate(tp)
    o.assignedTesterName = (await _names(db, [tp.assignedTesterId])).get(tp.assignedTesterId)
    return o


@router.post("/controls/{cid}/tests", response_model=S.TestOut, status_code=201)
async def record_test(cid: str, body: S.TestCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CONTROL.TEST")
    c = await db.get(Control, cid)
    if not c or c.isDeleted:
        raise HTTPException(404, "Control not found")
    # segregation: the recording tester must not be the control owner
    if not svc.segregation_ok(user.id, c.controlOwnerId):
        raise HTTPException(400, "A control cannot be tested by its owner — assign an independent tester.")
    t = ControlTest(controlId=cid, testPlanId=body.testPlanId, testType=body.testType, testDate=body.testDate, testerId=user.id,
                    method=body.method, sampleSize=body.sampleSize, exceptionsFound=body.exceptionsFound, conclusion=body.conclusion,
                    workpaperNotes=body.workpaperNotes, evidenceAttachmentIds=body.evidenceAttachmentIds, createdBy=user.id)
    db.add(t)
    await db.flush()
    # non-effective conclusion → create a deficiency
    if body.conclusion in svc.DEFICIENT_CONCLUSIONS:
        sev = body.conclusion if body.conclusion != "DEFICIENT" else "DEFICIENCY"
        d = ControlDeficiency(
            deficiencyCode=await _next_code(db, ControlDeficiency, "DEF", year=True), controlId=cid, sourceTestId=t.id, severity=sev,
            description=body.deficiencyDescription or f"{c.controlCode} test concluded {body.conclusion} ({body.exceptionsFound} exception(s)).",
            rootCause=body.deficiencyRootCause, identifiedRiskImpact=body.identifiedRiskImpact, status="OPEN",
            reportedToAuditCommittee=False, createdBy=user.id,
        )
        db.add(d)
        await db.flush()
        t.deficiencyId = d.id
    await svc.recompute_control_ratings(db, c)
    await db.commit()
    await db.refresh(t)
    o = S.TestOut.model_validate(t)
    o.testerName = (await _names(db, [user.id])).get(user.id)
    return o


@router.post("/controls/deficiencies/{did}/raise-capa")
async def raise_deficiency_capa(did: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CONTROL.DEFICIENCY")
    d = await db.get(ControlDeficiency, did)
    if not d:
        raise HTTPException(404, "Deficiency not found")
    if d.remediationCapaId:
        existing = await db.get(Capa, d.remediationCapaId)
        return {"ok": True, "capaId": d.remediationCapaId, "capaNumber": existing.capaNumber if existing else None, "alreadyLinked": True}
    c = await db.get(Control, d.controlId)
    capa = await _create_capa(
        db, source_code="CONTROL_DEFICIENCY", plant_id=c.siteId if c else None,
        title=f"Remediate control deficiency: {c.name if c else d.controlId}", problem=d.description,
        ref_id=d.id, ref_url=f"/erm/controls/deficiencies", ref_summary=f"{d.deficiencyCode} — {c.controlCode if c else ''}",
        metadata={"deficiencyCode": d.deficiencyCode, "severity": d.severity}, severity="HIGH" if d.severity == "MATERIAL_WEAKNESS" else "MODERATE",
        detected_method="CONTROL_TEST", detected_by=user.id, owner=(c.controlOwnerId if c else user.id), user=user,
    )
    await db.flush()
    d.remediationCapaId = capa.id
    d.status = "REMEDIATION_ACTIVE"
    await db.commit()
    return {"ok": True, "capaId": capa.id, "capaNumber": capa.capaNumber}


@router.patch("/controls/deficiencies/{did}", response_model=S.DeficiencyOut)
async def update_deficiency(did: str, dstatus: str = Query(..., alias="status"), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "CONTROL.DEFICIENCY")
    d = await db.get(ControlDeficiency, did)
    if not d:
        raise HTTPException(404, "Deficiency not found")
    if dstatus not in ("OPEN", "REMEDIATION_ACTIVE", "RETESTING", "CLOSED"):
        raise HTTPException(422, "Invalid status")
    # leaving OPEN for SIGNIFICANT_DEFICIENCY+ requires a remediation CAPA
    if d.status == "OPEN" and dstatus != "OPEN" and svc.requires_capa(d.severity) and not d.remediationCapaId:
        raise HTTPException(400, f"A {d.severity} requires a remediation CAPA before it can progress from OPEN.")
    # CLOSED requires a passing retest after the source test
    if dstatus == "CLOSED":
        src = await db.get(ControlTest, d.sourceTestId)
        after = src.testDate if src else d.createdAt
        if not await svc.has_passing_retest_after(db, d.controlId, after):
            raise HTTPException(400, "Closure requires a later OPERATING test concluding EFFECTIVE (a passing retest).")
    d.status = dstatus
    d.updatedBy = user.id
    await db.commit()
    c = await db.get(Control, d.controlId)
    return await _serialise_deficiency(db, d, c)


@router.post("/controls/deficiencies/{did}/report", response_model=S.DeficiencyOut)
async def report_deficiency(did: str, body: S.DeficiencyReport, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # material-weakness reporting to the audit committee is CRO-only
    role_codes = await get_user_role_codes(db, user.id)
    if "CRO" not in role_codes:
        raise HTTPException(403, "Only the CRO may mark a material weakness as reported to the audit committee.")
    d = await db.get(ControlDeficiency, did)
    if not d:
        raise HTTPException(404, "Deficiency not found")
    d.reportedToAuditCommittee = True
    d.auditCommitteeReference = body.auditCommitteeReference
    d.updatedBy = user.id
    await db.commit()
    c = await db.get(Control, d.controlId)
    return await _serialise_deficiency(db, d, c)


async def _build_control_detail(db, cid, user) -> S.ControlDetail:
    c = (await db.execute(select(Control).where(Control.id == cid).execution_options(populate_existing=True))).scalar_one_or_none()
    if not c or c.isDeleted:
        raise HTTPException(404, "Control not found")
    plants = await _plants(db)
    names = await _names(db, [c.controlOwnerId])
    base = await _serialise_control(db, c, plants, names)
    maps = (await db.execute(select(RiskControlMapping).where(RiskControlMapping.controlId == cid))).scalars().all()
    plans = (await db.execute(select(ControlTestPlan).where(ControlTestPlan.controlId == cid))).scalars().all()
    tests = (await db.execute(select(ControlTest).where(ControlTest.controlId == cid).order_by(ControlTest.testDate.desc()))).scalars().all()
    defs = (await db.execute(select(ControlDeficiency).where(ControlDeficiency.controlId == cid).where(ControlDeficiency.isDeleted.is_(False)))).scalars().all()
    tnames = await _names(db, [t.testerId for t in tests] + [p.assignedTesterId for p in plans])
    plan_out = []
    for p in plans:
        po = S.TestPlanOut.model_validate(p)
        po.assignedTesterName = tnames.get(p.assignedTesterId)
        plan_out.append(po)
    test_out = []
    for t in tests:
        to = S.TestOut.model_validate(t)
        to.testerName = tnames.get(t.testerId)
        test_out.append(to)
    return S.ControlDetail(
        **base.model_dump(), description=c.description, assertions=c.assertions or [], controlDesignNotes=c.controlDesignNotes,
        processName=c.processName, mappings=[await _serialise_mapping(db, m) for m in maps], testPlans=plan_out, tests=test_out,
        deficiencies=[await _serialise_deficiency(db, d, c) for d in defs], createdAt=c.createdAt,
    )


# ════════════════════════════════════════════════════════════════════════════
# VENDOR / THIRD-PARTY RISK
# ════════════════════════════════════════════════════════════════════════════
async def _scoring_config(db, lens) -> VendorScoringConfig | None:
    return (await db.execute(select(VendorScoringConfig).where(VendorScoringConfig.lens == lens))).scalar_one_or_none()


def _serialise_vendor_base(v, names) -> S.VendorListItem:
    return S.VendorListItem(
        id=v.id, vendorCode=v.vendorCode, masterDataRef=v.masterDataRef, legalName=v.legalName, category=v.category,
        criticality=v.criticality, tier=v.tier, relationshipOwnerId=v.relationshipOwnerId, relationshipOwnerName=names.get(v.relationshipOwnerId),
        annualSpendInr=v.annualSpendInr, isSingleSource=v.isSingleSource, onboardingStatus=v.onboardingStatus,
        currentRiskScore=v.currentRiskScore, currentRiskBand=v.currentRiskBand, currentEsgScore=v.currentEsgScore, currentEsgBand=v.currentEsgBand,
        nextReviewDate=v.nextReviewDate, reviewOverdue=svc.vendor_review_overdue(v), isActive=v.isActive, updatedAt=v.updatedAt,
    )


@router.get("/vendors", response_model=S.VendorListResponse)
async def list_vendors(criticality: str | None = Query(None), riskBand: str | None = Query(None), esgBand: str | None = Query(None),
                       singleSource: bool = Query(False), vstatus: str | None = Query(None, alias="status"),
                       user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "VENDOR.READ")
    rows = (await db.execute(select(VendorProfile).where(VendorProfile.isDeleted.is_(False)))).scalars().all()
    if criticality:
        rows = [v for v in rows if v.criticality == criticality]
    if riskBand:
        rows = [v for v in rows if v.currentRiskBand == riskBand]
    if esgBand:
        rows = [v for v in rows if v.currentEsgBand == esgBand]
    if singleSource:
        rows = [v for v in rows if v.isSingleSource]
    if vstatus:
        rows = [v for v in rows if v.onboardingStatus == vstatus]
    names = await _names(db, [v.relationshipOwnerId for v in rows])
    items = [_serialise_vendor_base(v, names) for v in rows]
    rb: dict[str, int] = {}
    eb: dict[str, int] = {}
    for v in rows:
        if v.currentRiskBand:
            rb[v.currentRiskBand] = rb.get(v.currentRiskBand, 0) + 1
        if v.currentEsgBand:
            eb[v.currentEsgBand] = eb.get(v.currentEsgBand, 0) + 1
    return S.VendorListResponse(items=items, total=len(items), riskBandCounts=rb, esgBandCounts=eb)


@router.post("/vendors", response_model=S.VendorDetail, status_code=201)
async def create_vendor(body: S.VendorUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "VENDOR.WRITE")
    code = body.masterDataRef if (body.masterDataRef and await vendor_master_provider.lookup(db, body.masterDataRef)) else await _next_code(db, VendorProfile, "VEN")
    v = VendorProfile(
        vendorCode=code, masterDataRef=body.masterDataRef, legalName=body.legalName, category=body.category, criticality=body.criticality,
        tier=body.tier, siteScope=body.siteScope, relationshipOwnerId=body.relationshipOwnerId, annualSpendInr=body.annualSpendInr,
        isSingleSource=body.isSingleSource, linkedProcessIds=body.linkedProcessIds, linkedRiskIds=body.linkedRiskIds,
        onboardingStatus="PROSPECT", createdBy=user.id,
    )
    db.add(v)
    await db.commit()
    return await _build_vendor_detail(db, v.id, user)


@router.get("/vendors/dashboard", response_model=S.VendorDashboard)
async def vendor_dashboard(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "VENDOR.READ")
    vendors = [v for v in (await db.execute(select(VendorProfile).where(VendorProfile.isDeleted.is_(False)))).scalars().all() if v.isActive]
    rb: dict[str, int] = {}
    eb: dict[str, int] = {}
    pipeline: dict[str, int] = {}
    for v in vendors:
        if v.currentRiskBand:
            rb[v.currentRiskBand] = rb.get(v.currentRiskBand, 0) + 1
        if v.currentEsgBand:
            eb[v.currentEsgBand] = eb.get(v.currentEsgBand, 0) + 1
        pipeline[v.onboardingStatus] = pipeline.get(v.onboardingStatus, 0) + 1
    total_spend = sum(v.annualSpendInr or 0 for v in vendors)
    lagging_spend = sum(v.annualSpendInr or 0 for v in vendors if v.currentEsgBand == "LAGGING")
    return S.VendorDashboard(
        activeVendors=len(vendors),
        strategicCritical=sum(1 for v in vendors if v.criticality in ("STRATEGIC", "CRITICAL")),
        highCriticalRisk=sum(1 for v in vendors if v.currentRiskBand in ("HIGH", "CRITICAL")),
        laggingEsg=sum(1 for v in vendors if v.currentEsgBand == "LAGGING"),
        singleSource=sum(1 for v in vendors if v.isSingleSource),
        overdueReviews=sum(1 for v in vendors if svc.vendor_review_overdue(v)),
        riskBandDistribution=rb, esgBandDistribution=eb,
        spendWeightedLaggingPct=round(lagging_spend * 100 / total_spend, 1) if total_spend else 0.0,
        onboardingPipeline=pipeline,
    )


@router.get("/vendors/esg-portfolio", response_model=S.EsgPortfolio)
async def esg_portfolio(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "VENDOR.READ")
    vendors = [v for v in (await db.execute(select(VendorProfile).where(VendorProfile.isDeleted.is_(False)))).scalars().all() if v.isActive]
    total = sum(v.annualSpendInr or 0 for v in vendors) or 0
    band_hex = {"LEADING": "#2E8B57", "ADEQUATE": "#7CB342", "DEVELOPING": "#E6A817", "LAGGING": "#C0392B", None: "#94a3b8"}
    by_band: dict[str, float] = {}
    by_cat: dict[str, float] = {}
    for v in vendors:
        sp = v.annualSpendInr or 0
        b = v.currentEsgBand or "NOT_ASSESSED"
        by_band[b] = by_band.get(b, 0) + sp
        by_cat[v.category] = by_cat.get(v.category, 0) + sp
    lagging = sum(sp for b, sp in by_band.items() if b == "LAGGING")
    return S.EsgPortfolio(
        totalSpend=total,
        spendByBand=[{"band": b, "spend": sp, "pct": round(sp * 100 / total, 1) if total else 0, "colorHex": band_hex.get(b, "#94a3b8")} for b, sp in sorted(by_band.items())],
        spendByCategory=[{"category": c, "spend": sp, "pct": round(sp * 100 / total, 1) if total else 0} for c, sp in sorted(by_cat.items(), key=lambda x: -x[1])],
        laggingWatchlist=[{"vendorCode": v.vendorCode, "legalName": v.legalName, "category": v.category, "annualSpendInr": v.annualSpendInr, "esgScore": v.currentEsgScore} for v in vendors if v.currentEsgBand == "LAGGING"],
        laggingSpendPct=round(lagging * 100 / total, 1) if total else 0.0,
    )


@router.get("/vendors/scoring-config", response_model=list[S.ScoringConfigOut])
async def get_scoring_config(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "VENDOR.READ")
    rows = (await db.execute(select(VendorScoringConfig))).scalars().all()
    return [S.ScoringConfigOut.model_validate(r) for r in rows]


@router.get("/vendors/{vid}", response_model=S.VendorDetail)
async def get_vendor(vid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "VENDOR.READ")
    return await _build_vendor_detail(db, vid, user)


@router.patch("/vendors/{vid}", response_model=S.VendorDetail)
async def update_vendor(vid: str, body: S.VendorUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    v = await db.get(VendorProfile, vid)
    if not v or v.isDeleted:
        raise HTTPException(404, "Vendor not found")
    await _require(db, user, "VENDOR.WRITE")
    for f in ("legalName", "category", "criticality", "tier", "siteScope", "relationshipOwnerId", "annualSpendInr", "isSingleSource", "linkedProcessIds", "linkedRiskIds"):
        setattr(v, f, getattr(body, f))
    v.updatedBy = user.id
    await db.commit()
    return await _build_vendor_detail(db, vid, user)


@router.post("/vendors/{vid}/assessments", response_model=S.AssessmentOut, status_code=201)
async def add_assessment(vid: str, body: S.AssessmentCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "VENDOR.ESG" if body.lens == "ESG" else "VENDOR.WRITE")
    v = await db.get(VendorProfile, vid)
    if not v or v.isDeleted:
        raise HTTPException(404, "Vendor not found")
    cfg = await _scoring_config(db, body.lens)
    domain_scores = [d.model_dump() for d in body.domainScores]
    score = svc.compute_weighted_score(domain_scores)
    band = svc.band_for(cfg.bandThresholds if cfg else [], score)
    # CRITICAL_GAP findings require a CAPA before finalising — block here, raise via finding endpoint after save?
    # Persist findings with synthetic ids; the UI raises CAPA per CRITICAL_GAP, which finalises.
    findings = [{"id": uuid.uuid4().hex, "lens": body.lens, "severity": f.severity, "description": f.description,
                 "capaId": None, "targetCloseDate": f.targetCloseDate.isoformat() if f.targetCloseDate else None} for f in body.findings]
    # supersede prior current of this lens
    for prior in (await db.execute(select(VendorAssessment).where(VendorAssessment.vendorId == vid).where(VendorAssessment.lens == body.lens).where(VendorAssessment.isCurrent.is_(True)))).scalars().all():
        prior.isCurrent = False
    a = VendorAssessment(vendorId=vid, lens=body.lens, assessmentDate=body.assessmentDate, assessorId=user.id, method=body.method,
                         domainScores=domain_scores, weightedScore=score, band=band, summaryNotes=body.summaryNotes,
                         validUntil=body.validUntil, isCurrent=True, findings=findings, createdBy=user.id)
    db.add(a)
    await db.flush()
    await svc.recompute_vendor_scores(db, v)
    await db.commit()
    await db.refresh(a)
    o = S.AssessmentOut.model_validate(a)
    o.assessorName = (await _names(db, [user.id])).get(user.id)
    return o


@router.post("/vendors/assessments/{aid}/findings/{fid}/raise-capa")
async def raise_vendor_capa(aid: str, fid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    a = await db.get(VendorAssessment, aid)
    if not a:
        raise HTTPException(404, "Assessment not found")
    await _require(db, user, "VENDOR.ESG" if a.lens == "ESG" else "VENDOR.WRITE")
    v = await db.get(VendorProfile, a.vendorId)
    findings = list(a.findings or [])
    finding = next((f for f in findings if f.get("id") == fid), None)
    if not finding:
        raise HTTPException(404, "Finding not found")
    if finding.get("capaId"):
        existing = await db.get(Capa, finding["capaId"])
        return {"ok": True, "capaId": finding["capaId"], "capaNumber": existing.capaNumber if existing else None, "alreadyLinked": True}
    plant_id = (v.siteScope[0] if v and v.siteScope else None)
    capa = await _create_capa(
        db, source_code="VENDOR_RISK", plant_id=plant_id,
        title=f"Vendor gap ({a.lens}): {v.legalName if v else ''}", problem=finding["description"],
        ref_id=fid, ref_url=f"/erm/vendors/{a.vendorId}", ref_summary=f"{v.vendorCode if v else ''} — {a.lens} {finding['severity']}",
        metadata={"vendorCode": v.vendorCode if v else None, "lens": a.lens, "assessmentId": aid, "findingId": fid},
        severity="HIGH" if finding["severity"] == "CRITICAL_GAP" else "MODERATE", detected_method="VENDOR_ASSESSMENT",
        detected_by=user.id, owner=(v.relationshipOwnerId if v else user.id), user=user,
    )
    await db.flush()
    finding["capaId"] = capa.id
    a.findings = findings
    await db.commit()
    return {"ok": True, "capaId": capa.id, "capaNumber": capa.capaNumber}


@router.post("/vendors/{vid}/onboarding", response_model=S.VendorDetail)
async def change_onboarding(vid: str, body: S.OnboardingChange, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "VENDOR.WRITE")
    v = await db.get(VendorProfile, vid)
    if not v or v.isDeleted:
        raise HTTPException(404, "Vendor not found")
    if body.onboardingStatus == "APPROVED" and v.criticality in ("STRATEGIC", "CRITICAL"):
        gaps = svc.open_critical_gaps((await db.execute(select(VendorAssessment).where(VendorAssessment.vendorId == vid).where(VendorAssessment.isDeleted.is_(False)))).scalars().all())
        if gaps > 0:
            raise HTTPException(400, f"This {v.criticality} vendor has {gaps} open CRITICAL_GAP finding(s). Route as CONDITIONAL (CRO sign-off) or close the gaps first.")
    if body.onboardingStatus == "CONDITIONAL" and v.criticality in ("STRATEGIC", "CRITICAL"):
        role_codes = await get_user_role_codes(db, user.id)
        if "CRO" not in role_codes:
            raise HTTPException(403, "Conditional approval of a strategic/critical vendor with open gaps requires CRO sign-off.")
    v.onboardingStatus = body.onboardingStatus
    v.updatedBy = user.id
    await db.commit()
    return await _build_vendor_detail(db, vid, user)


@router.post("/vendors/{vid}/raise-risk")
async def raise_vendor_risk(vid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "VENDOR.WRITE")
    v = await db.get(VendorProfile, vid)
    if not v or v.isDeleted:
        raise HTTPException(404, "Vendor not found")
    scm = (await db.execute(select(RiskCategory).where(RiskCategory.code == "SCM"))).scalar_one_or_none()
    if not scm:
        raise HTTPException(400, "SCM risk category missing")
    n = (await db.execute(select(func.count()).select_from(EnterpriseRisk))).scalar() or 0
    risk = EnterpriseRisk(
        riskCode=f"ERM-{_now().year}-{(n + 1):04d}", title=f"Third-party exposure — {v.legalName}",
        description=f"Vendor {v.vendorCode} ({v.legalName}) scored {v.currentRiskBand or '—'} on the third-party risk lens" + (" and is single-source." if v.isSingleSource else "."),
        categoryId=scm.id, orgLevel="ENTERPRISE", plantId=None, riskOwnerId=v.relationshipOwnerId, riskChampionId=user.id,
        lifecycleState="DRAFT", velocity="MODERATE", sourceType="MANUAL", identifiedDate=_now(), nextReviewDate=_now() + timedelta(days=90), createdBy=user.id,
    )
    db.add(risk)
    await db.flush()
    v.linkedRiskIds = list({*(v.linkedRiskIds or []), risk.id})
    await db.commit()
    return {"ok": True, "riskId": risk.id, "riskCode": risk.riskCode}


async def _build_vendor_detail(db, vid, user) -> S.VendorDetail:
    v = (await db.execute(select(VendorProfile).where(VendorProfile.id == vid).execution_options(populate_existing=True))).scalar_one_or_none()
    if not v or v.isDeleted:
        raise HTTPException(404, "Vendor not found")
    names = await _names(db, [v.relationshipOwnerId])
    base = _serialise_vendor_base(v, names)
    assessments = (await db.execute(select(VendorAssessment).where(VendorAssessment.vendorId == vid).where(VendorAssessment.isDeleted.is_(False)).order_by(VendorAssessment.assessmentDate.desc()))).scalars().all()
    anames = await _names(db, [a.assessorId for a in assessments])
    a_out = []
    for a in assessments:
        ao = S.AssessmentOut.model_validate(a)
        ao.assessorName = anames.get(a.assessorId)
        a_out.append(ao)
    risks = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.id.in_((v.linkedRiskIds or []) or ["__none__"])))).scalars().all()
    procs = (await db.execute(select(BusinessProcess).where(BusinessProcess.id.in_((v.linkedProcessIds or []) or ["__none__"])))).scalars().all()
    return S.VendorDetail(
        **base.model_dump(), siteScope=v.siteScope or [], linkedProcessIds=v.linkedProcessIds or [], linkedRiskIds=v.linkedRiskIds or [],
        linkedRisks=[{"id": r.id, "riskCode": r.riskCode, "title": r.title, "residualBand": r.residualBand} for r in risks],
        linkedProcesses=[{"id": p.id, "processCode": p.processCode, "name": p.name, "criticality": p.criticality} for p in procs],
        assessments=a_out, createdAt=v.createdAt,
    )


# ════════════════════════════════════════════════════════════════════════════
# INSURANCE & RISK TRANSFER
# ════════════════════════════════════════════════════════════════════════════
async def _serialise_policy(db, p, names) -> S.PolicyListItem:
    n_open, _ = await svc.open_claims_value(db, p.id)
    return S.PolicyListItem(
        id=p.id, policyCode=p.policyCode, policyName=p.policyName, policyType=p.policyType, insurerName=p.insurerName,
        policyNumber=p.policyNumber, sumInsuredInr=p.sumInsuredInr, premiumAnnualInr=p.premiumAnnualInr,
        coverageEndDate=p.coverageEndDate, status=svc.policy_status(p), daysToExpiry=svc.days_to_expiry(p),
        coveredRiskCount=len(p.coveredRiskIds or []), openClaimCount=n_open, ownerId=p.ownerId, ownerName=names.get(p.ownerId),
        isActive=p.isActive, updatedAt=p.updatedAt,
    )


@router.get("/insurance/policies", response_model=S.PolicyListResponse)
async def list_policies(policyType: str | None = Query(None), pstatus: str | None = Query(None, alias="status"),
                        user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "INSURANCE.READ")
    rows = (await db.execute(select(InsurancePolicy).where(InsurancePolicy.isDeleted.is_(False)))).scalars().all()
    if policyType:
        rows = [p for p in rows if p.policyType == policyType]
    names = await _names(db, [p.ownerId for p in rows])
    items = [await _serialise_policy(db, p, names) for p in rows]
    if pstatus:
        items = [it for it in items if it.status == pstatus]
    sc: dict[str, int] = {}
    for it in items:
        sc[it.status] = sc.get(it.status, 0) + 1
    return S.PolicyListResponse(items=items, total=len(items), statusCounts=sc)


@router.post("/insurance/policies", response_model=S.PolicyDetail, status_code=201)
async def create_policy(body: S.PolicyUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "INSURANCE.WRITE")
    if body.coverageEndDate <= body.coverageStartDate:
        raise HTTPException(400, "Coverage end date must be after the start date.")
    p = InsurancePolicy(
        policyCode=await _next_code(db, InsurancePolicy, "POL"), policyName=body.policyName, policyType=body.policyType,
        insurerName=body.insurerName, brokerName=body.brokerName, policyNumber=body.policyNumber, siteScope=body.siteScope,
        sumInsuredInr=body.sumInsuredInr, premiumAnnualInr=body.premiumAnnualInr, deductibleInr=body.deductibleInr,
        coverageStartDate=body.coverageStartDate, coverageEndDate=body.coverageEndDate, renewalLeadDays=body.renewalLeadDays,
        status="ACTIVE", keyExclusions=body.keyExclusions, coveredRiskIds=body.coveredRiskIds, coveredProcessIds=body.coveredProcessIds,
        ownerId=body.ownerId, createdBy=user.id,
    )
    db.add(p)
    await db.commit()
    return await _build_policy_detail(db, p.id, user)


@router.get("/insurance/dashboard", response_model=S.InsuranceDashboard)
async def insurance_dashboard(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "INSURANCE.READ")
    policies = [p for p in (await db.execute(select(InsurancePolicy).where(InsurancePolicy.isDeleted.is_(False)))).scalars().all()]
    active = [p for p in policies if svc.policy_status(p) not in ("EXPIRED", "LAPSED")]
    now = _now()
    expiring = [p for p in policies if svc.policy_status(p) == "EXPIRING_SOON"]
    claims = (await db.execute(select(InsuranceClaim).where(InsuranceClaim.isDeleted.is_(False)))).scalars().all()
    open_states = ("INTIMATED", "SURVEYOR_APPOINTED", "UNDER_ASSESSMENT", "APPROVED", "PARTIALLY_SETTLED")
    open_claims = [c for c in claims if c.status in open_states]
    # uncovered critical risks = HIGH/CRITICAL risks not in any policy's coveredRiskIds
    crit_risks = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.residualBand.in_(("HIGH", "CRITICAL"))))).scalars().all()
    covered_ids = {rid for p in policies for rid in (p.coveredRiskIds or [])}
    uncovered = [r for r in crit_risks if r.id not in covered_ids]
    pcodes = {c.policyId for c in open_claims}
    pmap = {p.id: p for p in policies}
    cov_type: dict[str, float] = {}
    for p in active:
        cov_type[p.policyType] = cov_type.get(p.policyType, 0) + (p.sumInsuredInr or 0)
    return S.InsuranceDashboard(
        activePolicies=len(active), totalSumInsured=sum(p.sumInsuredInr or 0 for p in active),
        annualPremium=sum(p.premiumAnnualInr or 0 for p in active), expiringSoon=len(expiring),
        openClaimsValue=sum(c.claimedAmountInr or 0 for c in open_claims), uncoveredCriticalRisks=len(uncovered),
        renewalCalendar=sorted([{"policyCode": p.policyCode, "policyName": p.policyName, "coverageEndDate": p.coverageEndDate.isoformat(), "daysToExpiry": svc.days_to_expiry(p, now), "status": svc.policy_status(p)} for p in policies if svc.days_to_expiry(p, now) is not None and svc.days_to_expiry(p, now) <= 90], key=lambda x: x["daysToExpiry"]),
        coverageByType=[{"policyType": t, "sumInsured": s} for t, s in sorted(cov_type.items(), key=lambda x: -x[1])],
        openClaims=[{"claimCode": c.claimCode, "policyCode": pmap[c.policyId].policyCode if c.policyId in pmap else None, "claimedAmountInr": c.claimedAmountInr, "status": c.status} for c in open_claims],
    )


@router.post("/insurance/claims", response_model=S.ClaimOut, status_code=201)
async def create_claim(body: S.ClaimCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "INSURANCE.CLAIM")
    p = await db.get(InsurancePolicy, body.policyId)
    if not p:
        raise HTTPException(404, "Policy not found")
    c = InsuranceClaim(claimCode=await _next_code(db, InsuranceClaim, "CLM", year=True), policyId=body.policyId, lossEventId=body.lossEventId,
                       claimDate=body.claimDate, description=body.description, claimedAmountInr=body.claimedAmountInr, status="INTIMATED", createdBy=user.id)
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return await _serialise_claim(db, c)


@router.patch("/insurance/claims/{clid}", response_model=S.ClaimOut)
async def update_claim(clid: str, body: S.ClaimUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "INSURANCE.CLAIM")
    c = await db.get(InsuranceClaim, clid)
    if not c:
        raise HTTPException(404, "Claim not found")
    c.status = body.status
    if body.settledAmountInr is not None:
        c.settledAmountInr = body.settledAmountInr
    if body.settlementDate is not None:
        c.settlementDate = body.settlementDate
    if body.remarks is not None:
        c.remarks = body.remarks
    if body.status == "SETTLED" and c.settledAmountInr is None:
        raise HTTPException(400, "Settled claims require a settled amount.")
    await db.commit()
    await db.refresh(c)
    return await _serialise_claim(db, c)


@router.post("/insurance/claims/{clid}/reconcile-loss")
async def reconcile_claim_loss(clid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Write the settled amount as recovery on the linked loss event (human-confirmed; never silent)."""
    await _require(db, user, "INSURANCE.CLAIM")
    c = await db.get(InsuranceClaim, clid)
    if not c:
        raise HTTPException(404, "Claim not found")
    if not c.lossEventId:
        raise HTTPException(400, "Claim is not linked to a loss event.")
    if c.settledAmountInr is None:
        raise HTTPException(400, "Claim has no settled amount to reconcile.")
    le = await db.get(LossEvent, c.lossEventId)
    if not le:
        raise HTTPException(404, "Linked loss event not found.")
    le.recoveredInr = c.settledAmountInr
    le.netLossInr = (le.grossLossInr or 0) - (le.recoveredInr or 0)
    await db.commit()
    return {"ok": True, "lossEventId": le.id, "recoveredInr": le.recoveredInr, "netLossInr": le.netLossInr}


@router.get("/insurance/coverage-gap", response_model=list[S.CoverageGapOut])
async def list_coverage_gaps(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "INSURANCE.READ")
    rows = (await db.execute(select(CoverageGapAssessment).where(CoverageGapAssessment.isDeleted.is_(False)).order_by(CoverageGapAssessment.reviewDate.desc()))).scalars().all()
    names = await _names(db, [r.reviewedBy for r in rows])
    out = []
    for r in rows:
        lines = r.lines or []
        out.append(S.CoverageGapOut(
            id=r.id, assessmentCycleLabel=r.assessmentCycleLabel, reviewDate=r.reviewDate, reviewedBy=r.reviewedBy,
            reviewedByName=names.get(r.reviewedBy), lines=lines, summaryNotes=r.summaryNotes,
            uncoveredCount=sum(1 for ln in lines if ln.get("gapType") != "FULLY_COVERED"),
            totalCriticalRisks=len(lines), createdAt=r.createdAt,
        ))
    return out


@router.get("/insurance/coverage-gap/risks")
async def coverage_gap_risks(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """HIGH/CRITICAL register risks to seed a new gap assessment."""
    await _require(db, user, "INSURANCE.GAP")
    risks = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.residualBand.in_(("HIGH", "CRITICAL"))))).scalars().all()
    return [{"riskId": r.id, "riskCode": r.riskCode, "title": r.title, "residualBand": r.residualBand} for r in risks]


@router.post("/insurance/coverage-gap", response_model=S.CoverageGapOut, status_code=201)
async def create_coverage_gap(body: S.CoverageGapUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "INSURANCE.GAP")
    # UNINSURABLE_ACCEPTED requires a note
    for ln in body.lines:
        if ln.gapType == "UNINSURABLE_ACCEPTED" and not (ln.gapNotes and ln.gapNotes.strip()):
            raise HTTPException(400, "UNINSURABLE_ACCEPTED lines require a note (CRO rationale).")
    a = CoverageGapAssessment(assessmentCycleLabel=body.assessmentCycleLabel, reviewDate=body.reviewDate, reviewedBy=user.id,
                              lines=[ln.model_dump() for ln in body.lines], summaryNotes=body.summaryNotes, createdBy=user.id)
    db.add(a)
    await db.commit()
    await db.refresh(a)
    lines = a.lines or []
    return S.CoverageGapOut(id=a.id, assessmentCycleLabel=a.assessmentCycleLabel, reviewDate=a.reviewDate, reviewedBy=a.reviewedBy,
                            reviewedByName=(await _names(db, [user.id])).get(user.id), lines=lines, summaryNotes=a.summaryNotes,
                            uncoveredCount=sum(1 for ln in lines if ln.get("gapType") != "FULLY_COVERED"), totalCriticalRisks=len(lines), createdAt=a.createdAt)


@router.post("/insurance/coverage-gap/raise-transfer")
async def raise_transfer_treatment(riskId: str = Query(...), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Create a TRANSFER-strategy risk-treatment CAPA on an uncovered risk (Phase-1 mechanism)."""
    await _require(db, user, "INSURANCE.GAP")
    r = await db.get(EnterpriseRisk, riskId)
    if not r:
        raise HTTPException(404, "Risk not found")
    capa = await _create_capa(
        db, source_code="RISK_TREATMENT", plant_id=r.plantId,
        title=f"Risk transfer — {r.title}", problem=f"{r.riskCode} ({r.residualBand}) is uncovered/partially covered. Evaluate insurance/risk-transfer options.",
        ref_id=r.id, ref_url=f"/erm/register/{r.id}", ref_summary=f"{r.riskCode} — transfer treatment",
        metadata={"treatmentStrategy": "TRANSFER", "expectedResidualReduction": 0, "raisedFrom": "COVERAGE_GAP"},
        severity="HIGH", detected_method="COVERAGE_GAP", detected_by=user.id, owner=r.riskOwnerId, user=user,
    )
    await db.commit()
    return {"ok": True, "capaId": capa.id, "capaNumber": capa.capaNumber}


async def _serialise_claim(db, c) -> S.ClaimOut:
    p = await db.get(InsurancePolicy, c.policyId)
    le_code = None
    if c.lossEventId:
        le = await db.get(LossEvent, c.lossEventId)
        le_code = le.eventCode if le else None
    return S.ClaimOut(id=c.id, claimCode=c.claimCode, policyId=c.policyId, policyCode=p.policyCode if p else None,
                      lossEventId=c.lossEventId, lossEventCode=le_code, claimDate=c.claimDate, description=c.description,
                      claimedAmountInr=c.claimedAmountInr, status=c.status, settledAmountInr=c.settledAmountInr,
                      settlementDate=c.settlementDate, remarks=c.remarks)


@router.get("/insurance/policies/{pid}", response_model=S.PolicyDetail)
async def get_policy(pid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "INSURANCE.READ")
    return await _build_policy_detail(db, pid, user)


@router.patch("/insurance/policies/{pid}", response_model=S.PolicyDetail)
async def update_policy(pid: str, body: S.PolicyUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(InsurancePolicy, pid)
    if not p or p.isDeleted:
        raise HTTPException(404, "Policy not found")
    await _require(db, user, "INSURANCE.WRITE")
    if body.coverageEndDate <= body.coverageStartDate:
        raise HTTPException(400, "Coverage end date must be after the start date.")
    for f in ("policyName", "policyType", "insurerName", "brokerName", "policyNumber", "siteScope", "sumInsuredInr", "premiumAnnualInr",
              "deductibleInr", "coverageStartDate", "coverageEndDate", "renewalLeadDays", "keyExclusions", "coveredRiskIds", "coveredProcessIds", "ownerId"):
        setattr(p, f, getattr(body, f))
    p.updatedBy = user.id
    await db.commit()
    return await _build_policy_detail(db, pid, user)


async def _build_policy_detail(db, pid, user) -> S.PolicyDetail:
    p = (await db.execute(select(InsurancePolicy).where(InsurancePolicy.id == pid).execution_options(populate_existing=True))).scalar_one_or_none()
    if not p or p.isDeleted:
        raise HTTPException(404, "Policy not found")
    names = await _names(db, [p.ownerId])
    base = await _serialise_policy(db, p, names)
    risks = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.id.in_((p.coveredRiskIds or []) or ["__none__"])))).scalars().all()
    procs = (await db.execute(select(BusinessProcess).where(BusinessProcess.id.in_((p.coveredProcessIds or []) or ["__none__"])))).scalars().all()
    claims = (await db.execute(select(InsuranceClaim).where(InsuranceClaim.policyId == pid).where(InsuranceClaim.isDeleted.is_(False)).order_by(InsuranceClaim.claimDate.desc()))).scalars().all()
    return S.PolicyDetail(
        **base.model_dump(), brokerName=p.brokerName, siteScope=p.siteScope or [], deductibleInr=p.deductibleInr,
        coverageStartDate=p.coverageStartDate, renewalLeadDays=p.renewalLeadDays, keyExclusions=p.keyExclusions or [],
        coveredRiskIds=p.coveredRiskIds or [], coveredProcessIds=p.coveredProcessIds or [],
        coveredRisks=[{"id": r.id, "riskCode": r.riskCode, "title": r.title, "residualBand": r.residualBand} for r in risks],
        coveredProcesses=[{"id": p2.id, "processCode": p2.processCode, "name": p2.name} for p2 in procs],
        claims=[await _serialise_claim(db, c) for c in claims], createdAt=p.createdAt,
    )


# ════════════════════════════════════════════════════════════════════════════
# Cross-cutting summary (E-01 home cards + E-11 board pack tier-3 section)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/risks/{rid}/tier3-context")
async def risk_tier3_context(rid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """E-04 — controls mitigating a risk (+ operating ratings), insurance transfer
    verdict, and linked vendors. Read with ERM.READ so the register detail can show it."""
    await _require(db, user, "ERM.READ")
    controls_out, has_primary, primary_deficient = [], False, False
    if (await can(db, user.id, "CONTROL.READ", PermissionContext())).allowed:
        maps = (await db.execute(select(RiskControlMapping).where(RiskControlMapping.riskId == rid))).scalars().all()
        for m in maps:
            c = await db.get(Control, m.controlId)
            if not c or c.isDeleted:
                continue
            if m.mitigationStrength == "PRIMARY":
                has_primary = True
                if c.currentOperatingRating == "DEFICIENT":
                    primary_deficient = True
            controls_out.append({"controlCode": c.controlCode, "name": c.name, "mitigationStrength": m.mitigationStrength, "operatingRating": c.currentOperatingRating})
    policies_out, verdict = [], "NOT_ASSESSED"
    if (await can(db, user.id, "INSURANCE.READ", PermissionContext())).allowed:
        pols = (await db.execute(select(InsurancePolicy).where(InsurancePolicy.isDeleted.is_(False)))).scalars().all()
        for p in pols:
            if rid in (p.coveredRiskIds or []):
                policies_out.append({"policyCode": p.policyCode, "policyName": p.policyName, "status": svc.policy_status(p)})
        latest_gap = (await db.execute(select(CoverageGapAssessment).where(CoverageGapAssessment.isDeleted.is_(False)).order_by(CoverageGapAssessment.reviewDate.desc()).limit(1))).scalar_one_or_none()
        if latest_gap:
            line = next((ln for ln in (latest_gap.lines or []) if ln.get("riskId") == rid), None)
            if line:
                verdict = line.get("gapType", "NOT_ASSESSED")
        elif policies_out:
            verdict = "FULLY_COVERED"
    vendors_out = []
    if (await can(db, user.id, "VENDOR.READ", PermissionContext())).allowed:
        vens = (await db.execute(select(VendorProfile).where(VendorProfile.isDeleted.is_(False)))).scalars().all()
        for v in vens:
            if rid in (v.linkedRiskIds or []):
                vendors_out.append({"vendorCode": v.vendorCode, "legalName": v.legalName, "currentRiskBand": v.currentRiskBand, "currentEsgBand": v.currentEsgBand})
    return {"controls": controls_out, "hasPrimaryControl": has_primary, "primaryControlDeficient": primary_deficient, "policies": policies_out, "coverageVerdict": verdict, "vendors": vendors_out}


@router.get("/tier3-summary")
async def tier3_summary(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    out: dict = {"controls": None, "vendor": None, "insurance": None}
    if (await can(db, user.id, "CONTROL.READ", PermissionContext())).allowed:
        out["controls"] = (await controls_dashboard(user=user, db=db)).model_dump()
    if (await can(db, user.id, "VENDOR.READ", PermissionContext())).allowed:
        out["vendor"] = (await vendor_dashboard(user=user, db=db)).model_dump()
    if (await can(db, user.id, "INSURANCE.READ", PermissionContext())).allowed:
        out["insurance"] = (await insurance_dashboard(user=user, db=db)).model_dump()
    return out
