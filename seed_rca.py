"""ERM Cross-Domain RCA & Causal Intelligence — Meridian demo seed.

ADDITIVE and idempotent. Seeds the two-layer cause taxonomy and ≥10 cross-domain
RCAs (event / risk / loss-originated) wired so the board-grade punchline renders:

  • Governance/Oversight Failure (GOV) appears as the rolled-up category behind
    operational, compliance AND financial RCAs → one enterprise cause spanning
    ≥3 risk domains (RCA-05 / RCA-T14).
  • "Inadequate isolation verification" drives ≥3 distinct risks (risk_reach demo)
    and fires the recurring-systemic-driver flag (RCA-T12/T15).

It NEVER recomputes the curated EnterpriseRisk / RiskAssessment scores the demo
depends on — it only reads existing risks/loss-events and inserts new RCA rows.
References existing loss events where present; falls back to risk-derived RCAs for
any domain whose loss-events aren't seeded (so no new LossEvent is created, which
would shift the dashboard's Net-Loss figures).

    python seed_rca.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models._base import gen_id
from app.models.capa import CapaSourceCategory, CapaSourceType
from app.models.erm import EnterpriseRisk, RiskCategory
from app.models.erm_p2 import LossEvent
from app.models.rca import (
    RcaIdentifiedCause,
    RcaRiskLink,
    RootCauseAnalysis,
    RootCauseCategory,
    RootCauseSubCause,
)
from app.models.user import User
from app.services.access_scope import system_scope
from app.services.rca_core import CATEGORY_CODE_TO_DOMAIN
from app.services import rca_analytics

BASE = datetime(2026, 3, 1, tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Enterprise cause categories (the ~7) ──────────────────────────────────────
CATEGORIES = [
    ("GOV", "Governance / Oversight Failure", "#B91C1C", 1),
    ("PROC", "Process / Procedure Failure", "#B45309", 2),
    ("PEOPLE", "Human / Competency Factors", "#2C6E91", 3),
    ("THIRD_PARTY", "Third-Party / Supplier Failure", "#D35400", 4),
    ("TECH", "Technology / Systems Failure", "#16A085", 5),
    ("EXTERNAL", "External / Environmental Factors", "#5D6D7E", 6),
    ("DESIGN", "Design / Engineering Inadequacy", "#6B4FA0", 7),
]

# ── Domain-scoped sub-causes (code, name, category, domains, synonyms) ─────────
SUBCAUSES = [
    # Operational
    ("OPS-ISO", "Inadequate isolation verification", "PROC", ["OPERATIONAL"], ["LOTO skipped", "isolation failure", "isolation not verified"]),
    ("OPS-PTW", "Permit-to-work control gap", "GOV", ["OPERATIONAL"], ["permit not raised"]),
    ("OPS-COMP", "Operator competency gap", "PEOPLE", ["OPERATIONAL"], ["training gap"]),
    ("OPS-MAINT", "Deferred preventive maintenance", "PROC", ["OPERATIONAL"], ["maintenance backlog"]),
    ("OPS-GUARD", "Inadequate machine guarding", "DESIGN", ["OPERATIONAL"], []),
    ("OPS-SUP", "Inadequate supervision", "GOV", ["OPERATIONAL"], []),
    # Financial
    ("FIN-HEDGE", "Inadequate hedging policy", "PROC", ["FINANCIAL"], ["no hedge"]),
    ("FIN-CONC", "Revenue / customer concentration", "GOV", ["FINANCIAL", "STRATEGIC"], ["concentration risk"]),
    ("FIN-LIQ", "Liquidity buffer inadequate", "PROC", ["FINANCIAL"], []),
    ("FIN-LIMIT", "Risk-limit framework not enforced", "GOV", ["FINANCIAL"], []),
    ("FIN-FX", "Unhedged FX exposure", "PROC", ["FINANCIAL"], []),
    # Compliance
    ("CMP-OWN", "No obligation ownership", "GOV", ["COMPLIANCE"], ["unowned obligation", "no owner"]),
    ("CMP-FILING", "Statutory filing process gap", "PROC", ["COMPLIANCE"], ["missed filing"]),
    ("CMP-MONITOR", "Inadequate regulatory monitoring", "GOV", ["COMPLIANCE"], []),
    ("CMP-TRAIN", "Compliance training gap", "PEOPLE", ["COMPLIANCE"], []),
    ("CMP-DOC", "Inadequate records / documentation", "PROC", ["COMPLIANCE"], []),
    # Reputational
    ("REP-SOCIAL", "No social-media listening control", "GOV", ["REPUTATIONAL"], []),
    ("REP-CRISIS", "Crisis-communications plan absent", "PROC", ["REPUTATIONAL"], []),
    ("REP-BRAND", "Brand-guardianship gap", "GOV", ["REPUTATIONAL"], []),
    # External
    ("EXT-MACRO", "Macro / geopolitical shift", "EXTERNAL", ["EXTERNAL"], []),
    ("EXT-REG", "Regulatory regime change", "EXTERNAL", ["EXTERNAL", "COMPLIANCE"], []),
    # Cyber
    ("CYB-PATCH", "Unpatched vulnerability", "TECH", ["CYBER"], []),
    ("CYB-ACCESS", "Excessive access / IAM gap", "TECH", ["CYBER"], []),
    ("CYB-VENDOR", "Third-party data-processor exposure", "THIRD_PARTY", ["CYBER"], []),
    ("CYB-GOV", "Inadequate cyber governance", "GOV", ["CYBER"], []),
    # Strategic / ESG
    ("STR-CONC", "Customer-concentration deterioration", "GOV", ["STRATEGIC"], []),
    ("ESG-SUP", "Supplier ESG non-compliance", "THIRD_PARTY", ["ESG"], []),
]


def _five_why(problem: str, chain: list[str], root: str) -> dict:
    return {
        "problemStatement": problem,
        "whys": [{"question": f"Why? ({i+1})", "answer": a} for i, a in enumerate(chain)],
        "rootCause": root,
    }


def _narrative(summary: str, factors: list[str]) -> dict:
    return {"summary": summary, "factors": [{"description": f} for f in factors]}


async def _ensure_capa_source(db) -> None:
    st = (await db.execute(select(CapaSourceType).where(CapaSourceType.code == "ENTERPRISE_RCA"))).scalar_one_or_none()
    if st is not None:
        return
    cat = (await db.execute(select(CapaSourceCategory).where(CapaSourceCategory.code == "ORGANIZATIONAL"))).scalar_one_or_none()
    if cat is None:
        cat = CapaSourceCategory(id=gen_id(), code="ORGANIZATIONAL", name="Organizational", prefix="O",
                                 description="Management review, training, MOC, kaizen, RCA.", sortOrder=4, isActive=True)
        db.add(cat)
        await db.flush()
    db.add(CapaSourceType(
        id=gen_id(), code="ENTERPRISE_RCA", name="Enterprise RCA", categoryId=cat.id,
        parentModuleLive=True, parentModuleName="ERM", sortOrder=34, isActive=True,
        description="Corrective action raised from an ERM Cross-Domain Root Cause Analysis.",
    ))


async def _seed_taxonomy(db) -> tuple[dict, dict]:
    """Upsert categories + sub-causes (idempotent by code). Returns
    {catCode: catId}, {subCode: (subId, catId)}."""
    cat_ids: dict[str, str] = {}
    for code, name, color, order in CATEGORIES:
        existing = (await db.execute(select(RootCauseCategory).where(RootCauseCategory.code == code))).scalar_one_or_none()
        if existing is None:
            existing = RootCauseCategory(id=gen_id(), code=code, name=name, description=name,
                                         colorHex=color, displayOrder=order, isActive=True, createdBy="seed:rca")
            db.add(existing)
            await db.flush()
        cat_ids[code] = existing.id

    sub_ids: dict[str, tuple[str, str]] = {}
    for code, name, cat_code, domains, syns in SUBCAUSES:
        existing = (await db.execute(select(RootCauseSubCause).where(RootCauseSubCause.code == code))).scalar_one_or_none()
        cat_id = cat_ids[cat_code]
        if existing is None:
            existing = RootCauseSubCause(id=gen_id(), categoryId=cat_id, code=code, name=name, description=name,
                                         applicableDomains=domains, synonyms=syns, isActive=True, createdBy="seed:rca")
            db.add(existing)
            await db.flush()
        sub_ids[code] = (existing.id, cat_id)
    return cat_ids, sub_ids


async def _domain_pools(db):
    """Existing risks + loss events grouped by canonical domain (read-only)."""
    cat_code = {c.id: c.code for c in (await db.execute(select(RiskCategory))).scalars().all()}
    risks_by_domain: dict[str, list[str]] = {}
    risks = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.isDeleted.is_(False)))).scalars().all()
    for r in risks:
        dom = CATEGORY_CODE_TO_DOMAIN.get(cat_code.get(r.categoryId, ""), "OPERATIONAL")
        risks_by_domain.setdefault(dom, []).append(r.id)
    all_risk_ids = [r.id for r in risks]

    loss_by_domain: dict[str, list] = {}
    losses = (await db.execute(select(LossEvent).where(LossEvent.isDeleted.is_(False)))).scalars().all()
    for le in losses:
        dom = CATEGORY_CODE_TO_DOMAIN.get(cat_code.get(le.categoryId, ""), "OPERATIONAL")
        loss_by_domain.setdefault(dom, []).append(le)
    return risks_by_domain, all_risk_ids, loss_by_domain


async def main() -> None:
    async with AsyncSessionLocal() as db:
        # analyst = CRO if present
        cro = (await db.execute(select(User).where(User.email == "anand.krishnan@safeops360.in"))).scalar_one_or_none()
        if cro is None:
            cro = (await db.execute(select(User).limit(1))).scalar_one_or_none()
        analyst = cro.id if cro else "SYSTEM"

        await _ensure_capa_source(db)
        cat_ids, sub_ids = await _seed_taxonomy(db)
        await db.commit()
        print(f"Taxonomy ready: {len(cat_ids)} categories, {len(sub_ids)} sub-causes.")

        # Idempotency: if the seed RCAs already exist, skip creation.
        marker = (
            await db.execute(
                select(RootCauseAnalysis).where(RootCauseAnalysis.rcaCode == "RCA-2026-9001")
                .execution_options(include_deleted=True)
            )
        ).scalar_one_or_none()
        if marker is not None:
            print("Seed RCAs already present — skipping RCA creation (idempotent).")
            await _report(db)
            return

        risks_by_domain, all_risk_ids, loss_by_domain = await _domain_pools(db)

        def pick(domain: str, n: int) -> list[str]:
            pool = list(risks_by_domain.get(domain, []))
            for rid in all_risk_ids:
                if len(pool) >= n:
                    break
                if rid not in pool:
                    pool.append(rid)
            return pool[:n]

        op = pick("OPERATIONAL", 3)
        fin = pick("FINANCIAL", 2)
        cmp = pick("COMPLIANCE", 2)
        strat = pick("STRATEGIC", 1)
        rep = pick("REPUTATIONAL", 1)
        cyb = pick("CYBER", 1)

        def loss_for(domain: str):
            lst = loss_by_domain.get(domain) or []
            return lst[0] if lst else None

        created: list[RootCauseAnalysis] = []

        def add_rca(code, title, origin, domain, methodology, payload, narrative, day_offset,
                    *, source_event=None, source_risk=None, source_loss=None, plant=None):
            rca = RootCauseAnalysis(
                id=gen_id(), rcaCode=code, title=title, originType=origin,
                sourceEventId=source_event, sourceRiskId=source_risk, sourceLossEventId=source_loss,
                primaryDomain=domain, methodology=methodology, status="APPROVED",
                analysisPayload=payload, narrative=narrative,
                analystId=analyst, approverId=analyst, approvedAt=_now(),
                occurrenceDate=BASE + timedelta(days=day_offset), plantId=plant,
                tenantId="default", createdBy="seed:rca", updatedBy="seed:rca",
            )
            db.add(rca)
            created.append(rca)
            return rca

        def tag(rca, sub_code, role, confidence=None, order=0):
            sub_id, cat_id = sub_ids[sub_code]
            db.add(RcaIdentifiedCause(id=gen_id(), rcaId=rca.id, subCauseId=sub_id, enterpriseCategoryId=cat_id,
                                      causalRole=role, confidence=confidence, sortOrder=order, createdBy="seed:rca"))

        def link(rca, risk_id, contribution, weight=None, note=None):
            if not risk_id:
                return
            db.add(RcaRiskLink(id=gen_id(), rcaId=rca.id, riskId=risk_id, contributionType=contribution,
                               weight=weight, note=note, createdBy="seed:rca"))

        # ── 3 event-derived operational RCAs (share OPS-ISO → recurring) ──
        r1 = add_rca("RCA-2026-9001", "Crane sling failure — RCA", "EVENT", "OPERATIONAL", "FIVE_WHY",
                     _five_why("Crane sling parted during a lift.",
                               ["Sling overloaded", "Pre-use isolation/lock not verified", "No supervisor check"],
                               "Isolation/verification step skipped under schedule pressure"),
                     None, 10, source_event="INC-CRANE-2026", plant=None)
        tag(r1, "OPS-ISO", "ROOT", "CONFIRMED", 0); tag(r1, "OPS-MAINT", "CONTRIBUTING", "PROBABLE", 1)
        link(r1, op[0] if op else None, "CAUSED", 0.8)

        r2 = add_rca("RCA-2026-9002", "Confined-space near-miss — RCA", "EVENT", "OPERATIONAL", "FIVE_WHY",
                     _five_why("Worker entered a vessel before isolation was confirmed.",
                               ["Permit not fully closed out", "Isolation not independently verified"],
                               "Permit-to-work governance gap + isolation not verified"),
                     None, 24, source_event="NM-CSPACE-2026")
        tag(r2, "OPS-ISO", "ROOT", "CONFIRMED", 0); tag(r2, "OPS-PTW", "CONTRIBUTING", "CONFIRMED", 1)  # OPS-PTW → GOV (operational)
        link(r2, op[1] if len(op) > 1 else (op[0] if op else None), "CAUSED", 0.7)

        r3 = add_rca("RCA-2026-9003", "Isolation-verification incident — RCA", "EVENT", "OPERATIONAL", "FIVE_WHY",
                     _five_why("Energy release during maintenance; isolation not verified.",
                               ["LOTO applied but not independently checked"],
                               "Inadequate isolation verification"),
                     None, 40, source_event="INC-ISO-2026")
        tag(r3, "OPS-ISO", "ROOT", "CONFIRMED", 0)
        link(r3, op[2] if len(op) > 2 else (op[0] if op else None), "ELEVATED", 0.6)

        # ── 2 risk-derived non-operational RCAs (no incident) ──
        r4 = add_rca("RCA-2026-9004", "Customer-concentration deterioration — RCA", "RISK", "STRATEGIC", "NARRATIVE",
                     _narrative("Top-customer revenue share drifted above appetite on review.",
                                ["Over-reliance on a single licensor channel", "No revenue-diversification target owned"]),
                     "Top-customer revenue share drifted above the board's appetite.", 55,
                     source_risk=strat[0] if strat else (all_risk_ids[0] if all_risk_ids else None))
        tag(r4, "STR-CONC", "ROOT", "PROBABLE", 0); tag(r4, "FIN-CONC", "CONTRIBUTING", "PROBABLE", 1)  # both GOV
        link(r4, strat[0] if strat else None, "ELEVATED", 0.7)
        link(r4, fin[0] if fin else None, "CAUSED", 0.5)

        r5 = add_rca("RCA-2026-9005", "Social-media brand incident — RCA", "RISK", "REPUTATIONAL", "NARRATIVE",
                     _narrative("A viral post on a labour allegation escalated before the brand team responded.",
                                ["No social-media listening control", "Crisis-comms plan not tested"]),
                     "Reputational escalation outran the response.", 62,
                     source_risk=rep[0] if rep else (all_risk_ids[0] if all_risk_ids else None))
        tag(r5, "REP-SOCIAL", "ROOT", "PROBABLE", 0); tag(r5, "REP-CRISIS", "CONTRIBUTING", "POSSIBLE", 1)
        link(r5, rep[0] if rep else None, "REVEALED", 0.6)

        # ── 3 loss-event-derived RCAs (financial / compliance / cyber) ──
        # Prefer an existing loss event; else fall back to a risk-derived RCA in the same domain.
        def loss_or_risk(domain, risk_list):
            le = loss_for(domain)
            if le is not None:
                return ("LOSS_EVENT", {"source_loss": le.id, "plant": le.siteId})
            return ("RISK", {"source_risk": (risk_list[0] if risk_list else (all_risk_ids[0] if all_risk_ids else None))})

        o6, kw6 = loss_or_risk("FINANCIAL", fin)
        r6 = add_rca("RCA-2026-9006", "FX / hedging loss — RCA", o6, "FINANCIAL", "FIVE_WHY",
                     _five_why("Unhedged FX position crystallised a loss on imported raw material.",
                               ["Hedging policy thresholds not enforced", "Concentration in one currency pair"],
                               "Inadequate hedging policy + unenforced risk limits"),
                     None, 30, **kw6)
        tag(r6, "FIN-HEDGE", "ROOT", "CONFIRMED", 0); tag(r6, "FIN-CONC", "CONTRIBUTING", "PROBABLE", 1)  # FIN-CONC → GOV (financial)
        link(r6, fin[0] if fin else None, "CAUSED", 0.8)
        link(r6, fin[1] if len(fin) > 1 else None, "ELEVATED", 0.4)

        o7, kw7 = loss_or_risk("COMPLIANCE", cmp)
        r7 = add_rca("RCA-2026-9007", "Factories Act filing missed — penalty — RCA", o7, "COMPLIANCE", "FIVE_WHY",
                     _five_why("A statutory return was filed late, triggering a penalty.",
                               ["Filing calendar not maintained", "No named owner for the obligation"],
                               "No obligation ownership + filing-process gap"),
                     None, 35, **kw7)
        tag(r7, "CMP-FILING", "ROOT", "CONFIRMED", 0); tag(r7, "CMP-OWN", "CONTRIBUTING", "CONFIRMED", 1)  # CMP-OWN → GOV (compliance)
        link(r7, cmp[0] if cmp else None, "CAUSED", 0.8)

        o8, kw8 = loss_or_risk("CYBER", cyb)
        r8 = add_rca("RCA-2026-9008", "Data-exposure remediation cost — RCA", o8, "CYBER", "FIVE_WHY",
                     _five_why("A misconfigured store exposed records; remediation incurred cost.",
                               ["Unpatched component", "No cyber-governance review of the asset"],
                               "Unpatched vulnerability + inadequate cyber governance"),
                     None, 45, **kw8)
        tag(r8, "CYB-PATCH", "ROOT", "CONFIRMED", 0); tag(r8, "CYB-GOV", "CONTRIBUTING", "PROBABLE", 1)  # CYB-GOV → GOV (cyber)
        link(r8, cyb[0] if cyb else None, "CAUSED", 0.7)

        # ── 2 audit-finding-derived compliance RCAs (recurring "no obligation ownership") ──
        r9 = add_rca("RCA-2026-9009", "Audit finding — no obligation ownership — RCA", "EVENT", "COMPLIANCE", "FIVE_WHY",
                     _five_why("Internal audit found obligations with no accountable owner.",
                               ["Register lacks a named owner column", "No governance routine to assign owners"],
                               "No obligation ownership"),
                     None, 50, source_event="AUDIT-FND-OWN-1")
        tag(r9, "CMP-OWN", "ROOT", "CONFIRMED", 0)
        link(r9, cmp[0] if cmp else None, "RECURRING_DRIVER", 0.6)

        r10 = add_rca("RCA-2026-9010", "Audit finding — recurring no obligation ownership — RCA", "EVENT", "COMPLIANCE", "FIVE_WHY",
                      _five_why("A follow-up audit found the same ownership gap in another function.",
                                ["Owner-assignment routine still not embedded"],
                                "No obligation ownership (recurring)"),
                      None, 58, source_event="AUDIT-FND-OWN-2")
        tag(r10, "CMP-OWN", "ROOT", "CONFIRMED", 0)
        link(r10, cmp[1] if len(cmp) > 1 else (cmp[0] if cmp else None), "RECURRING_DRIVER", 0.6)

        await db.commit()
        print(f"Created {len(created)} RCAs with tagged causes + risk links.")
        await _report(db)


async def _report(db) -> None:
    """Print the cross-domain proof (doubles as the RCA-T14 punchline check)."""
    scope = system_scope([], all_plants=True, job_name="seed-rca-report")
    analytics = await rca_analytics.compute_cause_analytics(db, scope)
    cats = {c["categoryCode"]: c for c in analytics["categories"]}
    gov = cats.get("GOV")
    print("\n-- Cross-domain proof (computed from approved RCA records) --")
    if gov:
        print(f"  GOV (Governance/Oversight) → domainSpread={gov['domainSpread']} "
              f"domains={gov['domains']} riskReach={gov['riskReach']} occurrences={gov['occurrences']}")
        punch = {"OPERATIONAL", "COMPLIANCE", "FINANCIAL"}.issubset(set(gov["domains"]))
        print(f"  PUNCHLINE governance spans operational+compliance+financial: {'PASS' if punch else 'CHECK'}")
    recurring = [c for c in analytics["causes"] if c["isRecurringDriver"]]
    print(f"  Recurring systemic drivers: {[ (c['subCauseCode'], c['riskReach'], c['occurrences']) for c in recurring ]}")
    iso = next((c for c in analytics["causes"] if c["subCauseCode"] == "OPS-ISO"), None)
    if iso:
        print(f"  Isolation (OPS-ISO) riskReach={iso['riskReach']} occurrences={iso['occurrences']} "
              f"recurring={iso['isRecurringDriver']}")


if __name__ == "__main__":
    asyncio.run(main())
