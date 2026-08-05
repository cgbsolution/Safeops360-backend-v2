"""Causal-analytics engine (ERM Cross-Domain RCA).

All engines read the structured RcaIdentifiedCause + RcaRiskLink data from APPROVED
RCAs and aggregate — the same on-demand pattern CAMS proves with repeat-finding
detection (app/services/cams_analytics.py). Outputs are "computed from RCA records",
not real-time (no scheduler in this pass).

Headline metrics per sub-cause:
  • occurrences   — citations across approved RCAs
  • risk_reach    — DISTINCT risks driven (the "combination" metric)
  • domain_spread — DISTINCT risk domains touched (the cross-domain headline)
Recurring systemic driver = risk_reach >= 2 AND occurrences >= threshold.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.erm import EnterpriseRisk, RiskCategory, RiskLinkage
from app.models.rca import (
    RcaRiskLink,
    RootCauseAnalysis,
    RootCauseCategory,
    RootCauseSubCause,
)
from app.services.access_scope import QueryScope
from app.services.rca_core import CATEGORY_CODE_TO_DOMAIN

RECURRING_DRIVER_THRESHOLD = 2  # configurable: occurrences >= this AND risk_reach >= 2

DOMAIN_COLOR = {
    "OPERATIONAL": "#C0392B",
    "FINANCIAL": "#1E6FB8",
    "COMPLIANCE": "#B45309",
    "EXTERNAL": "#5D6D7E",
    "REPUTATIONAL": "#8E44AD",
    "CYBER": "#16A085",
    "STRATEGIC": "#6B4FA0",
    "ESG": "#047857",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _load_approved_rcas(
    db: AsyncSession,
    scope: QueryScope,
    *,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    domain_filter: str | None = None,
) -> list[RootCauseAnalysis]:
    stmt = (
        select(RootCauseAnalysis)
        .where(RootCauseAnalysis.status == "APPROVED")
        .where(RootCauseAnalysis.isDeleted.is_(False))  # explicit (don't rely on the global ORM filter being armed)
    )
    stmt = scope.apply(stmt, RootCauseAnalysis)  # tenant + plant
    if domain_filter:
        stmt = stmt.where(RootCauseAnalysis.primaryDomain == domain_filter)
    if period_start:
        stmt = stmt.where(RootCauseAnalysis.occurrenceDate >= period_start)
    if period_end:
        stmt = stmt.where(RootCauseAnalysis.occurrenceDate <= period_end)
    stmt = stmt.options(
        selectinload(RootCauseAnalysis.identifiedCauses),
        selectinload(RootCauseAnalysis.riskLinks),
    )
    return list((await db.execute(stmt)).scalars().all())


async def _taxonomy_index(db: AsyncSession):
    cats = {c.id: c for c in (await db.execute(select(RootCauseCategory))).scalars().all()}
    subs = {s.id: s for s in (await db.execute(select(RootCauseSubCause))).scalars().all()}
    return cats, subs


def aggregate_cause_metrics(rcas: list, cats: dict, subs: dict, *, threshold: int = RECURRING_DRIVER_THRESHOLD) -> tuple[list, list]:
    """PURE aggregation (no DB) — the heart of the client's ask, so it's unit-
    tested directly. Given approved RCA-like objects (each exposing .id,
    .primaryDomain, .identifiedCauses[*].subCauseId/.enterpriseCategoryId and
    .riskLinks[*].riskId), returns (causes, categories) with:
      occurrences (citations), riskReach (distinct risks), domainSpread (distinct
      domains), rcaCount, isRecurringDriver — rolled up to the ~7 categories.
    """
    sub_agg: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "rca_ids": set(), "domains": set(), "risk_ids": set(), "category": None}
    )
    cat_agg: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "domains": set(), "risk_ids": set(), "sub_ids": set()}
    )

    for rca in rcas:
        risk_ids = [link.riskId for link in rca.riskLinks]
        # A RISK-originated RCA reaches its own source risk even without an
        # explicit RcaRiskLink — count it so reach reconciles with the drill.
        _src = getattr(rca, "sourceRiskId", None)
        if _src:
            risk_ids.append(_src)
        for c in rca.identifiedCauses:
            sa = sub_agg[c.subCauseId]
            sa["count"] += 1
            sa["rca_ids"].add(rca.id)
            sa["domains"].add(rca.primaryDomain)
            sa["category"] = c.enterpriseCategoryId
            sa["risk_ids"].update(risk_ids)

            ca = cat_agg[c.enterpriseCategoryId]
            ca["count"] += 1
            ca["domains"].add(rca.primaryDomain)
            ca["risk_ids"].update(risk_ids)
            ca["sub_ids"].add(c.subCauseId)

    causes = []
    for sub_id, v in sub_agg.items():
        sub = subs.get(sub_id)
        cat = cats.get(v["category"]) if v["category"] else None
        risk_reach = len(v["risk_ids"])
        occurrences = v["count"]
        causes.append(
            {
                "subCauseId": sub_id,
                "subCauseCode": sub.code if sub else sub_id,
                "subCauseName": sub.name if sub else sub_id,
                "enterpriseCategoryId": v["category"] or "",
                "categoryCode": cat.code if cat else "",
                "categoryName": cat.name if cat else "",
                "occurrences": occurrences,
                "riskReach": risk_reach,
                "domainSpread": len(v["domains"]),
                "domains": sorted(v["domains"]),
                "rcaCount": len(v["rca_ids"]),
                "isRecurringDriver": risk_reach >= 2 and occurrences >= threshold,
            }
        )
    causes.sort(key=lambda x: (x["riskReach"], x["occurrences"]), reverse=True)

    categories = []
    for cat_id, v in cat_agg.items():
        cat = cats.get(cat_id)
        categories.append(
            {
                "enterpriseCategoryId": cat_id,
                "categoryCode": cat.code if cat else "",
                "categoryName": cat.name if cat else "",
                "colorHex": cat.colorHex if cat else "#475569",
                "occurrences": v["count"],
                "riskReach": len(v["risk_ids"]),
                "domainSpread": len(v["domains"]),
                "domains": sorted(v["domains"]),
                "subCauseCount": len(v["sub_ids"]),
            }
        )
    categories.sort(key=lambda x: (x["domainSpread"], x["riskReach"], x["occurrences"]), reverse=True)
    return causes, categories


async def compute_cause_analytics(
    db: AsyncSession,
    scope: QueryScope,
    *,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    domain_filter: str | None = None,
    threshold: int = RECURRING_DRIVER_THRESHOLD,
) -> dict:
    """DB wrapper around aggregate_cause_metrics: load APPROVED, scoped RCAs, then
    aggregate per sub-cause + roll up to the ~7 enterprise categories (board view)."""
    rcas = await _load_approved_rcas(
        db, scope, period_start=period_start, period_end=period_end, domain_filter=domain_filter
    )
    cats, subs = await _taxonomy_index(db)
    causes, categories = aggregate_cause_metrics(rcas, cats, subs, threshold=threshold)
    return {
        "computedAt": _now(),
        "periodStart": period_start,
        "periodEnd": period_end,
        "domainFilter": domain_filter,
        "causes": causes,
        "categories": categories,
        "recurringDriverThreshold": threshold,
        "note": "Computed from approved RCA records.",
    }


async def compute_cause_detail(
    db: AsyncSession, scope: QueryScope, sub_cause_id: str, *, domain_filter: str | None = None
) -> dict:
    """Drill-through for one sub-cause: the underlying risks it reaches and the
    RCAs that cite it. Powers the Root-Cause Analytics 'click a cause row' drawer.
    Reconciles with the Pareto risk-reach (distinct risks)."""
    rcas = await _load_approved_rcas(db, scope, domain_filter=domain_filter)
    cats, subs = await _taxonomy_index(db)
    sub = subs.get(sub_cause_id)
    cat = cats.get(sub.categoryId) if sub and getattr(sub, "categoryId", None) else None

    citing = [r for r in rcas if any(c.subCauseId == sub_cause_id for c in r.identifiedCauses)]
    risk_ids: set[str] = set()
    rca_rows = []
    domains: set[str] = set()
    for r in citing:
        domains.add(r.primaryDomain)
        for lk in r.riskLinks:
            if lk.riskId:
                risk_ids.add(lk.riskId)
        if getattr(r, "sourceRiskId", None):
            risk_ids.add(r.sourceRiskId)
        rca_rows.append({"rcaId": r.id, "rcaCode": r.rcaCode, "title": r.title,
                         "originType": r.originType, "primaryDomain": r.primaryDomain})

    risk_map: dict[str, EnterpriseRisk] = {}
    if risk_ids:
        risk_map = {
            x.id: x for x in (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.id.in_(risk_ids)))).scalars().all()
        }
    risks = [
        {"riskId": rid, "riskCode": (risk_map[rid].riskCode if rid in risk_map else None),
         "riskTitle": (risk_map[rid].title if rid in risk_map else None),
         "residualBand": (risk_map[rid].residualBand if rid in risk_map else None),
         "residualScore": (risk_map[rid].residualScore if rid in risk_map else None)}
        for rid in risk_ids
    ]
    risks.sort(key=lambda x: (x["residualScore"] or 0), reverse=True)
    occurrences = sum(1 for r in citing for c in r.identifiedCauses if c.subCauseId == sub_cause_id)
    return {
        "computedAt": _now(),
        "subCauseId": sub_cause_id,
        "subCauseCode": sub.code if sub else sub_cause_id,
        "subCauseName": sub.name if sub else sub_cause_id,
        "categoryCode": cat.code if cat else "",
        "categoryName": cat.name if cat else "",
        "occurrences": occurrences,
        "riskReach": len(risk_ids),
        "domainSpread": len(domains),
        "domains": sorted(domains),
        "risks": risks,
        "rcas": rca_rows,
        "note": "Computed from approved RCA records.",
    }


async def detect_recurring_drivers(
    db: AsyncSession, scope: QueryScope, *, threshold: int = RECURRING_DRIVER_THRESHOLD
) -> list[dict]:
    """Sub-causes flagged as recurring systemic drivers (mirrors CAMS repeat-finding)."""
    analytics = await compute_cause_analytics(db, scope, threshold=threshold)
    return [c for c in analytics["causes"] if c["isRecurringDriver"]]


async def compute_contributing_causes_for_risk(
    db: AsyncSession, risk_id: str, scope: QueryScope
) -> dict:
    """For a single risk: which causes feed it, how often, latest occurrence.
    Powers the 'Contributing Root Causes' panel (RCA-03)."""
    # Approved, scoped RCAs that link to this risk.
    stmt = (
        select(RootCauseAnalysis)
        .join(RcaRiskLink, RcaRiskLink.rcaId == RootCauseAnalysis.id)
        .where(RcaRiskLink.riskId == risk_id)
        .where(RootCauseAnalysis.status == "APPROVED")
        .where(RootCauseAnalysis.isDeleted.is_(False))
        .options(selectinload(RootCauseAnalysis.identifiedCauses))
    )
    stmt = scope.apply(stmt, RootCauseAnalysis)
    rcas = list((await db.execute(stmt)).scalars().unique().all())
    _, subs = await _taxonomy_index(db)
    cats = {c.id: c for c in (await db.execute(select(RootCauseCategory))).scalars().all()}

    agg: dict[str, dict] = defaultdict(lambda: {"count": 0, "rcas": [], "latest": None})
    for rca in rcas:
        for cause in rca.identifiedCauses:
            o = agg[cause.subCauseId]
            o["count"] += 1
            o["rcas"].append(rca.rcaCode)
            occ = rca.occurrenceDate
            if occ and (o["latest"] is None or occ > o["latest"]):
                o["latest"] = occ

    out = []
    for sub_id, v in agg.items():
        sub = subs.get(sub_id)
        cat = cats.get(sub.categoryId) if sub else None
        out.append(
            {
                "subCauseId": sub_id,
                "subCauseName": sub.name if sub else sub_id,
                "categoryCode": cat.code if cat else "",
                "categoryName": cat.name if cat else "",
                "count": v["count"],
                "rcaCodes": sorted(set(v["rcas"])),
                "latestOccurrence": v["latest"],
            }
        )
    out.sort(key=lambda x: x["count"], reverse=True)
    return {"riskId": risk_id, "causes": out, "note": "Computed from approved RCA records."}


async def build_cause_to_risk_graph(
    db: AsyncSession,
    scope: QueryScope,
    *,
    sub_cause_id: str | None = None,
    include_chains: bool = True,
) -> dict:
    """Network graph: a focus sub-cause in the centre, the risks it drives radiating
    out (coloured by domain), optionally with risk→risk chains (RiskLinkage). When no
    sub-cause is given, returns the top recurring drivers and their risks."""
    rcas = await _load_approved_rcas(db, scope)
    _, subs = await _taxonomy_index(db)

    # Build (subCauseId -> {risks: {riskId: best weight/contribution}}) from approved RCAs.
    cause_to_risk: dict[str, dict[str, dict]] = defaultdict(dict)
    for rca in rcas:
        cited = {c.subCauseId for c in rca.identifiedCauses}
        for sc in cited:
            for link in rca.riskLinks:
                prev = cause_to_risk[sc].get(link.riskId)
                w = link.weight if link.weight is not None else 0.5
                if prev is None or w > prev["weight"]:
                    cause_to_risk[sc][link.riskId] = {
                        "weight": w,
                        "contributionType": link.contributionType,
                    }

    # Which sub-causes to render.
    if sub_cause_id:
        focus_ids = [sub_cause_id]
    else:
        # default view: sub-causes with the widest reach
        focus_ids = sorted(
            cause_to_risk.keys(), key=lambda s: len(cause_to_risk[s]), reverse=True
        )[:6]

    risk_ids: set[str] = set()
    for sc in focus_ids:
        risk_ids.update(cause_to_risk.get(sc, {}).keys())

    # Load risks + their category codes for domain colouring.
    risks: dict[str, EnterpriseRisk] = {}
    cat_code: dict[str, str] = {}
    if risk_ids:
        rows = (
            await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.id.in_(risk_ids)))
        ).scalars().all()
        risks = {r.id: r for r in rows}
        cat_rows = (await db.execute(select(RiskCategory))).scalars().all()
        cat_code = {c.id: c.code for c in cat_rows}

    nodes: list[dict] = []
    edges: list[dict] = []

    for sc in focus_ids:
        sub = subs.get(sc)
        nodes.append(
            {
                "id": f"cause:{sc}",
                "type": "cause",
                "label": sub.name if sub else sc,
                "sublabel": sub.code if sub else None,
                "domain": None,
                "colorHex": "#0f172a",
                "band": None,
            }
        )

    for rid in risk_ids:
        r = risks.get(rid)
        domain = "OPERATIONAL"
        if r is not None:
            domain = CATEGORY_CODE_TO_DOMAIN.get(cat_code.get(r.categoryId, ""), "OPERATIONAL")
        nodes.append(
            {
                "id": f"risk:{rid}",
                "type": "risk",
                "label": r.riskCode if r else rid,
                "sublabel": (r.title[:48] if r else None),
                "domain": domain,
                "colorHex": DOMAIN_COLOR.get(domain, "#475569"),
                "band": r.residualBand if r else None,
            }
        )

    for sc in focus_ids:
        for rid, meta in cause_to_risk.get(sc, {}).items():
            edges.append(
                {
                    "id": f"e:{sc}:{rid}",
                    "source": f"cause:{sc}",
                    "target": f"risk:{rid}",
                    "contributionType": meta["contributionType"],
                    "weight": meta["weight"],
                }
            )

    # Optional cause→risk→risk chains: risk-to-risk linkages among displayed risks.
    if include_chains and risk_ids:
        lk = (
            await db.execute(
                select(RiskLinkage).where(
                    RiskLinkage.sourceRiskId.in_(risk_ids), RiskLinkage.targetRiskId.in_(risk_ids)
                )
            )
        ).scalars().all()
        for link in lk:
            edges.append(
                {
                    "id": f"chain:{link.id}",
                    "source": f"risk:{link.sourceRiskId}",
                    "target": f"risk:{link.targetRiskId}",
                    "contributionType": link.linkageType,
                    "weight": link.correlationStrength,
                }
            )

    return {"nodes": nodes, "edges": edges, "focusSubCauseId": sub_cause_id}


__all__ = [
    "RECURRING_DRIVER_THRESHOLD",
    "compute_cause_analytics",
    "detect_recurring_drivers",
    "compute_contributing_causes_for_risk",
    "build_cause_to_risk_graph",
]
