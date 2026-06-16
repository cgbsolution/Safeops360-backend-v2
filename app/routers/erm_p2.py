"""ERM Phase 2 router — KRI / Appetite / Compliance / Loss.

Shares the /api/erm prefix with the Phase 1 router (no path collisions). All
endpoints tenant(=plant-set)-scoped + RBAC-enforced via can().

Permission codes (seeded in seed-rbac.ts):
  KRI.READ KRI.ADMIN KRI.ENTER KRI.ACK
  APPETITE.READ APPETITE.AUTHOR APPETITE.APPROVE APPETITE.DECIDE
  COMPLIANCE.READ COMPLIANCE.MANAGE COMPLIANCE.ATTEST COMPLIANCE.VERIFY COMPLIANCE.WAIVE
  LOSS.READ LOSS.CREATE LOSS.CLOSE
"""

from __future__ import annotations

import csv
import io
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.capa import Capa, CapaSourceCategory, CapaSourceType
from app.models.erm import EnterpriseRisk, RiskCategory
from app.models.erm_p2 import (
    AppetiteBreach,
    AppetiteStatement,
    ComplianceAttachment,
    ComplianceTask,
    KriBreachEvent,
    KriDefinition,
    KriReading,
    LegalObligation,
    LossEvent,
)
from app.models.plant import Plant
from app.models.user import User
from app.schemas import erm_p2 as S
from app.services import erm_metrics as metrics
from app.services import erm_p2 as svc
from app.services.permissions import PermissionContext, can, get_user_role_codes

router = APIRouter(prefix="/api/erm", tags=["erm-phase2"])


async def _require(db: AsyncSession, user: User, code: str, *, plant_id=None, record=None, record_id=None) -> None:
    res = await can(db, user.id, code, PermissionContext(plant_id=plant_id, record=record, record_id=record_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _cat_index(db: AsyncSession) -> dict[str, RiskCategory]:
    return {c.id: c for c in (await db.execute(select(RiskCategory))).scalars().all()}


async def _names(db: AsyncSession, ids) -> dict[str, str]:
    return await svc.user_name_map(db, ids)


# ════════════════════════════════════════════════════════════════════════════
# KRI
# ════════════════════════════════════════════════════════════════════════════
@router.get("/metric-catalogue", response_model=list[S.MetricCatalogEntry])
async def metric_catalogue(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "KRI.READ")
    out = []
    for entry in metrics.catalogue():
        prov = metrics.METRIC_PROVIDERS[entry["key"]]
        try:
            pv = await prov.compute(db, _now())
        except Exception:
            pv = None
        out.append(S.MetricCatalogEntry(**entry, previewValue=pv))
    return out


async def _kri_sparkline(db: AsyncSession, kri_id: str, n: int = 6) -> list[dict]:
    rows = (
        await db.execute(select(KriReading).where(KriReading.kriId == kri_id).order_by(KriReading.periodEnd.desc()).limit(n))
    ).scalars().all()
    return [{"periodLabel": r.periodLabel, "value": r.value, "status": r.status} for r in reversed(rows)]


async def _serialise_kri(db, k, cats, names, spark=True) -> S.KriOut:
    cat = cats.get(k.categoryId)
    open_b = (await db.execute(select(func.count()).select_from(KriBreachEvent).where(KriBreachEvent.kriId == k.id).where(KriBreachEvent.status != "RESOLVED"))).scalar() or 0
    o = S.KriOut.model_validate(k)
    o.categoryCode = cat.code if cat else None
    o.categoryName = cat.name if cat else None
    o.categoryColor = cat.colorHex if cat else None
    o.linkedRiskCount = len(k.linkedRiskIds or [])
    o.ownerName = names.get(k.ownerId)
    o.openBreaches = open_b
    o.sparkline = await _kri_sparkline(db, k.id) if spark else []
    return o


@router.get("/kris", response_model=S.KriListResponse)
async def list_kris(
    category: str | None = Query(None), kstatus: str | None = Query(None, alias="status"),
    feedType: str | None = Query(None), owner: str | None = Query(None),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "KRI.READ")
    role_codes = await get_user_role_codes(db, user.id)
    stmt = select(KriDefinition).where(KriDefinition.isDeleted.is_(False))
    rows = (await db.execute(stmt)).scalars().all()
    cats = await _cat_index(db)
    code_to_id = {c.code: cid for cid, c in cats.items()}
    # Plant HSE Head: only own-site OPS KRIs (scoping mirrors Phase 1 register).
    if "PLANT_HSE_HEAD" in role_codes and not any(r in role_codes for r in ("CRO", "RISK_CHAMPION", "EXECUTIVE_VIEWER", "SYSTEM_ADMIN", "ADMIN")):
        ops = code_to_id.get("OPS")
        rows = [k for k in rows if k.categoryId == ops]

    def keep(k):
        if category and k.categoryId != code_to_id.get(category):
            return False
        if kstatus and k.currentStatus != kstatus:
            return False
        if feedType and k.feedType != feedType:
            return False
        if owner and k.ownerId != owner:
            return False
        return True

    rows = [k for k in rows if keep(k)]
    names = await _names(db, [k.ownerId for k in rows])
    items = [await _serialise_kri(db, k, cats, names) for k in rows]
    items.sort(key=lambda x: ({"RED": 0, "AMBER": 1, "NO_DATA": 2, "GREEN": 3}.get(x.currentStatus, 4)))
    status_counts: dict[str, int] = {}
    for it in items:
        status_counts[it.currentStatus] = status_counts.get(it.currentStatus, 0) + 1
    breaches_open = (await db.execute(select(func.count()).select_from(KriBreachEvent).where(KriBreachEvent.status != "RESOLVED"))).scalar() or 0
    return S.KriListResponse(items=items, total=len(items), statusCounts=status_counts, breachesOpen=breaches_open)


async def _next_kri_code(db: AsyncSession) -> str:
    n = (await db.execute(select(func.count()).select_from(KriDefinition))).scalar() or 0
    return f"KRI-{(n + 1):04d}"


@router.post("/kris", response_model=S.KriOut, status_code=201)
async def create_kri(body: S.KriUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "KRI.ADMIN")
    api_token = secrets.token_urlsafe(24) if body.feedType == "API" else None
    k = KriDefinition(
        kriCode=await _next_kri_code(db), name=body.name, description=body.description, categoryId=body.categoryId,
        linkedRiskIds=body.linkedRiskIds, unit=body.unit, direction=body.direction, frequency=body.frequency,
        feedType=body.feedType, metricProviderKey=body.metricProviderKey, apiToken=api_token,
        thresholdGreen=body.thresholdGreen, thresholdAmber=body.thresholdAmber, ownerId=body.ownerId,
        graceDays=body.graceDays, isActive=body.isActive, createdBy=user.id,
    )
    db.add(k)
    await db.commit()
    await db.refresh(k)
    return await _serialise_kri(db, k, await _cat_index(db), await _names(db, [k.ownerId]), spark=False)


@router.patch("/kris/{kri_id}", response_model=S.KriOut)
async def update_kri(kri_id: str, body: S.KriUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "KRI.ADMIN")
    k = await db.get(KriDefinition, kri_id)
    if not k:
        raise HTTPException(404, "KRI not found")
    thresholds_changed = (k.thresholdGreen != body.thresholdGreen or k.thresholdAmber != body.thresholdAmber or k.direction != body.direction)
    for f in ("name", "description", "categoryId", "linkedRiskIds", "unit", "direction", "frequency", "feedType", "metricProviderKey", "thresholdGreen", "thresholdAmber", "ownerId", "graceDays", "isActive"):
        setattr(k, f, getattr(body, f))
    k.updatedBy = user.id
    if thresholds_changed:
        # recompute current reading only (history keeps its status)
        cur = (await db.execute(select(KriReading).where(KriReading.kriId == k.id).where(KriReading.isCurrent.is_(True)))).scalar_one_or_none()
        if cur:
            cur.status = svc.kri_status(k.direction, cur.value, k.thresholdGreen, k.thresholdAmber)
            k.currentStatus = cur.status
    await db.commit()
    await db.refresh(k)
    return await _serialise_kri(db, k, await _cat_index(db), await _names(db, [k.ownerId]))


@router.get("/kris/{kri_id}", response_model=S.KriDetail)
async def get_kri(kri_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "KRI.READ")
    k = await db.get(KriDefinition, kri_id)
    if not k or k.isDeleted:
        raise HTTPException(404, "KRI not found")
    cats = await _cat_index(db)
    names = await _names(db, [k.ownerId])
    base = await _serialise_kri(db, k, cats, names)
    readings = (await db.execute(select(KriReading).where(KriReading.kriId == k.id).order_by(KriReading.periodEnd.desc()))).scalars().all()
    rnames = await _names(db, [r.enteredBy for r in readings if r.enteredBy])
    breaches = (await db.execute(select(KriBreachEvent).where(KriBreachEvent.kriId == k.id).order_by(KriBreachEvent.createdAt.desc()))).scalars().all()
    bnames = await _names(db, [b.acknowledgedBy for b in breaches if b.acknowledgedBy])
    linked = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.id.in_(k.linkedRiskIds or ["__none__"])))).scalars().all()
    detail = S.KriDetail(**base.model_dump())
    detail.readings = [_reading_out(r, rnames) for r in readings]
    detail.breaches = [_breach_out(b, k, bnames) for b in breaches]
    detail.linkedRisks = [{"id": r.id, "riskCode": r.riskCode, "title": r.title, "residualBand": r.residualBand, "residualScore": r.residualScore} for r in linked]
    return detail


def _reading_out(r: KriReading, names: dict) -> S.ReadingOut:
    o = S.ReadingOut.model_validate(r)
    o.enteredByName = names.get(r.enteredBy) if r.enteredBy else None
    return o


def _breach_out(b: KriBreachEvent, k: KriDefinition | None, names: dict) -> S.KriBreachOut:
    o = S.KriBreachOut.model_validate(b)
    if k:
        o.kriCode, o.kriName = k.kriCode, k.name
    o.acknowledgedByName = names.get(b.acknowledgedBy) if b.acknowledgedBy else None
    return o


@router.post("/kris/{kri_id}/readings", response_model=S.ReadingOut, status_code=201)
async def add_reading(kri_id: str, body: S.ReadingCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    k = await db.get(KriDefinition, kri_id)
    if not k:
        raise HTTPException(404, "KRI not found")
    await _require(db, user, "KRI.ENTER", record={"ownerId": k.ownerId})
    pe = body.periodEnd or _now()
    r = await svc.record_reading(db, k, body.periodLabel, pe, body.value, "MANUAL", entered_by=user.id, notes=body.notes)
    if k.currentStatus == "RED":
        await svc.evaluate_appetite(db)
    await db.commit()
    await db.refresh(r)
    return _reading_out(r, await _names(db, [user.id]))


@router.post("/kris/readings/bulk")
async def bulk_readings(rows: list[S.BulkReadingRow], user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "KRI.ENTER")
    written, errors = 0, []
    try:
        for i, row in enumerate(rows):
            k = await db.get(KriDefinition, row.kriId)
            if not k:
                errors.append({"row": i, "error": "KRI not found"}); continue
            await svc.record_reading(db, k, row.periodLabel, row.periodEnd or _now(), row.value, "MANUAL", entered_by=user.id, notes=row.notes)
            written += 1
        if errors:
            await db.rollback()
            return {"ok": False, "written": 0, "errors": errors}
        await svc.evaluate_appetite(db)
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(400, f"Bulk save failed (rolled back): {e}")
    return {"ok": True, "written": written}


@router.post("/kris/{kri_code}/readings/api", status_code=201)
async def api_feed(kri_code: str, payload: dict = Body(...), authorization: str | None = None,
                   db: AsyncSession = Depends(get_db), token: str | None = Query(None)):
    """Authenticated inbound API feed (token-scoped per KRI). No user session."""
    k = (await db.execute(select(KriDefinition).where(KriDefinition.kriCode == kri_code))).scalar_one_or_none()
    if not k or k.feedType != "API":
        raise HTTPException(404, "API-fed KRI not found")
    supplied = token or (authorization or "").replace("Bearer ", "")
    if not k.apiToken or supplied != k.apiToken:
        raise HTTPException(401, "Invalid KRI API token")
    period_label = payload.get("periodLabel")
    value = payload.get("value")
    if period_label is None or value is None:
        raise HTTPException(422, "periodLabel and value required")
    # T2-05: duplicate period rejected on the inbound API feed.
    dup = (await db.execute(select(KriReading).where(KriReading.kriId == k.id).where(KriReading.periodLabel == str(period_label)))).scalar_one_or_none()
    if dup:
        raise HTTPException(409, f"A reading for period '{period_label}' already exists for {k.kriCode}.")
    pe = datetime.fromisoformat(payload["periodEnd"]) if payload.get("periodEnd") else _now()
    r = await svc.record_reading(db, k, str(period_label), pe, float(value), "API")
    if k.currentStatus == "RED":
        await svc.evaluate_appetite(db)
    await db.commit()
    return {"ok": True, "status": r.status}


@router.post("/kris/{kri_id}/breaches/{breach_id}/ack", response_model=S.KriBreachOut)
async def ack_breach(kri_id: str, breach_id: str, body: S.BreachAck, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    k = await db.get(KriDefinition, kri_id)
    await _require(db, user, "KRI.ACK", record={"ownerId": k.ownerId if k else None})
    b = await db.get(KriBreachEvent, breach_id)
    if not b:
        raise HTTPException(404, "Breach not found")
    b.acknowledgedBy = user.id
    b.acknowledgedAt = _now()
    b.resolutionNotes = body.resolutionNotes
    b.status = "RESOLVED" if body.resolve else "ACKNOWLEDGED"
    await db.commit()
    await db.refresh(b)
    return _breach_out(b, k, await _names(db, [user.id]))


@router.post("/kris/run-module-fed")
async def run_module_fed(period_end: str | None = Query(None, alias="periodEnd"), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "KRI.ADMIN")
    pe = datetime.fromisoformat(period_end) if period_end else _now()
    res = await svc.run_module_fed(db, pe)
    # KRI readings may have turned RED → re-evaluate appetite (MAX_RED_KRI_COUNT bands).
    await svc.evaluate_appetite(db)
    await db.commit()
    return res


@router.post("/kris/check-no-data")
async def check_no_data(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "KRI.ADMIN")
    res = await svc.check_no_data(db)
    await db.commit()
    return res


@router.get("/risks/{risk_id}/phase2-context")
async def risk_phase2_context(risk_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Phase-2 context for a Phase-1 risk detail: linked KRIs, 12-month loss
    history, compliance status, and the KRI-breach 'review recommended' flag."""
    await _require(db, user, "ERM.READ")
    risk = await db.get(EnterpriseRisk, risk_id)
    if not risk:
        raise HTTPException(404, "Risk not found")
    all_kris = (await db.execute(select(KriDefinition).where(KriDefinition.isDeleted.is_(False)))).scalars().all()
    linked_kris = [k for k in all_kris if risk_id in (k.linkedRiskIds or [])]
    kri_out = []
    for k in linked_kris:
        kri_out.append({"id": k.id, "kriCode": k.kriCode, "name": k.name, "currentStatus": k.currentStatus,
                        "currentValue": k.currentValue, "unit": k.unit, "sparkline": await _kri_sparkline(db, k.id)})
    losses = (await db.execute(select(LossEvent).where(LossEvent.isDeleted.is_(False)))).scalars().all()
    linked_losses = [le for le in losses if risk_id in (le.linkedRiskIds or [])]
    net_12m = sum(le.netLossInr for le in linked_losses if not le.isNearMiss and le.status in ("QUANTIFIED", "CLOSED"))
    loss_out = [{"id": le.id, "eventCode": le.eventCode, "title": le.title, "eventDate": le.eventDate.isoformat(),
                 "netLossInr": le.netLossInr, "isNearMiss": le.isNearMiss, "potentialLossInr": le.potentialLossInr, "status": le.status} for le in linked_losses]
    # compliance status: worst status among obligations linked to this risk
    obls = (await db.execute(select(LegalObligation).where(LegalObligation.isDeleted.is_(False)))).scalars().all()
    linked_obls = [o for o in obls if risk_id in (o.linkedRiskIds or [])]
    rank = {"OVERDUE": 0, "DUE_SOON": 1, "UNDER_RENEWAL": 2, "COMPLIANT": 3, "NOT_APPLICABLE": 4}
    comp_status = min((o.status for o in linked_obls), key=lambda s: rank.get(s, 5)) if linked_obls else None
    kri_breach_review = any(k.currentStatus == "RED" for k in linked_kris)
    return {
        "linkedKris": kri_out, "lossEvents": loss_out, "netLoss12m": net_12m,
        "complianceStatus": comp_status, "kriBreachReview": kri_breach_review,
    }


@router.get("/board-pack-phase2")
async def board_pack_phase2(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Phase-2 board-pack section data: KRI status, appetite compliance, loss
    summary, compliance status. Consumed by the board pack generator (E-11)."""
    await _require(db, user, "ERM.READ")
    kris = (await db.execute(select(KriDefinition).where(KriDefinition.isDeleted.is_(False)).where(KriDefinition.isActive.is_(True)))).scalars().all()
    cats = await _cat_index(db)
    kri_rows = [{"kriCode": k.kriCode, "name": k.name, "status": k.currentStatus, "value": k.currentValue, "unit": k.unit,
                 "categoryCode": cats[k.categoryId].code if k.categoryId in cats else None}
                for k in kris if k.currentStatus in ("RED", "AMBER")]
    appetite = await appetite_dashboard(user=user, db=db)
    open_breaches = await list_appetite_breaches(True, user=user, db=db)
    loss = await loss_analytics(user=user, db=db)
    comp = await compliance_dashboard(user=user, db=db)
    return {
        "kriStatus": kri_rows,
        "appetiteCompliance": [a.model_dump() for a in appetite],
        "appetiteBreaches": [b.model_dump() for b in open_breaches],
        "lossSummary": {"netLossByCategory": loss.netLossByCategory, "topLosses": loss.topLosses[:5]},
        "complianceStatus": {"compliantPct": comp.compliantPct, "overdue": comp.overdue, "dueSoon": comp.dueSoon,
                             "expiring": comp.renewalCalendar[:8]},
    }


@router.get("/kris/dashboard/summary")
async def kri_dashboard(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "KRI.READ")
    lst = await list_kris(None, None, None, None, user=user, db=db)
    return {"total": lst.total, "statusCounts": lst.statusCounts, "breachesOpen": lst.breachesOpen, "items": [i.model_dump() for i in lst.items]}


# ════════════════════════════════════════════════════════════════════════════
# Appetite
# ════════════════════════════════════════════════════════════════════════════
@router.get("/appetite/dashboard", response_model=list[S.AppetiteDashRow])
async def appetite_dashboard(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "APPETITE.READ")
    cats = await _cat_index(db)
    actives = {s.categoryId: s for s in (await db.execute(select(AppetiteStatement).where(AppetiteStatement.status == "ACTIVE").where(AppetiteStatement.isDeleted.is_(False)))).scalars().all()}
    out = []
    for cid, cat in sorted(cats.items(), key=lambda kv: kv[1].displayOrder):
        st = actives.get(cid)
        gauges = []
        open_breaches = 0
        if st:
            for band in (st.toleranceBands or []):
                observed, _ = await svc._observed_value(db, cid, band["bandType"])
                thr = float(band["thresholdValue"])
                state = "BREACH" if observed > thr else ("APPROACHING" if thr > 0 and observed >= 0.8 * thr else "WITHIN")
                gauges.append(S.BandGauge(bandType=band["bandType"], thresholdValue=thr, observedValue=observed, state=state))
            open_breaches = (await db.execute(select(func.count()).select_from(AppetiteBreach).where(AppetiteBreach.appetiteStatementId == st.id).where(AppetiteBreach.status.in_(svc._OPEN_BREACH_STATES)))).scalar() or 0
        out.append(S.AppetiteDashRow(
            categoryId=cid, categoryCode=cat.code, categoryName=cat.name, categoryColor=cat.colorHex,
            appetiteLevel=st.appetiteLevel if st else None, statementExcerpt=(st.statementText[:140] if st else ""),
            statementId=st.id if st else None, status=st.status if st else None, gauges=gauges, openBreaches=open_breaches,
        ))
    return out


def _stmt_out(st: AppetiteStatement, cats: dict, names: dict) -> S.AppetiteStatementOut:
    o = S.AppetiteStatementOut.model_validate(st)
    cat = cats.get(st.categoryId)
    if cat:
        o.categoryCode, o.categoryName, o.categoryColor = cat.code, cat.name, cat.colorHex
    o.approvedByName = names.get(st.approvedBy) if st.approvedBy else None
    return o


@router.get("/appetite/statements", response_model=list[S.AppetiteStatementOut])
async def list_statements(categoryId: str | None = Query(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "APPETITE.READ")
    stmt = select(AppetiteStatement).where(AppetiteStatement.isDeleted.is_(False))
    if categoryId:
        stmt = stmt.where(AppetiteStatement.categoryId == categoryId)
    rows = (await db.execute(stmt.order_by(AppetiteStatement.version.desc()))).scalars().all()
    cats = await _cat_index(db)
    names = await _names(db, [s.approvedBy for s in rows if s.approvedBy])
    return [_stmt_out(s, cats, names) for s in rows]


@router.post("/appetite/statements", response_model=S.AppetiteStatementOut, status_code=201)
async def create_statement(body: S.AppetiteUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "APPETITE.AUTHOR")
    prior = (await db.execute(select(func.max(AppetiteStatement.version)).where(AppetiteStatement.categoryId == body.categoryId))).scalar() or 0
    st = AppetiteStatement(
        categoryId=body.categoryId, statementText=body.statementText, appetiteLevel=body.appetiteLevel,
        version=prior + 1, status="DRAFT", toleranceBands=[b.model_dump() for b in body.toleranceBands], createdBy=user.id,
    )
    db.add(st)
    await db.commit()
    await db.refresh(st)
    return _stmt_out(st, await _cat_index(db), {})


@router.patch("/appetite/statements/{sid}", response_model=S.AppetiteStatementOut)
async def edit_statement(sid: str, body: S.AppetiteUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "APPETITE.AUTHOR")
    st = await db.get(AppetiteStatement, sid)
    if not st:
        raise HTTPException(404, "Statement not found")
    if st.status in ("ACTIVE", "SUPERSEDED"):
        # editing an ACTIVE statement always forks a new DRAFT version
        prior = (await db.execute(select(func.max(AppetiteStatement.version)).where(AppetiteStatement.categoryId == st.categoryId))).scalar() or st.version
        nv = AppetiteStatement(
            categoryId=st.categoryId, statementText=body.statementText, appetiteLevel=body.appetiteLevel,
            version=prior + 1, status="DRAFT", toleranceBands=[b.model_dump() for b in body.toleranceBands], createdBy=user.id,
        )
        db.add(nv)
        await db.commit()
        await db.refresh(nv)
        return _stmt_out(nv, await _cat_index(db), {})
    st.statementText = body.statementText
    st.appetiteLevel = body.appetiteLevel
    st.toleranceBands = [b.model_dump() for b in body.toleranceBands]
    st.updatedBy = user.id
    await db.commit()
    await db.refresh(st)
    return _stmt_out(st, await _cat_index(db), {})


@router.post("/appetite/statements/{sid}/submit", response_model=S.AppetiteStatementOut)
async def submit_statement(sid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "APPETITE.AUTHOR")
    st = await db.get(AppetiteStatement, sid)
    if not st or st.status != "DRAFT":
        raise HTTPException(400, "Only DRAFT statements can be submitted")
    st.status = "PENDING_APPROVAL"
    st.updatedBy = user.id
    await db.commit()
    await db.refresh(st)
    return _stmt_out(st, await _cat_index(db), {})


@router.post("/appetite/statements/{sid}/approve", response_model=S.AppetiteStatementOut)
async def approve_statement(sid: str, body: S.AppetiteApprove, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "APPETITE.APPROVE")  # CRO only
    st = await db.get(AppetiteStatement, sid)
    if not st:
        raise HTTPException(404, "Statement not found")
    if not (body.approvalReference and body.approvalReference.strip()):
        raise HTTPException(400, "Approval reference (RMC minute) is required to activate.")
    # supersede prior ACTIVE for this category (one ACTIVE per category)
    prior = (await db.execute(select(AppetiteStatement).where(AppetiteStatement.categoryId == st.categoryId).where(AppetiteStatement.status == "ACTIVE"))).scalars().all()
    for p in prior:
        p.status = "SUPERSEDED"
    st.status = "ACTIVE"
    st.approvedBy = user.id
    st.approvalReference = body.approvalReference
    st.approvedAt = _now()
    st.effectiveFrom = _now()
    st.updatedBy = user.id
    await db.commit()
    await svc.evaluate_appetite(db)
    await db.commit()
    await db.refresh(st)
    return _stmt_out(st, await _cat_index(db), await _names(db, [st.approvedBy]))


@router.get("/appetite/breaches", response_model=list[S.AppetiteBreachOut])
async def list_appetite_breaches(open_only: bool = Query(False, alias="openOnly"), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "APPETITE.READ")
    stmt = select(AppetiteBreach)
    if open_only:
        stmt = stmt.where(AppetiteBreach.status != "RESOLVED")
    rows = (await db.execute(stmt.order_by(AppetiteBreach.detectedAt.desc()))).scalars().all()
    cats = await _cat_index(db)
    names = await _names(db, [b.decisionBy for b in rows if b.decisionBy])
    risk_ids = {i for b in rows for i in (b.triggeringEntityIds or [])}
    risks = {r.id: r for r in (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.id.in_(risk_ids or ["__none__"])))).scalars().all()}
    kris = {k.id: k for k in (await db.execute(select(KriDefinition).where(KriDefinition.id.in_(risk_ids or ["__none__"])))).scalars().all()}
    out = []
    now = _now()
    for b in rows:
        o = S.AppetiteBreachOut.model_validate(b)
        cat = cats.get(b.categoryId)
        o.categoryCode = cat.code if cat else None
        o.categoryName = cat.name if cat else None
        o.decisionByName = names.get(b.decisionBy) if b.decisionBy else None
        o.ageDays = (now - svc._aware(b.detectedAt)).days
        ents = []
        for eid in (b.triggeringEntityIds or []):
            if eid in risks:
                ents.append({"id": eid, "type": "RISK", "code": risks[eid].riskCode, "title": risks[eid].title})
            elif eid in kris:
                ents.append({"id": eid, "type": "KRI", "code": kris[eid].kriCode, "title": kris[eid].name})
        o.triggeringEntities = ents
        out.append(o)
    return out


@router.post("/appetite/breaches/{bid}/decision", response_model=S.AppetiteBreachOut)
async def breach_decision(bid: str, body: S.BreachDecision, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "APPETITE.DECIDE")  # CRO only
    b = await db.get(AppetiteBreach, bid)
    if not b:
        raise HTTPException(404, "Breach not found")
    if body.action == "TEMPORARILY_ACCEPTED":
        if not body.reviewByDate:
            raise HTTPException(400, "Temporary acceptance requires a review-by date.")
        if not (body.committeeDecision and body.committeeDecision.strip()):
            raise HTTPException(400, "Committee decision text is required.")
        rb = svc._aware(body.reviewByDate)
        if rb > _now() + timedelta(days=90):
            raise HTTPException(400, "Review-by date cannot exceed 90 days.")
        b.reviewByDate = body.reviewByDate
    if body.action == "TREATMENT_MANDATED":
        # cannot resolve later until a treatment CAPA exists; just set the state now
        pass
    if body.action == "RESOLVED":
        # only allow manual resolve if a treatment CAPA exists on a triggering risk (T2-13)
        if b.status == "TREATMENT_MANDATED":
            has_capa = (await db.execute(
                select(func.count()).select_from(Capa).where(Capa.sourceTypeCode == "RISK_TREATMENT").where(Capa.sourceReferenceId.in_(b.triggeringEntityIds or ["__none__"]))
            )).scalar() or 0
            if not has_capa:
                raise HTTPException(400, "A treatment CAPA must exist on a triggering risk before this mandated breach can be resolved.")
        b.resolvedAt = _now()
    if body.committeeDecision:
        b.committeeDecision = body.committeeDecision
    b.status = body.action
    b.decisionBy = user.id
    await db.commit()
    await db.refresh(b)
    return (await list_appetite_breaches(False, user=user, db=db))[0] if False else _breach_min(b, await _cat_index(db), await _names(db, [b.decisionBy]))


def _breach_min(b, cats, names) -> S.AppetiteBreachOut:
    o = S.AppetiteBreachOut.model_validate(b)
    cat = cats.get(b.categoryId)
    o.categoryCode = cat.code if cat else None
    o.categoryName = cat.name if cat else None
    o.decisionByName = names.get(b.decisionBy) if b.decisionBy else None
    return o


@router.post("/appetite/evaluate")
async def run_appetite_eval(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "APPETITE.READ")
    res = await svc.evaluate_appetite(db)
    await db.commit()
    return res


# ════════════════════════════════════════════════════════════════════════════
# Compliance
# ════════════════════════════════════════════════════════════════════════════
async def _plant_index(db: AsyncSession) -> dict[str, str]:
    return {r[0]: r[1] for r in (await db.execute(select(Plant.id, Plant.name))).all()}


async def _serialise_obligation(db, o, plants, names, tasks=None) -> S.ObligationOut:
    out = S.ObligationOut.model_validate(o)
    out.siteName = plants.get(o.siteId) if o.siteId else "Corporate"
    out.ownerName = names.get(o.ownerId)
    if tasks is None:
        tasks = (await db.execute(select(ComplianceTask).where(ComplianceTask.obligationId == o.id))).scalars().all()
    open_tasks = [t for t in tasks if t.status in ("PENDING", "SUBMITTED", "OVERDUE")]
    out.openTaskCount = len(open_tasks)
    if open_tasks:
        out.nextDueDate = min(t.dueDate for t in open_tasks)
    return out


@router.get("/compliance/obligations", response_model=S.ObligationListResponse)
async def list_obligations(
    obligationType: str | None = Query(None), ostatus: str | None = Query(None, alias="status"),
    siteId: str | None = Query(None), owner: str | None = Query(None),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "COMPLIANCE.READ")
    rows = (await db.execute(select(LegalObligation).where(LegalObligation.isDeleted.is_(False)))).scalars().all()

    def keep(o):
        if obligationType and o.obligationType != obligationType:
            return False
        if ostatus and o.status != ostatus:
            return False
        if siteId and o.siteId != siteId:
            return False
        if owner and o.ownerId != owner:
            return False
        return True

    rows = [o for o in rows if keep(o)]
    plants = await _plant_index(db)
    names = await _names(db, [o.ownerId for o in rows])
    items = [await _serialise_obligation(db, o, plants, names) for o in rows]
    items.sort(key=lambda x: ({"OVERDUE": 0, "DUE_SOON": 1, "UNDER_RENEWAL": 2, "COMPLIANT": 3, "NOT_APPLICABLE": 4}.get(x.status, 5)))
    sc, tc = {}, {}
    for it in items:
        sc[it.status] = sc.get(it.status, 0) + 1
        tc[it.obligationType] = tc.get(it.obligationType, 0) + 1
    return S.ObligationListResponse(items=items, total=len(items), statusCounts=sc, typeCounts=tc)


async def _next_obl_code(db: AsyncSession) -> str:
    n = (await db.execute(select(func.count()).select_from(LegalObligation))).scalar() or 0
    return f"OBL-{(n + 1):04d}"


@router.post("/compliance/obligations", response_model=S.ObligationOut, status_code=201)
async def create_obligation(body: S.ObligationUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "COMPLIANCE.MANAGE")
    o = LegalObligation(
        obligationCode=await _next_obl_code(db), title=body.title, obligationType=body.obligationType,
        statuteReference=body.statuteReference, regulatorName=body.regulatorName, siteId=body.siteId, ownerId=body.ownerId,
        frequency=body.frequency, validFrom=body.validFrom, validUntil=body.validUntil, renewalLeadDays=body.renewalLeadDays,
        conditions=body.conditions, linkedRiskIds=body.linkedRiskIds, isActive=body.isActive, createdBy=user.id,
    )
    db.add(o)
    await db.commit()
    await db.refresh(o)
    return await _serialise_obligation(db, o, await _plant_index(db), await _names(db, [o.ownerId]), tasks=[])


@router.patch("/compliance/obligations/{oid}", response_model=S.ObligationOut)
async def edit_obligation(oid: str, body: S.ObligationUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "COMPLIANCE.MANAGE")
    o = await db.get(LegalObligation, oid)
    if not o:
        raise HTTPException(404, "Obligation not found")
    for f in ("title", "obligationType", "statuteReference", "regulatorName", "siteId", "ownerId", "frequency", "validFrom", "validUntil", "renewalLeadDays", "conditions", "linkedRiskIds", "isActive"):
        setattr(o, f, getattr(body, f))
    o.updatedBy = user.id
    await db.commit()
    await db.refresh(o)
    return await _serialise_obligation(db, o, await _plant_index(db), await _names(db, [o.ownerId]))


@router.get("/compliance/obligations/{oid}", response_model=S.ObligationDetail)
async def get_obligation(oid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "COMPLIANCE.READ")
    o = await db.get(LegalObligation, oid)
    if not o or o.isDeleted:
        raise HTTPException(404, "Obligation not found")
    plants = await _plant_index(db)
    names = await _names(db, [o.ownerId])
    tasks = (await db.execute(select(ComplianceTask).where(ComplianceTask.obligationId == o.id).order_by(ComplianceTask.dueDate.desc()))).scalars().all()
    base = await _serialise_obligation(db, o, plants, names, tasks=tasks)
    tnames = await _names(db, [x for t in tasks for x in (t.attestedBy, t.verifiedBy) if x])
    atts = (await db.execute(select(ComplianceAttachment).where(ComplianceAttachment.taskId.in_([t.id for t in tasks] or ["__none__"])).where(ComplianceAttachment.deletedAt.is_(None)))).scalars().all()
    att_by_task: dict[str, int] = {}
    for a in atts:
        att_by_task[a.taskId] = att_by_task.get(a.taskId, 0) + 1
    anames = await _names(db, [a.uploadedById for a in atts])
    linked = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.id.in_(o.linkedRiskIds or ["__none__"])))).scalars().all()
    detail = S.ObligationDetail(**base.model_dump())
    detail.tasks = [_task_out(t, tnames, att_by_task.get(t.id, 0)) for t in tasks]
    detail.attachments = [_att_out(a, anames) for a in atts]
    detail.linkedRisks = [{"id": r.id, "riskCode": r.riskCode, "title": r.title, "residualBand": r.residualBand} for r in linked]
    return detail


def _task_out(t: ComplianceTask, names: dict, att_count: int = 0) -> S.TaskOut:
    o = S.TaskOut.model_validate(t)
    o.attestedByName = names.get(t.attestedBy) if t.attestedBy else None
    o.verifiedByName = names.get(t.verifiedBy) if t.verifiedBy else None
    o.attachmentCount = att_count
    od = (_now() - svc._aware(t.dueDate)).days
    o.overdueDays = od if od > 0 and t.status in ("PENDING", "OVERDUE") else 0
    return o


def _att_out(a: ComplianceAttachment, names: dict) -> S.AttachmentOut:
    o = S.AttachmentOut.model_validate(a)
    o.uploadedByName = names.get(a.uploadedById)
    return o


@router.get("/compliance/tasks", response_model=list[S.TaskOut])
async def list_tasks(mine: bool = Query(False), verifyQueue: bool = Query(False), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "COMPLIANCE.READ")
    rows = (await db.execute(select(ComplianceTask, LegalObligation).join(LegalObligation, LegalObligation.id == ComplianceTask.obligationId))).all()
    out = []
    names = await _names(db, [x for t, _ in rows for x in (t.attestedBy, t.verifiedBy) if x])
    for t, obl in rows:
        if mine and obl.ownerId != user.id:
            continue
        if verifyQueue and t.status != "SUBMITTED":
            continue
        o = _task_out(t, names)
        o.obligationCode = obl.obligationCode
        o.obligationTitle = obl.title
        out.append(o)
    out.sort(key=lambda x: (x.status != "SUBMITTED", x.dueDate))
    return out


@router.post("/compliance/tasks/{tid}/attest", response_model=S.TaskOut)
async def attest_task(tid: str, body: S.TaskAttest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    t = await db.get(ComplianceTask, tid)
    if not t:
        raise HTTPException(404, "Task not found")
    await _require(db, user, "COMPLIANCE.ATTEST", record={"ownerId": (await db.get(LegalObligation, t.obligationId)).ownerId})
    if t.taskType in ("RENEWAL", "FILING"):
        att = (await db.execute(select(func.count()).select_from(ComplianceAttachment).where(ComplianceAttachment.taskId == t.id).where(ComplianceAttachment.deletedAt.is_(None)))).scalar() or 0
        if att == 0:
            raise HTTPException(400, f"{t.taskType} attestation requires at least one evidence attachment.")
    t.status = "SUBMITTED"
    t.attestedBy = user.id
    t.attestedAt = _now()
    t.remarks = body.remarks
    await db.commit()
    await svc.refresh_statuses(db)
    await db.commit()
    await db.refresh(t)
    return _task_out(t, await _names(db, [t.attestedBy]))


@router.post("/compliance/tasks/{tid}/verify", response_model=S.TaskOut)
async def verify_task(tid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "COMPLIANCE.VERIFY")  # Compliance Officer only
    t = await db.get(ComplianceTask, tid)
    if not t:
        raise HTTPException(404, "Task not found")
    if t.attestedBy == user.id:
        raise HTTPException(403, "Segregation of duties: you cannot verify a task you attested.")
    if t.status != "SUBMITTED":
        raise HTTPException(400, "Only SUBMITTED tasks can be verified.")
    t.status = "VERIFIED"
    t.verifiedBy = user.id
    t.verifiedAt = _now()
    await db.commit()
    await svc.refresh_statuses(db)
    await db.commit()
    await db.refresh(t)
    return _task_out(t, await _names(db, [t.attestedBy, t.verifiedBy]))


@router.post("/compliance/tasks/{tid}/waive", response_model=S.TaskOut)
async def waive_task(tid: str, body: S.TaskWaive, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "COMPLIANCE.WAIVE")  # Compliance Officer only
    t = await db.get(ComplianceTask, tid)
    if not t:
        raise HTTPException(404, "Task not found")
    t.status = "WAIVED"
    t.waiverJustification = body.waiverJustification
    t.verifiedBy = user.id
    t.verifiedAt = _now()
    await db.commit()
    await svc.refresh_statuses(db)
    await db.commit()
    await db.refresh(t)
    return _task_out(t, await _names(db, [t.verifiedBy]))


@router.post("/compliance/tasks/{tid}/attachments", response_model=S.AttachmentOut, status_code=201)
async def add_attachment(tid: str, payload: dict = Body(...), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    t = await db.get(ComplianceTask, tid)
    if not t:
        raise HTTPException(404, "Task not found")
    await _require(db, user, "COMPLIANCE.ATTEST", record={"ownerId": (await db.get(LegalObligation, t.obligationId)).ownerId})
    fname = payload.get("fileName", "evidence.pdf")
    path = f"compliance/{tid}/{secrets.token_hex(4)}-{fname}"
    a = ComplianceAttachment(
        taskId=tid, fileName=fname, storagePath=path, fileSize=payload.get("fileSize"),
        mimeType=payload.get("mimeType"), caption=payload.get("caption"), uploadedById=user.id,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return _att_out(a, await _names(db, [user.id]))


@router.post("/compliance/tasks/{tid}/raise-capa")
async def raise_compliance_capa(tid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "COMPLIANCE.MANAGE")
    t = await db.get(ComplianceTask, tid)
    if not t:
        raise HTTPException(404, "Task not found")
    obl = await db.get(LegalObligation, t.obligationId)
    st = (await db.execute(select(CapaSourceType).where(CapaSourceType.code == "COMPLIANCE"))).scalar_one_or_none()
    if st is None:
        raise HTTPException(400, "COMPLIANCE CAPA source type not seeded.")
    cat = await db.get(CapaSourceCategory, st.categoryId)
    plant = (await db.get(Plant, obl.siteId)) if obl.siteId else (await db.execute(select(Plant).order_by(Plant.code).limit(1))).scalar_one_or_none()
    year = _now().year
    count = (await db.execute(select(func.count()).select_from(Capa).where(Capa.plantId == plant.id).where(Capa.sourceCategoryId == st.categoryId))).scalar() or 0
    capa_number = f"CAPA-{cat.prefix if cat else 'CMP'}-{year}-{plant.code}-{(count + 1):03d}"
    capa = Capa(
        capaNumber=capa_number, title=f"Compliance: {obl.title}"[:200], plantId=plant.id,
        sourceCategoryId=st.categoryId, sourceTypeId=st.id, sourceTypeCode="COMPLIANCE",
        sourceReferenceId=t.id, sourceReferenceUrl=f"/erm/compliance/{obl.id}",
        sourceReferenceSummary=f"{obl.obligationCode} — {obl.title}",
        sourceMetadata={"obligationCode": obl.obligationCode, "periodLabel": t.periodLabel, "statute": obl.statuteReference},
        problemDescription=f"Overdue compliance obligation {obl.obligationCode}: {obl.title} ({obl.statuteReference})",
        detectionMethod="COMPLIANCE_OVERDUE", detectedAt=_now(), detectedByUserId=user.id,
        primaryCategory="Compliance", actionType="CORRECTIVE_AND_PREVENTIVE", severity="HIGH", priority="HIGH",
        state="ACTIONS_PLANNED", stateChangedAt=_now(), stateChangedByUserId=user.id,
        raisedByUserId=user.id, primaryOwnerUserId=obl.ownerId, createdByUserId=user.id,
    )
    db.add(capa)
    t.capaId = None  # set after flush
    await db.flush()
    t.capaId = capa.id
    await db.commit()
    await db.refresh(capa)
    return {"ok": True, "capaId": capa.id, "capaNumber": capa.capaNumber}


@router.post("/compliance/generate-tasks")
async def gen_tasks(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "COMPLIANCE.MANAGE")
    res = await svc.generate_tasks(db)
    await svc.refresh_statuses(db)
    await db.commit()
    return res


@router.get("/compliance/dashboard", response_model=S.ComplianceDashboard)
async def compliance_dashboard(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "COMPLIANCE.READ")
    obls = (await db.execute(select(LegalObligation).where(LegalObligation.isDeleted.is_(False)).where(LegalObligation.isActive.is_(True)))).scalars().all()
    plants = await _plant_index(db)
    total = len(obls)
    compliant = sum(1 for o in obls if o.status == "COMPLIANT")
    due_soon = sum(1 for o in obls if o.status == "DUE_SOON")
    overdue = sum(1 for o in obls if o.status == "OVERDUE")
    under_renewal = sum(1 for o in obls if o.status == "UNDER_RENEWAL")
    tc, site_split = {}, {}
    for o in obls:
        tc[o.obligationType] = tc.get(o.obligationType, 0) + 1
        key = plants.get(o.siteId, "Corporate") if o.siteId else "Corporate"
        site_split[key] = site_split.get(key, 0) + 1
    now = _now()
    cal = sorted(
        [{"obligationCode": o.obligationCode, "title": o.title, "validUntil": o.validUntil.isoformat() if o.validUntil else None,
          "daysToExpiry": (svc._aware(o.validUntil) - now).days if o.validUntil else None, "status": o.status}
         for o in obls if o.validUntil and 0 <= (svc._aware(o.validUntil) - now).days <= 60],
        key=lambda x: x["daysToExpiry"] if x["daysToExpiry"] is not None else 999,
    )
    names = await _names(db, [o.ownerId for o in obls if o.status == "OVERDUE"])
    overdue_table = [{"obligationCode": o.obligationCode, "title": o.title, "owner": names.get(o.ownerId), "siteName": plants.get(o.siteId, "Corporate") if o.siteId else "Corporate", "validUntil": o.validUntil.isoformat() if o.validUntil else None} for o in obls if o.status == "OVERDUE"]
    return S.ComplianceDashboard(
        totalObligations=total, compliantPct=round(compliant * 100 / total, 1) if total else 0.0,
        dueSoon=due_soon, overdue=overdue, underRenewal=under_renewal, typeCounts=tc, siteSplit=site_split,
        renewalCalendar=cal, overdueTable=overdue_table,
    )


# ════════════════════════════════════════════════════════════════════════════
# Loss Events
# ════════════════════════════════════════════════════════════════════════════
async def _serialise_loss(o, cats, plants) -> S.LossEventOut:
    out = S.LossEventOut.model_validate(o)
    cat = cats.get(o.categoryId)
    if cat:
        out.categoryCode, out.categoryName, out.categoryColor = cat.code, cat.name, cat.colorHex
    out.siteName = plants.get(o.siteId) if o.siteId else None
    return out


@router.get("/loss/events", response_model=S.LossListResponse)
async def list_loss(category: str | None = Query(None), lstatus: str | None = Query(None, alias="status"), source: str | None = Query(None),
                    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "LOSS.READ")
    rows = (await db.execute(select(LossEvent).where(LossEvent.isDeleted.is_(False)).order_by(LossEvent.eventDate.desc()))).scalars().all()
    cats = await _cat_index(db)
    plants = await _plant_index(db)
    code_to_id = {c.code: cid for cid, c in cats.items()}

    def keep(o):
        if category and o.categoryId != code_to_id.get(category):
            return False
        if lstatus and o.status != lstatus:
            return False
        if source and o.source != source:
            return False
        return True

    rows = [o for o in rows if keep(o)]
    items = [await _serialise_loss(o, cats, plants) for o in rows]
    sc = {}
    for it in items:
        sc[it.status] = sc.get(it.status, 0) + 1
    net = sum(o.netLossInr for o in rows if not o.isNearMiss and o.status in ("QUANTIFIED", "CLOSED"))
    nm = sum((o.potentialLossInr or 0) for o in rows if o.isNearMiss)
    return S.LossListResponse(items=items, total=len(items), statusCounts=sc, netLossTotal=net, nearMissPotentialTotal=nm)


@router.post("/loss/events", response_model=S.LossEventOut, status_code=201)
async def create_loss(body: S.LossUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "LOSS.CREATE")
    if body.recoveredInr > body.grossLossInr:
        raise HTTPException(400, "Recovered amount cannot exceed gross loss.")
    net = max(0.0, body.grossLossInr - body.recoveredInr)
    o = LossEvent(
        eventCode=await svc._next_loss_code(db), title=body.title, description=body.description, eventDate=body.eventDate,
        siteId=body.siteId, categoryId=body.categoryId, subCategoryId=body.subCategoryId, linkedRiskIds=body.linkedRiskIds,
        source="MANUAL", isNearMiss=body.isNearMiss, grossLossInr=body.grossLossInr, recoveredInr=body.recoveredInr,
        netLossInr=net, potentialLossInr=body.potentialLossInr, lossTypes=body.lossTypes, status="DRAFT", createdBy=user.id,
    )
    db.add(o)
    await db.commit()
    await db.refresh(o)
    return await _serialise_loss(o, await _cat_index(db), await _plant_index(db))


@router.patch("/loss/events/{lid}", response_model=S.LossEventOut)
async def edit_loss(lid: str, body: S.LossUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "LOSS.CREATE")
    o = await db.get(LossEvent, lid)
    if not o:
        raise HTTPException(404, "Loss event not found")
    if body.recoveredInr > body.grossLossInr:
        raise HTTPException(400, "Recovered amount cannot exceed gross loss.")
    for f in ("title", "description", "eventDate", "siteId", "categoryId", "subCategoryId", "linkedRiskIds", "isNearMiss", "grossLossInr", "recoveredInr", "potentialLossInr", "lossTypes"):
        setattr(o, f, getattr(body, f))
    o.netLossInr = max(0.0, o.grossLossInr - o.recoveredInr)
    o.updatedBy = user.id
    await db.commit()
    await db.refresh(o)
    return await _serialise_loss(o, await _cat_index(db), await _plant_index(db))


@router.post("/loss/events/{lid}/quantify", response_model=S.LossEventOut)
async def quantify_loss(lid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "LOSS.CREATE")
    o = await db.get(LossEvent, lid)
    if not o:
        raise HTTPException(404, "Loss event not found")
    if o.grossLossInr <= 0 or not o.lossTypes or not o.categoryId:
        raise HTTPException(400, "Quantification requires a gross figure, at least one loss type, and a category.")
    o.status = "QUANTIFIED"
    o.updatedBy = user.id
    await db.commit()
    await db.refresh(o)
    return await _serialise_loss(o, await _cat_index(db), await _plant_index(db))


@router.post("/loss/events/{lid}/close", response_model=S.LossEventOut)
async def close_loss(lid: str, body: S.LossClose, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "LOSS.CLOSE")
    o = await db.get(LossEvent, lid)
    if not o:
        raise HTTPException(404, "Loss event not found")
    if not (body.closureNotes and body.closureNotes.strip()):
        raise HTTPException(400, "Closure notes required.")
    o.status = "CLOSED"
    o.closureNotes = body.closureNotes
    o.updatedBy = user.id
    await db.commit()
    await db.refresh(o)
    return await _serialise_loss(o, await _cat_index(db), await _plant_index(db))


@router.post("/loss/auto-feed")
async def loss_auto_feed(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "LOSS.CREATE")
    res = await svc.auto_feed_incidents(db, actor_id=user.id)
    await db.commit()
    return res


@router.get("/loss/analytics", response_model=S.LossAnalytics)
async def loss_analytics(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "LOSS.READ")
    cats = await _cat_index(db)
    now = _now()
    start = now - timedelta(days=365)
    events = (await db.execute(select(LossEvent).where(LossEvent.isDeleted.is_(False)).where(LossEvent.status.in_(("QUANTIFIED", "CLOSED"))))).scalars().all()
    real = [e for e in events if not e.isNearMiss]
    by_cat: dict[str, float] = {}
    for e in real:
        if svc._aware(e.eventDate) >= start:
            by_cat[e.categoryId] = by_cat.get(e.categoryId, 0) + e.netLossInr
    net_by_cat = [{"categoryCode": cats[cid].code if cid in cats else "?", "categoryName": cats[cid].name if cid in cats else "?", "colorHex": cats[cid].colorHex if cid in cats else "#888", "netLoss": v} for cid, v in sorted(by_cat.items(), key=lambda kv: -kv[1])]
    by_q: dict[str, float] = {}
    for e in real:
        d = svc._aware(e.eventDate)
        q = f"{d.year}-Q{(d.month - 1)//3 + 1}"
        by_q[q] = by_q.get(q, 0) + e.netLossInr
    trend = [{"quarter": q, "netLoss": v} for q, v in sorted(by_q.items())]
    top = sorted(real, key=lambda e: -e.netLossInr)[:10]
    top_losses = [{"eventCode": e.eventCode, "title": e.title, "netLoss": e.netLossInr, "categoryCode": cats[e.categoryId].code if e.categoryId in cats else None} for e in top]
    nm = [{"eventCode": e.eventCode, "title": e.title, "potentialLoss": e.potentialLossInr} for e in events if e.isNearMiss and e.potentialLossInr] + \
         [{"eventCode": e.eventCode, "title": e.title, "potentialLoss": e.potentialLossInr} for e in (await db.execute(select(LossEvent).where(LossEvent.isNearMiss.is_(True)).where(LossEvent.isDeleted.is_(False)))).scalars().all() if e.status == "DRAFT" and e.potentialLossInr]
    calib = [S.CalibrationRow(**c) for c in await svc.calibration(db)]
    return S.LossAnalytics(netLossByCategory=net_by_cat, lossTrendByQuarter=trend, topLosses=top_losses, nearMissPotential=nm, calibration=calib)


# ════════════════════════════════════════════════════════════════════════════
# Reports (Phase 2 CSV exports)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/reports-p2/{kind}.csv")
async def export_p2(kind: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "KRI.READ")
    buf = io.StringIO()
    w = csv.writer(buf)
    if kind == "kri-readings":
        w.writerow(["KRI", "Period", "Value", "Status", "Source"])
        rows = (await db.execute(select(KriReading, KriDefinition).join(KriDefinition, KriDefinition.id == KriReading.kriId).order_by(KriReading.periodEnd.desc()))).all()
        for r, k in rows:
            w.writerow([k.kriCode, r.periodLabel, r.value, r.status, r.source])
    elif kind == "appetite-breaches":
        w.writerow(["Category", "Band", "Observed", "Threshold", "Status", "Decision", "DetectedAt"])
        cats = await _cat_index(db)
        for b in (await db.execute(select(AppetiteBreach).order_by(AppetiteBreach.detectedAt.desc()))).scalars().all():
            w.writerow([cats[b.categoryId].code if b.categoryId in cats else "", b.bandType, b.observedValue, b.thresholdValue, b.status, (b.committeeDecision or "")[:120], b.detectedAt.date().isoformat()])
    elif kind == "obligations":
        w.writerow(["Code", "Title", "Type", "Regulator", "Status", "ValidUntil"])
        for o in (await db.execute(select(LegalObligation).where(LegalObligation.isDeleted.is_(False)))).scalars().all():
            w.writerow([o.obligationCode, o.title, o.obligationType, o.regulatorName, o.status, o.validUntil.date().isoformat() if o.validUntil else ""])
    elif kind == "compliance-tasks":
        w.writerow(["Obligation", "Task", "Period", "Due", "Status", "AttestedBy", "VerifiedBy"])
        rows = (await db.execute(select(ComplianceTask, LegalObligation).join(LegalObligation, LegalObligation.id == ComplianceTask.obligationId))).all()
        names = await _names(db, [x for t, _ in rows for x in (t.attestedBy, t.verifiedBy) if x])
        for t, o in rows:
            w.writerow([o.obligationCode, t.taskType, t.periodLabel, t.dueDate.date().isoformat(), t.status, names.get(t.attestedBy, ""), names.get(t.verifiedBy, "")])
    elif kind == "loss-events":
        w.writerow(["Code", "Date", "Title", "Category", "Gross", "Recovered", "Net", "Status"])
        cats = await _cat_index(db)
        for e in (await db.execute(select(LossEvent).where(LossEvent.isDeleted.is_(False)).order_by(LossEvent.eventDate.desc()))).scalars().all():
            w.writerow([e.eventCode, e.eventDate.date().isoformat(), e.title, cats[e.categoryId].code if e.categoryId in cats else "", e.grossLossInr, e.recoveredInr, e.netLossInr, e.status])
    else:
        raise HTTPException(404, f"Unknown report kind '{kind}'")
    return Response(content=buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=erm-{kind}.csv"})
