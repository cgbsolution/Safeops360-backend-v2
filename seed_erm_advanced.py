"""ERM ADVANCED demo-data enrichment.

Enriches the existing seeded ERM register with the quantitative-spine data so the
ADVANCED engines produce real numbers for a CRO walkthrough:
  • per-assessment ₹ best/expected/worst + annualised probability  → expected loss
  • target risk level (one band below residual)                    → inherent→residual→target
  • correlation weights on existing linkages                       → propagation / correlated exposure
  • three-lines-of-defence owners                                  → governance
  • a structured bow-tie on the top-exposure risks                 → causal model

Runs through the REAL engine (recompute_risk_scores) so the denormalised exposure,
control-derived residual and override variance stay internally consistent.
Idempotent — safe to re-run. Reads/writes the live DB via the app session.

    python seed_erm_advanced.py
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.erm import EnterpriseRisk, RiskAssessment, RiskLinkage
import app.services.erm as svc

# Single-event ₹ severity by band: (best, expected, worst).
_FIN_BY_BAND = {
    "CRITICAL": (30_000_000, 80_000_000, 250_000_000),
    "HIGH": (10_000_000, 30_000_000, 100_000_000),
    "MEDIUM": (2_000_000, 8_000_000, 30_000_000),
    "LOW": (500_000, 1_500_000, 6_000_000),
}


def _fin_for(band: str | None, scale: float = 1.0) -> tuple[float, float, float]:
    b, e, w = _FIN_BY_BAND.get((band or "MEDIUM").upper(), _FIN_BY_BAND["MEDIUM"])
    return (round(b * scale), round(e * scale), round(w * scale))


async def main() -> None:
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        risks = (
            await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.isDeleted.is_(False)))
        ).scalars().all()
        print(f"Enriching {len(risks)} risks…")

        # 1) Financial figures on each current assessment + time horizon.
        for r in risks:
            assessments = (
                await db.execute(
                    select(RiskAssessment)
                    .where(RiskAssessment.riskId == r.id)
                    .where(RiskAssessment.isCurrent.is_(True))
                )
            ).scalars().all()
            for a in assessments:
                best, exp, worst = _fin_for(a.ratingBand)
                a.likelihoodPct = svc.default_likelihood_pct(a.likelihood)
                a.financialBestInr = best
                a.financialExpectedInr = exp
                a.financialWorstInr = worst
                a.expectedLossInr = svc.expected_loss(a.likelihoodPct, exp)
                a.unexpectedLossInr = svc.unexpected_loss(a.likelihoodPct, exp, worst)
                a.timeHorizon = "THREE_YEAR"
            await db.flush()
            # Recompute denormalised exposure + control-derived residual + override.
            await svc.recompute_risk_scores(db, r)

            # 2) Three-lines-of-defence accountability.
            if not r.firstLineOwnerId:
                r.firstLineOwnerId = r.riskOwnerId
                r.secondLineOwnerId = r.riskChampionId
                r.thirdLineAssurance = "Internal Audit"

            # 3) Target one band below residual (steer-to position).
            if r.residualLikelihood and r.residualImpact and r.targetScore is None:
                tl = max(1, r.residualLikelihood - 1)
                ti = max(1, r.residualImpact - 1)
                bands = svc.DEFAULT_BANDS
                svc.set_target(
                    r, tl, ti, bands,
                    target_date=now + timedelta(days=270),
                    rationale="Steer to within appetite within 9 months via the active treatment plan.",
                    financial_expected_inr=_fin_for(svc.band_for_score(tl * ti, bands))[1],
                )

        # 4) Correlation weights on existing linkages (TRIGGERS propagate hardest).
        links = (await db.execute(select(RiskLinkage))).scalars().all()
        _w = {"TRIGGERS": (0.8, 0.4), "AMPLIFIES": (0.7, 0.3), "CORRELATED": (0.6, 0.15)}
        for l in links:
            cs, ifac = _w.get(l.linkageType, (0.5, 0.2))
            l.correlationStrength = cs
            l.impactFactor = ifac
        print(f"  weighted {len(links)} linkages")

        # 5) Bow-tie on the two highest-exposure risks.
        ranked = sorted(risks, key=lambda r: (r.residualExpectedLossInr or 0), reverse=True)
        for r in ranked[:2]:
            if r.bowtie:
                continue
            r.bowtie = {
                "topEvent": r.title,
                "threats": [
                    {"id": "t1", "description": (r.causes or ["Process deviation"])[0],
                     "preventiveBarriers": [
                         {"id": "b1", "description": "Primary preventive control", "barrierType": "PREVENTIVE", "status": "WORKED"},
                         {"id": "b2", "description": "Secondary preventive control", "barrierType": "PREVENTIVE", "status": "UNTESTED"},
                     ]},
                ],
                "consequences": [
                    {"id": "c1", "description": (r.consequences or ["Financial / safety loss"])[0],
                     "mitigatingBarriers": [
                         {"id": "m1", "description": "Emergency response / containment", "barrierType": "MITIGATING", "status": "WORKED"},
                         {"id": "m2", "description": "Insurance / business-continuity", "barrierType": "MITIGATING", "status": "UNTESTED"},
                     ]},
                ],
            }
        print(f"  bow-tie on: {[r.riskCode for r in ranked[:2]]}")

        # 6) Re-run the alert engines so deficient-control / RED-KRI flags reflect data.
        ctrl = await svc.sync_control_alerts(db)
        kri = await svc.sync_kri_alerts(db)
        await svc.reconcile_treatment_closures(db)
        await db.commit()
        print(f"  control alerts {ctrl}; KRI alerts {kri}")

        # Report the headline exposure number.
        risks2 = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.isDeleted.is_(False)).where(EnterpriseRisk.lifecycleState != "CLOSED"))).scalars().all()
        total_el = sum(r.residualExpectedLossInr or 0 for r in risks2)
        quantified = sum(1 for r in risks2 if r.residualExpectedLossInr)
        print(f"✅  Enriched. Enterprise residual exposure = ₹{total_el:,.0f} across {quantified} quantified risks.")


if __name__ == "__main__":
    asyncio.run(main())
